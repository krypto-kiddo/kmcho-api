from pydantic import BaseModel
from typing import Optional
from datetime import date, datetime


# --- Nested objects ---

class PricingSchema(BaseModel):
    fixed: bool = False
    amount: Optional[float] = None

class ProteinSchema(BaseModel):
    cycle: list[str]
    qty: int

class BaseIngredientSchema(BaseModel):
    cycle: list[str]
    qty: int  # 0 = protein only

class DaysSchema(BaseModel):
    frequency: int  # 0=daily, 1=alternate even dates, 2=fixed days
    on: list[int] = []  # day numbers, used only if frequency=2

class BowlEntrySchema(BaseModel):
    protein: ProteinSchema
    base: BaseIngredientSchema
    days: DaysSchema
    qty: int = 1
    boneless: bool = False
    pricing: PricingSchema = PricingSchema()

class BrothSchema(BaseModel):
    days: DaysSchema
    qty: int = 1
    pricing: PricingSchema = PricingSchema()


# --- Request / Response ---

class SubscriptionCreate(BaseModel):
    user_id: int
    bowl: Optional[list[BowlEntrySchema]] = None
    broth: Optional[BrothSchema] = None
    active: bool = True
    paused_until: Optional[date] = None

class SubscriptionUpdate(BaseModel):
    bowl: Optional[list[BowlEntrySchema]] = None
    broth: Optional[BrothSchema] = None
    active: Optional[bool] = None
    paused_until: Optional[date] = None

class SubscriptionResponse(BaseModel):
    id: int
    user_id: int
    active: bool
    paused_until: Optional[date]
    bowl: Optional[list[BowlEntrySchema]]
    broth: Optional[BrothSchema]
    protein_index: int
    base_index: int
    created_at: datetime
    updated_at: Optional[datetime]

    class Config:
        from_attributes = True