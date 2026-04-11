import asyncio
from datetime import datetime

from fastapi import HTTPException

from ...database.mongo import get_db
from ...models.enums import LoanStatus
from ...utils.dates import next_month_date
from ...utils.id import loan_id_filter
from ...utils.sequences import next_transaction_id
from ...utils.serializers import normalize_doc

from ..audit_service import write_audit_log
from ..emi import ensure_emi_schedule_generated, pay_next_installment
from ..wallet_service import debit_wallet


def _mask_pan(value: str | None) -> str | None:
    pan = str(value or "").strip().upper()
    if not pan:
        return None
    if len(pan) != 10:
        return pan
    return f"{pan[:2]}******{pan[-2:]}"


def _sanitize_loan_doc(doc: dict) -> dict:
    out = normalize_doc(doc)
    if not out.get("pan_masked"):
        out["pan_masked"] = _mask_pan(out.get("pan_number"))
    if not out.get("guarantor_pan_masked"):
        out["guarantor_pan_masked"] = _mask_pan(out.get("guarantor_pan"))
    out.pop("pan_number", None)
    out.pop("guarantor_pan", None)
    out.pop("pan_hash", None)
    out.pop("guarantor_pan_hash", None)
    return out


# ── Shared loan lookup ────────────────────────────────────────
async def _find_loan_collection(loan_id: str, customer_id: str | int) -> str:
    """Find which collection this loan belongs to. Raises 404 if not found.
    Tries both int and str variants of customer_id to handle type mismatches
    between services (payment-service sends str, loans stored with int).
    """
    db = await get_db()
    base_filt = loan_id_filter(loan_id)

    # Build customer_id variants — loans may be stored with int or str customer_id
    cid_variants = []
    try:
        cid_variants.append(int(customer_id))
    except (ValueError, TypeError):
        pass
    cid_variants.append(str(customer_id))
    # deduplicate preserving order
    seen = []
    for v in cid_variants:
        if v not in seen:
            seen.append(v)
    cid_variants = seen

    for col in ("personal_loans", "vehicle_loans", "education_loans", "home_loans"):
        for cid in cid_variants:
            filt = {**base_filt, "customer_id": cid}
            if await db[col].find_one(filt, projection={"_id": 1}):
                return col

    raise HTTPException(
        status_code=404,
        detail=f"Loan {loan_id} not found for customer",
    )


# =========================
# PAY EMI (bank_accounts path — legacy, kept for internal use only)
# =========================
async def pay_emi(loan_collection: str, loan_id: str, customer_id: str):
    db = await get_db()
    filt = loan_id_filter(loan_id)
    filt["customer_id"] = customer_id

    loan = await db[loan_collection].find_one(filt)
    if not loan or loan["status"] != LoanStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Loan not active")

    try:
        await ensure_emi_schedule_generated(loan_collection, loan)
    except Exception:
        pass

    acc = await db.bank_accounts.find_one({"customer_id": customer_id})
    if not acc:
        raise HTTPException(status_code=404, detail="Bank account not found")

    emi = float(loan["emi_per_month"])
    next_emi_doc = await db.emi_schedules.find_one(
        {
            "loan_id": loan.get("loan_id"),
            "customer_id": customer_id,
            "status": {"$in": ["pending", "overdue"]},
        },
        sort=[("due_date", 1)],
    )
    penalty_amount = float(next_emi_doc.get("penalty_amount") or 0) if next_emi_doc else 0.0
    total_due = round(emi + penalty_amount, 2)

    if acc["balance"] < total_due:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    new_balance = acc["balance"] - total_due
    await db.bank_accounts.update_one(
        {"_id": acc["_id"]}, {"$set": {"balance": new_balance}}
    )

    txn_id = await next_transaction_id()
    await db.transactions.insert_one(
        {
            "_id": txn_id,
            "transaction_id": txn_id,
            "customer_id": customer_id,
            "loan_id": loan.get("loan_id"),
            "type": "emi_payment",
            "amount": total_due,
            "balance_after": new_balance,
            "created_at": datetime.utcnow(),
        }
    )

    remaining_tenure = loan["remaining_tenure"] - 1
    remaining_amount = max(0.0, round(float(loan["remaining_amount"]) - emi, 2))

    await db[loan_collection].update_one(
        filt,
        {
            "$set": {
                "remaining_tenure": remaining_tenure,
                "remaining_amount": remaining_amount,
                "status": LoanStatus.COMPLETED if remaining_tenure <= 0 else LoanStatus.ACTIVE,
                "next_emi_date": next_month_date(),
                "total_paid": float(loan.get("total_paid") or 0) + total_due,
                "penalties_paid_total": float(loan.get("penalties_paid_total") or 0) + penalty_amount,
            }
        },
    )

    try:
        await pay_next_installment(
            loan.get("loan_id"),
            customer_id,
            paid_total_amount=total_due,
            paid_emi_amount=emi,
            paid_penalty_amount=penalty_amount,
        )
    except Exception:
        pass

    await write_audit_log(
        action="emi_payment",
        actor_role="customer",
        actor_id=customer_id,
        entity_type="loan",
        entity_id=loan.get("loan_id"),
        details={
            "loan_collection": loan_collection,
            "emi_amount": emi,
            "penalty_amount": penalty_amount,
            "total_paid_amount": total_due,
            "transaction_id": txn_id,
            "balance_after": new_balance,
            "remaining_tenure": remaining_tenure,
        },
    )
    return {"message": "EMI paid successfully"}


