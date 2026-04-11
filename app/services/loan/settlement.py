from datetime import datetime

from fastapi import HTTPException

from ...database.mongo import get_db
from ...models.enums import LoanStatus

from ..audit_service import write_audit_log
from ..emi import ensure_emi_schedule_generated
from ..settings_service import get_settings
from ..wallet_service import debit_wallet

from .noc import issue_foreclosure_noc
from .queries import _find_loan_any, _find_loan_any_by_customer


# =========================
# FORECLOSURE / SETTLEMENT
# =========================
def _customer_match(customer_id: str | int):
    vals: list[str | int] = [customer_id]
    if isinstance(customer_id, str) and customer_id.isdigit():
        vals.append(int(customer_id))
    elif isinstance(customer_id, int):
        vals.append(str(customer_id))
    uniq: list[str | int] = []
    for v in vals:
        if v not in uniq:
            uniq.append(v)
    return {"customer_id": uniq[0]} if len(uniq) == 1 else {"customer_id": {"$in": uniq}}


async def calculate_settlement_any(loan_id: str, customer_id: str | int) -> dict:
    """Calculate a foreclosure/settlement amount for a given loan and customer.

    Returns a breakdown with remaining_amount, penalties, foreclosure_fee_percentage and total_settlement.
    """
    db = await get_db()
    loan_collection, loan = await _find_loan_any_by_customer(loan_id, customer_id)
    if not loan_collection or not loan:
        raise HTTPException(status_code=404, detail="Loan not found")

    # Ensure schedule exists to correctly compute penalties
    try:
        await ensure_emi_schedule_generated(loan_collection, loan)
    except Exception:
        pass

    remaining_amount = float(loan.get("remaining_amount") or 0.0)

    # Sum penalty amounts on pending/overdue installments (best-effort)
    penalties = 0.0
    try:
        agg = await db.emi_schedules.aggregate(
            [
                {"$match": {"loan_id": loan.get("loan_id"), **_customer_match(customer_id), "status": {"$in": ["pending", "overdue"]}}},
                {"$group": {"_id": None, "total_penalty": {"$sum": {"$ifNull": ["$penalty_amount", 0]}}}},
            ]
        ).to_list(length=1)
        if agg:
            penalties = float(agg[0].get("total_penalty") or 0.0)
    except Exception:
        penalties = 0.0

    settings = await get_settings()
    fee_pct = float(settings.get("foreclosure_fee_percentage") if settings and settings.get("foreclosure_fee_percentage") is not None else 2.0)

    base = round(remaining_amount + penalties, 2)
    fee = round(base * (fee_pct / 100.0), 2)
    settlement = round(base + fee, 2)

    return {
        "loan_id": loan.get("loan_id"),
        "loan_collection": loan_collection,
        "remaining_amount": round(remaining_amount, 2),
        "pending_penalties": round(penalties, 2),
        "foreclosure_fee_percentage": fee_pct,
        "foreclosure_fee": fee,
        "settlement_amount": settlement,
    }


async def foreclose_any(loan_id: str, customer_id: str | int) -> dict:
    """Perform foreclosure settlement: debit customer account, create transaction, mark loan foreclosed."""
    db = await get_db()
    loan_collection, loan = await _find_loan_any_by_customer(loan_id, customer_id)
    if not loan_collection or not loan:
        raise HTTPException(status_code=404, detail="Loan not found")

    if loan.get("status") != LoanStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Loan not active or cannot be foreclosed")

    settlement_info = await calculate_settlement_any(loan_id, customer_id)
    amount = float(settlement_info.get("settlement_amount") or 0.0)

    wallet_txn = await debit_wallet(
        customer_id,
        amount,
        f"Foreclosure settlement for loan {loan.get('loan_id') or loan_id}",
    )

    await db.transactions.update_one(
        {"transaction_id": wallet_txn.get("transaction_id")},
        {
            "$set": {
                "loan_id": loan.get("loan_id"),
                "type": "foreclosure",
                "balance_after": wallet_txn.get("new_balance"),
                "created_at": datetime.utcnow(),
            }
        },
    )

    # Mark loan foreclosed/closed
    foreclosed_at = datetime.utcnow()
    await db[loan_collection].update_one(
        {"loan_id": loan.get("loan_id")},
        {"$set": {
            "status": "foreclosed",
            "remaining_amount": 0.0,
            "remaining_tenure": 0,
            "next_emi_date": None,
            "total_paid": float(loan.get("total_paid") or 0) + amount,
            "foreclosed_at": foreclosed_at,
        }}
    )
    loan["foreclosed_at"] = foreclosed_at

    # Move all pending/overdue installments to paid so schedule/history stays consistent
    # after foreclosure settlement.
    await db.emi_schedules.update_many(
        {
            "loan_id": loan.get("loan_id"),
            **_customer_match(customer_id),
            "status": {"$in": ["pending", "overdue"]},
        },
        {
            "$set": {
                "status": "paid",
                "paid_at": foreclosed_at,
                "updated_at": foreclosed_at,
            }
        },
    )

    await write_audit_log(
        action="loan_foreclosure",
        actor_role="customer",
        actor_id=customer_id,
        entity_type="loan",
        entity_id=loan.get("loan_id"),
        details={
            "loan_collection": loan_collection,
            "settlement_amount": amount,
            "transaction_id": wallet_txn.get("transaction_id"),
            "wallet_new_balance": wallet_txn.get("new_balance"),
        },
    )

    noc = await issue_foreclosure_noc(
        loan_collection=loan_collection,
        loan=loan,
        settlement_amount=amount,
        transaction_id=wallet_txn.get("transaction_id"),
        actor_role="customer",
        actor_id=customer_id,
    )
    return {
        "message": "Loan foreclosed successfully",
        "loan_id": loan.get("loan_id"),
        "settlement_amount": amount,
        "noc_issued": True,
        "noc_number": noc.get("noc_number"),
        "noc_document_id": noc.get("document_id"),
    }


