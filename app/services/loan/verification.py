from datetime import datetime

from fastapi import HTTPException

from ...database.mongo import get_db
from ...models.enums import LoanStatus
from ...utils.id import loan_id_filter

from ..audit_service import write_audit_log
from .actor_meta import resolve_actor_meta


# =========================
# ASSIGN TO VERIFICATION
# =========================
async def assign_verification(
    loan_collection: str,
    loan_id: str,
    verification_id: str,
    assigned_by_id: str | int | None = None,
):
    db = await get_db()
    filt = loan_id_filter(loan_id)
    now = datetime.utcnow()
    assigned_by = await resolve_actor_meta(assigned_by_id, fallback_role="manager")
    assigned_to = await resolve_actor_meta(verification_id, fallback_role="verification")

    await db[loan_collection].update_one(
        filt,
        {"$set": {
            "verification_id": verification_id,
            "status": LoanStatus.ASSIGNED_TO_VERIFICATION,
            "verification_assigned_at": now,
            "verification_assigned_by_id": assigned_by["actor_id"],
            "verification_assigned_by_name": assigned_by["actor_name"],
            "verification_assigned_by_role": assigned_by["actor_role"],
            "verification_assigned_to_id": assigned_to["actor_id"],
            "verification_assigned_to_name": assigned_to["actor_name"],
            "verification_assigned_to_role": assigned_to["actor_role"],
        }}
    )
    return {"message": "Loan assigned to verification team"}


# =========================
# VERIFICATION COMPLETE
# =========================
async def verification_complete(
    loan_collection: str,
    loan_id: str,
    approved: bool,
    verified_by_id: str | int | None = None,
):
    db = await get_db()
    filt = loan_id_filter(loan_id)
    loan = await db[loan_collection].find_one(filt)
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")

    if loan.get("status") != LoanStatus.ASSIGNED_TO_VERIFICATION:
        raise HTTPException(status_code=400, detail="Loan not assigned to verification")

    now = datetime.utcnow()
    verifier = await resolve_actor_meta(verified_by_id or loan.get("verification_id"), fallback_role="verification")

    if approved:
        await db[loan_collection].update_one(
            filt,
            {"$set": {
                "status": LoanStatus.VERIFICATION_DONE,
                "verification_completed_at": now,
                "verification_completed_by_id": verifier["actor_id"],
                "verification_completed_by_name": verifier["actor_name"],
                "verification_completed_by_role": verifier["actor_role"],
            }}
        )
        await write_audit_log(
            action="loan_verification_approve",
            actor_role="verification",
            actor_id=verifier["actor_id"],
            entity_type="loan",
            entity_id=loan.get("loan_id"),
            details={"loan_collection": loan_collection},
        )
        return {
            "message": "Loan verified successfully",
            "loan_id": int(loan_id),
            "status": LoanStatus.VERIFICATION_DONE
        }
    else:
        await db[loan_collection].update_one(
            filt,
            {
                "$set": {
                    "status": LoanStatus.REJECTED,
                    "verification_completed_at": now,
                    "rejected_by": "verification",
                    "rejected_at": now,
                    "rejected_by_id": verifier["actor_id"],
                    "rejected_by_name": verifier["actor_name"],
                    "rejected_by_role": verifier["actor_role"],
                    "verification_completed_by_id": verifier["actor_id"],
                    "verification_completed_by_name": verifier["actor_name"],
                    "verification_completed_by_role": verifier["actor_role"],
                }
            }
        )
        await write_audit_log(
            action="loan_verification_reject",
            actor_role="verification",
            actor_id=verifier["actor_id"],
            entity_type="loan",
            entity_id=loan.get("loan_id"),
            details={"loan_collection": loan_collection},
        )
        return {
            "message": "Loan rejected during verification",
            "loan_id": int(loan_id),
            "status": LoanStatus.REJECTED
        }
