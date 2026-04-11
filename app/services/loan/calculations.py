from fastapi import HTTPException


# =========================
# EMI CALCULATION
# =========================
def compute_emi(amount: float, interest_rate: float, tenure_months: int) -> float:
    if tenure_months <= 0:
        raise HTTPException(status_code=400, detail="Invalid tenure_months")

    monthly_interest = interest_rate / 12 / 100
    if monthly_interest == 0:
        return round(amount / tenure_months, 2)

    r = monthly_interest
    n = tenure_months
    emi = amount * r * (1 + r) ** n / ((1 + r) ** n - 1)
    return round(emi, 2)

