from datetime import datetime

from fastapi import HTTPException

from ...database.mongo import get_db
from ...models.enums import LoanStatus
from ...utils.dates import next_month_date
from ...utils.id import loan_id_filter

from ..audit_service import write_audit_log
from ..emi import ensure_emi_schedule_generated
from ..sanction_service import build_sanction_letter_pdf_bytes, store_pdf_document
from ..settings_service import get_settings
from ..wallet_service import credit_wallet
from .actor_meta import resolve_actor_meta

from .calculations import compute_emi



# =========================
# ADMIN FINAL APPROVAL
# =========================
async def admin_final_approve(
    loan_collection: str,
    loan_id: str,
    admin_id: str,
    approved_amount: float | None = None,
    interest_rate: float | None = None,
):
    db = await get_db()
    filt = loan_id_filter(loan_id)
    loan = await db[loan_collection].find_one(filt)

    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")

    if loan.get("status") != LoanStatus.PENDING_ADMIN_APPROVAL:
        raise HTTPException(status_code=400, detail="Loan not pending admin approval")

    if float(loan.get("loan_amount") or 0) <= 1500000:
        raise HTTPException(status_code=400, detail="Admin approval is only for loans above 15,00,000")

    requested_amount = float(loan["loan_amount"])
    tenure = int(loan.get("tenure_months") or 0)
    settings = await get_settings()
    default_interest = (
        float(settings["vehicle_loan_interest"])
        if loan_collection == "vehicle_loans"
        else float(settings["personal_loan_interest"])
    )

    final_interest_rate = (
        float(interest_rate)
        if interest_rate is not None
        else float(loan.get("interest_rate") or default_interest)
    )
    if final_interest_rate <= 0:
        raise HTTPException(status_code=400, detail="interest_rate must be > 0")

    final_approved_amount = (
        float(approved_amount)
        if approved_amount is not None
        else float(loan.get("approved_amount") or requested_amount)
    )
    if final_approved_amount <= 0:
        raise HTTPException(status_code=400, detail="approved_amount must be > 0")
    if final_approved_amount > requested_amount:
        raise HTTPException(status_code=400, detail="approved_amount cannot exceed requested loan_amount")

    emi = compute_emi(final_approved_amount, final_interest_rate, tenure) if tenure > 0 else loan.get("emi_per_month")
    remaining_amount = round(float(emi or 0) * tenure, 2) if tenure > 0 else loan.get("remaining_amount")

    now = datetime.utcnow()
    admin = await resolve_actor_meta(admin_id, fallback_role="admin")

    await db[loan_collection].update_one(
        filt,
        {"$set": {
            "status": LoanStatus.ADMIN_APPROVED,
            "admin_id": admin_id,
            "admin_approved_at": now,
            "admin_approved_by_id": admin["actor_id"],
            "admin_approved_by_name": admin["actor_name"],
            "admin_approved_by_role": admin["actor_role"],
            "approved_at": now,
            "approved_amount": final_approved_amount,
            "interest_rate": final_interest_rate,
            "emi_per_month": emi,
            "remaining_amount": remaining_amount,
        }}
    )

    await write_audit_log(
        action="admin_approve",
        actor_role="admin",
        actor_id=admin_id,
        entity_type="loan",
        entity_id=loan.get("loan_id"),
        details={
            "loan_collection": loan_collection,
            "approved_amount": final_approved_amount,
            "interest_rate": final_interest_rate,
        },
    )

    return {
        "message": "Loan approved successfully by admin",
        "loan_id": loan["loan_id"],
        "status": LoanStatus.ADMIN_APPROVED
    }


