from datetime import datetime
import random
from bson import ObjectId
from fastapi import HTTPException
from ..database.mongo import get_db
from ..services.audit_service import write_audit_log
from ..utils.serializers import normalize_doc


def _normalize_customer_id(cid):
    try:
        if isinstance(cid, str) and cid.isdigit():
            return int(cid)
    except Exception:
        pass
    return cid


def _normalize_pan(value) -> str:
    return str(value or "").strip().upper()


def _normalize_aadhaar(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _mask_pan(value: str) -> str:
    pan = _normalize_pan(value)
    if len(pan) != 10:
        return "-" if not pan else pan
    return f"{pan[:2]}******{pan[-2:]}"


def _mask_aadhaar(value: str) -> str:
    aadhaar = _normalize_aadhaar(value)
    if len(aadhaar) < 4:
        return "-" if not aadhaar else aadhaar
    return f"XXXX-XXXX-{aadhaar[-4:]}"


def _sanitize_kyc_doc(doc: dict | None, *, include_sensitive: bool = False) -> dict | None:
    if not doc:
        return doc
    out = normalize_doc(doc)
    pan_raw = _normalize_pan(out.get("pan_number"))
    aadhaar_raw = _normalize_aadhaar(out.get("aadhaar_number") or out.get("aadhar_number"))
    if not out.get("pan_masked") and pan_raw:
        out["pan_masked"] = _mask_pan(pan_raw)
    if not out.get("aadhaar_masked") and aadhaar_raw:
        out["aadhaar_masked"] = _mask_aadhaar(aadhaar_raw)
    if include_sensitive:
        out["pan_number"] = pan_raw or None
        out["aadhaar_number"] = aadhaar_raw or None
        out.pop("aadhar_number", None)
    else:
        out.pop("pan_number", None)
        out.pop("aadhaar_number", None)
        out.pop("aadhar_number", None)
    out.pop("pan_hash", None)
    out.pop("aadhaar_hash", None)
    return out


async def submit_kyc(customer_id: str, payload: dict) -> dict:
    db = await get_db()
    customer_id = _normalize_customer_id(customer_id)
    existing = await db.kyc_details.find_one({"customer_id": customer_id})
    user = await db.users.find_one({"customer_id": customer_id})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    errors = {}

    def norm(v):
        return str(v).strip().lower() if v is not None else None

    pan_number = _normalize_pan(payload.get("pan_number"))
    aadhaar_number = _normalize_aadhaar(payload.get("aadhaar_number"))
    if payload.get("full_name") and norm(payload["full_name"]) != norm(user.get("full_name")):
        errors["full_name"] = "Full name does not match registration"
    if payload.get("dob"):
        dob = payload["dob"]
        dob = dob.isoformat() if hasattr(dob, "isoformat") else str(dob)
        if norm(dob) != norm(user.get("dob")):
            errors["dob"] = "Date of birth does not match registration"
        payload["dob"] = dob
    user_pan = _normalize_pan(user.get("pan_number"))
    if pan_number:
        if not (user_pan and user_pan == pan_number):
            errors["pan_number"] = "PAN number does not match registration"
    if pan_number:
        pan_exists = await db.users.find_one(
            {"customer_id": {"$ne": customer_id}, "pan_number": pan_number}
        )
        if pan_exists:
            errors["pan_number"] = "PAN number already registered with another user"
    if aadhaar_number:
        aadhaar_exists = await db.kyc_details.find_one(
            {
                "customer_id": {"$ne": customer_id},
                "$or": [{"aadhaar_number": aadhaar_number}, {"aadhar_number": aadhaar_number}],
            }
        )
        if aadhaar_exists:
            errors["aadhaar_number"] = "Aadhaar number already used by another customer"
    if errors:
        raise HTTPException(status_code=400, detail={"error": "KYC validation failed", "details": errors})

    safe_payload = dict(payload)
    if pan_number:
        safe_payload["pan_number"] = pan_number
        safe_payload["pan_last4"] = pan_number[-4:]
        safe_payload["pan_masked"] = _mask_pan(pan_number)
    if aadhaar_number:
        safe_payload["aadhaar_number"] = aadhaar_number
        safe_payload["aadhaar_last4"] = aadhaar_number[-4:]
        safe_payload["aadhaar_masked"] = _mask_aadhaar(aadhaar_number)

    doc = {
        **safe_payload,
        "customer_id": customer_id,
        "employment_score": None,
        "income_score": None,
        "emi_score": None,
        "experience_score": None,
        "total_score": None,
        "cibil_score": None,
        "loan_eligible": False,
        "kyc_status": "pending",
        "verified_by": None,
        "remarks": None,
        "submitted_at": datetime.utcnow(),
        "verified_at": None,
    }
    if existing:
        if existing.get("kyc_status") == "rejected":
            await db.kyc_details.update_one({"_id": existing["_id"]}, {"$set": doc})
            updated_doc = await db.kyc_details.find_one({"_id": existing["_id"]})
            await write_audit_log(action="kyc_resubmit", actor_role="customer", actor_id=customer_id, entity_type="kyc", entity_id=str(customer_id), details={})
            return _sanitize_kyc_doc(updated_doc)
        update_payload = {**safe_payload, "updated_at": datetime.utcnow()}
        await db.kyc_details.update_one({"_id": existing["_id"]}, {"$set": update_payload})
        updated_doc = await db.kyc_details.find_one({"_id": existing["_id"]})
        await write_audit_log(action="kyc_update", actor_role="customer", actor_id=customer_id, entity_type="kyc", entity_id=str(customer_id), details={"status": str(existing.get("kyc_status") or "pending")})
        return _sanitize_kyc_doc(updated_doc)
    await db.kyc_details.insert_one(doc)
    await write_audit_log(action="kyc_submit", actor_role="customer", actor_id=customer_id, entity_type="kyc", entity_id=str(customer_id), details={})
    return _sanitize_kyc_doc(doc)


async def get_kyc_by_customer(customer_id: str, *, include_sensitive: bool = False):
    db = await get_db()
    customer_id = _normalize_customer_id(customer_id)
    kyc = await db.kyc_details.find_one({"customer_id": customer_id})
    if not kyc:
        raise HTTPException(status_code=404, detail="KYC not found")
    out = _sanitize_kyc_doc(kyc, include_sensitive=include_sensitive)
    if include_sensitive and out is not None:
        user = await db.users.find_one({"customer_id": customer_id}, {"pan_masked": 1, "pan_last4": 1, "pan_number": 1})
        pan_masked = str((user or {}).get("pan_masked") or "").strip()
        if pan_masked:
            out["pan_masked"] = pan_masked
        if not out.get("pan_number"):
            user_pan = _normalize_pan((user or {}).get("pan_number"))
            if user_pan:
                out["pan_number"] = user_pan
    return out


async def attach_kyc_document(customer_id: int, doc_type: str, document_id: str):
    db = await get_db()
    await db.kyc_details.update_one(
        {"customer_id": customer_id},
        {"$set": {doc_type: ObjectId(document_id)}}
    )