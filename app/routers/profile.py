from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.database import get_db
from app.models.user import User
from app.schemas.user import ProfileUpdate, PasswordChange, OnboardingComplete, UserResponse
from app.auth import get_current_user, hash_password, verify_password

router = APIRouter(prefix="/profile", tags=["Profile"])

@router.get("/", response_model=UserResponse)
async def get_profile(current_user: User = Depends(get_current_user)):
    return current_user

@router.patch("/", response_model=UserResponse)
async def update_profile(
    payload: ProfileUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if payload.name is not None:
        current_user.name = payload.name
    if payload.phone is not None:
        current_user.phone = payload.phone
    if payload.email is not None:
        # Check email not taken by another user
        result = await db.execute(select(User).where(User.email == payload.email))
        existing = result.scalar_one_or_none()
        if existing and existing.id != current_user.id:
            raise HTTPException(status_code=400, detail="Email already in use")
        current_user.email = payload.email

    await db.commit()
    await db.refresh(current_user)
    return current_user

@router.patch("/password", response_model=UserResponse)
async def change_password(
    payload: PasswordChange,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if not verify_password(payload.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")

    current_user.password_hash = hash_password(payload.new_password)
    await db.commit()
    await db.refresh(current_user)
    return current_user

@router.post("/onboarding", response_model=UserResponse)
async def complete_onboarding(
    payload: OnboardingComplete,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    if current_user.is_onboarded:
        raise HTTPException(status_code=400, detail="Already onboarded")

    if payload.name:
        current_user.name = payload.name
    if payload.phone:
        current_user.phone = payload.phone

    current_user.password_hash = hash_password(payload.new_password)
    current_user.is_onboarded = True

    await db.commit()
    await db.refresh(current_user)
    return current_user