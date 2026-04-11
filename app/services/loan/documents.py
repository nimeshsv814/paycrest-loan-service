from datetime import datetime

from bson import ObjectId
from fastapi import HTTPException

from ...database.mongo import get_db
from ...models.enums import LoanStatus
from ...utils.id import loan_id_filter

from ..audit_service import write_audit_log

from .queries import _find_loan_any_by_customer


async def upload_signed_sanction_letter(loan_id: str, customer_id: str | int, document_id: str):
    db = await get_db()
    loan_collection, loan = await _find_loan_any_by_customer(loan_id, customer_id)
    if not loan_collection or not loan:
        raise HTTPException(status_code=404, detail="Loan not found")

    if loan.get("status") != LoanStatus.SANCTION_SENT:
        raise HTTPException(status_code=400, detail="Sanction letter not available for signing")

    await db[loan_collection].update_one(
        loan_id_filter(loan_id),
        {"$set": {
            "signed_sanction_letter_document_id": document_id,
            "status": LoanStatus.SIGNED_RECEIVED,
            "signed_uploaded_at": datetime.utcnow(),
        }}
    )

    await write_audit_log(
        action="sanction_letter_signed_upload",
        actor_role="customer",
        actor_id=customer_id,
        entity_type="loan",
        entity_id=loan.get("loan_id"),
        details={"loan_collection": loan_collection, "document_id": document_id},
    )

    return {
        "message": "Signed sanction letter uploaded",
        "loan_id": loan.get("loan_id"),
        "status": LoanStatus.SIGNED_RECEIVED,
    }


async def attach_loan_document(
    loan_collection: str,
    loan_id: int,
    doc_type: str,
    document_id: str
):
    db = await get_db()
    await db[loan_collection].update_one(
        {"loan_id": loan_id},
        {"$set": {doc_type: ObjectId(document_id)}}
    )

