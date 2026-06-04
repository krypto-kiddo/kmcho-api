from pydantic import BaseModel
from datetime import datetime
from typing import Optional
from decimal import Decimal

class OrderCreate(BaseModel):
    user_id: int
    description: Optional[str] = None
    order_date: Optional[datetime] = None
    amount: Decimal = Decimal("100.00")

class OrderUpdateStatus(BaseModel):
    status: str

class OrderResponse(BaseModel):
    id: int
    user_id: int
    status: str
    description: Optional[str]
    order_date: Optional[datetime]
    created_at: datetime
    amount: str | None = None
    porter_link: Optional[str] = None

    model_config = {"from_attributes": True}

class OrderUpdate(BaseModel):
    description: Optional[str] = None
    order_date: Optional[datetime] = None

class OrderUpdatePorterLink(BaseModel):
    order_ids: list[int]
    porter_link: Optional[str] = None