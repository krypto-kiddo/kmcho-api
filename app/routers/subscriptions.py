from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from app.database import get_db
from app.models.subscription import Subscription
from app.models.user import User
from app.schemas.subscription import (
    SubscriptionCreate,
    SubscriptionUpdate,
    SubscriptionResponse
)
from app.auth import get_current_admin

from datetime import date as DateType
from typing import Optional


router = APIRouter(prefix="/subscriptions", tags=["subscriptions"])


@router.get("/daily-preview")
async def daily_preview(
    date: Optional[DateType] = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin)
):
    if date is None:
        date = DateType.today()

    result = await db.execute(
        select(Subscription)
        .options(joinedload(Subscription.user))
        .where(
            Subscription.active == True,
            (Subscription.paused_until == None) | (Subscription.paused_until < date)
        )
    )
    subs = result.scalars().all()

    orders = []
    ingredients = {}

    for sub in subs:
        customer_orders = []

        if sub.bowl:
            for bowl in sub.bowl:
                if not is_due(bowl["days"], date):
                    continue

                protein = resolve_cycle(bowl["protein"]["cycle"], sub.protein_index)
                base = resolve_cycle(bowl["base"]["cycle"], sub.base_index) if bowl["base"]["qty"] > 0 else None
                qty = bowl["qty"]

                # aggregate ingredients
                p_key = protein.lower()
                ingredients[p_key] = ingredients.get(p_key, 0) + (bowl["protein"]["qty"] * qty)
                if base:
                    b_key = base.lower()
                    ingredients[b_key] = ingredients.get(b_key, 0) + (bowl["base"]["qty"] * qty)

                customer_orders.append({
                    "type": "bowl",
                    "protein": protein,
                    "protein_qty": bowl["protein"]["qty"],
                    "base": base,
                    "base_qty": bowl["base"]["qty"] if base else 0,
                    "qty": qty,
                    "boneless": bowl["boneless"],
                    "pricing": bowl["pricing"],
                })

        if sub.broth:
            if is_due(sub.broth["days"], date):
                customer_orders.append({
                    "type": "broth",
                    "qty": sub.broth["qty"],
                    "pricing": sub.broth["pricing"],
                })

        if customer_orders:
            orders.append({
                "user_id": sub.user_id,
                "user_name": sub.user.name,
                "items": customer_orders,
            })

    # format ingredients
    compendium = [
        {
            "ingredient": k.capitalize(),
            "grams": v,
            "display": f"{v/1000:.2f} kg" if v >= 1000 else f"{v} g"
        }
        for k, v in sorted(ingredients.items())
    ]

    return {
        "date": date.isoformat(),
        "total_customers": len(orders),
        "total_items": sum(len(o["items"]) for o in orders),
        "orders": orders,
        "compendium": compendium,
    }


@router.post("/", response_model=SubscriptionResponse)
async def create_subscription(
    payload: SubscriptionCreate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin)
):
    result = await db.execute(
        select(Subscription).where(Subscription.user_id == payload.user_id)
    )
    if result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Subscription already exists for this user")

    sub = Subscription(
        user_id=payload.user_id,
        active=payload.active,
        paused_until=payload.paused_until,
        bowl=[b.model_dump() for b in payload.bowl] if payload.bowl else None,
        broth=payload.broth.model_dump() if payload.broth else None,
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return sub


@router.get("/{user_id}", response_model=SubscriptionResponse)
async def get_subscription(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin)
):
    result = await db.execute(
        select(Subscription).where(Subscription.user_id == user_id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")
    return sub


@router.patch("/{user_id}", response_model=SubscriptionResponse)
async def update_subscription(
    user_id: int,
    payload: SubscriptionUpdate,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin)
):
    result = await db.execute(
        select(Subscription).where(Subscription.user_id == user_id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    if payload.bowl is not None:
        sub.bowl = [b.model_dump() for b in payload.bowl]
    if payload.broth is not None:
        sub.broth = payload.broth.model_dump()
    if payload.active is not None:
        sub.active = payload.active
    if payload.paused_until is not None:
        sub.paused_until = payload.paused_until

    await db.commit()
    await db.refresh(sub)
    return sub

@router.get("/", response_model=list[SubscriptionResponse])
async def get_all_subscriptions(
    active_only: bool = True,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin)
):
    query = select(Subscription)
    if active_only:
        query = query.where(Subscription.active == True)
    result = await db.execute(query)
    return result.scalars().all()


@router.delete("/{user_id}", response_model=SubscriptionResponse)
async def deactivate_subscription(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_admin)
):
    result = await db.execute(
        select(Subscription).where(Subscription.user_id == user_id)
    )
    sub = result.scalar_one_or_none()
    if not sub:
        raise HTTPException(status_code=404, detail="Subscription not found")

    sub.active = False
    await db.commit()
    await db.refresh(sub)
    return sub

def is_due(days: dict, date: DateType) -> bool:
    freq = days["frequency"]
    if freq == 0:
        return True
    if freq == 1:
        return date.day % 2 == 0
    if freq == 2:
        dow = date.isoweekday()  # Mon=1 .. Sun=7
        return dow in days["on"]
    return False


def resolve_cycle(cycle: list, index: int) -> str:
    return cycle[index % len(cycle)]