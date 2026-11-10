"""v2 login: WhatsApp number + password (interim, until WhatsApp OTP is approved).

POST /v2/auth/login                          {phone, password, remember_me} -> same JWT as v1 /auth/login
POST /v2/admin/users/{user_id}/temp-password  admin: one-time password to hand a customer

Flow for customers the sheet sync created (they have no usable password):
  1. Admin calls temp-password and sends the password to the customer on WhatsApp.
  2. Customer logs in with phone + temp password; response says must_set_password = true.
  3. App calls v1 POST /profile/onboarding {new_password} -> customer's own password.
Existing v1 customers whose wa_phone is set can log in with their current password.

Later the OTP flow replaces step 1-2 and issues the same token; nothing else changes.
"""
import secrets
import time
from collections import defaultdict, deque
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import create_access_token, get_current_admin, hash_password, verify_password
from app.config import settings
from app.database import get_db
from app.models.user import User
from app.routers.v2_schedule import normalize_phone

router = APIRouter(tags=["v2 auth"])

# Per-number lockout: 5 wrong passwords in 15 minutes -> wait. Keyed by number, not IP:
# behind Railway's proxy every customer can appear to come from the same address.
# In-memory is fine for one API instance; a redeploy just resets the counters.
MAX_FAILS, WINDOW_S = 5, 15 * 60
_fails: dict[str, deque] = defaultdict(deque)
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))  # equalises timing for unknown numbers
_TEMP_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"   # no 0/o, 1/l/i


def _recent_fails(key: str) -> deque:
    q = _fails[key]
    cutoff = time.monotonic() - WINDOW_S
    while q and q[0] < cutoff:
        q.popleft()
    return q


class PhoneLogin(BaseModel):
    phone: str = Field(min_length=8, max_length=24)
    password: str = Field(min_length=1, max_length=128)
    remember_me: bool = False


@router.post("/v2/auth/login")
async def login_with_phone(p: PhoneLogin, db: AsyncSession = Depends(get_db)):
    bad = HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid phone number or password")
    wa = normalize_phone(p.phone)
    if not wa:
        raise bad
    fails = _recent_fails(wa)
    if len(fails) >= MAX_FAILS:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                            "Too many attempts. Try again in 15 minutes.")

    user = (await db.execute(select(User).where(User.wa_phone == wa))).scalar_one_or_none()
    ok = verify_password(p.password, user.password_hash if user else _DUMMY_HASH)
    if not user or not ok:
        fails.append(time.monotonic())
        raise bad
    _fails.pop(wa, None)

    expires = timedelta(days=30) if p.remember_me else timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    token = create_access_token({"sub": str(user.id)}, expires_delta=expires)
    return {"access_token": token, "token_type": "bearer", "must_set_password": not user.is_onboarded}


@router.post("/v2/admin/users/{user_id}/temp-password")
async def set_temp_password(user_id: int, db: AsyncSession = Depends(get_db),
                            _: User = Depends(get_current_admin)):
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    if user.is_admin:
        raise HTTPException(400, "Not for admin accounts")
    if not user.wa_phone:
        raise HTTPException(400, "User has no WhatsApp number; set users.wa_phone first")
    temp = "".join(secrets.choice(_TEMP_ALPHABET) for _ in range(10))
    user.password_hash = hash_password(temp)
    user.is_onboarded = False          # app asks them to set their own on first login
    await db.commit()
    _fails.pop(user.wa_phone, None)
    return {"user_id": user.id, "name": user.name, "wa_phone": user.wa_phone, "temp_password": temp,
            "note": "Shown once. Send it to the customer; they set their own password on first login."}
