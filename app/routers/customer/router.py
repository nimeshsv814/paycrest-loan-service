from fastapi import APIRouter, Depends, File, UploadFile, Form, HTTPException
from fastapi.responses import StreamingResponse
from typing import Optional
import io
from datetime import datetime
from uuid import uuid4
from ...core.security import require_roles
from ...models.enums import Roles, VechicleType, PropertyType, EmploymentStatus, Gender, MaritalStatus
from ...schemas.kyc import KYCOut
from ...schemas.loan import LoanOut, LoanApplyResponse
from ...schemas.support import SupportTicketCreate
from ...utils.serializers import normalize_doc
from .service import get_db
from .service import (
    add_money,
    profile_dashboard,
    submit_kyc,
    get_kyc_by_customer,
    apply_loan,
    compute_customer_eligibility,
    list_customer_loans,
    pay_emi_any_wallet,
    upload_signed_sanction_letter,
    get_customer_emi_details,
    get_customer_noc,
    calculate_settlement_any,
    foreclose_any,
    list_customer_notifications,
    get_settings,
    upload_document,
    attach_kyc_document,
    get_document_binary,
    write_audit_log,
)


router = APIRouter(tags=["customer"])

LOAN_PRODUCT_CONFIG: dict[str, dict] = {
    "personal": {"min_amount": 10000, "max_amount": 2500000, "min_tenure_months": 12, "max_tenure_months": 120, "eligibility_factor": 1.0},
    "vehicle": {"min_amount": 500000, "max_amount": 6000000, "min_tenure_months": 12, "max_tenure_months": 84, "eligibility_factor": 1.25},
    "education": {"min_amount": 200000, "max_amount": 3500000, "min_tenure_months": 12, "max_tenure_months": 120, "eligibility_factor": 1.1},
    "home": {"min_amount": 1500000, "max_amount": 20000000, "min_tenure_months": 60, "max_tenure_months": 360, "eligibility_factor": 3.5},
}

@router.post('/add-money')
async def add_money_route(amount: float, user=Depends(require_roles(Roles.CUSTOMER))):
    cid = user.get("customer_id") or user.get("_id") or user.get("user_id")
    return await add_money(cid, amount)

@router.get('/get/profile')
async def profile(user=Depends(require_roles(Roles.CUSTOMER))):
    cid = user.get("customer_id") or user.get("_id") or user.get("user_id")
    return await profile_dashboard(cid)

@router.get('/kyc')
async def customer_kyc(user=Depends(require_roles(Roles.CUSTOMER))):
    cid = user.get("customer_id") or user.get("_id") or user.get("user_id")
    return await get_kyc_by_customer(cid)

@router.get("/notifications")
async def customer_notifications(limit: int = 100, user=Depends(require_roles(Roles.CUSTOMER))):
    cid = user.get("customer_id") or user.get("_id") or user.get("user_id")
    return await list_customer_notifications(cid, limit=limit)


@router.post("/support/tickets")
async def create_support_ticket(payload: SupportTicketCreate, user=Depends(require_roles(Roles.CUSTOMER))):
    cid = str(user.get("customer_id") or user.get("_id"))
    now = datetime.utcnow()
    db = await get_db()
    ticket_id = f"TKT-{now.strftime('%Y%m%d')}-{uuid4().hex[:8].upper()}"
    doc = {
        "ticket_id": ticket_id,
        "customer_id": cid,
        "category": payload.category,
        "subject": payload.subject.strip(),
        "message": payload.message.strip(),
        "attachment": payload.attachment.dict() if payload.attachment else None,
        "status": "open",
        "admin_reply": None,
        "resolved_at": None,
        "resolved_by": None,
        "created_at": now,
        "updated_at": now,
    }
    await db.support_tickets.insert_one(doc)
    await write_audit_log(
        action="support_ticket_created",
        actor_role=Roles.CUSTOMER,
        actor_id=cid,
        entity_type="support_ticket",
        entity_id=ticket_id,
        details={"category": payload.category, "subject": payload.subject.strip()},
    )
    return normalize_doc(doc)