# =========================
# PAY EMI (wallet path — primary path used by frontend)
# =========================
async def pay_emi_wallet(loan_collection: str, loan_id: str, customer_id: str | int):
    """Pay the next EMI using the internal wallet balance."""
    db = await get_db()

    # Build customer_id variants for lookup
    cid_variants = []
    try:
        cid_variants.append(int(customer_id))
    except (ValueError, TypeError):
        pass
    cid_variants.append(str(customer_id))
    seen = []
    for v in cid_variants:
        if v not in seen:
            seen.append(v)
    cid_variants = seen

    # Find loan trying both int and str customer_id
    base_filt = loan_id_filter(loan_id)
    loan = None
    matched_cid = None
    for cid in cid_variants:
        filt = {**base_filt, "customer_id": cid}
        loan = await db[loan_collection].find_one(filt)
        if loan:
            matched_cid = cid
            break

    if not loan or loan.get("status") != LoanStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Loan not active")

    try:
        await ensure_emi_schedule_generated(loan_collection, loan)
    except Exception:
        pass

    emi = float(loan.get("emi_per_month") or 0)
    if emi <= 0:
        raise HTTPException(status_code=400, detail="Invalid emi_per_month on loan")

    next_emi_doc = await db.emi_schedules.find_one(
        {
            "loan_id": loan.get("loan_id"),
            "customer_id": matched_cid,
            "status": {"$in": ["pending", "overdue"]},
        },
        sort=[("due_date", 1)],
    )
    penalty_amount = float(next_emi_doc.get("penalty_amount") or 0) if next_emi_doc else 0.0
    total_due = round(emi + penalty_amount, 2)

    # Debit wallet — raises HTTPException on insufficient balance or service error
    wallet_txn = await debit_wallet(
        customer_id,
        total_due,
        f"EMI payment for loan {loan.get('loan_id') or loan_id}",
    )

    remaining_tenure = int(loan.get("remaining_tenure") or 0) - 1
    # Never go negative on last EMI due to floating point rounding
    remaining_amount = max(0.0, round(float(loan.get("remaining_amount") or 0) - emi, 2))

    update_filt = {**base_filt, "customer_id": matched_cid}
    await db[loan_collection].update_one(
        update_filt,
        {
            "$set": {
                "remaining_tenure": remaining_tenure,
                "remaining_amount": remaining_amount,
                "status": LoanStatus.COMPLETED if remaining_tenure <= 0 else LoanStatus.ACTIVE,
                "next_emi_date": next_month_date(),
                "total_paid": float(loan.get("total_paid") or 0) + total_due,
                "penalties_paid_total": float(loan.get("penalties_paid_total") or 0) + penalty_amount,
            }
        },
    )

    try:
        await pay_next_installment(
            loan.get("loan_id"),
            matched_cid,
            paid_total_amount=total_due,
            paid_emi_amount=emi,
            paid_penalty_amount=penalty_amount,
        )
    except Exception:
        pass

    await write_audit_log(
        action="emi_payment_wallet",
        actor_role="customer",
        actor_id=customer_id,
        entity_type="loan",
        entity_id=loan.get("loan_id"),
        details={
            "loan_collection": loan_collection,
            "emi_amount": emi,
            "penalty_amount": penalty_amount,
            "total_paid_amount": total_due,
            "wallet_transaction_id": wallet_txn.get("transaction_id"),
            "remaining_tenure": remaining_tenure,
        },
    )
    return {"message": "EMI paid successfully", "wallet_transaction": wallet_txn}


