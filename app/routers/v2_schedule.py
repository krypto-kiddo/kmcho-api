"""v2 schedule: the kitchen sheet pushes its month grid here; the app reads it back.

POST /v2/schedule/sync   Apps Script only (HMAC-signed). Full snapshot of one month tab.
GET  /v2/schedule/me     Customer's deliveries for a date range (app cards).

Sync rules
- A cell becomes one `deliveries` row per (dog, date). Blank cell = no row.
- Unbilled rows follow the sheet exactly: created, updated, or deleted.
- Billed rows (billed_ledger_id set) are locked:
    same text                 -> nothing
    text edit, same billable  -> applied, logged as auto_applied
    billable flips / cleared  -> row untouched, pending DeliveryChange for an admin
    error cell (#REF! etc.)   -> ignored, reported as an issue
- Dogs not present in the push are left alone (a deleted sheet row never wipes history).

Customers come from the sheet too (the sheet is the source of truth for who is a customer):
- A row's WhatsApp number is the customer's identity. Names are labels only.
- Unknown number -> a user is created (name = Owner column, no email, unusable password,
  so v1 login is impossible; v2 login is meant to be OTP on this number).
- Unknown dog for a known number -> a dog is created, with NO monthly_rate. Billing must
  skip dogs without a rate, so a sheet typo can create a stray user/dog but never a charge.
- Nothing is ever deleted, renamed or deactivated by the sync. Existing users.name is
  never overwritten by the sheet.
- Every created user/dog is reported in `issues` (level "info") so it shows in the Sync log.
- Rows without a usable number, dog or (for a new customer) owner are reported, not created.
- A dog an admin deactivated is not recreated; its row is reported as dog_inactive.
"""
import secrets
import hashlib
import hmac
import json
import re
import time
from calendar import monthrange
from collections import Counter
from dataclasses import dataclass
from datetime import date as Date, datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, hash_password
from app.config import settings
from app.database import get_db
from app.models.user import User
from app.models.v2 import Delivery, DeliveryChange, Dog, SyncRun

router = APIRouter(prefix="/v2/schedule", tags=["v2 schedule"])

IST = timezone(timedelta(hours=5, minutes=30))
MAX_SKEW_SECONDS = 300
ERROR_CELL = re.compile(r"^#(N/A|[A-Z0-9/]+[!?])$")  # #N/A #REF! #VALUE! #DIV/0! #NAME? #ERROR!


# ---------- helpers ----------

def normalize_phone(raw: str) -> Optional[str]:
    """Same rules as normalisePhone_() in the WhatsApp sender:
    10 digits -> 91 + number; 11 digits starting 0 -> 91 + rest; anything else
    kept as-is (foreign numbers carry their own country code).
    Only addition: fewer than 8 or more than 15 digits can't be a WhatsApp number -> None."""
    d = re.sub(r"\D", "", raw or "")
    if len(d) == 10:
        return "91" + d
    if len(d) == 11 and d.startswith("0"):
        return "91" + d[1:]
    return d if 8 <= len(d) <= 15 else None


def name_key(name: str) -> str:
    return " ".join((name or "").split()).casefold()


def today_ist() -> Date:
    return datetime.now(IST).date()


@dataclass(frozen=True)
class Cell:
    raw: str
    kind: str                 # meal | keyword | error
    dish: Optional[str]
    keyword: Optional[str]
    reason: Optional[str]
    billable: bool


def classify(raw: str, keywords: dict) -> Optional[Cell]:
    v = " ".join((raw or "").split())
    if not v:
        return None
    if ERROR_CELL.match(v):
        return Cell(v, "error", None, None, None, False)
    k = keywords.get(v.upper())
    if k is not None:
        return Cell(v, "keyword", None, v.upper(), (k.reason or "").strip() or None, k.billable)
    return Cell(v, "meal", v, None, None, True)


def apply_cell(d: Delivery, c: Cell) -> None:
    d.raw_value, d.kind, d.dish, d.keyword, d.reason, d.billable = (
        c.raw, c.kind, c.dish, c.keyword, c.reason, c.billable
    )


def same(d: Delivery, c: Cell) -> bool:
    return (d.raw_value, d.kind, d.reason, d.billable) == (c.raw, c.kind, c.reason, c.billable)


async def verified_body(request: Request) -> bytes:
    """Signature = hex(HMAC-SHA256(secret, f"{timestamp}.{raw body}"))."""
    secret = settings.SHEET_SYNC_SECRET
    if not secret:
        raise HTTPException(503, "Sheet sync is not configured")
    ts = request.headers.get("X-KMCho-Timestamp", "")
    sig = request.headers.get("X-KMCho-Signature", "").lower()
    try:
        ts_int = int(ts)
    except ValueError:
        raise HTTPException(401, "Bad timestamp")
    if abs(time.time() - ts_int) > MAX_SKEW_SECONDS:
        raise HTTPException(401, "Stale request")
    body = await request.body()
    expected = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise HTTPException(401, "Bad signature")
    return body