# =========================
# DISBURSE LOAN
# =========================
async def disburse(loan_collection: str, loan_id: str, admin_id: str | None = None):
    db = await get_db()
    filt = loan_id_filter(loan_id)
    loan = await db[loan_collection].find_one(filt)

    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")

    if loan.get("status") != LoanStatus.READY_FOR_DISBURSEMENT:
        raise HTTPException(status_code=400, detail="Loan not ready for disbursement")

    disburse_amount = float(loan.get("approved_amount") or loan["loan_amount"])
    wallet_txn = await credit_wallet(
        loan["customer_id"],
        disburse_amount,
        f"Loan disbursement for loan {loan.get('loan_id')}",
    )
    await db.transactions.update_one(
        {
            "transaction_id": wallet_txn.get("transaction_id"),
            "customer_id": loan["customer_id"],
        },
        {
            "$set": {
                "loan_id": loan["loan_id"],
                "type": "disbursement",
                "balance_after": wallet_txn.get("new_balance"),
                "created_at": datetime.utcnow(),
            }
        },
    )

    now = datetime.utcnow()
    admin = await resolve_actor_meta(admin_id, fallback_role="admin") if admin_id is not None else {
        "actor_id": None,
        "actor_name": None,
        "actor_role": None,
    }

    await db[loan_collection].update_one(
        filt,
        {"$set": {
            "status": LoanStatus.ACTIVE,
            "disbursed_at": now,
            "next_emi_date": next_month_date(),
            "disbursed_by_id": admin["actor_id"],
            "disbursed_by_name": admin["actor_name"],
            "disbursed_by_role": admin["actor_role"],
        }}
    )

    # Best-effort: generate EMI schedule on first disbursement.
    try:
        updated_loan = await db[loan_collection].find_one(filt)
        await ensure_emi_schedule_generated(loan_collection, updated_loan or loan)
    except Exception:
        pass

    await write_audit_log(
        action="admin_disburse",
        actor_role="admin" if admin_id is not None else None,
        actor_id=admin_id,
        entity_type="loan",
        entity_id=loan.get("loan_id"),
        details={
            "loan_collection": loan_collection,
            "amount": disburse_amount,
            "transaction_id": wallet_txn.get("transaction_id"),
            "wallet_new_balance": wallet_txn.get("new_balance"),
        },
    )

    return {"message": "Loan amount disbursed successfully"}


# =========================
# SEND SANCTION LETTER
# =========================
async def send_sanction(loan_collection: str, loan_id: str, admin_id: str | None = None):
    from ...database.mongo import get_db
    db = await get_db()
    filt = loan_id_filter(loan_id)
    loan = await db[loan_collection].find_one(filt)
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
 
    amount = float(loan.get("loan_amount") or 0)
    status = loan.get("status")
 
    if amount > 1500000 and status != LoanStatus.ADMIN_APPROVED:
        raise HTTPException(
            status_code=400,
            detail="Admin approval required before sanction letter generation"
        )
    if amount <= 1500000 and status not in [LoanStatus.MANAGER_APPROVED, LoanStatus.ADMIN_APPROVED]:
        raise HTTPException(
            status_code=400,
            detail="Manager approval required before sanction letter generation"
        )
 
    sanction_id = loan.get("sanction_letter_document_id")
 
    if not sanction_id:
        customer_id = loan.get("customer_id")
 
        user_doc = await db.users.find_one(
            {"customer_id": customer_id},
            {"_id": 0, "full_name": 1, "email": 1, "phone": 1},
        )
        if not user_doc:
            user_doc = await db.users.find_one(
                {"_id": customer_id},
                {"_id": 0, "full_name": 1, "email": 1, "phone": 1},
            )
 
        kyc_doc = await db.kyc_details.find_one(
            {"customer_id": customer_id},
            {"_id": 0, "address": 1, "phone_number": 1},
        )
 
        loan_type_map = {
            "personal_loans": "Personal",
            "vehicle_loans": "Vehicle",
            "education_loans": "Education",
            "home_loans": "Home",
        }
 
        payload = {
            "issue_date": datetime.utcnow().isoformat(),
            "generated_at": datetime.utcnow().isoformat(),
            "sanction_reference_number": f"SL-{loan.get('loan_id')}",
            "loan_id": loan.get("loan_id"),
            "customer_id": customer_id,
            "full_name": loan.get("full_name") or (user_doc.get("full_name") if user_doc else ""),
            "customer_name": loan.get("full_name") or (user_doc.get("full_name") if user_doc else ""),
            "address_line_1": loan.get("address") or (kyc_doc.get("address") if kyc_doc else "") or "",
            "city": loan.get("city") or "",
            "state": loan.get("state") or "",
            "pin_code": loan.get("pin_code") or loan.get("pincode") or "",
            "mobile_number": (
                (user_doc.get("phone") if user_doc else "")
                or (kyc_doc.get("phone_number") if kyc_doc else "")
                or ""
            ),
            "email": user_doc.get("email") if user_doc else "",
            "loan_type": loan_type_map.get(loan_collection, "Personal"),
            "approved_amount": loan.get("approved_amount") or loan.get("loan_amount"),
            "loan_account_number": loan.get("loan_account_number") or loan.get("loan_id"),
            "loan_purpose": loan.get("loan_purpose") or "",
            "interest_rate": loan.get("interest_rate") or "",
            "interest_rate_basis": loan.get("interest_rate_basis") or "fixed",
            "tenure_months": loan.get("tenure_months") or "",
            "tenure_text": f"{loan.get('tenure_months')} months" if loan.get("tenure_months") else "",
            "emi_per_month": loan.get("emi_per_month") or "",
            "emi_start_date": loan.get("next_emi_date"),
            "repayment_mode": loan.get("repayment_mode") or "Auto Debit",
            "disbursement_mode": loan.get("disbursement_mode") or "Bank Transfer",
            "validity_days": loan.get("sanction_validity_days") or 30,
            "lender_name": loan.get("lender_name") or "PayCrest",
        }
 
        pdf_bytes = build_sanction_letter_pdf_bytes(payload)
 
        # ✅ FIX Bug 1: correct positional argument order matching store_pdf_document signature:
        # store_pdf_document(pdf_bytes, filename, customer_id, doc_type, metadata=None)
        sanction_id = await store_pdf_document(
            pdf_bytes,                                              # 1. pdf_bytes
            f"sanction_letter_loan_{loan.get('loan_id')}.pdf",     # 2. filename
            loan.get("customer_id"),                                # 3. customer_id
            "sanction_letter",                                      # 4. doc_type
            {"loan_id": loan.get("loan_id")},                      # 5. metadata
        )
 
    now = datetime.utcnow()
    admin = (
        await resolve_actor_meta(admin_id, fallback_role="admin")
        if admin_id is not None
        else {"actor_id": None, "actor_name": None, "actor_role": None}
    )
 
    await db[loan_collection].update_one(
        filt,
        {
            "$set": {
                "status": LoanStatus.SANCTION_SENT,
                "sanction_letter_document_id": sanction_id,
                "sanction_sent_at": now,
                "sanction_sent_by_id": admin["actor_id"],
                "sanction_sent_by_name": admin["actor_name"],
                "sanction_sent_by_role": admin["actor_role"],
            }
        },
    )
 
    await write_audit_log(
        action="sanction_letter_generate",
        actor_role="admin" if admin_id is not None else None,
        actor_id=admin_id,
        entity_type="loan",
        entity_id=loan.get("loan_id"),
        details={
            "loan_collection": loan_collection,
            "sanction_letter_document_id": str(sanction_id),
        },
    )
 
    return {
        "message": "Sanction letter sent successfully",
        "loan_id": loan.get("loan_id"),
        "status": LoanStatus.SANCTION_SENT,
        "sanction_letter_document_id": sanction_id,
    }
 



