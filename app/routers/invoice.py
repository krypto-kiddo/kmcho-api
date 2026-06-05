from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
from decimal import Decimal
from datetime import datetime, date
from typing import Optional
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.database import get_db
from app.models.user import User
from app.models.ledger import Ledger
from app.models.order import Order
from app.auth import get_current_user
from app.limiter import limiter

router = APIRouter(prefix="/invoice", tags=["invoice"])

MAX_RANGE_DAYS = 90

@router.get("/{user_id}")
@limiter.limit("10/minute")
async def get_invoice(
    request: Request,
    user_id: int,
    from_date: date,
    to_date: date,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if not current_user.is_admin and current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    if (to_date - from_date).days > MAX_RANGE_DAYS:
        raise HTTPException(status_code=400, detail="Date range cannot exceed 90 days")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    from_dt = datetime.combine(from_date, datetime.min.time())
    to_dt = datetime.combine(to_date, datetime.max.time())

    # Opening balance — use order_date if linked, else created_at
    all_before = await db.execute(
        select(Ledger)
        .outerjoin(Order, Ledger.order_id == Order.id)
        .where(
            and_(
                Ledger.user_id == user_id,
                Ledger.status == "completed",
                func.coalesce(Order.order_date, Ledger.created_at) < from_dt
            )
        )
    )
    entries_before = all_before.scalars().all()

    opening_balance = Decimal("0")
    for e in entries_before:
        if e.type in ("credit", "refund"):
            opening_balance += e.amount
        elif e.type == "debit":
            opening_balance -= e.amount

    # In-range entries — same coalesce, sort by effective date
    in_range = await db.execute(
        select(Ledger, Order.order_date)
        .outerjoin(Order, Ledger.order_id == Order.id)
        .where(
            and_(
                Ledger.user_id == user_id,
                Ledger.status == "completed",
                func.coalesce(Order.order_date, Ledger.created_at) >= from_dt,
                func.coalesce(Order.order_date, Ledger.created_at) <= to_dt
            )
        )
        .order_by(func.coalesce(Order.order_date, Ledger.created_at))
    )
    results = in_range.all()  # list of (Ledger, order_date | None)

    # Build rows with running balance
    rows = []
    running = opening_balance
    entries_only = []  # for totals

    for e, order_date in results:
        effective_date = order_date or e.created_at
        entries_only.append(e)

        if e.type == "credit":
            running += e.amount
            rows.append({
                "id": e.id,
                "date": effective_date.isoformat(),
                "description": e.description or "Payment received",
                "mode": e.mode_of_payment,
                "transaction_id": e.transaction_id,
                "credit": str(e.amount),
                "debit": None,
                "balance": str(running)
            })
        elif e.type == "debit":
            running -= e.amount
            rows.append({
                "id": e.id,
                "date": effective_date.isoformat(),
                "description": e.description or "Meal deduction",
                "mode": None,
                "transaction_id": None,
                "credit": None,
                "debit": str(e.amount),
                "balance": str(running)
            })
        elif e.type == "refund":
            running += e.amount
            rows.append({
                "id": e.id,
                "date": effective_date.isoformat(),
                "description": e.description or "Refund",
                "mode": None,
                "transaction_id": None,
                "credit": str(e.amount),
                "debit": None,
                "balance": str(running)
            })

    total_credits = sum(e.amount for e in entries_only if e.type in ("credit", "refund"))
    total_debits = sum(e.amount for e in entries_only if e.type == "debit")

    return {
        "user": {"id": user.id, "name": user.name, "phone": user.phone},
        "period": {"from": from_date.isoformat(), "to": to_date.isoformat()},
        "opening_balance": str(opening_balance),
        "closing_balance": str(running),
        "total_credits": str(total_credits),
        "total_debits": str(total_debits),
        "entries": rows
    }