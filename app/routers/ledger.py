from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models.ledger import Ledger
from app.models.user import User
from app.models.order import Order
from app.schemas.ledger import LedgerCreate, LedgerResponse, StatementResponse
from app.auth import get_current_user, get_current_admin

router = APIRouter(prefix="/ledger", tags=["Ledger"])

@router.post("/credit", response_model=LedgerResponse)
async def add_credit(
    payload: LedgerCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin)
):
    result = await db.execute(select(User).where(User.id == payload.user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    entry = Ledger(
        user_id=payload.user_id,
        type="credit",
        amount=payload.amount,
        mode_of_payment=payload.mode_of_payment,
        transaction_id=payload.transaction_id,
        description=payload.description,
        status="completed"
    )
    db.add(entry)
    user.current_balance += payload.amount
    await db.commit()
    await db.refresh(entry)
    return entry

@router.get("/statement/{user_id}", response_model=StatementResponse)
async def get_statement(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if not current_user.is_admin and current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    entries = await db.execute(
        select(Ledger)
        .where(Ledger.user_id == user_id)
        .order_by(Ledger.created_at.desc())
    )

    return StatementResponse(
        user_id=user.id,
        name=user.name,
        current_balance=user.current_balance,
        entries=entries.scalars().all()
    )

@router.get("/{user_id}", response_model=list[LedgerResponse])
async def get_ledger_entries(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if not current_user.is_admin and current_user.id != user_id:
        raise HTTPException(status_code=403, detail="Access denied")

    result = await db.execute(
        select(Ledger)
        .where(Ledger.user_id == user_id)
        .order_by(Ledger.created_at.desc())
    )
    entries = result.scalars().all()

    # Fetch linked orders to get order_date
    order_ids = [e.order_id for e in entries if e.order_id is not None]
    orders_map = {}
    if order_ids:
        orders_result = await db.execute(
            select(Order).where(Order.id.in_(order_ids))
        )
        for order in orders_result.scalars().all():
            orders_map[order.id] = order

    # Inject order_date into entries
    for entry in entries:
        if entry.order_id and entry.order_id in orders_map:
            entry.created_at = orders_map[entry.order_id].order_date or entry.created_at

    entries.sort(key=lambda e: e.created_at, reverse=True)
    return entries