"""
loan-service/app/services/emi/notifications.py

Provides create_customer_notification used by loan/noc.py.
Writes notification records directly to MongoDB.
"""
from datetime import datetime
from ...database.mongo import get_db
from ...utils.serializers import normalize_doc


async def create_customer_notification(
    customer_id,
    message: str,
    notif_type: str = "info",
    metadata: dict | None = None,
) -> dict:
    """Create a notification record for a customer."""
    db = await get_db()
    doc = {
        "customer_id": customer_id,
        "message": message,
        "type": notif_type,
        "is_read": False,
        "metadata": metadata or {},
        "created_at": datetime.utcnow(),
    }
    await db.customer_notifications.insert_one(doc)
    return normalize_doc(doc)


async def list_customer_notifications(customer_id, limit: int = 100) -> list:
    """List notifications for a customer, newest first."""
    db = await get_db()
    docs = (
        await db.customer_notifications.find({"customer_id": customer_id})
        .sort("created_at", -1)
        .to_list(length=limit)
    )
    return [normalize_doc(d) for d in docs]