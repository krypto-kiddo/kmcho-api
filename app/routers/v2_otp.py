"""v2 login with a WhatsApp code (template kmcho_login_otp).

POST /v2/auth/otp/request  {phone}                        -> same reply whether or not the number is a customer
POST /v2/auth/otp/verify   {phone, code, remember_me}     -> same JWT as v1 /auth/login (+ role)

Cost and abuse rules (each WhatsApp authentication message costs about ₹0.115):
- Codes go only to numbers that belong to an account (users.wa_phone). Anything else
  gets the identical reply and nothing is sent, so random numbers cost nothing and
  nobody can use this to find out who is a customer.
- Per number: 3 codes per 15 minutes, 10 per day (failed sends count too).
- Per code: 10 minutes (matches the message), 5 wrong guesses and it's dead.
  A new code replaces any older unused one.
- Everyone: OTP_DAILY_CAP codes per IST day; past that, sending stops (503) and an
  error is logged. Keeps a bug or an attack from running up the WhatsApp bill.
No per-IP limit: behind Railway's proxy every client shares an address.
"""
import hashlib
import hmac
import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import create_access_token
from app.config import settings
from app.database import get_db
from app.models.user import User
from app.models.v2 import OtpCode
from app.routers.v2_schedule import normalize_phone

log = logging.getLogger("kmcho.otp")
router = APIRouter(tags=["v2 auth"])

IST = timezone(timedelta(hours=5, minutes=30))
CODE_TTL = timedelta(minutes=10)
MAX_GUESSES = 5
PER_15_MIN, PER_DAY = 3, 10
SENT_REPLY = {"sent": True, "expires_in": int(CODE_TTL.total_seconds()),
              "message": "If this number is registered with KMCho, a code is on its way on WhatsApp."}


def code_hash(phone: str, code: str) -> str:
    # keyed with the JWT secret so a leaked table can't be brute-forced offline
    return hmac.new(settings.JWT_SECRET.encode(), f"{phone}:{code}".encode(), hashlib.sha256).hexdigest()


async def send_whatsapp_code(phone: str, code: str) -> tuple[bool, str]:
    """Returns (ok, message id or error). Replaced in tests."""
    if not (settings.WA_TOKEN and settings.PHONE_NUMBER_ID):
        return False, "WA_TOKEN / PHONE_NUMBER_ID not configured"
    payload = {
        "messaging_product": "whatsapp", "to": phone, "type": "template",
        "template": {
            "name": settings.OTP_TEMPLATE, "language": {"code": "en"},
            "components": [
                {"type": "body", "parameters": [{"type": "text", "text": code}]},
                {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": code}]},
            ],
        },
    }
    url = f"https://graph.facebook.com/{settings.WA_GRAPH_VERSION}/{settings.PHONE_NUMBER_ID}/messages"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload, headers={"Authorization": f"Bearer {settings.WA_TOKEN}"})
        body = r.json() if r.content else {}
    except (httpx.HTTPError, ValueError) as e:
        return False, f"network: {e}"
    if 200 <= r.status_code < 300:
        return True, ((body.get("messages") or [{}])[0]).get("id", "")
    err = body.get("error") or {}
    return False, f"{r.status_code} {err.get('message', '')} {(err.get('error_data') or {}).get('details', '')}".strip()


class OtpRequest(BaseModel):
    phone: str = Field(min_length=8, max_length=24)


class OtpVerify(BaseModel):
    phone: str = Field(min_length=8, max_length=24)
    code: str = Field(min_length=4, max_length=8)
    remember_me: bool = False


