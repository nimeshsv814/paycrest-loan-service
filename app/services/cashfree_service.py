# loan-service/app/services/cashfree_service.py
# loan/customer.py may import cashfree helpers for gateway payments.
# In microservices, these live in payment-service.

import httpx
from fastapi import HTTPException
from ..core.config import settings


async def create_cashfree_order(
    customer_id,
    loan_id: str,
    amount: float,
    order_id: str,
    return_url: str | None = None,
) -> dict:
    url = f"{settings.PAYMENT_SERVICE_URL}/api/payments/cashfree/create"
    payload = {
        "customer_id": str(customer_id),
        "loan_id": loan_id,
        "amount": float(amount),
        "order_id": order_id,
        "return_url": return_url or settings.CASHFREE_RETURN_URL_EMI,
    }
    headers = {
        "X-Internal-Token": settings.INTERNAL_SERVICE_TOKEN,
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code == 200:
                return response.json()
            raise HTTPException(
                status_code=response.status_code,
                detail=f"payment-service error: {response.text}",
            )
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="payment-service is unavailable")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="payment-service timed out")