# =========================
# PAY EMI (gateway path — for external payment providers)
# =========================
async def pay_emi_gateway(
    loan_collection: str,
    loan_id: str,
    customer_id: str | int,
    *,
    paid_total_amount: float,
    gateway: str,
    gateway_order_id: str,
):
    """Apply EMI payment collected via a payment gateway. Does NOT debit wallet."""
    db = await get_db()

    cid_variants = []
    try:
        cid_variants.append(int(customer_id))
    except (ValueError, TypeError):
        pass
    cid_variants.append(str(customer_id))
    seen = []
    for v in cid_variants:
        if v not in seen:
            seen.append(v)
    cid_variants = seen

    base_filt = loan_id_filter(loan_id)
    loan = None
    matched_cid = None
    for cid in cid_variants:
        filt = {**base_filt, "customer_id": cid}
        loan = await db[loan_collection].find_one(filt)
        if loan:
            matched_cid = cid
            break

    if not loan or loan.get("status") != LoanStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Loan not active")

    try:
        await ensure_emi_schedule_generated(loan_collection, loan)
    except Exception:
        pass

    emi = float(loan.get("emi_per_month") or 0)
    if emi <= 0:
        raise HTTPException(status_code=400, detail="Invalid EMI amount")

    next_emi_doc = await db.emi_schedules.find_one(
        {
            "loan_id": loan.get("loan_id"),
            "customer_id": matched_cid,
            "status": {"$in": ["pending", "overdue"]},
        },
        sort=[("due_date", 1)],
    )
    penalty_amount = float(next_emi_doc.get("penalty_amount") or 0) if next_emi_doc else 0.0
    expected_total = round(emi + penalty_amount, 2)

    paid = float(paid_total_amount or 0)
    if paid <= 0:
        raise HTTPException(status_code=400, detail="paid_total_amount must be > 0")
    if abs(paid - expected_total) > 1:
        raise HTTPException(
            status_code=400,
            detail=f"Paid amount mismatch. Expected: {expected_total}, Received: {paid}",
        )

    txn_id = await next_transaction_id()
    await db.transactions.insert_one(
        {
            "_id": txn_id,
            "transaction_id": txn_id,
            "customer_id": customer_id,
            "loan_id": loan.get("loan_id"),
            "type": "emi_payment_gateway",
            "amount": expected_total,
            "gateway": gateway,
            "gateway_order_id": gateway_order_id,
            "created_at": datetime.utcnow(),
        }
    )

    remaining_tenure = int(loan.get("remaining_tenure") or 0) - 1
    remaining_amount = max(0.0, round(float(loan.get("remaining_amount") or 0) - emi, 2))

    update_filt = {**base_filt, "customer_id": matched_cid}
    await db[loan_collection].update_one(
        update_filt,
        {
            "$set": {
                "remaining_tenure": remaining_tenure,
                "remaining_amount": remaining_amount,
                "status": LoanStatus.COMPLETED if remaining_tenure <= 0 else LoanStatus.ACTIVE,
                "next_emi_date": next_month_date(),
                "total_paid": float(loan.get("total_paid") or 0) + expected_total,
                "penalties_paid_total": float(loan.get("penalties_paid_total") or 0) + penalty_amount,
            }
        },
    )

    try:
        await pay_next_installment(
            loan.get("loan_id"),
            matched_cid,
            paid_total_amount=expected_total,
            paid_emi_amount=emi,
            paid_penalty_amount=penalty_amount,
        )
    except Exception:
        pass

    await write_audit_log(
        action="emi_payment_gateway",
        actor_role="customer",
        actor_id=customer_id,
        entity_type="loan",
        entity_id=loan.get("loan_id"),
        details={
            "loan_collection": loan_collection,
            "emi_amount": emi,
            "penalty_amount": penalty_amount,
            "total_paid_amount": expected_total,
            "transaction_id": txn_id,
            "gateway": gateway,
            "gateway_order_id": gateway_order_id,
            "remaining_tenure": remaining_tenure,
        },
    )
    return {"message": "EMI paid successfully"}


