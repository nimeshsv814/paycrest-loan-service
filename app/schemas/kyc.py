from pydantic import BaseModel, Field
from typing import Optional, Union
from datetime import date


class KYCSubmit(BaseModel):
    full_name: str = Field(..., min_length=3, max_length=100)

    dob: date = Field(
        ...,
        description="Date of birth in YYYY-MM-DD format"
    )

    nationality: str = Field(
        ...,
        min_length=3,
        max_length=50,
        examples=["Indian"]
    )

    gender: Optional[str] = Field(
        None,
        pattern=r"(?i)^(male|female|other)$"
    )

    father_or_spouse_name: Optional[str] = Field(
        None,
        min_length=3,
        max_length=100
    )

    marital_status: Optional[str] = Field(
        None,
        pattern=r"(?i)^(single|married|divorced|widowed)$"
    )

    phone_number: Optional[str] = Field(
        None,
        pattern=r"^\+?\d{10,15}$"
    )

    pan_number: Optional[str] = Field(
        None,
        pattern=r"^[A-Z]{5}[0-9]{4}[A-Z]$"
    )

    aadhaar_number: Optional[str] = Field(
        None,
        pattern=r"^\d{12}$"
    )

    employment_status: Optional[str] = Field(
        None,
        pattern=r"(?i)^(employed|self-employed|unemployed|student|retired)$"
    )

    employment_type: Optional[str] = Field(
        None,
        pattern=r"(?i)^(private|government|business|freelancer)$"
    )

    company_name: Optional[str] = Field(
        None,
        min_length=2,
        max_length=150
    )

    monthly_income: Optional[float] = Field(None, ge=0, le=10_000_000)
    existing_emi_months: Optional[int] = Field(None, ge=0, le=360)
    years_of_experience: Optional[int] = Field(None, ge=0, le=60)

    address: Optional[str] = Field(None, min_length=10, max_length=300)

    photo: Optional[str] = Field(None, max_length=100)
    pan_card: Optional[str] = Field(None, max_length=100)
    aadhar_card: Optional[str] = Field(None, max_length=100)


class KYCOut(BaseModel):
    id: str = Field(alias="_id")

    customer_id: Optional[int] = None
    full_name: Optional[str] = None
    phone_number: Optional[str] = None
    employment_status: Optional[str] = None
    monthly_income: Optional[float] = None

    kyc_status: str = Field(
        ...,
        pattern=r"(?i)^(pending|approved|rejected)$"
    )

    total_score: Optional[int] = Field(None, ge=0, le=100)
    cibil_score: Optional[int] = Field(None, ge=300, le=900)
    loan_eligible: Optional[bool] = None

    dob: Optional[Union[str, date]] = None
    pan_number: Optional[str] = None
    aadhaar_number: Optional[str] = None
    aadhar_number: Optional[str] = None
    pan_masked: Optional[str] = None
    aadhaar_masked: Optional[str] = None

    photo: Optional[str] = None
    pan_card: Optional[str] = None
    aadhar_card: Optional[str] = None

    model_config = {"populate_by_name": True}


class KYCVerify(BaseModel):
    approve: bool

    employment_score: int = Field(..., ge=0, le=25)
    income_score: int = Field(..., ge=0, le=25)
    emi_score: int = Field(..., ge=0, le=25)
    experience_score: int = Field(..., ge=0, le=25)
    total_score: Optional[int] = Field(None, ge=0, le=100)
    cibil_score: Optional[int] = Field(None, ge=300, le=900)

    remarks: Optional[str] = Field(None, min_length=3, max_length=300)