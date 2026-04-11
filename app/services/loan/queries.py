from ...database.mongo import get_db
from ...utils.id import loan_id_filter


def _customer_match(customer_id: str | int):
    vals: list[str | int] = [customer_id]
    if isinstance(customer_id, str) and customer_id.isdigit():
        vals.append(int(customer_id))
    elif isinstance(customer_id, int):
        vals.append(str(customer_id))
    uniq: list[str | int] = []
    for v in vals:
        if v not in uniq:
            uniq.append(v)
    return {"customer_id": uniq[0]} if len(uniq) == 1 else {"customer_id": {"$in": uniq}}


async def _find_loan_any_by_customer(loan_id: str, customer_id: str | int):
    db = await get_db()
    filt = loan_id_filter(loan_id)
    filt.update(_customer_match(customer_id))

    loan = await db.personal_loans.find_one(filt)
    if loan:
        return "personal_loans", loan

    loan = await db.vehicle_loans.find_one(filt)
    if loan:
        return "vehicle_loans", loan

    loan = await db.education_loans.find_one(filt)
    if loan:
        return "education_loans", loan

    loan = await db.home_loans.find_one(filt)
    if loan:
        return "home_loans", loan

    return None, None


async def _find_loan_any(loan_id: str):
    db = await get_db()
    filt = loan_id_filter(loan_id)
    loan = await db.personal_loans.find_one(filt)
    if loan:
        return "personal_loans", loan
    loan = await db.vehicle_loans.find_one(filt)
    if loan:
        return "vehicle_loans", loan
    loan = await db.education_loans.find_one(filt)
    if loan:
        return "education_loans", loan
    loan = await db.home_loans.find_one(filt)
    if loan:
        return "home_loans", loan
    return None, None