# =========================
# CUSTOMER LOANS
# =========================
async def list_customer_loans(customer_id: str):
    db = await get_db()

    cid_variants = [customer_id]
    try:
        cid_variants.append(int(customer_id))
    except (ValueError, TypeError):
        pass
    try:
        cid_variants.append(str(customer_id))
    except Exception:
        pass
    cid_variants = list(dict.fromkeys(cid_variants))

    filt = {"customer_id": {"$in": cid_variants}} if len(cid_variants) > 1 else {"customer_id": customer_id}

    loans = await db.personal_loans.find(filt).to_list(200)
    loans += await db.vehicle_loans.find(filt).to_list(200)
    loans += await db.education_loans.find(filt).to_list(200)
    loans += await db.home_loans.find(filt).to_list(200)

    async def enrich_emi_fields(loan: dict) -> dict:
        defaults = {
            "emi_next_due_date": loan.get("next_emi_date"),
            "emi_last_paid_at": None,
            "emi_last_paid_amount": None,
            "emi_overdue_count": 0,
            "emi_overdue_amount": 0.0,
        }
        loan_id = loan.get("loan_id")
        cust_id = loan.get("customer_id")
        if loan_id is None or cust_id is None:
            return {**loan, **defaults}
        try:
            now = datetime.utcnow()
            next_due = await db.emi_schedules.find_one(
                {
                    "loan_id": loan_id,
                    "customer_id": cust_id,
                    "status": {"$in": ["pending", "overdue"]},
                },
                sort=[("due_date", 1)],
            )
            last_paid = await db.emi_schedules.find_one(
                {
                    "loan_id": loan_id,
                    "customer_id": cust_id,
                    "status": "paid",
                    "paid_at": {"$ne": None},
                },
                sort=[("paid_at", -1)],
            )
            overdue_match = {
                "loan_id": loan_id,
                "customer_id": cust_id,
                "$or": [
                    {"status": "overdue"},
                    {"status": "pending", "due_date": {"$lt": now}},
                ],
            }
            overdue_count = await db.emi_schedules.count_documents(overdue_match)
            overdue_amount = 0.0
            try:
                agg = await db.emi_schedules.aggregate(
                    [
                        {"$match": overdue_match},
                        {
                            "$group": {
                                "_id": None,
                                "total_due": {
                                    "$sum": {
                                        "$add": [
                                            {"$ifNull": ["$emi_amount", 0]},
                                            {"$ifNull": ["$penalty_amount", 0]},
                                        ]
                                    }
                                },
                            }
                        },
                    ]
                ).to_list(length=1)
                if agg:
                    overdue_amount = float(agg[0].get("total_due") or 0)
            except Exception:
                pass
        except Exception:
            return {**loan, **defaults}

        return {
            **loan,
            "emi_next_due_date": (next_due or {}).get("due_date") or loan.get("next_emi_date"),
            "emi_last_paid_at": (last_paid or {}).get("paid_at"),
            "emi_last_paid_amount": (last_paid or {}).get("paid_amount"),
            "emi_overdue_count": int(overdue_count),
            "emi_overdue_amount": round(overdue_amount, 2),
        }

    enriched = await asyncio.gather(*(enrich_emi_fields(l) for l in loans))
    return [_sanitize_loan_doc(l) for l in enriched]


