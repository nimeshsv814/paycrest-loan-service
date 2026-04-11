"""
loan-service/app/services/sanction_service.py

Handles PDF document storage for sanction letters and NOC documents.
Stores binary data in MongoDB GridFS-style documents collection.
"""
from datetime import datetime
from bson import ObjectId
from ..database.mongo import get_db


async def store_pdf_document(
    customer_id,        # ← move this up
    doc_type: str,
    filename: str,
    data: bytes,        # ← rename from pdf_bytes to data
    metadata: dict | None = None,
) -> str:
    """
    Store a PDF as a binary document in MongoDB.
    Returns the document _id as a string.
    """
    db = await get_db()
    doc = {
        "customer_id": customer_id,
        "filename": filename,
        "doc_type": doc_type,
        "content_type": "application/pdf",
        "data": data,
        "size": len(data),
        "metadata": metadata or {},
        "created_at": datetime.utcnow(),
    }
    result = await db.documents.insert_one(doc)
    return str(result.inserted_id)


async def get_pdf_document(document_id: str) -> dict:
    """Retrieve a stored PDF document by its ID."""
    db = await get_db()
    try:
        oid = ObjectId(document_id)
    except Exception:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="Invalid document ID")
    doc = await db.documents.find_one({"_id": oid})
    if not doc:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Document not found")
    return {
        "_id": str(doc["_id"]),
        "filename": doc.get("filename"),
        "content_type": doc.get("content_type", "application/pdf"),
        "data": doc.get("data"),
        "doc_type": doc.get("doc_type"),
        "customer_id": doc.get("customer_id"),
        "created_at": doc.get("created_at"),
    }