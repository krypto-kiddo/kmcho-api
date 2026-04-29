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

    model_config = {"from_attributes": True}