from __future__ import annotations

from datetime import datetime
from io import BytesIO
from uuid import uuid4

from fastapi import HTTPException
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas

from ...database.mongo import get_db
from ...utils.serializers import normalize_doc
from ..audit_service import write_audit_log
from ..emi.notifications import create_customer_notification
from ..sanction_service import store_pdf_document
from .queries import _find_loan_any_by_customer


def _fmt_money(value: float | int | None) -> str:
    try:
        return f"{float(value or 0):,.2f}"
    except Exception:
        return "0.00"


def _fmt_date(value: datetime | None) -> str:
    return (value or datetime.utcnow()).strftime("%d/%m/%Y")


def _build_noc_pdf_bytes(payload: dict) -> bytes:
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4
    left = 54
    right = width - 54
    max_w = right - left
    y = height - 56

    def ensure_space(min_y: int = 72):
        nonlocal y
        if y <= min_y:
            c.showPage()
            y = height - 56

    def draw_line(text: str, *, bold: bool = False, size: int = 10, gap_after: int = 4):
        nonlocal y
        ensure_space()
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(left, y, str(text))
        y -= (size + gap_after)

    def draw_para(text: str, *, bold: bool = False, size: int = 10, gap_after: int = 8):
        nonlocal y
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        lines = simpleSplit(str(text), "Helvetica-Bold" if bold else "Helvetica", size, max_w)
        for ln in lines:
            ensure_space()
            c.drawString(left, y, ln)
            y -= (size + 3)
        y -= gap_after

    customer_name = payload.get("customer_name") or "Customer"
    customer_address = payload.get("customer_address") or "Address on record"
    loan_id = payload.get("loan_id") or "-"
    loan_type = payload.get("loan_type") or "Loan"
    closure_date = _fmt_date(payload.get("foreclosed_at"))
    issue_date = _fmt_date(payload.get("issued_at"))
    bank_name = payload.get("bank_name") or "PayCrest"
    noc_number = payload.get("noc_number") or "-"

    draw_line("LOAN FORECLOSURE & NO OBJECTION CERTIFICATE (NOC)", bold=True, size=12, gap_after=8)
    draw_line(f"Date: {issue_date}", size=10, gap_after=8)
    draw_line("To,", size=10, gap_after=2)
    draw_line(f"Mr./Ms. {customer_name}", size=10, gap_after=2)
    draw_para(str(customer_address), size=10, gap_after=8)

    draw_para(
        "Subject: Confirmation of Loan Closure and Issuance of No Objection Certificate",
        bold=True,
        size=10,
        gap_after=8,
    )
    draw_line(f"Dear Mr./Ms. {customer_name},", size=10, gap_after=8)
    draw_para(
        f"This is to inform you that your Loan Account bearing Number {loan_id}, availed under the {loan_type}, "
        "has been successfully foreclosed and fully repaid as per our records.",
        size=10,
    )
    draw_para(
        f"We hereby confirm that you have paid all outstanding dues, including principal, interest, foreclosure "
        f"charges (if applicable), and any other associated fees, up to the date of closure. The loan account stands "
        f"closed with effect from {closure_date}, and there are no outstanding amounts payable as of this date.",
        size=10,
    )
    draw_para(
        f"Accordingly, this letter serves as a formal No Objection Certificate (NOC) issued by {bank_name}, "
        "certifying that:",
        size=10,
        gap_after=4,
    )
    draw_para("- The above-mentioned loan account has been fully satisfied.", size=10, gap_after=2)
    draw_para("- There are no dues pending against the borrower in relation to this loan account.", size=10, gap_after=2)
    draw_para("- The bank has no claim whatsoever against the borrower with respect to this loan.", size=10, gap_after=8)

    draw_para("In case of secured loans, we further confirm that:", size=10, gap_after=4)
    draw_para("- The charge/lien marked on the collateral/security has been released.", size=10, gap_after=2)
    draw_para(
        "- All original documents submitted at the time of loan processing are being returned / have been returned to you.",
        size=10,
        gap_after=2,
    )
    draw_para(
        "- Necessary updates will be made with relevant authorities (e.g., RTO in case of vehicle loan, CERSAI, "
        "or other statutory bodies, if applicable).",
        size=10,
        gap_after=8,
    )
    draw_para(
        "We also confirm that the loan closure status will be updated with all major credit bureaus, including "
        "CIBIL, Experian, Equifax, and CRIF High Mark, in accordance with regulatory guidelines.",
        size=10,
    )
    draw_para("This certificate is issued at your request for record and reference purposes.", size=10)
    draw_para(
        f"We thank you for choosing {bank_name} for your financial needs and look forward to serving you again in the future.",
        size=10,
    )

    draw_line(f"NOC Number: {noc_number}", size=10, gap_after=2)
    draw_line(f"Settlement Amount Paid: INR {_fmt_money(payload.get('settlement_amount'))}", size=10, gap_after=2)
    draw_line(f"Settlement Transaction ID: {payload.get('transaction_id') or '-'}", size=10, gap_after=10)
    draw_line(f"For {bank_name}", bold=True, size=10, gap_after=8)
    draw_line("Authorized Signatory", size=10, gap_after=2)
    draw_line("Name: _______________________", size=10, gap_after=2)
    draw_line("Designation: __________________", size=10, gap_after=2)
    draw_line("Employee ID: _________________", size=10, gap_after=2)
    draw_line("Branch: ______________________", size=10, gap_after=2)
    draw_line("Contact Details: ______________", size=10, gap_after=8)
    draw_line("(Official Seal)", size=10, gap_after=2)

    c.save()
    return buffer.getvalue()


