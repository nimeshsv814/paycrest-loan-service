from ...database.mongo import get_db


# =========================
# ELIGIBILITY / SCORING
# =========================
async def compute_customer_eligibility(customer_id: str | int) -> dict:
    db = await get_db()
    customer_filters: list[dict] = [{"customer_id": customer_id}]
    if isinstance(customer_id, str) and customer_id.isdigit():
        customer_filters.append({"customer_id": int(customer_id)})
    elif isinstance(customer_id, int):
        customer_filters.append({"customer_id": str(customer_id)})

    query = {"$or": customer_filters} if len(customer_filters) > 1 else customer_filters[0]
    kyc = await db.kyc_details.find_one(query) or {}
    cibil = int(kyc.get("cibil_score") or 0)
    cibil_comp = max(0, min(100, ((cibil - 300) / (900 - 300)) * 100))

    # Eligibility now depends ONLY on CIBIL score bands.
    # Amounts are fixed per band and do not use income/balance.
    if cibil < 500:
        band = "<500"
        suggested_min = 0.0
        suggested_max = 0.0
    elif cibil < 600:
        band = "500-599"
        suggested_min = 100000.0
        suggested_max = 300000.0
    elif cibil < 700:
        band = "600-699"
        suggested_min = 300000.0
        suggested_max = 700000.0
    elif cibil < 800:
        band = "700-799"
        suggested_min = 700000.0
        suggested_max = 1500000.0
    else:
        band = "800-900"
        suggested_min = 1500000.0
        suggested_max = 2500000.0

    # Keep "score" for compatibility; now it represents only normalized CIBIL.
    score = round(cibil_comp, 2)

    return {
        "customer_id": customer_id,
        "cibil_score": cibil,
        "cibil_band": band,
        "score": score,
        "breakdown": {
            "cibil_component": round(cibil_comp, 2),
        },
        "suggested_min_loan": suggested_min,
        "suggested_max_loan": suggested_max,
    }
