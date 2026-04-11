# loan-service/app/services/loan_service.py
# Re-exports from the services/loan/ sub-package that was migrated from the monolith.
# This file exists so customer/service.py can do:
#   from ...services.loan_service import apply_loan, ...

from .loan.applications import apply_loan
from .loan.calculations import compute_emi
from .loan.customer import (
    get_customer_emi_details,
    list_customer_loans,
    pay_emi,
    pay_emi_any,
    pay_emi_any_wallet,
)
from .loan.documents import upload_signed_sanction_letter
from .loan.eligibility import compute_customer_eligibility
from .loan.noc import get_customer_noc
from .loan.settlement import calculate_settlement_any, foreclose_any
from .loan.verification import verification_complete

__all__ = [
    "apply_loan",
    "compute_customer_eligibility",
    "compute_emi",
    "foreclose_any",
    "get_customer_emi_details",
    "get_customer_noc",
    "list_customer_loans",
    "pay_emi",
    "pay_emi_any",
    "pay_emi_any_wallet",
    "upload_signed_sanction_letter",
    "calculate_settlement_any",
    "verification_complete",
]