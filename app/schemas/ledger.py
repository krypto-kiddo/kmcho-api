from pydantic import BaseModel
from decimal import Decimal
from datetime import datetime
from typing import Optional

class LedgerCreate(BaseModel):
    user_id: int
    order_id: Optional[int] = None
    type: str
    amount: Decimal
    mode_of_payment: Optional[str] = None
    transaction_id: Optional[str] = None
    description: Optional[str] = None

class LedgerResponse(BaseModel):
    id: int
    user_id: int
    order_id: Optional[int]
    type: str
    amount: Decimal
    mode_of_payment: Optional[str]
    transaction_id: Optional[str]
    status: str
    description: Optional[str]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

class StatementResponse(BaseModel):
    user_id: int
    name: str
    current_balance: Decimal
    entries: list[LedgerResponse]