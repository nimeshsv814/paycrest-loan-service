from fastapi import FastAPI
from app.core.config import settings
from app.database.mongo import connect_db, close_db
from app.middleware.idempotency import IdempotencyMiddleware

from app.routers.customer.router import router as customer_router
from app.routers.loan.router import router as loan_internal_router

app = FastAPI(title="Loan Service API")
app.add_middleware(IdempotencyMiddleware)

# Gateway strips /api/customer → we mount at root
app.include_router(customer_router)

# Gateway strips /api/loans → internal router handles /internal/verification-complete
# loan/router.py has prefix="/internal" on the APIRouter itself, so mount at root here
app.include_router(loan_internal_router)


@app.on_event("startup")
async def startup_db_client():
    await connect_db()


@app.on_event("shutdown")
async def shutdown_db_client():
    await close_db()


@app.get("/health")
async def health_check():
    return {"status": "ok", "service": "loan-service", "version": "1.0.0"}