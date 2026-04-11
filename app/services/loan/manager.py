from datetime import datetime

from fastapi import HTTPException

from ...database.mongo import get_db
from ...models.enums import LoanStatus
from ...utils.id import loan_id_filter
from ...utils.serializers import normalize_doc

from ..audit_service import write_audit_log
from ..settings_service import get_settings
from .actor_meta import resolve_actor_meta

from .calculations import compute_emi


# =========================
# MANAGER DASHBOARD
# =========================
async def list_manager_loans(manager_id: str):
    from ...database.mongo import get_db
    db = await get_db()
 
    # ✅ FIX Bug 3: include all four loan types
    loans = await db.personal_loans.find({}).to_list(200)
    loans += await db.vehicle_loans.find({}).to_list(200)
    loans += await db.education_loans.find({}).to_list(200)
    loans += await db.home_loans.find({}).to_list(200)
 
    return [normalize_doc(l) for l in loans]


# =========================
# MANAGER APPROVAL
# =========================
async def manager_approve_or_reject(loan_collection: str, loan_id: str, manager_id: str, approve: bool):
    db = await get_db()
    filt = loan_id_filter(loan_id)
    loan = await db[loan_collection].find_one(filt)

    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")

    if loan.get("status") != LoanStatus.VERIFICATION_DONE:
        raise HTTPException(status_code=400, detail="Loan not ready for manager decision")

    now = datetime.utcnow()
    manager = await resolve_actor_meta(manager_id, fallback_role="manager")

    if not approve:
        await db[loan_collection].update_one(
            filt,
            {
                "$set": {
                    "status": LoanStatus.REJECTED,
                    "manager_id": manager_id,
                    "rejected_by": "manager",
                    "rejected_at": now,
                    "rejected_by_id": manager["actor_id"],
                    "rejected_by_name": manager["actor_name"],
                    "rejected_by_role": manager["actor_role"],
                    "manager_reviewed_at": now,
                    "manager_reviewed_by_id": manager["actor_id"],
                    "manager_reviewed_by_name": manager["actor_name"],
                    "manager_reviewed_by_role": manager["actor_role"],
                }
            }
        )
        # record manager rejection timestamp
        await write_audit_log(
            action="manager_reject",
            actor_role="manager",
            actor_id=manager_id,
            entity_type="loan",
            entity_id=loan.get("loan_id"),
            details={"loan_collection": loan_collection},
        )
        return {
            "message": "Loan rejected by manager",
            "loan_id": loan["loan_id"],
            "status": LoanStatus.REJECTED
        }

    amount = float(loan["loan_amount"])
    tenure = int(loan.get("tenure_months") or 0)
    settings = await get_settings()
    default_interest = (
        float(settings["vehicle_loan_interest"])
        if loan_collection == "vehicle_loans"
        else float(settings["personal_loan_interest"])
    )
    interest_rate = float(loan.get("interest_rate") or default_interest)
    approved_amount = float(loan.get("approved_amount") or amount)
    emi = compute_emi(approved_amount, interest_rate, tenure) if tenure > 0 else loan.get("emi_per_month")
    remaining_amount = round(float(emi or 0) * tenure, 2) if tenure > 0 else loan.get("remaining_amount")

    # Loans <= 15 Lakhs are approved by manager (doc rule) and then go to Admin only for sanction/disbursement.
    if amount <= 1500000:
        await db[loan_collection].update_one(
            filt,
            {"$set": {
                "status": LoanStatus.MANAGER_APPROVED,
                "manager_id": manager_id,
                "manager_approved_at": now,
                "manager_reviewed_at": now,
                "manager_reviewed_by_id": manager["actor_id"],
                "manager_reviewed_by_name": manager["actor_name"],
                "manager_reviewed_by_role": manager["actor_role"],
                "manager_approved_by_id": manager["actor_id"],
                "manager_approved_by_name": manager["actor_name"],
                "manager_approved_by_role": manager["actor_role"],
                "approved_at": now,
                "approved_amount": approved_amount,
                "interest_rate": interest_rate,
                "emi_per_month": emi,
                "remaining_amount": remaining_amount,
            }}
        )
        await write_audit_log(
            action="manager_approve",
            actor_role="manager",
            actor_id=manager_id,
            entity_type="loan",
            entity_id=loan.get("loan_id"),
            details={
                "loan_collection": loan_collection,
                "approved_amount": approved_amount,
                "interest_rate": interest_rate,
            },
        )
        return {
            "message": "Loan approved by manager and forwarded for admin sanction/disbursement",
            "loan_id": loan["loan_id"],
            "status": LoanStatus.MANAGER_APPROVED,
        }

    # Loans > 15 Lakhs must be forwarded for Admin approval (doc rule).
    await db[loan_collection].update_one(
        filt,
        {"$set": {
            "status": LoanStatus.PENDING_ADMIN_APPROVAL,
            "manager_id": manager_id,
            "manager_forwarded_at": now,
            "manager_reviewed_at": now,
            "manager_reviewed_by_id": manager["actor_id"],
            "manager_reviewed_by_name": manager["actor_name"],
            "manager_reviewed_by_role": manager["actor_role"],
            "approved_amount": approved_amount,
            "interest_rate": interest_rate,
            "emi_per_month": emi,
            "remaining_amount": remaining_amount,
        }}
    )
    await write_audit_log(
        action="manager_forward_to_admin",
        actor_role="manager",
        actor_id=manager_id,
        entity_type="loan",
        entity_id=loan.get("loan_id"),
        details={
            "loan_collection": loan_collection,
            "approved_amount": approved_amount,
            "interest_rate": interest_rate,
        },
    )
    return {
        "message": "Loan forwarded for admin approval",
        "loan_id": loan["loan_id"],
        "status": LoanStatus.PENDING_ADMIN_APPROVAL,
    }


