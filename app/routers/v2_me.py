"""v2 customer home data and admin "view as customer".

GET  /v2/me/summary                      Everything the Home cards need, in one call.
GET  /v2/admin/customers                 Admin: customers to pick from (name, number, dogs).
POST /v2/admin/view-as/{user_id}         Admin: a 30-minute, read-only token for that customer.

Read-only is enforced centrally in app/auth.get_current_user: a token with ro=True is
refused on every non-GET request, v1 and v2 alike.
"""
import logging
import re
from calendar import monthrange
from collections import defaultdict
from datetime import date as Date, datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import create_access_token, get_current_admin, get_current_user
from app.database import get_db
from app.models.user import User
from app.models.v2 import Delivery, Dog, MonthlyAmount

log = logging.getLogger("kmcho.viewas")
router = APIRouter(tags=["v2 me"])

IST = timezone(timedelta(hours=5, minutes=30))
VIEW_AS_TTL = timedelta(minutes=30)
PAISA = Decimal("0.01")
RUNWAY_CAP_DAYS = 366
QTY = re.compile(r"^(?P<dish>.*?)[\s,·\-]*(?P<n>\d+(?:\.\d+)?)\s*(?P<u>kg|kgs|g|gm|gms|gram|grams)\.?$", re.I)


def today_ist() -> Date:
    return datetime.now(IST).date()


def split_dish(text: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """'Chicken & millet 500g' -> ('Chicken & millet', '500 g'). Free text, so best effort."""
    if not text:
        return None, None
    m = QTY.match(text.strip())
    if not m or not m.group("dish").strip():
        return text.strip(), None
    unit = "kg" if m.group("u").lower().startswith("kg") else "g"
    return m.group("dish").strip(), f"{m.group('n')} {unit}"


def money(d: Decimal) -> str:
    return str(Decimal(d).quantize(PAISA))


@router.get("/v2/me/summary")
async def my_summary(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    today = today_ist()
    month = today.strftime("%Y-%m")
    first, last = today.replace(day=1), today.replace(day=monthrange(today.year, today.month)[1])

    dogs = (await db.execute(select(Dog).where(Dog.user_id == user.id, Dog.active.is_(True)).order_by(Dog.name))).scalars().all()
    dog_ids = [d.id for d in dogs]
    amounts = {m.dog_id: Decimal(m.amount) for m in (await db.execute(
        select(MonthlyAmount).where(MonthlyAmount.month == month, MonthlyAmount.dog_id.in_(dog_ids or [0])))).scalars()}
    rows = (await db.execute(
        select(Delivery).where(Delivery.dog_id.in_(dog_ids or [0]), Delivery.delivery_date.between(first, last))
        .order_by(Delivery.delivery_date)
    )).scalars().all()
    name = {d.id: d.name for d in dogs}

    # ---- today's meals
    meals_today = []
    for r in rows:
        if r.delivery_date != today:
            continue
        dish, qty = split_dish(r.dish) if r.kind == "meal" else (None, None)
        meals_today.append({"dog": name[r.dog_id], "kind": r.kind, "billing": r.billing, "text": r.raw_value,
                            "dish": dish, "quantity": qty, "keyword": r.keyword, "reason": r.reason})
    meals_today.sort(key=lambda m: m["dog"].casefold())

    # ---- this month
    planned = sum(1 for r in rows if r.billing == "charge")
    delivered = sum(1 for r in rows if r.billing == "charge" and (r.billed_at is not None or r.delivery_date < today))
    paused = sum(1 for r in rows if r.billing == "credit")
    holidays = len({r.delivery_date for r in rows if r.billing == "skip"})
    credited = sum((Decimal(r.amount) for r in rows if r.billing == "credit" and r.billed_at is not None and r.amount is not None), Decimal(0))

    # ---- how long the balance lasts (same share rule as billing, then the month's average rate)
    balance = Decimal(user.current_balance)
    covers_until: Optional[str] = None
    if amounts and balance > 0:
        per_dog = defaultdict(list)
        for r in rows:
            if r.billing in ("charge", "credit", "hold"):
                per_dog[r.dog_id].append(r)
        cost_on: dict[Date, Decimal] = defaultdict(Decimal)
        for dog_id, slots in per_dog.items():
            if dog_id not in amounts:
                continue
            used = sum((Decimal(s.amount or 0) for s in slots if s.billed_at is not None), Decimal(0))
            open_slots = [s for s in slots if s.billed_at is None]
            if not open_slots:
                continue
            share = max(amounts[dog_id] - used, Decimal(0)) / len(open_slots)
            for s in open_slots:
                if s.billing == "charge" and s.delivery_date >= today:
                    cost_on[s.delivery_date] += share
        left, day, last_paid = balance, today, today - timedelta(days=1)
        while day <= last:
            if left < cost_on.get(day, Decimal(0)):
                break
            left -= cost_on.get(day, Decimal(0)); last_paid = day; day += timedelta(days=1)
        if day > last:  # money left after this month: carry on at this month's average daily cost
            rate = sum(amounts.values(), Decimal(0)) / monthrange(today.year, today.month)[1]
            extra = int(left / rate) if rate > 0 else RUNWAY_CAP_DAYS
            last_paid = last + timedelta(days=min(extra, RUNWAY_CAP_DAYS))
        covers_until = last_paid.isoformat() if last_paid >= today else None

    return {
        "today": today.isoformat(),
        "balance": money(balance),
        "covers_until": covers_until,
        "meals_today": meals_today,
        "month": {"month": month, "delivered": delivered, "planned": planned, "paused_days": paused,
                  "kitchen_holidays": holidays, "credited": money(credited)},
        "dogs": [{"id": d.id, "name": d.name, "monthly_amount": money(amounts[d.id]) if d.id in amounts else None} for d in dogs],
    }


@router.get("/v2/admin/customers")
async def list_customers(db: AsyncSession = Depends(get_db), _: User = Depends(get_current_admin)):
    users = (await db.execute(select(User).where(User.is_admin.is_(False)).order_by(User.name))).scalars().all()
    dogs = defaultdict(list)
    for d in (await db.execute(select(Dog).where(Dog.active.is_(True)).order_by(Dog.name))).scalars():
        dogs[d.user_id].append(d.name)
    return [{"id": u.id, "name": u.name, "wa_phone": u.wa_phone, "dogs": dogs.get(u.id, [])} for u in users]


@router.post("/v2/admin/view-as/{user_id}")
async def view_as(user_id: int, db: AsyncSession = Depends(get_db), admin: User = Depends(get_current_admin)):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "Customer not found")
    if user.is_admin:
        raise HTTPException(400, "Pick a customer, not an admin account")
    token = create_access_token({"sub": str(user.id), "ro": True, "view_by": admin.id}, expires_delta=VIEW_AS_TTL)
    log.warning("view-as: admin %s (%s) is viewing customer %s (%s), read-only, %s min",
                admin.id, admin.name, user.id, user.name, int(VIEW_AS_TTL.total_seconds() // 60))
    return {"access_token": token, "token_type": "bearer", "role": "customer", "read_only": True,
            "name": user.name, "expires_in": int(VIEW_AS_TTL.total_seconds())}