@router.get("/support/tickets")
async def list_support_tickets(user=Depends(require_roles(Roles.CUSTOMER))):
    cid = str(user.get("customer_id") or user.get("_id"))
    db = await get_db()
    rows = (
        await db.support_tickets.find({"customer_id": cid})
        .sort([("created_at", -1), ("_id", -1)])
        .to_list(length=300)
    )
    return [normalize_doc(r) for r in rows]

@router.get("/loan-offers")
async def loan_offers(user=Depends(require_roles(Roles.CUSTOMER))):
    cid = user.get("customer_id") or user.get("_id") or user.get("user_id")
    eligibility = await compute_customer_eligibility(cid)
    settings = await get_settings()

    suggested_max = float(eligibility.get("suggested_max_loan") or 0.0)
    cibil_score = int(eligibility.get("cibil_score") or 0)
    min_cibil_required = int(settings.get("min_cibil_required") or 650)
    cibil_eligible = cibil_score >= min_cibil_required
    rate_map = {
        "personal": float(settings.get("personal_loan_interest") or 12.0),
        "vehicle": float(settings.get("vehicle_loan_interest") or settings.get("personal_loan_interest") or 12.0),
        "education": float(settings.get("education_loan_interest") or settings.get("personal_loan_interest") or 12.0),
        "home": float(settings.get("home_loan_interest") or settings.get("personal_loan_interest") or 12.0),
    }

    offers: dict[str, dict] = {}
    for loan_type, cfg in LOAN_PRODUCT_CONFIG.items():
        raw_max = min(float(cfg["max_amount"]), suggested_max * float(cfg["eligibility_factor"]))
        eligible_max = round(max(0.0, raw_max), 2)
        base_min = float(cfg["min_amount"])
        if eligible_max <= 0:
            eligible_min = 0.0
        elif eligible_max < base_min:
            eligible_min = round(max(0.0, eligible_max * 0.5), 2)
        else:
            eligible_min = base_min
        offers[loan_type] = {
            "loan_type": loan_type,
            "interest_rate": rate_map[loan_type],
            "min_amount": float(cfg["min_amount"]),
            "max_amount": float(cfg["max_amount"]),
            "eligible_min_amount": eligible_min,
            "eligible_max_amount": eligible_max,
            "min_tenure_months": int(cfg["min_tenure_months"]),
            "max_tenure_months": int(cfg["max_tenure_months"]),
            "cibil_eligible": cibil_eligible,
        }

    return {
        "cibil_score": cibil_score,
        "min_cibil_required": min_cibil_required,
        "score": float(eligibility.get("score") or 0),
        "suggested_max_loan": suggested_max,
        "offers": offers,
    }

@router.post('/submit-kyc', response_model=KYCOut)
async def submit_kyc_route(
    full_name: str = Form(...),
    dob: str = Form(...),
    nationality: str = Form(...),
    gender: Optional[Gender] = Form(None),
    father_or_spouse_name: Optional[str] = Form(None),
    marital_status: Optional[MaritalStatus] = Form(None),
    phone_number: Optional[str] = Form(None),
    pan_number: Optional[str] = Form(None),
    aadhaar_number: Optional[str] = Form(None),
    employment_status: Optional[EmploymentStatus] = Form(None),
    employment_type: Optional[str] = Form(None),
    company_name: Optional[str] = Form(None),
    monthly_income: Optional[float] = Form(None),
    existing_emi_months: Optional[int] = Form(None),
    years_of_experience: Optional[int] = Form(None),
    address: Optional[str] = Form(None),
    pan_card: UploadFile = File(None),
    aadhar_card: UploadFile = File(None),
    photo: UploadFile = File(None),
    user=Depends(require_roles(Roles.CUSTOMER)),
):
    cid = user.get("customer_id") or user.get("_id") or user.get("user_id")

    payload = {
        "full_name": full_name,
        "dob": dob,
        "nationality": nationality,
        "gender": gender,
        "father_or_spouse_name": father_or_spouse_name,
        "marital_status": marital_status,
        "phone_number": phone_number,
        "pan_number": pan_number,
        "aadhaar_number": aadhaar_number,
        "employment_status": employment_status,
        "employment_type": employment_type,
        "company_name": company_name,
        "monthly_income": monthly_income,
        "existing_emi_months": existing_emi_months,
        "years_of_experience": years_of_experience,
        "address": address,
    }

    # 1. Submit base KYC (no files yet)
    await submit_kyc(cid, payload)

    # 2. Upload & attach documents
    if pan_card:
        doc_id = await upload_document(pan_card, cid, "pan_card")
        await attach_kyc_document(cid, "pan_card", doc_id)

    if aadhar_card:
        doc_id = await upload_document(aadhar_card, cid, "aadhar_card")
        await attach_kyc_document(cid, "aadhar_card", doc_id)

    if photo:
        doc_id = await upload_document(photo, cid, "photo")
        await attach_kyc_document(cid, "photo", doc_id)

    # 3. Return sanitized updated KYC
    return await get_kyc_by_customer(cid)



