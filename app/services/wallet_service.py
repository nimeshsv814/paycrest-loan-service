"""
services/loan-service/app/services/wallet_service.py

Calls wallet-service DIRECTLY (not via gateway).
WALLET_SERVICE_URL = http://localhost:3004
Internal routes on wallet-service are registered at /internal/...
So correct URL is: http://localhost:3004/internal/debit (NOT /api/wallet/internal/debit)
"""
import httpx
from fastapi import HTTPException
from ..core.config import settings


async def debit_wallet(customer_id, amount: float, description: str = "EMI payment") -> dict:
    # Direct call to wallet-service — no gateway prefix needed
    url = f"{settings.WALLET_SERVICE_URL}/internal/debit"
    payload = {
        "customer_id": str(customer_id),
        "amount": float(amount),
        "description": description,
    }
    headers = {
        "X-Internal-Token": settings.INTERNAL_SERVICE_TOKEN,
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, json=payload, headers=headers)
            if response.status_code == 200:
                return response.json()
            raise HTTPException(
                status_code=response.status_code,
                detail=f"wallet-service debit error: {response.text}",
            )
    except httpx.ConnectError:
        raise HTTPException(status_code=503, detail="wallet-service is unavailable")
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="wallet-service timed out")


async def get_wallet_balance(customer_id) -> float:
    # Direct call to wallet-service — no gateway prefix needed
    url = f"{settings.WALLET_SERVICE_URL}/internal/balance/{customer_id}"
    headers = {"X-Internal-Token": settings.INTERNAL_SERVICE_TOKEN}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                return float(data.get("balance", 0))
            return 0.0
    except Exception:
        return 0.0