from pydantic import BaseModel, EmailStr
from decimal import Decimal
from datetime import datetime
from typing import Optional

class UserCreate(BaseModel):
    name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    password: str

class UserLogin(BaseModel):
    email: str
    password: str

class UserResponse(BaseModel):
    id: int
    name: str
    phone: Optional[str]
    email: Optional[str]
    is_admin: bool
    current_balance: Decimal
    created_at: datetime

    model_config = {"from_attributes": True}

class UserBalanceResponse(BaseModel):
    id: int
    name: str
    current_balance: Decimal

    model_config = {"from_attributes": True}

class Token(BaseModel):
    access_token: str
    token_type: str

class TokenData(BaseModel):
    user_id: Optional[int] = None