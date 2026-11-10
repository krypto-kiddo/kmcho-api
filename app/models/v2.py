"""v2 schedule models.

The kitchen's Google Sheet is the source of truth for what is delivered each
day. Apps Script pushes the month grid to POST /v2/schedule/sync, which keeps
`deliveries` in step with it, and the Payments tab into `monthly_amounts`.
Money only moves through v1's orders + ledger: billing (app/v2_billing.py) writes
an order + ledger debit per delivered meal and links them back on the delivery.
"""
from typing import Optional

from sqlalchemy import (
    Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Dog(Base):
    """One dog = one row in the monthly sheet = one billable subscription."""
    __tablename__ = "dogs"
    __table_args__ = (UniqueConstraint("user_id", "name_key", name="uq_dogs_user_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    # casefolded, whitespace-collapsed name; what the sheet's "Dog" column is matched on
    name_key: Mapped[str] = mapped_column(String, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    user = relationship("User")


class Delivery(Base):
    """What the sheet says for one dog on one date. Blank cells have no row."""
    __tablename__ = "deliveries"
    __table_args__ = (UniqueConstraint("dog_id", "delivery_date", name="uq_deliveries_dog_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    dog_id: Mapped[int] = mapped_column(ForeignKey("dogs.id"), nullable=False)
    delivery_date: Mapped[Date] = mapped_column(Date, nullable=False, index=True)

    raw_value: Mapped[str] = mapped_column(Text, nullable=False)          # cell text as shown in the sheet
    kind: Mapped[str] = mapped_column(String(16), nullable=False)         # meal | keyword | error
    dish: Mapped[Optional[str]] = mapped_column(Text, nullable=True)      # kind=meal
    keyword: Mapped[Optional[str]] = mapped_column(String, nullable=True) # kind=keyword, e.g. PAUSE
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)    # Keywords tab "Reason"
    billable: Mapped[bool] = mapped_column(Boolean, nullable=False)       # billing == "charge"
    # charge: delivered meal, billed its share | credit: counted slot, share stays in balance (PAUSE)
    # skip: not a slot, month spread over the rest (HOLIDAY) | hold: formula error, never auto-billed
    billing: Mapped[str] = mapped_column(String(8), nullable=False)

    wa_message_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    # Set by billing. billed_at != NULL locks the row against sheet edits.
    # amount = this slot's share (charged for charge, kept as credit for credit).
    billed_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True), nullable=True)
    amount: Mapped[Optional[float]] = mapped_column(Numeric(10, 2), nullable=True)
    order_id: Mapped[Optional[int]] = mapped_column(ForeignKey("orders.id"), nullable=True)
    billed_ledger_id: Mapped[Optional[int]] = mapped_column(ForeignKey("ledger.id"), nullable=True)

    sheet_tab: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sheet_row: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    dog = relationship("Dog")


class DeliveryChange(Base):
    """A sheet edit to an already-billed day.

    status:
      pending       billing mode changed / cell cleared; row untouched until an admin decides
      approved      admin accepted; ledger_id points at the refund/adjustment (if money moved)
      rejected      admin kept the original; the same sheet value is not flagged again
      withdrawn     sheet went back to the original value before anyone decided
      auto_applied  text-only edit (same billing mode); applied, kept for audit
    """
    __tablename__ = "delivery_changes"

    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_id: Mapped[int] = mapped_column(ForeignKey("deliveries.id"), nullable=False, index=True)
    old_raw: Mapped[str] = mapped_column(Text, nullable=False)
    new_raw: Mapped[str] = mapped_column(Text, nullable=False)  # "" = cell cleared
    old_billable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    new_billable: Mapped[bool] = mapped_column(Boolean, nullable=False)
    old_billing: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    new_billing: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    new_cell: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)  # classified new value
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    ledger_id: Mapped[Optional[int]] = mapped_column(ForeignKey("ledger.id"), nullable=True)
    note: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    detected_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[Optional[DateTime]] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)

    delivery = relationship("Delivery")


class MonthlyAmount(Base):
    """What one dog costs for one month, from the sheet's Payments tab."""
    __tablename__ = "monthly_amounts"
    __table_args__ = (UniqueConstraint("dog_id", "month", name="uq_monthly_amounts_dog_month"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    dog_id: Mapped[int] = mapped_column(ForeignKey("dogs.id"), nullable=False)
    month: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    amount: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    source_row: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SyncRun(Base):
    """One push from Apps Script, kept for debugging."""
    __tablename__ = "sync_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    month: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    sheet_tab: Mapped[str] = mapped_column(String, nullable=False)
    stats: Mapped[dict] = mapped_column(JSONB, nullable=False)
    issues: Mapped[list] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())