async def get_customer_emi_details(loan_id: str, customer_id: str | int) -> dict:
    db = await get_db()

    cid_variants = []
    try:
        cid_variants.append(int(customer_id))
    except (ValueError, TypeError):
        pass
    cid_variants.append(str(customer_id))
    seen = []
    for v in cid_variants:
        if v not in seen:
            seen.append(v)
    cid_variants = seen

    base_filt = loan_id_filter(loan_id)
    loan = None
    loan_collection = None
    matched_cid = None

    for col in ("personal_loans", "vehicle_loans", "education_loans", "home_loans"):
        for cid in cid_variants:
            filt = {**base_filt, "customer_id": cid}
            loan = await db[col].find_one(filt)
            if loan:
                loan_collection = col
                matched_cid = cid
                break
        if loan:
            break

    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")

    # Backfill: older foreclosures may have left EMI rows as pending/overdue
    if str(loan.get("status") or "").lower() == LoanStatus.FORECLOSED:
        paid_at = loan.get("foreclosed_at") or datetime.utcnow()
        await db.emi_schedules.update_many(
            {
                "loan_id": loan.get("loan_id"),
                "customer_id": matched_cid,
                "status": {"$in": ["pending", "overdue"]},
            },
            {"$set": {"status": "paid", "paid_at": paid_at, "updated_at": datetime.utcnow()}},
        )

    try:
        await ensure_emi_schedule_generated(loan_collection, loan)
    except Exception:
        pass

    loan_numeric_id = loan.get("loan_id")
    now = datetime.utcnow()

    upcoming = (
        await db.emi_schedules.find(
            {
                "loan_id": loan_numeric_id,
                "customer_id": matched_cid,
                "status": {"$in": ["pending", "overdue"]},
            }
        )
        .sort("due_date", 1)
        .to_list(length=500)
    )
    history = (
        await db.emi_schedules.find(
            {
                "loan_id": loan_numeric_id,
                "customer_id": matched_cid,
                "status": "paid",
            }
        )
        .sort("paid_at", -1)
        .to_list(length=500)
    )

    next_due = upcoming[0].get("due_date") if upcoming else loan.get("next_emi_date")

    overdue_match = {
        "loan_id": loan_numeric_id,
        "customer_id": matched_cid,
        "$or": [
            {"status": "overdue"},
            {"status": "pending", "due_date": {"$lt": now}},
        ],
    }
    overdue_count = 0
    overdue_amount = 0.0
    try:
        overdue_count = int(await db.emi_schedules.count_documents(overdue_match))
        agg = await db.emi_schedules.aggregate(
            [
                {"$match": overdue_match},
                {
                    "$group": {
                        "_id": None,
                        "total_due": {
                            "$sum": {
                                "$add": [
                                    {"$ifNull": ["$emi_amount", 0]},
                                    {"$ifNull": ["$penalty_amount", 0]},
                                ]
                            }
                        },
                    }
                },
            ]
        ).to_list(length=1)
        if agg:
            overdue_amount = float(agg[0].get("total_due") or 0)
    except Exception:
        pass

    def _to_row(e: dict) -> dict:
        principal = float(e.get("principal_amount") or 0)
        interest = float(e.get("interest_amount") or 0)
        penalty = float(e.get("penalty_amount") or 0)
        total_due = float(e.get("emi_amount") or (principal + interest)) + penalty
        return {
            "installment_no": e.get("installment_no"),
            "due_date": e.get("due_date"),
            "principal_amount": round(principal, 2),
            "interest_amount": round(interest, 2),
            "penalty_amount": round(penalty, 2),
            "total_due": round(total_due, 2),
            "status": e.get("status"),
        }

    return normalize_doc(
        {
            "loan_id": loan_numeric_id,
            "loan_collection": loan_collection,
            "next_due_date": next_due,
            "overdue_count": overdue_count,
            "overdue_amount": round(overdue_amount, 2),
            "upcoming": [_to_row(e) for e in upcoming],
            "history": [
                {
                    "installment_no": e.get("installment_no"),
                    "due_date": e.get("due_date"),
                    "paid_at": e.get("paid_at"),
                    "paid_amount": e.get("paid_amount"),
                    "status": e.get("status"),
                }
                for e in history
            ],
        }
    )


# =========================
# pay_emi_any_* — lookup-first pattern (fixes silent error swallowing)
# =========================
async def pay_emi_any(loan_id: str, customer_id: str):
    col = await _find_loan_collection(loan_id, customer_id)
    await pay_emi(col, loan_id, customer_id)
    return {"message": "EMI paid successfully", "collection": col}


async def pay_emi_any_wallet(loan_id: str, customer_id: str | int):
    col = await _find_loan_collection(loan_id, customer_id)
    result = await pay_emi_wallet(col, loan_id, customer_id)
    return {"message": "EMI paid successfully", "collection": col, **result}


async def pay_emi_any_gateway(
    loan_id: str,
    customer_id: str | int,
    *,
    paid_total_amount: float,
    gateway: str,
    gateway_order_id: str,
):
    col = await _find_loan_collection(loan_id, customer_id)
    await pay_emi_gateway(
        col,
        loan_id,
        customer_id,
        paid_total_amount=paid_total_amount,
        gateway=gateway,
        gateway_order_id=gateway_order_id,
    )
    return {"message": "EMI paid successfully", "collection": col}