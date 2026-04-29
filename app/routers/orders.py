from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models.order import Order
from app.models.ledger import Ledger
from app.models.user import User
from app.schemas.order import OrderCreate, OrderUpdateStatus, OrderResponse
from app.auth import get_current_user, get_current_admin
from decimal import Decimal

router = APIRouter(prefix="/orders", tags=["Orders"])

@router.post("/", response_model=OrderResponse)
async def create_order(
    payload: OrderCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin)
):
    result = await db.execute(select(User).where(User.id == payload.user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if user.current_balance < payload.amount:
        raise HTTPException(status_code=400, detail="Insufficient balance")

    order = Order(
        user_id=payload.user_id,
        description=payload.description,
        order_date=payload.order_date,
        status="pending"
    )
    db.add(order)
    await db.flush()

    ledger_entry = Ledger(
        user_id=payload.user_id,
        order_id=order.id,
        type="debit",
        amount=payload.amount,
        status="completed",
        description=payload.description or "Meal deduction"
    )
    db.add(ledger_entry)

    user.current_balance -= payload.amount
    await db.commit()
    await db.refresh(order)
    return order

@router.get("/", response_model=list[OrderResponse])
async def get_orders(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.is_admin:
        result = await db.execute(select(Order))
    else:
        result = await db.execute(select(Order).where(Order.user_id == current_user.id))
    return result.scalars().all()

@router.get("/{order_id}", response_model=OrderResponse)
async def get_order(
    order_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if not current_user.is_admin and order.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Access denied")
    return order

@router.patch("/{order_id}/status", response_model=OrderResponse)
async def update_order_status(
    order_id: int,
    payload: OrderUpdateStatus,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_admin)
):
    valid_statuses = ["pending", "delivered", "cancelled"]
    if payload.status not in valid_statuses:
        raise HTTPException(status_code=400, detail=f"Status must be one of {valid_statuses}")

    result = await db.execute(select(Order).where(Order.id == order_id))
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    order.status = payload.status
    await db.commit()
    await db.refresh(order)
    return order