# =========================
# MARK SIGNED DOCUMENT RECEIVED
# =========================
async def mark_signed_received(loan_collection: str, loan_id: str, admin_id: str | None = None):
    db = await get_db()
    filt = loan_id_filter(loan_id)

    loan = await db[loan_collection].find_one(filt)
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")

    now = datetime.utcnow()
    admin = await resolve_actor_meta(admin_id, fallback_role="admin") if admin_id is not None else {
        "actor_id": None,
        "actor_name": None,
        "actor_role": None,
    }

    await db[loan_collection].update_one(
        filt,
        {"$set": {
            "status": LoanStatus.SIGNED_RECEIVED,
            "signed_uploaded_at": now,
            "signed_received_by_id": admin["actor_id"],
            "signed_received_by_name": admin["actor_name"],
            "signed_received_by_role": admin["actor_role"],
        }}
    )

    return {
        "message": "Signed documents received",
        "loan_id": loan.get("loan_id"),
        "status": LoanStatus.SIGNED_RECEIVED
    }


async def admin_reject(loan_collection: str, loan_id: str, admin_id: str, reason: str | None = None):
    db = await get_db()
    filt = loan_id_filter(loan_id)
    loan = await db[loan_collection].find_one(filt)

    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")

    if loan.get("status") != LoanStatus.PENDING_ADMIN_APPROVAL:
        raise HTTPException(status_code=400, detail="Loan not pending admin approval")

    if float(loan.get("loan_amount") or 0) <= 1500000:
        raise HTTPException(status_code=400, detail="Admin rejection applies only to loans above 15,00,000")

    now = datetime.utcnow()
    admin = await resolve_actor_meta(admin_id, fallback_role="admin")

    await db[loan_collection].update_one(
        filt,
        {"$set": {
            "status": LoanStatus.REJECTED,
            "admin_id": admin_id,
            "rejected_by": "admin",
            "rejected_reason": reason,
            "rejected_at": now,
            "rejected_by_id": admin["actor_id"],
            "rejected_by_name": admin["actor_name"],
            "rejected_by_role": admin["actor_role"],
        }}
    )

    await write_audit_log(
        action="admin_reject",
        actor_role="admin",
        actor_id=admin_id,
        entity_type="loan",
        entity_id=loan.get("loan_id"),
        details={"loan_collection": loan_collection, "reason": reason},
    )

    return {
        "message": "Loan rejected by admin",
        "loan_id": loan.get("loan_id"),
        "status": LoanStatus.REJECTED,
    }
