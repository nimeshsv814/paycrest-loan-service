from datetime import datetime
from ..database.mongo import get_db
from ..core.config import settings
from ..utils.sequences import next_account_number, next_transaction_id
from ..utils.serializers import normalize_doc


async def auto_create_account_for(customer_id: str) -> dict:
    db = await get_db()
    account_number = await next_account_number()
    doc = {
        "customer_id": customer_id,
        "account_number": account_number,
        "ifsc_code": settings.DEFAULT_IFSC,
        "balance": 0.0,
        "created_at": datetime.utcnow(),
    }
    res = await db.bank_accounts.insert_one(doc)
    out = {"_id": str(res.inserted_id), **doc}
    return normalize_doc(out)


async def add_money(customer_id: str, amount: float) -> dict:
    db = await get_db()
    acc = await db.bank_accounts.find_one({"customer_id": customer_id})
    if not acc:
        raise Exception("Account not found")
    new_balance = float(acc.get("balance", 0)) + amount
    await db.bank_accounts.update_one(
        {"_id": acc["_id"]}, {"$set": {"balance": new_balance}}
    )
    tid = await next_transaction_id()
    txn = {
        "_id": tid,
        "transaction_id": tid,
        "customer_id": customer_id,
        "loan_id": None,
        "loan_type": None,
        "type": "credit",
        "amount": amount,
        "balance_after": new_balance,
        "created_at": datetime.utcnow(),
    }
    await db.transactions.insert_one(txn)
    n = normalize_doc(txn)
    return {"transaction_id": n.get("_id"), "balance": new_balance}