@router.post('/apply-personal-loan', response_model=LoanApplyResponse)
async def apply_personal(
    bank_account_number: int = Form(...),
    full_name: str = Form(...),
    pan_number: Optional[str] = Form(None),
    loan_amount: float = Form(...),
    loan_purpose: str = Form(...),
    salary_income: float = Form(...),
    monthly_avg_balance: float = Form(...),
    tenure_months: int = Form(...),
    guarantor_name: Optional[str] = Form(None),
    guarantor_phone: Optional[str] = Form(None),
    guarantor_pan: Optional[str] = Form(None),
    pay_slip: UploadFile = File(None),
    user=Depends(require_roles(Roles.CUSTOMER)),
):
    settings = await get_settings()
    cid = user.get("customer_id") or user.get("_id") or user.get("user_id")
    
    payload = {
        "bank_account_number": bank_account_number,
        "full_name": full_name,
        "pan_number": pan_number,
        "loan_amount": loan_amount,
        "loan_purpose": loan_purpose,
        "salary_income": salary_income,
        "monthly_avg_balance": monthly_avg_balance,
        "tenure_months": tenure_months,
        "guarantor_name": guarantor_name,
        "guarantor_phone": guarantor_phone,
        "guarantor_pan": guarantor_pan,
    }
    
    if pay_slip :
        pay_slip_path = await upload_document(pay_slip, cid, "pay_slip")
        payload["pay_slip"] = pay_slip_path
    
    return await apply_loan('personal_loans', cid, payload, settings['personal_loan_interest'])

@router.post('/apply-vehicle-loan', response_model=LoanApplyResponse)
async def apply_vehicle(
    bank_account_number: int = Form(...),
    full_name: str = Form(...),
    pan_number: Optional[str] = Form(None),
    loan_amount: float = Form(...),
    loan_purpose: str = Form(...),
    salary_income: float = Form(...),
    monthly_avg_balance: float = Form(...),
    tenure_months: int = Form(...),
    vehicle_type: VechicleType = Form(...),
    vehicle_model: str = Form(...),
    guarantor_name: Optional[str] = Form(None),
    guarantor_phone: Optional[str] = Form(None),
    guarantor_pan: Optional[str] = Form(None),
    pay_slip: UploadFile = File(None),
    vehicle_price_doc: UploadFile = File(None),
    user=Depends(require_roles(Roles.CUSTOMER)),
):
    settings = await get_settings()
    cid = user.get("customer_id") or user.get("_id") or user.get("user_id")
    
    payload = {
        "bank_account_number": bank_account_number,
        "full_name": full_name,
        "pan_number": pan_number,
        "loan_amount": loan_amount,
        "loan_purpose": loan_purpose,
        "salary_income": salary_income,
        "monthly_avg_balance": monthly_avg_balance,
        "tenure_months": tenure_months,
        "vehicle_type": vehicle_type,
        "vehicle_model": vehicle_model,
        "guarantor_name": guarantor_name,
        "guarantor_phone": guarantor_phone,
        "guarantor_pan": guarantor_pan,
    }
    if pay_slip is not None:
        pay_slip_id = await upload_document(
        pay_slip,
        cid,              # ✅ customer_id ONLY
        "pay_slip"        # ✅ exact doc_type
    )
        payload["pay_slip"] = pay_slip_id

    if vehicle_price_doc is not None:
        vehicle_price_id = await upload_document(
        vehicle_price_doc,
        cid,                   # ✅ customer_id ONLY
        "vehicle_price_doc"    # ✅ exact doc_type
    )
        payload["vehicle_price_doc"] = vehicle_price_id

    
    return await apply_loan('vehicle_loans', cid, payload, settings['vehicle_loan_interest'])


