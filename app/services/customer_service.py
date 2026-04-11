from ..database.mongo import get_db


def _mask_pan(value: str | None) -> str | None:
    pan = str(value or "").strip().upper()
    if not pan:
        return None
    if len(pan) != 10:
        return pan
    return f"{pan[:2]}******{pan[-2:]}"


async def profile_dashboard(customer_id):
    db = await get_db()
    customer_filters = [{"customer_id": customer_id}]
    if isinstance(customer_id, str) and customer_id.isdigit():
        customer_filters.append({"customer_id": int(customer_id)})
    elif isinstance(customer_id, int):
        customer_filters.append({"customer_id": str(customer_id)})
    query = {"$or": customer_filters} if len(customer_filters) > 1 else customer_filters[0]

    user = await db.users.find_one(query)
    acc = await db.bank_accounts.find_one(query)
    kyc = await db.kyc_details.find_one(query)
    pan_details = await db.users.find_one(query, {"pan_masked": 1, "pan_last4": 1, "pan_number": 1})

    cibil_score = kyc.get("cibil_score") if kyc else None
    if kyc and cibil_score is None and str(kyc.get("kyc_status") or "").lower() == "approved":
        total_score = kyc.get("total_score")
        if total_score is None:
            total_score = (
                int(kyc.get("employment_score") or 0)
                + int(kyc.get("income_score") or 0)
                + int(kyc.get("emi_score") or 0)
                + int(kyc.get("experience_score") or 0)
            )
        cibil_score = max(300, min(900, 300 + round(int(total_score or 0) * 6)))
        await db.kyc_details.update_one(
            {"_id": kyc["_id"]},
            {"$set": {"total_score": int(total_score or 0), "cibil_score": int(cibil_score), "loan_eligible": int(cibil_score) >= 650}},
        )

    active_personal = await db.personal_loans.find(
        {"$and": [query, {"status": {"$in": ["active", "admin_approved", "manager_approved"]}}]}
    ).to_list(length=50)
    active_vehicle = await db.vehicle_loans.find(
        {"$and": [query, {"status": {"$in": ["active", "admin_approved", "manager_approved"]}}]}
    ).to_list(length=50)

    def agg(loans):
        rt = sum(int(l.get("remaining_tenure", 0)) for l in loans)
        ra = sum(float(l.get("remaining_amount", 0)) for l in loans)
        return rt, ra

    rt1, ra1 = agg(active_personal)
    rt2, ra2 = agg(active_vehicle)

    return {
        "name": user.get("full_name") if user else None,
        "email": user.get("email") if user else None,
        "account_number": acc.get("account_number") if acc else None,
        "ifsc": acc.get("ifsc_code") if acc else None,
        "pan_number": (pan_details.get("pan_masked") if pan_details else None) or _mask_pan(pan_details.get("pan_number") if pan_details else None),
        "pan_masked": (pan_details.get("pan_masked") if pan_details else None) or _mask_pan(pan_details.get("pan_number") if pan_details else None),
        "balance": acc.get("balance") if acc else 0.0,
        "cibil_score": cibil_score if kyc else None,
        "kyc_status": kyc.get("kyc_status") if kyc else "not_submitted",
        "active_loans": len(active_personal) + len(active_vehicle),
        "remaining_tenure": rt1 + rt2,
        "remaining_amount": ra1 + ra2,
    }