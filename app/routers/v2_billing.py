"""v2 billing endpoints. The rules live in app/v2_billing.py.

POST /v2/billing/run                      Apps Script, nightly (HMAC-signed like /v2/schedule/sync).
                                          Bills today's slots only.
GET  /v2/admin/billing/review             Admin: what needs a human this month.
POST /v2/admin/billing/bill               Admin: bill specific older days.
POST /v2/admin/changes/{id}/approve       Admin: accept a sheet edit to a billed day (moves money).
POST /v2/admin/changes/{id}/reject        Admin: keep the billed value.
"""
from datetime import date as Date, datetime, timezone
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_admin
from app.database import get_db
from app.models.user import User
from app.models.v2 import Delivery, DeliveryChange, Dog, MonthlyAmount
from app.routers.v2_schedule import verified_body
from app.v2_billing import (
    SLOT_MODES, approve_change, bill_day, bill_selected, month_of, today_ist,
)

router = APIRouter(tags=["v2 billing"])


class RunIn(BaseModel):
    date: Optional[Date] = None


@router.post("/v2/billing/run")
async def run_billing(request: Request, db: AsyncSession = Depends(get_db)):
    body = await verified_body(request)
    p = RunIn.model_validate_json(body or b"{}")
    today = today_ist()
    if p.date is not None and p.date != today:
        raise HTTPException(422, "Only today is billed automatically; older days go through admin review")
    out = await bill_day(db, today)
    month, first, _ = month_of(today)
    out["older_unbilled"] = (await db.execute(
        select(func.count()).select_from(Delivery).join(Dog, Dog.id == Delivery.dog_id)
        .where(Delivery.delivery_date >= first, Delivery.delivery_date < today,
               Delivery.billed_at.is_(None),
               Delivery.billing.in_(("charge", "credit")), Dog.active.is_(True))
    )).scalar_one()
    out["pending_changes"] = (await db.execute(
        select(func.count()).select_from(DeliveryChange).where(DeliveryChange.status == "pending")
    )).scalar_one()
    await db.commit()
    return out


@router.get("/v2/admin/billing/review")
async def review(
    month: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin),
):
    today = today_ist()
    if month:
        try:
            y, m = map(int, month.split("-"))
            ref = Date(y, m, 1)
        except ValueError:
            raise HTTPException(422, "month must be YYYY-MM")
    else:
        ref = today
    month, first, last = month_of(ref)

    rows = (await db.execute(
        select(Delivery, Dog, User)
        .join(Dog, Dog.id == Delivery.dog_id).join(User, User.id == Dog.user_id)
        .where(Delivery.delivery_date.between(first, last), Dog.active.is_(True))
        .order_by(Delivery.delivery_date, User.name, Dog.name)
    )).all()
    amounts = {m.dog_id: Decimal(m.amount) for m in (await db.execute(
        select(MonthlyAmount).where(MonthlyAmount.month == month))).scalars()}

    def item(d, dog, u):
        return {"delivery_id": d.id, "date": d.delivery_date.isoformat(), "owner": u.name,
                "user_id": u.id, "dog": dog.name, "value": d.raw_value, "billing": d.billing}

    unbilled_past, holds, per_dog = [], [], {}
    for d, dog, u in rows:
        s = per_dog.setdefault(dog.id, {
            "dog_id": dog.id, "dog": dog.name, "owner": u.name, "user_id": u.id,
            "monthly_amount": float(amounts[dog.id]) if dog.id in amounts else None,
            "slots": 0, "billed_slots": 0, "charged": Decimal(0), "credited": Decimal(0)})
        if d.billing in SLOT_MODES:
            s["slots"] += 1
            if d.billed_at is not None:
                s["billed_slots"] += 1
                if d.billing == "charge":
                    s["charged"] += Decimal(d.amount or 0)
                elif d.billing == "credit":
                    s["credited"] += Decimal(d.amount or 0)
        if d.billed_at is None and d.billing == "hold" and d.delivery_date <= today:
            holds.append(item(d, dog, u))
        elif d.billed_at is None and d.billing in ("charge", "credit") and d.delivery_date < today:
            unbilled_past.append(item(d, dog, u))
    for s in per_dog.values():
        s["charged"], s["credited"] = float(s["charged"]), float(s["credited"])

    changes = (await db.execute(
        select(DeliveryChange, Delivery, Dog, User)
        .join(Delivery, Delivery.id == DeliveryChange.delivery_id)
        .join(Dog, Dog.id == Delivery.dog_id).join(User, User.id == Dog.user_id)
        .where(DeliveryChange.status == "pending").order_by(Delivery.delivery_date)
    )).all()
    pending = []
    for c, d, dog, u in changes:
        share = float(d.amount or 0)
        effect = (share if d.billing == "charge" and c.new_billing != "charge"
                  else -share if d.billing == "credit" and c.new_billing == "charge" else 0.0)
        pending.append({"change_id": c.id, "date": d.delivery_date.isoformat(), "owner": u.name,
                        "dog": dog.name, "from": c.old_raw, "to": c.new_raw or "(cleared)",
                        "from_billing": d.billing, "to_billing": c.new_billing,
                        "customer_balance_effect_if_approved": effect})

    return {
        "month": month,
        "missing_amounts": [s for s in per_dog.values() if s["monthly_amount"] is None and s["slots"]],
        "holds": holds,
        "unbilled_past": unbilled_past,
        "pending_changes": pending,
        "dogs": sorted(per_dog.values(), key=lambda s: (s["owner"], s["dog"])),
    }


class BillIn(BaseModel):
    delivery_ids: list[int] = Field(min_length=1, max_length=500)


@router.post("/v2/admin/billing/bill")
async def bill_older(p: BillIn, db: AsyncSession = Depends(get_db), _: User = Depends(get_current_admin)):
    today = today_ist()
    future = (await db.execute(select(Delivery.id).where(
        Delivery.id.in_(p.delivery_ids), Delivery.delivery_date > today))).scalars().all()
    if future:
        raise HTTPException(422, f"Can't bill future days: {list(future)[:10]}")
    out = await bill_selected(db, p.delivery_ids)
    await db.commit()
    return out


class DecisionIn(BaseModel):
    note: Optional[str] = None


async def pending_change(db: AsyncSession, change_id: int) -> DeliveryChange:
    c = (await db.execute(select(DeliveryChange).where(DeliveryChange.id == change_id)
                          .with_for_update())).scalar_one_or_none()
    if c is None:
        raise HTTPException(404, "Change not found")
    if c.status != "pending":
        raise HTTPException(409, f"Change is already {c.status}")
    return c


@router.post("/v2/admin/changes/{change_id}/approve")
async def approve(change_id: int, p: DecisionIn = DecisionIn(), db: AsyncSession = Depends(get_db),
                  admin: User = Depends(get_current_admin)):
    c = await pending_change(db, change_id)
    out = await approve_change(db, c, admin.id, p.note)
    await db.commit()
    return out


@router.post("/v2/admin/changes/{change_id}/reject")
async def reject(change_id: int, p: DecisionIn = DecisionIn(), db: AsyncSession = Depends(get_db),
                 admin: User = Depends(get_current_admin)):
    c = await pending_change(db, change_id)
    c.status, c.note = "rejected", p.note
    c.resolved_at, c.resolved_by = datetime.now(timezone.utc), admin.id
    await db.commit()
    return {"change_id": c.id, "status": "rejected"}