def _customer_match(customer_id: str | int) -> dict:
    values: list[str | int] = [customer_id]
    if isinstance(customer_id, str) and customer_id.isdigit():
        values.append(int(customer_id))
    elif isinstance(customer_id, int):
        values.append(str(customer_id))
    unique: list[str | int] = []
    for v in values:
        if v not in unique:
            unique.append(v)
    if len(unique) == 1:
        return {"customer_id": unique[0]}
    return {"customer_id": {"$in": unique}}


def _loan_match(loan_id: str) -> dict:
    values: list[str | int] = [loan_id]
    if isinstance(loan_id, str) and loan_id.isdigit():
        values.append(int(loan_id))
    unique: list[str | int] = []
    for v in values:
        if v not in unique:
            unique.append(v)
    if len(unique) == 1:
        return {"loan_id": unique[0]}
    return {"loan_id": {"$in": unique}}


async def issue_foreclosure_noc(
    *,
    loan_collection: str,
    loan: dict,
    settlement_amount: float,
    transaction_id: str | None,
    actor_role: str,
    actor_id: str | int,
) -> dict:
    db = await get_db()
    loan_id = loan.get("loan_id")
    customer_id = loan.get("customer_id")
    if loan_id is None or customer_id is None:
        raise HTTPException(status_code=400, detail="Loan details missing for NOC issuance")

    existing = await db.loan_nocs.find_one({"loan_id": loan_id, **_customer_match(customer_id)})
    if existing:
        return normalize_doc(existing)

    issued_at = datetime.utcnow()
    noc_number = f"NOC-{loan_id}-{issued_at.strftime('%Y%m%d')}-{uuid4().hex[:6].upper()}"
    payload = {
        "noc_number": noc_number,
        "issued_at": issued_at,
        "customer_name": loan.get("full_name") or "Customer",
        "customer_address": loan.get("address") or loan.get("property_address") or "Address on record",
        "customer_id": customer_id,
        "loan_id": loan_id,
        "loan_type": str(loan_collection).replace("_loans", "").capitalize(),
        "bank_name": "PayCrest",
        "settlement_amount": float(settlement_amount or 0.0),
        "transaction_id": transaction_id or "-",
        "foreclosed_at": loan.get("foreclosed_at") if isinstance(loan.get("foreclosed_at"), datetime) else issued_at,
    }
    pdf = _build_noc_pdf_bytes(payload)
    document_id = await store_pdf_document(
        customer_id=customer_id,
        doc_type="loan_noc",
        filename=f"loan_noc_{loan_id}.pdf",
        data=pdf,
    )

    noc_doc = {
        "loan_id": loan_id,
        "loan_collection": loan_collection,
        "customer_id": customer_id,
        "noc_number": noc_number,
        "status": "issued",
        "issued_at": issued_at,
        "document_id": document_id,
        "transaction_id": transaction_id,
        "settlement_amount": float(settlement_amount or 0.0),
    }
    res = await db.loan_nocs.insert_one(noc_doc)
    noc_doc["_id"] = res.inserted_id

    await db[loan_collection].update_one(
        {"loan_id": loan_id},
        {"$set": {"noc_document_id": document_id, "noc_issued_at": issued_at, "noc_number": noc_number}},
    )

    try:
        await create_customer_notification(
            customer_id,
            title="Loan NOC issued",
            message=f"Your foreclosure NOC for loan {loan_id} is ready for download.",
            kind="success",
            meta={"loan_id": loan_id, "document_type": "loan_noc", "noc_number": noc_number},
        )
    except Exception:
        pass

    await write_audit_log(
        action="loan_noc_issued",
        actor_role=actor_role,
        actor_id=actor_id,
        entity_type="loan",
        entity_id=loan_id,
        details={
            "loan_collection": loan_collection,
            "noc_number": noc_number,
            "document_id": document_id,
            "transaction_id": transaction_id,
            "settlement_amount": settlement_amount,
        },
    )
    return normalize_doc(noc_doc)


async def get_customer_noc(loan_id: str, customer_id: str | int) -> dict:
    db = await get_db()
    noc = await db.loan_nocs.find_one({**_loan_match(loan_id), **_customer_match(customer_id)})
    if noc:
        return normalize_doc(noc)

    # Legacy backfill: some older foreclosures/completions may not have created a loan_nocs row.
    loan_collection, loan = await _find_loan_any_by_customer(loan_id, customer_id)
    tx = await db.transactions.find_one(
        {
            **_loan_match(loan_id),
            **_customer_match(customer_id),
            "type": "foreclosure",
        },
        sort=[("created_at", -1), ("_id", -1)],
    )
    status = str((loan or {}).get("status") or "").lower()
    can_backfill = status in {"foreclosed", "completed", "closed"} or bool(tx)
    if loan_collection and loan and can_backfill:
        settlement_amount = float((tx or {}).get("amount") or 0.0)
        if settlement_amount < 0:
            settlement_amount = abs(settlement_amount)
        noc = await issue_foreclosure_noc(
            loan_collection=loan_collection,
            loan=loan,
            settlement_amount=settlement_amount,
            transaction_id=(tx or {}).get("transaction_id"),
            actor_role="system",
            actor_id="system-backfill",
        )
        return normalize_doc(noc)

    raise HTTPException(status_code=404, detail="NOC not issued yet")
