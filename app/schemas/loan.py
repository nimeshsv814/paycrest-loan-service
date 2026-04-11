from pydantic import BaseModel, Field
from typing import Optional


class ApplyPersonalLoan(BaseModel):
    bank_account_number: str = Field(
        ...,
        min_length=9,
        max_length=18,
        pattern=r"^\d+$"
    )

    full_name: str = Field(..., min_length=3, max_length=100)

    pan_number: str = Field(
        ...,
        pattern=r"^[A-Z]{5}[0-9]{4}[A-Z]$"
    )

    loan_amount: float = Field(
        ...,
        gt=0,
        le=50_000_000
    )

    loan_purpose: str = Field(
        ...,
        min_length=5,
        max_length=200
    )

    salary_income: float = Field(
        ...,
        ge=0,
        le=10_000_000
    )

    monthly_avg_balance: float = Field(
        ...,
        ge=0
    )

    tenure_months: int = Field(
        ...,
        ge=6,
        le=360
    )

    pay_slip: Optional[str] = Field(None, max_length=100)

    guarantor_name: Optional[str] = Field(
        None,
        min_length=3,
        max_length=100
    )

    guarantor_phone: Optional[str] = Field(
        None,
        pattern=r"^\+?\d{10,15}$"
    )

    guarantor_pan: Optional[str] = Field(
        None,
        pattern=r"^[A-Z]{5}[0-9]{4}[A-Z]$"
    )

class ApplyVehicleLoan(ApplyPersonalLoan):
    vehicle_type: str = Field(
        ...,
        pattern=r"(?i)^(two-wheeler|four-wheeler|commercial)$"
    )

    vehicle_model: str = Field(
        ...,
        min_length=2,
        max_length=100
    )

    vehicle_price_doc: Optional[str] = Field(None, max_length=100)


class ApplyEducationLoan(BaseModel):
    bank_account_number: str = Field(..., min_length=9, max_length=18, pattern=r"^\d+$")
    full_name: str = Field(..., min_length=3, max_length=100)
    pan_number: str = Field(..., pattern=r"^[A-Z]{5}[0-9]{4}[A-Z]$")
    college_details: str = Field(..., min_length=3, max_length=300)
    course_details: str = Field(..., min_length=3, max_length=300)
    loan_amount: float = Field(..., gt=0, le=50_000_000)
    tenure_months: int = Field(..., ge=6, le=360)
    guarantor_name: Optional[str] = Field(None, min_length=3, max_length=100)
    guarantor_phone: Optional[str] = Field(None, pattern=r"^\+?\d{10,15}$")
    guarantor_pan: Optional[str] = Field(None, pattern=r"^[A-Z]{5}[0-9]{4}[A-Z]$")
    collateral: Optional[str] = Field(None, max_length=200)
    fees_structure: Optional[str] = Field(None, max_length=100)
    bonafide_certificate: Optional[str] = Field(None, max_length=100)
    collateral_doc: Optional[str] = Field(None, max_length=100)


class ApplyHomeLoan(ApplyPersonalLoan):
    property_type: str = Field(..., max_length=60)
    property_address: str = Field(..., min_length=3, max_length=300)
    property_value: float = Field(..., gt=0)
    down_payment: float = Field(..., ge=0)
    home_property_doc: Optional[str] = Field(None, max_length=100)


class LoanOut(BaseModel):
    id: str = Field(alias="_id")
    loan_amount: float
    tenure_months: int
    remaining_tenure: int
    emi_per_month: float
    remaining_amount: float
    status: str

class LoanApplyResponse(BaseModel):
    message: str
    loan_id: int
    status: str
    emi_per_month: float
    tenure_months: int


class LoanApprovalResponse(BaseModel):
    message: str
    loan_id: int
    status: str