@router.post('/apply-education-loan', response_model=LoanApplyResponse)
async def apply_education(
    bank_account_number: int = Form(...),
    full_name: str = Form(...),
    pan_number: Optional[str] = Form(None),
    college_details: str = Form(...),
    course_details: str = Form(...),
    loan_amount: float = Form(...),
    tenure_months: int = Form(...),
    guarantor_name: Optional[str] = Form(None),
    guarantor_phone: Optional[str] = Form(None),
    guarantor_pan: Optional[str] = Form(None),
    collateral: Optional[str] = Form(None),
    pay_slip: UploadFile = File(None),
    fees_structure: UploadFile = File(...),
    bonafide_certificate: UploadFile = File(...),
    collateral_doc: UploadFile = File(None),
    user=Depends(require_roles(Roles.CUSTOMER)),
):
    settings = await get_settings()
    cid = user.get("customer_id") or user.get("_id") or user.get("user_id")

    payload = {
        "bank_account_number": bank_account_number,
        "full_name": full_name,
        "pan_number": pan_number,
        "college_details": college_details,
        "course_details": course_details,
        "loan_amount": loan_amount,
        "loan_purpose": "Education",
        "salary_income": 0,
        "monthly_avg_balance": 0,
        "tenure_months": tenure_months,
        "guarantor_name": guarantor_name,
        "guarantor_phone": guarantor_phone,
        "guarantor_pan": guarantor_pan,
        "collateral": collateral,
    }

    payload["fees_structure"] = await upload_document(fees_structure, cid, "fees_structure")
    payload["bonafide_certificate"] = await upload_document(bonafide_certificate, cid, "bonafide_certificate")
    if pay_slip is not None:
        payload["pay_slip"] = await upload_document(pay_slip, cid, "pay_slip")

    if collateral_doc is not None:
        payload["collateral_doc"] = await upload_document(collateral_doc, cid, "collateral_doc")

    return await apply_loan(
        'education_loans',
        cid,
        payload,
        float(settings.get('education_loan_interest', settings.get('personal_loan_interest', 12.0))),
    )


@router.post('/apply-home-loan', response_model=LoanApplyResponse)
async def apply_home(
    bank_account_number: int = Form(...),
    full_name: str = Form(...),
    pan_number: Optional[str] = Form(None),
    loan_amount: float = Form(...),
    loan_purpose: str = Form(...),
    salary_income: float = Form(...),
    monthly_avg_balance: float = Form(...),
    tenure_months: int = Form(...),
    property_type: PropertyType = Form(...),
    property_address: str = Form(...),
    property_value: float = Form(...),
    down_payment: float = Form(...),
    guarantor_name: Optional[str] = Form(None),
    guarantor_phone: Optional[str] = Form(None),
    guarantor_pan: Optional[str] = Form(None),
    pay_slip: UploadFile = File(None),
    home_property_doc: UploadFile = File(...),
    user=Depends(require_roles(Roles.CUSTOMER)),
):
    settings = await get_settings()
    cid = user.get("customer_id") or user.get("_id") or user.get("user_id")

    payload = {
        "bank_account_number": bank_account_number,
        "full_name": full_name,
        "pan_number": pan_number,
        "loan_amount": loan_amount,
        "loan_purpose": loan_purpose,
        "salary_income": salary_income,
        "monthly_avg_balance": monthly_avg_balance,
        "tenure_months": tenure_months,
        "property_type": property_type.value,
        "property_address": property_address,
        "property_value": property_value,
        "down_payment": down_payment,
        "guarantor_name": guarantor_name,
        "guarantor_phone": guarantor_phone,
        "guarantor_pan": guarantor_pan,
    }

    if pay_slip is not None:
        payload["pay_slip"] = await upload_document(pay_slip, cid, "pay_slip")

    payload["home_property_doc"] = await upload_document(home_property_doc, cid, "home_property_doc")

    return await apply_loan(
        'home_loans',
        cid,
        payload,
        float(settings.get('home_loan_interest', settings.get('personal_loan_interest', 12.0))),
    )

