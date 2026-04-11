import os
from bson import Binary, ObjectId
from datetime import datetime
from fastapi import UploadFile, HTTPException
from ..database.mongo import get_db
from ..core.config import settings

MAX_FILE_SIZE = 5 * 1024 * 1024  # 5MB

ALLOWED_TYPES = {
    "pan_card": ["application/pdf"],
    "aadhar_card": ["application/pdf"],
    "photo": ["image/jpeg", "image/png"],
    "pay_slip": ["application/pdf"],
    "vehicle_price_doc": ["application/pdf"],
    "signed_sanction_letter": ["application/pdf"],
    "home_property_doc": ["application/pdf"],
    "fees_structure": ["application/pdf"],
    "bonafide_certificate": ["application/pdf"],
    "collateral_doc": ["application/pdf"],
}


def _normalize_customer_id(cid):
    try:
        if isinstance(cid, str) and cid.isdigit():
            return int(cid)
    except Exception:
        pass
    return cid


async def upload_document(
    file: UploadFile,
    customer_id: int | str,
    doc_type: str
) -> str:
    db = await get_db()
    customer_id = _normalize_customer_id(customer_id)

    content = await file.read()

    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(status_code=400, detail="File size exceeds 5MB")

    if doc_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail="Invalid document type")

    if file.content_type not in ALLOWED_TYPES[doc_type]:
        raise HTTPException(status_code=400, detail="Invalid file type")

    # ✅ USE UPLOAD_BASE_PATH INSTEAD OF MONGO BINARY
    os.makedirs(settings.UPLOAD_BASE_PATH, exist_ok=True)
    file_id = f"{customer_id}_{doc_type}_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}_{file.filename}"
    file_path = os.path.join(settings.UPLOAD_BASE_PATH, file_id)
    
    with open(file_path, "wb") as f:
        f.write(content)

    doc = {
        "customer_id": customer_id,
        "doc_type": doc_type,
        "filename": file.filename,
        "content_type": file.content_type,
        "size": len(content),
        "file_path": file_path,  # ✅ STORE THE PATH
        "uploaded_at": datetime.utcnow(),
    }

    res = await db.documents.insert_one(doc)
    return str(res.inserted_id)


async def get_document_binary(document_id: str | ObjectId):
    db = await get_db()

    if isinstance(document_id, str):
        document_id = ObjectId(document_id)

    doc = await db.documents.find_one({"_id": document_id})
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # ✅ IF DOCUMENT HAS A FILE PATH, READ FROM DISK
    if "file_path" in doc:
        file_path = doc["file_path"]
        if os.path.exists(file_path):
            with open(file_path, "rb") as f:
                doc["data"] = f.read()
        else:
            raise HTTPException(status_code=404, detail="Document file not found on disk")

    return doc