async def manager_forward_to_admin(
    loan_collection: str,
    loan_id: str,
    manager_id: str,
    recommendation: str | None = None,
    remarks: str | None = None,
):
    """Forward a high-value loan (>15L) to admin with recommendation/remarks (doc endpoint)."""
    db = await get_db()
    filt = loan_id_filter(loan_id)
    loan = await db[loan_collection].find_one(filt)

    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")

    if loan.get("status") != LoanStatus.VERIFICATION_DONE:
        raise HTTPException(status_code=400, detail="Loan not ready to forward to admin")

    amount = float(loan.get("loan_amount") or 0)
    if amount <= 1500000:
        raise HTTPException(status_code=400, detail="Forward-to-admin is only for loans above 15,00,000")

    now = datetime.utcnow()
    manager = await resolve_actor_meta(manager_id, fallback_role="manager")

    await db[loan_collection].update_one(
        filt,
        {"$set": {
            "status": LoanStatus.PENDING_ADMIN_APPROVAL,
            "manager_id": manager_id,
            "manager_forwarded_at": now,
            "manager_reviewed_at": now,
            "manager_reviewed_by_id": manager["actor_id"],
            "manager_reviewed_by_name": manager["actor_name"],
            "manager_reviewed_by_role": manager["actor_role"],
            "manager_recommendation": recommendation,
            "manager_remarks": remarks,
        }}
    )

    await write_audit_log(
        action="manager_forward_to_admin",
        actor_role="manager",
        actor_id=manager_id,
        entity_type="loan",
        entity_id=loan.get("loan_id"),
        details={
            "loan_collection": loan_collection,
            "recommendation": recommendation,
            "remarks": remarks,
        },
    )

    return {
        "message": "Loan forwarded to admin",
        "loan_id": loan.get("loan_id"),
        "status": LoanStatus.PENDING_ADMIN_APPROVAL,
    }


async def manager_verify_signed_sanction(loan_id: str, manager_id: str, approve: bool, remarks: str | None = None):
    db = await get_db()
    filt = loan_id_filter(loan_id)

    loan = await db.personal_loans.find_one(filt)
    loan_collection = "personal_loans"
    if not loan:
        loan = await db.vehicle_loans.find_one(filt)
        loan_collection = "vehicle_loans"
    if not loan:
        loan = await db.education_loans.find_one(filt)
        loan_collection = "education_loans"
    if not loan:
        loan = await db.home_loans.find_one(filt)
        loan_collection = "home_loans"

    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")

    if loan.get("status") != LoanStatus.SIGNED_RECEIVED:
        raise HTTPException(status_code=400, detail="No signed sanction letter pending verification")

    now = datetime.utcnow()
    manager = await resolve_actor_meta(manager_id, fallback_role="manager")

    if approve:
        next_status = LoanStatus.READY_FOR_DISBURSEMENT
    else:
        next_status = LoanStatus.SANCTION_SENT

    updates = {
        "status": next_status,
        "signature_verified_by": manager_id,
        "signature_verified_at": now,
        "signature_verification_remarks": remarks,
        "signature_verified_by_id": manager["actor_id"],
        "signature_verified_by_name": manager["actor_name"],
        "signature_verified_by_role": manager["actor_role"],
    }
    if approve:
        updates["ready_for_disbursement_at"] = now
    else:
        updates["sanction_sent_at"] = now

    await db[loan_collection].update_one(
        filt,
        {"$set": updates}
    )

    await write_audit_log(
        action="sanction_signature_verify",
        actor_role="manager",
        actor_id=manager_id,
        entity_type="loan",
        entity_id=loan.get("loan_id"),
        details={"loan_collection": loan_collection, "approve": bool(approve), "remarks": remarks},
    )

    return {
        "message": "Signature verified" if approve else "Signature rejected",
        "loan_id": loan.get("loan_id"),
        "status": next_status,
    }
