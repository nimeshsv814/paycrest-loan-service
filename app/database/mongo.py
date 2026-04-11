from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import ASCENDING
from pymongo.errors import OperationFailure
from ..core.config import settings

client: AsyncIOMotorClient | None = None


async def _safe_create_index(collection, keys, **kwargs):
    try:
        await collection.create_index(keys, **kwargs)
    except OperationFailure as exc:
        if getattr(exc, "code", None) == 85 and "already exists with a different name" in str(exc):
            return
        raise


def get_client() -> AsyncIOMotorClient:
    global client
    if client is None:
        print("✅ Connecting to MongoDB at:", settings.MONGODB_URI)
        client = AsyncIOMotorClient(settings.MONGODB_URI)
    return client


async def get_db():
    return get_client()[settings.MONGODB_DB]


async def init_indexes():
    print("🚀 Initializing MongoDB indexes...")
    db = await get_db()

    await _safe_create_index(db.users, [("email", ASCENDING)], unique=True, name="uniq_email")
    await _safe_create_index(db.staff_users, [("email", ASCENDING)], unique=True, name="uniq_staff_email")
    await _safe_create_index(db.staff_users, [("role", ASCENDING)], name="staff_role_idx")

    await _safe_create_index(db.bank_accounts, [("account_number", ASCENDING)], unique=True, name="uniq_account")

    await _safe_create_index(db.personal_loans, [("customer_id", ASCENDING)], name="pl_cust_idx")
    await _safe_create_index(db.vehicle_loans, [("customer_id", ASCENDING)], name="vl_cust_idx")
    await _safe_create_index(db.education_loans, [("customer_id", ASCENDING)], name="el_cust_idx")
    await _safe_create_index(db.home_loans, [("customer_id", ASCENDING)], name="hl_cust_idx")

    await _safe_create_index(db.transactions, [("customer_id", ASCENDING)], name="txn_cust_idx")
    await _safe_create_index(db.transactions, [("loan_id", ASCENDING)], name="txn_loan_idx")

    await _safe_create_index(db.kyc_details, [("customer_id", ASCENDING)], unique=True, name="uniq_kyc_customer")

    await _safe_create_index(
        db.users,
        [("pan_number", ASCENDING)],
        unique=True,
        sparse=True,
        name="uniq_pan_number",
    )

    await _safe_create_index(
        db.kyc_details,
        [("aadhaar_number", ASCENDING)],
        unique=True,
        sparse=True,
        name="uniq_aadhaar_number"
    )

    await _safe_create_index(db.audit_logs, [("created_at", ASCENDING)], name="audit_created_at")

    # --- YOU CAN KEEP REST SAME OR REMOVE MIGRATION IF NOT NEEDED ---


# ✅ ADD THIS (MISSING FUNCTION → ROOT CAUSE FIX)
async def connect_db():
    print("🚀 Starting DB connection...")
    get_client()
    await init_indexes()
    print("✅ MongoDB ready")


# ✅ ADD THIS TOO
async def close_db():
    global client
    if client:
        client.close()
        client = None
        print("❌ MongoDB connection closed")