def _wait_minutes(oldest: datetime, window: timedelta, now: datetime) -> int:
    return max(1, int(((oldest + window) - now).total_seconds() // 60) + 1)


@router.post("/v2/auth/otp/request")
async def request_code(p: OtpRequest, db: AsyncSession = Depends(get_db)):
    wa = normalize_phone(p.phone)
    if not wa:
        raise HTTPException(422, "Enter a valid WhatsApp number")
    now = datetime.now(timezone.utc)

    user = (await db.execute(select(User).where(User.wa_phone == wa))).scalar_one_or_none()
    if user is None:
        return SENT_REPLY  # nothing sent, nothing stored, same answer

    # Lock this user's row so two quick taps can't both slip under the limits.
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    recent = (await db.execute(
        select(OtpCode.created_at).where(OtpCode.wa_phone == wa, OtpCode.created_at > now - timedelta(days=1))
        .order_by(OtpCode.created_at)
    )).scalars().all()
    last15 = [t for t in recent if t > now - timedelta(minutes=15)]
    if len(last15) >= PER_15_MIN:
        m = _wait_minutes(last15[0], timedelta(minutes=15), now)
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"Too many codes requested. Try again in {m} min.")
    if len(recent) >= PER_DAY:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many codes today. Try again tomorrow.")

    day_start = datetime.now(IST).replace(hour=0, minute=0, second=0, microsecond=0)
    sent_today = (await db.execute(select(func.count()).select_from(OtpCode)
                                   .where(OtpCode.created_at >= day_start))).scalar_one()
    if sent_today >= settings.OTP_DAILY_CAP:
        log.error("OTP daily cap reached (%s); login codes paused until midnight IST", settings.OTP_DAILY_CAP)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Login codes are paused for today. Contact KMCho on WhatsApp.")

    # one live code per number
    await db.execute(update(OtpCode).where(OtpCode.wa_phone == wa, OtpCode.status == "sent")
                     .values(status="replaced"))
    code = f"{secrets.randbelow(10**6):06d}"
    row = OtpCode(user_id=user.id, wa_phone=wa, code_hash=code_hash(wa, code),
                  created_at=now, expires_at=now + CODE_TTL)
    db.add(row)
    await db.flush()

    ok, detail = await send_whatsapp_code(wa, code)
    if ok:
        row.wa_message_id = detail
    else:
        row.status, row.error = "failed", detail[:500]
        log.warning("OTP send failed for user %s: %s", user.id, detail)
    await db.commit()
    if not ok:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, "Couldn't send the code on WhatsApp. Try again in a minute.")
    return SENT_REPLY


@router.post("/v2/auth/otp/verify")
async def verify_code(p: OtpVerify, db: AsyncSession = Depends(get_db)):
    wrong = HTTPException(status.HTTP_400_BAD_REQUEST, "That code is wrong or has expired. Request a new one.")
    wa = normalize_phone(p.phone)
    code = "".join(ch for ch in p.code if ch.isdigit())
    if not wa or len(code) != 6:
        raise wrong
    now = datetime.now(timezone.utc)

    row: Optional[OtpCode] = (await db.execute(
        select(OtpCode).where(OtpCode.wa_phone == wa, OtpCode.status == "sent")
        .order_by(OtpCode.created_at.desc()).limit(1).with_for_update()
    )).scalar_one_or_none()
    if row is None or row.expires_at <= now:
        if row is not None:
            row.status = "dead"; await db.commit()
        raise wrong

    if not hmac.compare_digest(row.code_hash, code_hash(wa, code)):
        row.attempts += 1
        if row.attempts >= MAX_GUESSES:
            row.status = "dead"
        await db.commit()
        if row.status == "dead":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Too many wrong tries. Request a new code.")
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"That code is wrong. {MAX_GUESSES - row.attempts} tries left.")

    row.status = "used"
    user = await db.get(User, row.user_id)
    await db.commit()
    if user is None or user.wa_phone != wa:
        raise wrong

    expires = timedelta(days=30) if p.remember_me else timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    token = create_access_token({"sub": str(user.id)}, expires_delta=expires)
    return {"access_token": token, "token_type": "bearer",
            "role": "admin" if user.is_admin else "customer"}
