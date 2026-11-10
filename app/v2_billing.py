"""v2 billing: turns the sheet's deliveries into v1 orders + ledger debits.

The rule (agreed Nov 2026)
- Each dog has a monthly amount for the month (Payments tab -> monthly_amounts).
- Slots = the month's cells that count toward that amount:
    charge (meal), credit (PAUSE), hold (formula error, until fixed).  skip (HOLIDAY) is not a slot.
- When a slot is billed, its share = what is left of the monthly amount / slots not yet billed.
    charge -> that share is debited (order + ledger debit, like v1 "Charge Order")
    credit -> nothing is debited; the share is recorded and stays in the customer's balance
- The last slot takes the exact remainder, so a fully delivered month always totals the amount.
  A HOLIDAY spreads the amount over the remaining slots; a PAUSE leaves its share as credit.
- Only TODAY is billed automatically (nightly). Older unbilled slots wait for an admin.
- Dogs without a monthly amount for the month are never billed (reported instead).
- Billed rows are locked: later sheet edits that change the billing mode need admin approval,
  which writes the refund / extra debit.
"""
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from calendar import monthrange
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ledger import Ledger
from app.models.order import Order
from app.models.user import User
from app.models.v2 import Delivery, DeliveryChange, Dog, MonthlyAmount

IST = timezone(timedelta(hours=5, minutes=30))
SLOT_MODES = ("charge", "credit", "hold")
PAISA = Decimal("0.01")


def today_ist() -> Date:
    return datetime.now(IST).date()


def month_of(d: Date) -> tuple[str, Date, Date]:
    return d.strftime("%Y-%m"), d.replace(day=1), d.replace(day=monthrange(d.year, d.month)[1])


def describe(dog_name: str, d: Delivery) -> str:
    return f"{dog_name}: {d.dish or d.keyword or d.raw_value}"


async def next_share(db: AsyncSession, delivery: Delivery) -> tuple[Optional[Decimal], Optional[str]]:
    """Share of the monthly amount for this (unbilled slot) delivery, or (None, reason)."""
    month, first, last = month_of(delivery.delivery_date)
    amount = (await db.execute(select(MonthlyAmount.amount).where(
        MonthlyAmount.dog_id == delivery.dog_id, MonthlyAmount.month == month))).scalar_one_or_none()
    if amount is None:
        return None, "no_amount"

    in_month = (Delivery.dog_id == delivery.dog_id,
                Delivery.delivery_date.between(first, last),
                Delivery.billing.in_(SLOT_MODES))
    used = (await db.execute(select(func.coalesce(func.sum(Delivery.amount), 0))
                             .where(*in_month, Delivery.billed_at.is_not(None)))).scalar_one()
    left = (await db.execute(select(func.count())
                             .where(*in_month, Delivery.billed_at.is_(None)))).scalar_one()
    if left < 1:
        return None, "not_a_slot"
    remaining = Decimal(amount) - Decimal(used)
    if remaining <= 0:
        return Decimal("0.00"), None
    if left == 1:
        return remaining.quantize(PAISA), None
    return (remaining / left).quantize(PAISA, rounding=ROUND_HALF_UP), None


