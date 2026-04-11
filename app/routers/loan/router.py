"""
services/loan-service/app/routers/loan/router.py

Internal routes called by other microservices (not the frontend).
Protected by X-Internal-Token header only.
"""
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from ...core.config import settings
from ...database.mongo import get_db
from ...models.enums import LoanStatus
from ...utils.serializers import normalize_doc
from ...services.loan.customer import pay_emi_any_wallet

router = APIRouter(prefix="/internal", tags=["internal"])


def _check_token(x_internal_token: str):
    if x_internal_token != settings.INTERNAL_SERVICE_TOKEN:
        raise HTTPException(status_code=403, detail="Invalid internal token")


# ── Verification complete ─────────────────────────────────────
class VerificationCompletePayload(BaseModel):
    loan_collection: str
    loan_id: str
    approved: bool
    verifier_id: str


@router.post("/verification-complete")
async def verification_complete(
    payload: VerificationCompletePayload,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    _check_token(x_internal_token)

    db = await get_db()
    collection_map = {
        "personal_loans": db.personal_loans,
        "vehicle_loans": db.vehicle_loans,
        "education_loans": db.education_loans,
        "home_loans": db.home_loans,
    }
    collection = collection_map.get(payload.loan_collection)
    if collection is None:
        raise HTTPException(status_code=400, detail=f"Unknown loan collection: {payload.loan_collection}")

    loan = None
    try:
        loan = await collection.find_one({"loan_id": int(payload.loan_id)})
    except (ValueError, TypeError):
        pass
    if not loan:
        loan = await collection.find_one({"loan_id": payload.loan_id})
    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")

    now = datetime.utcnow()

    if payload.approved:
        update = {
            "status": LoanStatus.VERIFICATION_DONE,
            "verified_by": payload.verifier_id,
            "verification_approved": True,
            "verification_completed_at": now,
            "verification_completed_by_id": payload.verifier_id,
        }
    else:
        update = {
            "status": LoanStatus.REJECTED,
            "verified_by": payload.verifier_id,
            "verification_approved": False,
            "verification_completed_at": now,
            "rejected_by": "verification",
            "rejected_at": now,
            "rejected_by_id": payload.verifier_id,
        }

    await collection.update_one({"_id": loan["_id"]}, {"$set": update})
    updated = await collection.find_one({"_id": loan["_id"]})
    return normalize_doc(updated)


# ── Internal EMI payment (called by payment-service) ─────────
class PayEmiPayload(BaseModel):
    customer_id: str


@router.post("/pay-emi/{loan_id}")
async def internal_pay_emi(
    loan_id: str,
    payload: PayEmiPayload,
    x_internal_token: str = Header(..., alias="X-Internal-Token"),
):
    """Pay next EMI from wallet. Called internally by payment-service."""
    _check_token(x_internal_token)
    return await pay_emi_any_wallet(loan_id, payload.customer_id)