@router.get('/loans')
async def customer_loans(user=Depends(require_roles(Roles.CUSTOMER))):
    cid = user.get("customer_id") or user.get("_id") or user.get("user_id")
    return await list_customer_loans(cid)

@router.get("/loans/{loan_id}/emi-details")
async def loan_emi_details(loan_id: str, user=Depends(require_roles(Roles.CUSTOMER))):
    cid = user.get("customer_id") or user.get("_id") or user.get("user_id")
    return await get_customer_emi_details(loan_id, cid)


@router.post('/pay-emi/{loan_id}')
async def pay_emi_by_id(loan_id: str, user=Depends(require_roles(Roles.CUSTOMER))):
    cid = user.get("customer_id") or user.get("_id") or user.get("user_id")
    return await pay_emi_any_wallet(loan_id, cid)


@router.get('/loans/{loan_id}/settlement')
async def get_settlement(loan_id: str, user=Depends(require_roles(Roles.CUSTOMER))):
    cid = user.get("customer_id") or user.get("_id") or user.get("user_id")
    return await calculate_settlement_any(loan_id, cid)


@router.post('/loans/{loan_id}/foreclose')
async def foreclose_loan_route(loan_id: str, user=Depends(require_roles(Roles.CUSTOMER))):
    print("USER OBJECT:", user)
    cid = user.get("customer_id") or user.get("_id") or user.get("user_id")
    return await foreclose_any(loan_id, cid)

@router.get("/loans/{loan_id}/noc")
async def download_loan_noc(loan_id: str, user=Depends(require_roles(Roles.CUSTOMER))):
    cid = user.get("customer_id") or user.get("_id") or user.get("user_id")
    noc = await get_customer_noc(loan_id, cid)
    document_id = noc.get("document_id")
    if not document_id:
        raise HTTPException(status_code=404, detail="NOC document not available")
    doc = await get_document_binary(str(document_id))
    return StreamingResponse(
        io.BytesIO(doc["data"]),
        media_type=doc["content_type"],
        headers={"Content-Disposition": f'inline; filename="{doc.get("filename") or f"loan_noc_{loan_id}.pdf"}"'},
    )


@router.get("/loans/{loan_id}/sanction-letter")
async def download_sanction_letter(loan_id: str, user=Depends(require_roles(Roles.CUSTOMER))):
    cid = user.get("customer_id") or user.get("_id") or user.get("user_id")
    db = await get_db()
    from ...utils.id import loan_id_filter

    filt = loan_id_filter(loan_id)
    filt["customer_id"] = cid

    loan = await db.personal_loans.find_one(filt)
    if not loan:
        loan = await db.vehicle_loans.find_one(filt)
    if not loan:
        loan = await db.education_loans.find_one(filt)
    if not loan:
        loan = await db.home_loans.find_one(filt)

    if not loan:
        raise HTTPException(status_code=404, detail="Loan not found")

    # ✅ FIXED HERE
    doc_id = loan.get("sanction_document_id")

    if not doc_id:
        raise HTTPException(status_code=404, detail="Sanction letter not generated yet")

    doc = await get_document_binary(str(doc_id))

    return StreamingResponse(
        io.BytesIO(doc["data"]),
        media_type=doc["content_type"],
        headers={"Content-Disposition": f'inline; filename="{doc["filename"]}"'},
    )


@router.post("/loans/{loan_id}/sanction-letter/upload")
async def upload_signed_sanction_letter_route(
    loan_id: str,
    signed_sanction_letter: UploadFile = File(...),
    user=Depends(require_roles(Roles.CUSTOMER)),
):
    cid = user.get("customer_id") or user.get("_id") or user.get("user_id")
    doc_id = await upload_document(signed_sanction_letter, cid, "signed_sanction_letter")
    return await upload_signed_sanction_letter(loan_id, cid, doc_id)



