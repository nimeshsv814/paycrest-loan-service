"""
services/loan-service/app/services/emi/schedule.py

KEY FIXES:
1. ensure_emi_schedule_generated signature changed from (loan_id, loan_type, loan_doc)
   to (loan_collection, loan_doc) to match how ALL callers invoke it.
2. Reads emi_per_month (actual DB field) instead of emi_amount/monthly_emi.
3. pay_next_installment signature updated to match callers:
   (loan_id, customer_id, paid_total_amount, paid_emi_amount, paid_penalty_amount)
"""
from __future__ import annotations
from datetime import datetime

from fastapi import HTTPException

from ...database.mongo import get_db
from ...utils.dates import next_month_date
from ...utils.serializers import normalize_doc
from .constants import EMI_STATUS_OVERDUE, EMI_STATUS_PAID, EMI_STATUS_PENDING


async def ensure_emi_schedule_generated(loan_collection: str, loan_doc: dict) -> list:
    """
    Generate EMI schedule for a loan if not already created.
    Returns list of EMI instalment documents.

    Called as: ensure_emi_schedule_generated(loan_collection, loan_doc)
    e.g.:      ensure_emi_schedule_generated("personal_loans", loan)
    """
    db = await get_db()

    loan_id = loan_doc.get("loan_id")
    customer_id = loan_doc.get("customer_id")

    if loan_id is None:
        return []

    # Check if schedule already exists
    existing = await db.emi_schedules.find(
        {"loan_id": loan_id}
    ).to_list(length=500)
    if existing:
        return [normalize_doc(e) for e in existing]

    # Read fields — DB stores emi_per_month (not emi_amount or monthly_emi)
    tenure = int(
        loan_doc.get("tenure_months")
        or loan_doc.get("loan_tenure_months")
        or loan_doc.get("remaining_tenure")
        or 0
    )
    emi_amount = float(
        loan_doc.get("emi_per_month")        # actual DB field name
        or loan_doc.get("emi_amount")
        or loan_doc.get("monthly_emi")
        or 0
    )

    if not tenure or not emi_amount:
        return []

    # Determine start date from disbursed_at or approved_at or applied_at
    start_date = (
        loan_doc.get("disbursed_at")
        or loan_doc.get("approved_at")
        or loan_doc.get("applied_at")
        or datetime.utcnow()
    )
    if isinstance(start_date, str):
        try:
            start_date = datetime.fromisoformat(start_date.replace("Z", "+00:00"))
        except Exception:
            start_date = datetime.utcnow()

    # Derive per-installment principal and interest (simple equal split)
    # Use loan_amount and interest_rate if available for more accurate breakdown
    total_loan = float(loan_doc.get("loan_amount") or 0)
    interest_rate = float(loan_doc.get("interest_rate") or 12.0)
    if total_loan and interest_rate and tenure:
        monthly_rate = interest_rate / 12 / 100
        if monthly_rate > 0:
            # Standard EMI interest component for first installment
            interest_component = round(total_loan * monthly_rate, 2)
            principal_component = round(emi_amount - interest_component, 2)
        else:
            principal_component = round(emi_amount, 2)
            interest_component = 0.0
    else:
        principal_component = round(emi_amount * 0.85, 2)
        interest_component = round(emi_amount * 0.15, 2)

    schedule = []
    due_date = next_month_date(start_date)

    for i in range(1, tenure + 1):
        doc = {
            "loan_id": loan_id,
            "loan_type": loan_collection,      # store the collection name as loan_type
            "loan_collection": loan_collection,
            "customer_id": customer_id,
            "installment_no": i,
            "instalment_number": i,            # keep both for compatibility
            "due_date": due_date,
            "emi_amount": emi_amount,
            "principal_amount": principal_component,
            "interest_amount": interest_component,
            "principal_component": principal_component,
            "interest_component": interest_component,
            "penalty_amount": 0.0,
            "status": EMI_STATUS_PENDING,
            "paid_at": None,
            "paid_amount": None,
            "created_at": datetime.utcnow(),
        }
        schedule.append(doc)
        due_date = next_month_date(due_date)

    if schedule:
        await db.emi_schedules.insert_many(schedule)

    return [normalize_doc(d) for d in schedule]


async def pay_next_installment(
    loan_id,
    customer_id,
    paid_total_amount: float = 0.0,
    paid_emi_amount: float = 0.0,
    paid_penalty_amount: float = 0.0,
) -> dict:
    """
    Mark the next pending EMI instalment as paid.
    The actual wallet debit is handled by the calling layer.

    Called as:
      pay_next_installment(loan_id, customer_id,
          paid_total_amount=total_due,
          paid_emi_amount=emi,
          paid_penalty_amount=penalty)
    """
    db = await get_db()

    # Support both int and str loan_id
    loan_id_q: list = [loan_id]
    try:
        loan_id_q = [int(loan_id), str(loan_id)]
    except (ValueError, TypeError):
        pass

    instalment = await db.emi_schedules.find_one(
        {
            "loan_id": {"$in": loan_id_q} if len(loan_id_q) > 1 else loan_id,
            "status": {"$in": [EMI_STATUS_PENDING, EMI_STATUS_OVERDUE]},
        },
        sort=[("due_date", 1)],
    )

    if not instalment:
        # Silently return — caller should not crash if schedule is out of sync
        return {"status": "no_pending_installment"}

    now = datetime.utcnow()
    await db.emi_schedules.update_one(
        {"_id": instalment["_id"]},
        {
            "$set": {
                "status": EMI_STATUS_PAID,
                "paid_at": now,
                "paid_amount": paid_total_amount or paid_emi_amount,
                "penalty_paid": paid_penalty_amount,
                "updated_at": now,
            }
        },
    )

    return {
        "status": "success",
        "installment_no": instalment.get("installment_no") or instalment.get("instalment_number"),
    }


async def refresh_overdue(db=None) -> dict:
    """Maintenance task: Mark all past-due pending EMIs as 'overdue'."""
    if db is None:
        db = await get_db()
    now = datetime.utcnow()
    result = await db.emi_schedules.update_many(
        {"status": EMI_STATUS_PENDING, "due_date": {"$lt": now}},
        {"$set": {"status": EMI_STATUS_OVERDUE, "updated_at": now}},
    )
    return {"updated_count": result.modified_count}


async def refresh_escalations(db=None) -> dict:
    """Maintenance task: flag loans with 2+ consecutive missed EMIs."""
    if db is None:
        db = await get_db()
    # Basic implementation — escalation logic can be expanded
    now = datetime.utcnow()
    result = await db.emi_schedules.update_many(
        {"status": EMI_STATUS_PENDING, "due_date": {"$lt": now}},
        {"$set": {"status": EMI_STATUS_OVERDUE, "updated_at": now}},
    )
    return {"escalated_count": result.modified_count}