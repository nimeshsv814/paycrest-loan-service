from ...database.mongo import get_db
from ...services.account_service import add_money
from ...services.customer_service import profile_dashboard
from ...services.kyc_service import submit_kyc, attach_kyc_document, get_kyc_by_customer
from ...services.loan_service import (
    apply_loan,
    compute_customer_eligibility,
    pay_emi,
    list_customer_loans,
    pay_emi_any,
    pay_emi_any_wallet,
    upload_signed_sanction_letter,
    get_customer_emi_details,
    get_customer_noc,
    calculate_settlement_any,
    foreclose_any,
)
from ...services.emi import list_customer_notifications
from ...services.settings_service import get_settings
from ...services.document_service import upload_document, get_document_binary
from ...services.audit_service import write_audit_log