# ---------- payload ----------

class KeywordIn(BaseModel):
    keyword: str
    reason: Optional[str] = None
    billable: bool = False


class RowIn(BaseModel):
    row: int                  # sheet row number, for error reports
    owner: str = ""
    dog: str = ""
    phone: str = ""
    cells: list[str]          # display values, one per entry in `dates`


class MessageIn(BaseModel):
    phone: str
    dog: str
    date: Date
    wa_message_id: str = Field(max_length=200)


class SyncPayload(BaseModel):
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    sheet_tab: str
    dates: list[Date]
    keywords: list[KeywordIn] = []
    rows: list[RowIn]
    messages: list[MessageIn] = []


# ---------- sync ----------

@router.post("/sync")
async def sync_schedule(request: Request, db: AsyncSession = Depends(get_db)):
    body = await verified_body(request)
    try:
        p = SyncPayload.model_validate_json(body)
    except ValidationError as e:
        raise HTTPException(422, json.loads(e.json(include_url=False)))

    y, m = map(int, p.month.split("-"))
    first, last = Date(y, m, 1), Date(y, m, monthrange(y, m)[1])
    if len(set(p.dates)) != len(p.dates) or any(not (first <= d <= last) for d in p.dates):
        raise HTTPException(422, "dates must be unique and inside month")
    bad = [r.row for r in p.rows if len(r.cells) != len(p.dates)]
    if bad:
        raise HTTPException(422, f"cells/dates length mismatch on rows {bad[:10]}")

    # The sender only treats a Keywords row as a keyword when it also has a reason
    # (otherwise it sends the cell as a dish), so do the same here.
    keywords = {" ".join(k.keyword.split()).upper(): k
                for k in p.keywords if k.keyword.strip() and (k.reason or "").strip()}
    issues: list[dict] = []
    stats: Counter = Counter()
    now = datetime.now(timezone.utc)

    # -- match sheet rows to dogs: normalised WhatsApp number -> user, dog name -> dog.
    # Missing users/dogs are created (see module docstring). Two pushes racing to create
    # the same user hit the unique constraint, roll back whole, and the next push succeeds.
    phones = {normalize_phone(x.phone) for x in [*p.rows, *p.messages]} - {None}
    user_by_phone: dict[str, int] = {}
    if phones:
        res = await db.execute(select(User.id, User.wa_phone).where(User.wa_phone.in_(phones)))
        user_by_phone = {wa: uid for uid, wa in res.all()}
    dog_by_key: dict[tuple, Dog] = {}  # includes inactive dogs, so they're never recreated
    if user_by_phone:
        res = await db.execute(select(Dog).where(Dog.user_id.in_(user_by_phone.values())))
        dog_by_key = {(d.user_id, d.name_key): d for d in res.scalars()}

    def problem(r: RowIn, issue: str, **extra) -> None:
        issues.append({"row": r.row, "owner": r.owner, "dog": r.dog, "phone": r.phone,
                       "issue": issue, "level": "problem", **extra})

    def info(r: RowIn, issue: str, **extra) -> None:
        issues.append({"row": r.row, "owner": r.owner, "dog": r.dog, "issue": issue,
                       "level": "info", **extra})

    matched: list[tuple[RowIn, Dog]] = []
    first_row_for: dict[int, int] = {}
    for r in p.rows:
        pn = normalize_phone(r.phone)
        dog_name = " ".join(r.dog.split())
        owner = " ".join(r.owner.split())
        if not pn:
            problem(r, "bad_number")
            continue
        if not dog_name:
            problem(r, "missing_dog")
            continue

        uid = user_by_phone.get(pn)
        if uid is None:
            if not owner:
                problem(r, "missing_owner")
                continue
            user = User(
                name=owner,
                phone=" ".join(r.phone.split()),
                wa_phone=pn,
                password_hash=hash_password(secrets.token_urlsafe(32)),  # nobody knows it
                is_admin=False,
                is_onboarded=False,
                current_balance=0,
            )
            db.add(user)
            await db.flush()
            uid = user_by_phone[pn] = user.id
            stats["created_users"] += 1
            info(r, "created_user", wa_phone=pn)

        key = (uid, name_key(dog_name))
        dog = dog_by_key.get(key)
        if dog is None:
            dog = Dog(user_id=uid, name=dog_name, name_key=key[1], active=True)
            db.add(dog)
            await db.flush()
            dog_by_key[key] = dog
            stats["created_dogs"] += 1
            info(r, "created_dog")
        elif not dog.active:
            problem(r, "dog_inactive")
            continue

        if dog.id in first_row_for:
            problem(r, "duplicate_row", first_row=first_row_for[dog.id])
            continue
        first_row_for[dog.id] = r.row
        matched.append((r, dog))

    wa_ids: dict[tuple, str] = {}
    for msg in p.messages:
        pn = normalize_phone(msg.phone)
        dog = dog_by_key.get((user_by_phone.get(pn), name_key(msg.dog))) if pn else None
        if dog and dog.active and first <= msg.date <= last:
            wa_ids[(dog.id, msg.date)] = msg.wa_message_id

    # -- load what we already have for these dogs this month (locked against the billing job)
    existing: dict[tuple, Delivery] = {}
    pending: dict[int, DeliveryChange] = {}
    dog_ids = [d.id for _, d in matched]
    if dog_ids:
        res = await db.execute(
            select(Delivery)
            .where(Delivery.dog_id.in_(dog_ids), Delivery.delivery_date.between(first, last))
            .with_for_update()
        )
        existing = {(d.dog_id, d.delivery_date): d for d in res.scalars()}
        locked_ids = [d.id for d in existing.values() if d.billed_ledger_id is not None]
        if locked_ids:
            res = await db.execute(
                select(DeliveryChange).where(
                    DeliveryChange.delivery_id.in_(locked_ids), DeliveryChange.status == "pending"
                )
            )
            pending = {c.delivery_id: c for c in res.scalars()}

    def withdraw(c: Optional[DeliveryChange]) -> None:
        if c is not None and c.status == "pending":
            c.status, c.resolved_at = "withdrawn", now

    # -- diff
    for r, dog in matched:
        for i, day in enumerate(p.dates):
            cell = classify(r.cells[i], keywords)
            key = (dog.id, day)
            cur = existing.get(key)
            wa_id = wa_ids.get(key)

            if cur is None:
                if cell is None:
                    continue
                d = Delivery(dog_id=dog.id, delivery_date=day, sheet_tab=p.sheet_tab,
                             sheet_row=r.row, wa_message_id=wa_id)
                apply_cell(d, cell)
                db.add(d)
                stats["created"] += 1
                continue

            if wa_id and cur.wa_message_id != wa_id:
                cur.wa_message_id = wa_id
            if cur.sheet_row != r.row:
                cur.sheet_row = r.row

            if cur.billed_ledger_id is None:                 # unlocked: mirror the sheet
                if cell is None:
                    await db.delete(cur)
                    stats["deleted"] += 1
                elif same(cur, cell):
                    stats["unchanged"] += 1
                else:
                    apply_cell(cur, cell)
                    stats["updated"] += 1
                continue

            # locked (already billed)
            open_change = pending.get(cur.id)
            new_raw = cell.raw if cell else ""
            if new_raw == cur.raw_value:
                withdraw(open_change)
                stats["unchanged"] += 1
                continue
            if cell is not None and cell.kind == "error":
                issues.append({"row": r.row, "dog": r.dog, "date": day.isoformat(),
                               "issue": "error_on_billed_day", "level": "problem", "value": cell.raw})
                continue
            new_billable = cell.billable if cell else False
            if cell is not None and new_billable == cur.billable:
                db.add(DeliveryChange(delivery_id=cur.id, old_raw=cur.raw_value, new_raw=new_raw,
                                      old_billable=cur.billable, new_billable=new_billable,
                                      status="auto_applied", resolved_at=now))
                withdraw(open_change)
                apply_cell(cur, cell)
                stats["auto_applied"] += 1
                continue
            if open_change is None:
                db.add(DeliveryChange(delivery_id=cur.id, old_raw=cur.raw_value, new_raw=new_raw,
                                      old_billable=cur.billable, new_billable=new_billable,
                                      status="pending", last_seen_at=now))
            else:
                open_change.new_raw, open_change.new_billable = new_raw, new_billable
                open_change.last_seen_at = now
            stats["flagged"] += 1

    stats["rows_matched"] = len(matched)
    stats["rows_unmatched"] = len({x["row"] for x in issues
                                   if x["issue"] in ("bad_number", "missing_dog", "missing_owner",
                                                     "dog_inactive", "duplicate_row")})
    db.add(SyncRun(month=p.month, sheet_tab=p.sheet_tab, stats=dict(stats), issues=issues))
    await db.commit()
    return {"ok": True, "month": p.month, "stats": dict(stats), "issues": issues}


# ---------- app read ----------

@router.get("/me")
async def my_schedule(
    start: Optional[Date] = None,
    end: Optional[Date] = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Defaults to yesterday..tomorrow (IST) for the three summary cards."""
    t = today_ist()
    start = start or t - timedelta(days=1)
    end = end or t + timedelta(days=1)
    if end < start or (end - start).days > 62:
        raise HTTPException(422, "range must be 0-62 days")

    res = await db.execute(
        select(Delivery, Dog.name)
        .join(Dog, Dog.id == Delivery.dog_id)
        .where(Dog.user_id == user.id, Delivery.delivery_date.between(start, end))
        .order_by(Delivery.delivery_date, Dog.name)
    )
    return [
        {
            "date": d.delivery_date.isoformat(),
            "dog": dog_name,
            "kind": d.kind,
            "dish": d.dish,
            "keyword": d.keyword,
            "reason": d.reason,
            "billable": d.billable,
            "billed": d.billed_ledger_id is not None,
        }
        for d, dog_name in res.all()
    ]
