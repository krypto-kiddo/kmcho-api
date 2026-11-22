"""v2 schedule: the kitchen sheet pushes its month grid here; the app reads it back.

POST /v2/schedule/sync   Apps Script only (HMAC-signed). Full snapshot of one month tab.
GET  /v2/schedule/me     Customer's deliveries for a date range (app cards).

Sync rules
- A cell becomes one `deliveries` row per (dog, date). Blank cell = no row.
- Unbilled rows follow the sheet exactly: created, updated, or deleted.
- Each cell gets a billing mode: meal -> charge; keyword -> its Keywords "Billing" value
  (Charge / Credit / Skip, default Credit); formula error -> hold. See app/v2_billing.py.
- Billed rows (billed_at set) are locked:
    same text                     -> nothing
    text edit, same billing mode  -> applied, logged as auto_applied
    billing mode changes / cleared -> row untouched, pending DeliveryChange for an admin
    a value an admin rejected     -> not flagged again
    error cell (#REF! etc.)       -> ignored, reported as an issue
- Payments tab (current month only) -> monthly_amounts, one amount per dog. The Dog
  column may be blank only when the number has exactly one dog.
- Payments "Previous dues" (current month only) -> opening_balances, summed per number,
  until an admin applies it (app/v2_opening.py); then frozen.
- Received tab (current month pushes) -> payments_received. Unposted rows follow the sheet;
  rows already posted to the ledger are never removed (a missing one is reported).
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
- Admin accounts are never matched: a row (or Payments row) with an admin's number is
  skipped and reported as admin_number. Admins log in by number, so their wa_phone is set.
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
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user, hash_password
from app.config import settings
from app.database import get_db
from app.models.user import User
from app.models.v2 import (
    Delivery, DeliveryChange, Dog, MonthlyAmount, OpeningBalance, PaymentReceived, SyncRun,
)

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


BILLING_MODES = ("charge", "credit", "skip")


@dataclass(frozen=True)
class Cell:
    raw: str
    kind: str                 # meal | keyword | error
    dish: Optional[str]
    keyword: Optional[str]
    reason: Optional[str]
    billing: str              # charge | credit | skip | hold

    @property
    def billable(self) -> bool:
        return self.billing == "charge"

    def as_dict(self) -> dict:
        return {"raw": self.raw, "kind": self.kind, "dish": self.dish, "keyword": self.keyword,
                "reason": self.reason, "billing": self.billing}


def keyword_mode(k) -> str:
    """Keywords tab "Billing" column; old "Billable?" Yes = Charge; default Credit."""
    mode = (k.billing or "").strip().lower()
    if mode in BILLING_MODES:
        return mode
    return "charge" if k.billable else "credit"


def classify(raw: str, keywords: dict) -> Optional[Cell]:
    v = " ".join((raw or "").split())
    if not v:
        return None
    if ERROR_CELL.match(v):
        return Cell(v, "error", None, None, None, "hold")
    k = keywords.get(v.upper())
    if k is not None:
        return Cell(v, "keyword", None, v.upper(), (k.reason or "").strip() or None, keyword_mode(k))
    return Cell(v, "meal", v, None, None, "charge")


def apply_cell(d: Delivery, c: Cell) -> None:
    d.raw_value, d.kind, d.dish, d.keyword, d.reason, d.billing = (
        c.raw, c.kind, c.dish, c.keyword, c.reason, c.billing
    )
    d.billable = c.billable


def same(d: Delivery, c: Cell) -> bool:
    return (d.raw_value, d.kind, d.reason, d.billing) == (c.raw, c.kind, c.reason, c.billing)


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
    billable: bool = False            # legacy "Billable?" column
    billing: Optional[str] = None     # "Billing" column: Charge | Credit | Skip


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


class PaymentIn(BaseModel):
    row: int
    owner: str = ""
    phone: str = ""
    dog: str = ""                     # blank = the number's only dog
    amount: Optional[float] = None    # monthly amount for this dog; blank = not set
    previous_dues: Optional[float] = None  # owed before this month (+) or advance (-); per number


class ReceivedIn(BaseModel):
    """One row of the Received tab: a payment the kitchen got."""
    row: int
    phone: str = ""
    date: str = ""                    # yyyy-mm-dd from the script; anything else is reported
    amount: Optional[float] = None
    mode: str = Field("", max_length=40)
    reference: str = Field("", max_length=120)


class SyncPayload(BaseModel):
    month: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    sheet_tab: str
    dates: list[Date]
    keywords: list[KeywordIn] = []
    rows: list[RowIn]
    messages: list[MessageIn] = []
    payments: list[PaymentIn] = []    # only applied when `month` is the current IST month
    dues_column: bool = False         # Payments tab has a "Previous dues" column
    received: Optional[list[ReceivedIn]] = None  # Received tab; None = tab not sent


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
    phones = {normalize_phone(x.phone) for x in [*p.rows, *p.messages, *p.payments]} - {None}
    user_by_phone: dict[str, int] = {}
    admin_phones: set[str] = set()  # admins log in by number too; their rows are never synced
    if phones:
        res = await db.execute(select(User.id, User.wa_phone, User.is_admin).where(User.wa_phone.in_(phones)))
        for uid, wa, is_admin in res.all():
            if is_admin:
                admin_phones.add(wa)      # never matched, created over, or billed
            else:
                user_by_phone[wa] = uid
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

        if pn in admin_phones:
            problem(r, "admin_number")    # use a separate customer number, or add it to MONITOR_NUMBERS
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
    rejected: dict[int, set] = {}   # delivery_id -> sheet values an admin already said no to
    dog_ids = [d.id for _, d in matched]
    if dog_ids:
        res = await db.execute(
            select(Delivery)
            .where(Delivery.dog_id.in_(dog_ids), Delivery.delivery_date.between(first, last))
            .with_for_update()
        )
        existing = {(d.dog_id, d.delivery_date): d for d in res.scalars()}
        locked_ids = [d.id for d in existing.values() if d.billed_at is not None]
        if locked_ids:
            res = await db.execute(
                select(DeliveryChange)
                .where(DeliveryChange.delivery_id.in_(locked_ids),
                       DeliveryChange.status.in_(("pending", "rejected")))
                .order_by(DeliveryChange.id)
            )
            for c in res.scalars():
                if c.status == "pending":
                    pending[c.delivery_id] = c
                else:
                    rejected.setdefault(c.delivery_id, set()).add(c.new_raw)

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

            if cur.billed_at is None:                        # unlocked: mirror the sheet
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
            if new_raw in rejected.get(cur.id, ()):
                withdraw(open_change)
                stats["rejected_kept"] += 1
                continue
            if cell is not None and cell.kind == "error":
                issues.append({"row": r.row, "dog": r.dog, "date": day.isoformat(),
                               "issue": "error_on_billed_day", "level": "problem", "value": cell.raw})
                continue
            new_billing = cell.billing if cell else "skip"
            if cell is not None and new_billing == cur.billing:
                db.add(DeliveryChange(delivery_id=cur.id, old_raw=cur.raw_value, new_raw=new_raw,
                                      old_billable=cur.billable, new_billable=cell.billable,
                                      old_billing=cur.billing, new_billing=new_billing,
                                      new_cell=cell.as_dict(), status="auto_applied", resolved_at=now))
                withdraw(open_change)
                apply_cell(cur, cell)
                stats["auto_applied"] += 1
                continue
            snapshot = cell.as_dict() if cell else None
            if open_change is None:
                db.add(DeliveryChange(delivery_id=cur.id, old_raw=cur.raw_value, new_raw=new_raw,
                                      old_billable=cur.billable, new_billable=new_billing == "charge",
                                      old_billing=cur.billing, new_billing=new_billing,
                                      new_cell=snapshot, status="pending", last_seen_at=now))
            else:
                open_change.new_raw, open_change.new_billable = new_raw, new_billing == "charge"
                open_change.new_billing, open_change.new_cell = new_billing, snapshot
                open_change.last_seen_at = now
            stats["flagged"] += 1

    # -- Payments tab -> monthly_amounts (current month only: the tab only ever holds this month)
    if p.payments and p.month == today_ist().strftime("%Y-%m"):
        await apply_payments(db, p, user_by_phone, dog_by_key, issues, stats, admin_phones)
        if p.dues_column:
            await apply_dues(db, p, user_by_phone, issues, stats, admin_phones)
    if p.received is not None and p.month == today_ist().strftime("%Y-%m"):
        await apply_received(db, p.received, user_by_phone, issues, stats, admin_phones)

    stats["rows_matched"] = len(matched)
    stats["rows_unmatched"] = len({x["row"] for x in issues
                                   if x.get("tab") is None and x["issue"] in (
                                                     "bad_number", "missing_dog", "missing_owner", "admin_number",
                                                     "dog_inactive", "duplicate_row")})
    db.add(SyncRun(month=p.month, sheet_tab=p.sheet_tab, stats=dict(stats), issues=issues))
    await db.commit()
    return {"ok": True, "month": p.month, "stats": dict(stats), "issues": issues}


def month_bounds(month: str) -> tuple[Date, Date]:
    y, m = map(int, month.split("-"))
    first = Date(y, m, 1)
    return first, (Date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))


async def apply_payments(db, p, user_by_phone, dog_by_key, issues, stats, admin_phones=frozenset()) -> None:
    def note(r, issue, level, **extra):
        issues.append({"tab": "Payments", "row": r.row, "owner": r.owner, "dog": r.dog,
                       "issue": issue, "level": level, **extra})

    dogs_of: dict[int, list[Dog]] = {}
    for (uid, _), d in dog_by_key.items():
        if d.active:
            dogs_of.setdefault(uid, []).append(d)

    res = await db.execute(select(MonthlyAmount).where(
        MonthlyAmount.month == p.month,
        MonthlyAmount.dog_id.in_([d.id for d in dog_by_key.values()] or [0]),
    ))
    current = {m.dog_id: m for m in res.scalars()}
    seen: dict[int, int] = {}

    for r in p.payments:
        # Blank amount: if the dog's amount was set earlier, clear it (the kitchen emptied the
        # cell). Rows that don't resolve to a dog are only reported when they carry an amount.
        blank = r.amount is None
        pn = normalize_phone(r.phone)
        if pn in admin_phones:
            if not blank:
                note(r, "admin_number", "problem")
            continue
        uid = user_by_phone.get(pn) if pn else None
        if uid is None:
            if not blank:
                note(r, "payments_unknown_number", "problem", phone=r.phone)
            continue
        if not blank and r.amount < 0:
            note(r, "payments_negative_amount", "problem", amount=r.amount)
            continue
        if r.dog.strip():
            dog = dog_by_key.get((uid, name_key(r.dog)))
            if dog is None or not dog.active:
                if not blank:
                    note(r, "payments_unknown_dog", "problem")
                continue
        else:
            mine = dogs_of.get(uid, [])
            if len(mine) != 1:
                if not blank:
                    note(r, "payments_dog_required", "problem", dogs=len(mine))
                continue
            dog = mine[0]
        if blank:
            m = current.get(dog.id)
            if m is not None:
                first, last = month_bounds(p.month)
                billed = (await db.execute(select(func.count()).where(
                    Delivery.dog_id == dog.id, Delivery.delivery_date.between(first, last),
                    Delivery.billed_at.is_not(None)))).scalar_one()
                if billed:
                    note(r, "amount_blank_after_billing", "problem", amount=float(m.amount), dog=dog.name)
                else:
                    note(r, "amount_cleared", "info", old=float(m.amount), new=None, dog=dog.name)
                    await db.delete(m)
                    current.pop(dog.id)
                    stats["amounts_cleared"] += 1
            continue
        if dog.id in seen:
            note(r, "payments_duplicate", "problem", first_row=seen[dog.id])
            continue
        seen[dog.id] = r.row

        amount = round(float(r.amount), 2)
        m = current.get(dog.id)
        if m is None:
            db.add(MonthlyAmount(dog_id=dog.id, month=p.month, amount=amount, source_row=r.row))
            stats["amounts_set"] += 1
            note(r, "amount_set", "info", amount=amount, dog=dog.name)
        elif float(m.amount) != amount:
            note(r, "amount_changed", "info", old=float(m.amount), new=amount, dog=dog.name)
            m.amount, m.source_row = amount, r.row
            stats["amounts_changed"] += 1


async def apply_dues(db, p, user_by_phone, issues, stats, admin_phones=frozenset()) -> None:
    """Payments "Previous dues" -> opening_balances, summed per number. Frozen once applied."""
    def note(r, issue, level, **extra):
        issues.append({"tab": "Payments", "row": r.row, "owner": r.owner, "dog": r.dog,
                       "issue": issue, "level": level, **extra})

    sums: dict[int, Decimal] = {}
    rows_of: dict[int, list[int]] = {}
    owner_of: dict[int, str] = {}
    for r in p.payments:
        if r.previous_dues is None:
            continue
        pn = normalize_phone(r.phone)
        if pn in admin_phones:
            continue  # already reported by apply_payments
        uid = user_by_phone.get(pn) if pn else None
        if uid is None:
            if r.amount is None:  # apply_payments didn't report this row
                note(r, "payments_unknown_number", "problem", phone=r.phone)
            continue
        sums[uid] = sums.get(uid, Decimal("0")) + Decimal(str(r.previous_dues))
        rows_of.setdefault(uid, []).append(r.row)
        owner_of.setdefault(uid, r.owner)

    existing = {o.user_id: o for o in (await db.execute(select(OpeningBalance))).scalars()}
    first_row = {uid: rows[0] for uid, rows in rows_of.items()}
    for uid in set(sums) | {u for u, o in existing.items() if o.applied_at is None}:
        dues = sums.get(uid, Decimal("0")).quantize(Decimal("0.01"))
        o = existing.get(uid)
        where = {"tab": "Payments", "row": first_row.get(uid, 0), "owner": owner_of.get(uid, ""),
                 "dog": "previous dues", "level": "info"}
        if o is None:
            if dues == 0:
                continue
            db.add(OpeningBalance(user_id=uid, month=p.month, dues=dues,
                                  source_rows=",".join(map(str, rows_of.get(uid, [])))))
            issues.append({**where, "issue": "dues_set", "amount": float(dues)})
            stats["dues_set"] += 1
        elif o.applied_at is not None:
            if Decimal(o.dues) != dues:
                issues.append({**where, "issue": "dues_ignored_opening_applied", "amount": float(dues)})
        elif Decimal(o.dues) != dues:
            issues.append({**where, "issue": "dues_changed", "old": float(o.dues), "new": float(dues)})
            o.dues, o.month = dues, p.month
            o.source_rows = ",".join(map(str, rows_of.get(uid, []))) or None
            stats["dues_changed"] += 1


async def apply_received(db, received, user_by_phone, issues, stats, admin_phones=frozenset()) -> None:
    """Received tab -> payments_received. Unposted rows follow the sheet; posted rows are kept."""
    def note(r, issue, level, **extra):
        issues.append({"tab": "Received", "row": r.row, "owner": "", "dog": "",
                       "issue": issue, "level": level, **extra})

    today = today_ist()
    wanted: dict[str, tuple[ReceivedIn, int, Date]] = {}
    seen: Counter = Counter()
    for r in received:
        pn = normalize_phone(r.phone)
        if not pn:
            note(r, "bad_number", "problem")
            continue
        if pn in admin_phones:
            note(r, "admin_number", "problem")
            continue
        uid = user_by_phone.get(pn)
        if uid is None:
            note(r, "received_unknown_number", "problem", phone=r.phone)
            continue
        try:
            paid_on = Date.fromisoformat(r.date.strip())
        except ValueError:
            paid_on = None
        if paid_on is None or paid_on > today:
            note(r, "received_bad_date", "problem")
            continue
        if r.amount is None or r.amount <= 0:
            note(r, "received_bad_amount", "problem")
            continue
        amount = Decimal(str(r.amount)).quantize(Decimal("0.01"))
        base = f"{pn}|{paid_on.isoformat()}|{amount}|{' '.join(r.reference.split())}"
        seen[base] += 1  # two identical payments on one day stay two rows
        wanted[f"{base}|{seen[base]}"[:200]] = (r, uid, paid_on)

    have = {x.sheet_key: x for x in (await db.execute(select(PaymentReceived))).scalars()}
    for key, (r, uid, paid_on) in wanted.items():
        if key in have:
            continue
        db.add(PaymentReceived(user_id=uid, paid_on=paid_on, amount=Decimal(str(r.amount)),
                               mode=" ".join(r.mode.split()) or None,
                               reference=" ".join(r.reference.split()) or None,
                               sheet_key=key, source_row=r.row))
        note(r, "received_added", "info", amount=float(r.amount))
        stats["received_added"] += 1
    for key, x in have.items():
        if key in wanted:
            continue
        if x.posted_at is None:
            await db.delete(x)
            stats["received_removed"] += 1
        else:
            issues.append({"tab": "Received", "row": x.source_row or 0, "owner": "", "dog": "",
                           "issue": "received_posted_row_missing", "level": "problem",
                           "amount": float(x.amount), "date": x.paid_on.isoformat()})


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
            "billing": d.billing,
            "billed": d.billed_at is not None,
            "amount": float(d.amount) if d.amount is not None else None,
        }
        for d, dog_name in res.all()
    ]