async def debit(db: AsyncSession, user_id: int, day: Date, amount: Decimal, text: str) -> tuple[Order, Ledger]:
    """Same shape as v1 POST /orders: order + linked ledger debit + cached balance."""
    user = (await db.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one()
    order = Order(user_id=user_id, status="delivered", description=text,
                  order_date=datetime.combine(day, time(12, 0), IST))
    db.add(order)
    await db.flush()
    entry = Ledger(user_id=user_id, order_id=order.id, type="debit", amount=amount,
                   status="completed", description=text)
    db.add(entry)
    user.current_balance = Decimal(user.current_balance) - amount
    await db.flush()
    return order, entry


async def refund(db: AsyncSession, user_id: int, order_id: Optional[int], amount: Decimal, text: str) -> Ledger:
    """Same shape as v1 order cancellation: ledger 'refund' linked to the order."""
    user = (await db.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one()
    entry = Ledger(user_id=user_id, order_id=order_id, type="refund", amount=amount,
                   status="completed", description=text)
    db.add(entry)
    user.current_balance = Decimal(user.current_balance) + amount
    if order_id is not None:
        order = await db.get(Order, order_id)
        if order is not None:
            order.status = "cancelled"
    await db.flush()
    return entry


async def bill_delivery(db: AsyncSession, d: Delivery, dog: Dog, now: datetime) -> dict:
    """Bill one unbilled charge/credit slot. Caller holds a row lock on `d`."""
    base = {"delivery_id": d.id, "dog": dog.name, "date": d.delivery_date.isoformat()}
    if d.billed_at is not None:
        return {**base, "result": "already_billed"}
    if d.billing not in ("charge", "credit"):
        return {**base, "result": "not_billable", "billing": d.billing}
    share, why = await next_share(db, d)
    if share is None:
        return {**base, "result": why}
    if d.billing == "charge":
        order, entry = await debit(db, dog.user_id, d.delivery_date, share, describe(dog.name, d))
        d.order_id, d.billed_ledger_id = order.id, entry.id
    d.amount, d.billed_at = share, now
    await db.flush()
    return {**base, "result": "charged" if d.billing == "charge" else "credited", "amount": float(share)}


async def bill_day(db: AsyncSession, day: Date) -> dict:
    """Bill every unbilled slot on `day`. Idempotent; safe to run twice."""
    now = datetime.now(timezone.utc)
    rows = (await db.execute(
        select(Delivery, Dog).join(Dog, Dog.id == Delivery.dog_id)
        .where(Delivery.delivery_date == day, Dog.active.is_(True))
        .order_by(Delivery.id)
        .with_for_update(of=Delivery)
    )).all()
    results = []
    for d, dog in rows:
        if d.billing == "hold" and d.billed_at is None:
            results.append({"delivery_id": d.id, "dog": dog.name, "date": day.isoformat(),
                            "result": "hold", "value": d.raw_value})
        elif d.billing in ("charge", "credit") and d.billed_at is None:
            results.append(await bill_delivery(db, d, dog, now))
    return summarize(day, results)


async def bill_selected(db: AsyncSession, ids: list[int]) -> dict:
    """Admin: bill specific older slots, oldest first so shares come out right."""
    now = datetime.now(timezone.utc)
    rows = (await db.execute(
        select(Delivery, Dog).join(Dog, Dog.id == Delivery.dog_id)
        .where(Delivery.id.in_(ids))
        .order_by(Delivery.delivery_date, Delivery.id)
        .with_for_update(of=Delivery)
    )).all()
    found = {d.id for d, _ in rows}
    results = [{"delivery_id": i, "result": "not_found"} for i in ids if i not in found]
    for d, dog in rows:
        results.append(await bill_delivery(db, d, dog, now))
    return summarize(None, results)


def summarize(day: Optional[Date], results: list[dict]) -> dict:
    counts: dict[str, int] = {}
    total = Decimal("0")
    for r in results:
        counts[r["result"]] = counts.get(r["result"], 0) + 1
        if r["result"] == "charged":
            total += Decimal(str(r["amount"]))
    return {"date": day.isoformat() if day else None, "counts": counts,
            "charged_total": float(total), "results": results}


def cell_from(snapshot: Optional[dict]) -> dict:
    if snapshot is None:  # cell was cleared
        return {"raw": "", "kind": "keyword", "dish": None, "keyword": None, "reason": None, "billing": "skip"}
    return snapshot


async def approve_change(db: AsyncSession, change: DeliveryChange, admin_id: int, note: Optional[str]) -> dict:
    """Apply a pending sheet edit to a billed day, moving money if the billing mode changed."""
    d = (await db.execute(select(Delivery).where(Delivery.id == change.delivery_id).with_for_update())).scalar_one()
    dog = await db.get(Dog, d.dog_id)
    new = cell_from(change.new_cell)
    old_mode, new_mode = d.billing, new["billing"]
    share = Decimal(d.amount or 0)
    entry_id, money = None, 0.0

    if old_mode == "charge" and new_mode != "charge":
        if share > 0:
            e = await refund(db, dog.user_id, d.order_id, share,
                             f"Refund {d.delivery_date:%d %b}: {dog.name} ({new['raw'] or 'removed'})")
            entry_id, money = e.id, float(share)
    elif old_mode == "credit" and new_mode == "charge":
        if share > 0:
            order, e = await debit(db, dog.user_id, d.delivery_date, share,
                                   f"{dog.name}: {new['dish'] or new['raw']} (corrected)")
            d.order_id, entry_id, money = order.id, e.id, -float(share)
        d.billed_ledger_id = entry_id

    d.raw_value, d.kind, d.dish, d.keyword, d.reason, d.billing = (
        new["raw"], new["kind"], new["dish"], new["keyword"], new["reason"], new_mode)
    d.billable = new_mode == "charge"
    if new_mode == "skip":
        d.amount = None  # no longer a slot; its share spreads over the month's unbilled slots

    change.status, change.ledger_id, change.note = "approved", entry_id, note
    change.resolved_at, change.resolved_by = datetime.now(timezone.utc), admin_id
    await db.flush()
    return {"change_id": change.id, "status": "approved", "from": old_mode, "to": new_mode,
            "balance_change": money, "ledger_id": entry_id}
