from __future__ import annotations
from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field

SupportCategory = Literal["kyc", "payment", "loan", "wallet", "documents", "other"]
SupportStatus = Literal["open", "closed"]


class SupportAttachment(BaseModel):
    name: str = Field(..., min_length=1, max_length=256)
    size: int = Field(..., ge=0)
    type: Optional[str] = Field(default=None, max_length=128)


class SupportTicketCreate(BaseModel):
    category: SupportCategory
    subject: str = Field(..., min_length=3, max_length=180)
    message: str = Field(..., min_length=5, max_length=5000)
    attachment: Optional[SupportAttachment] = None


class SupportTicketAdminResolve(BaseModel):
    reply_message: str = Field(..., min_length=2, max_length=5000)
    close_ticket: bool = True


class SupportTicketOut(BaseModel):
    ticket_id: str
    customer_id: str
    category: SupportCategory
    subject: str
    message: str
    attachment: Optional[SupportAttachment] = None
    status: SupportStatus
    admin_reply: Optional[str] = None
    resolved_at: Optional[datetime] = None
    resolved_by: Optional[str] = None
    created_at: datetime
    updated_at: datetime