async def calculate_settlement_admin(loan_id: str) -> dict:
    """Calculate settlement for any loan (manager/admin view)."""
    loan_collection, loan = await _find_loan_any(loan_id)
    if not loan_collection or not loan:
        raise HTTPException(status_code=404, detail="Loan not found")
    # reuse existing customer-aware calculation by delegating with customer_id
    return await calculate_settlement_any(loan_id, loan.get("customer_id"))


async def manager_foreclose_any(loan_id: str, manager_id: str | int, fee_override_pct: float | None = None) -> dict:
    """Manager-initiated foreclosure with optional fee override. This debits the customer's account and marks loan foreclosed.

    WARNING: action is destructive; ensure audit logs.
    """
    db = await get_db()
    loan_collection, loan = await _find_loan_any(loan_id)
    if not loan_collection or not loan:
        raise HTTPException(status_code=404, detail="Loan not found")

    if loan.get("status") != LoanStatus.ACTIVE:
        raise HTTPException(status_code=400, detail="Loan not active or cannot be foreclosed")

    # compute settlement components
    remaining_amount = float(loan.get("remaining_amount") or 0.0)
    penalties = 0.0
    try:
        agg = await db.emi_schedules.aggregate(
            [
                {"$match": {"loan_id": loan.get("loan_id"), "customer_id": loan.get("customer_id"), "status": {"$in": ["pending", "overdue"]}}},
                {"$group": {"_id": None, "total_penalty": {"$sum": {"$ifNull": ["$penalty_amount", 0]}}}},
            ]
        ).to_list(length=1)
        if agg:
            penalties = float(agg[0].get("total_penalty") or 0.0)
    except Exception:
        penalties = 0.0

    settings = await get_settings()
    default_fee_pct = float(settings.get("foreclosure_fee_percentage") if settings and settings.get("foreclosure_fee_percentage") is not None else 2.0)
    fee_pct = float(fee_override_pct) if fee_override_pct is not None else default_fee_pct

    base = round(remaining_amount + penalties, 2)
    fee = round(base * (fee_pct / 100.0), 2)
    settlement = round(base + fee, 2)

    cid = loan.get("customer_id")
    wallet_txn = await debit_wallet(
        cid,
        settlement,
        f"Foreclosure settlement for loan {loan.get('loan_id') or loan_id}",
    )
    await db.transactions.update_one(
        {"transaction_id": wallet_txn.get("transaction_id")},
        {
            "$set": {
                "loan_id": loan.get("loan_id"),
                "type": "foreclosure",
                "balance_after": wallet_txn.get("new_balance"),
                "created_at": datetime.utcnow(),
            }
        },
    )

    foreclosed_at = datetime.utcnow()
    await db[loan_collection].update_one(
        {"loan_id": loan.get("loan_id")},
        {"$set": {
            "status": LoanStatus.FORECLOSED,
            "remaining_amount": 0.0,
            "remaining_tenure": 0,
            "next_emi_date": None,
            "total_paid": float(loan.get("total_paid") or 0) + settlement,
            "foreclosed_at": foreclosed_at,
            "foreclosure_handled_by": str(manager_id),
            "foreclosure_fee_pct": fee_pct,
        }}
    )
    loan["foreclosed_at"] = foreclosed_at

    # Move all pending/overdue installments to paid so schedule/history stays consistent
    # after foreclosure settlement.
    await db.emi_schedules.update_many(
        {
            "loan_id": loan.get("loan_id"),
            "customer_id": loan.get("customer_id"),
            "status": {"$in": ["pending", "overdue"]},
        },
        {
            "$set": {
                "status": "paid",
                "paid_at": foreclosed_at,
                "updated_at": foreclosed_at,
            }
        },
    )

    await write_audit_log(
        action="manager_loan_foreclosure",
        actor_role="manager",
        actor_id=manager_id,
        entity_type="loan",
        entity_id=loan.get("loan_id"),
        details={
            "loan_collection": loan_collection,
            "settlement_amount": settlement,
            "fee_pct": fee_pct,
            "transaction_id": wallet_txn.get("transaction_id"),
            "wallet_new_balance": wallet_txn.get("new_balance"),
        },
    )

    noc = await issue_foreclosure_noc(
        loan_collection=loan_collection,
        loan=loan,
        settlement_amount=settlement,
        transaction_id=wallet_txn.get("transaction_id"),
        actor_role="manager",
        actor_id=manager_id,
    )
    return {
        "message": "Loan foreclosed by manager",
        "loan_id": loan.get("loan_id"),
        "settlement_amount": settlement,
        "fee_pct": fee_pct,
        "noc_issued": True,
        "noc_number": noc.get("noc_number"),
        "noc_document_id": noc.get("document_id"),
    }
