"""v2 opening balances: the day a customer's money moves into v2.

POST /v2/admin/opening   Admin. dry_run (default true) shows exactly what would happen,
                         then rolls everything back. dry_run false writes it.

For each customer (or the user_ids given), in one transaction:
1. Opening balance (only once per customer). The balance becomes -dues, where dues is the
   Payments tab's "Previous dues" for that number (0 if blank). One ledger entry, dated the
   last second before this month, moves the old v1 balance to that figure; the old balance
   is kept on opening_balances.old_balance. After this the sheet's dues are ignored.
2. Payments received (Received tab) not yet posted -> ledger credits, dated the day paid.
   Only payments from this month on: older ones belong inside "Previous dues".
3. This month's unbilled meal/pause days before today are billed, oldest first, with the
   normal rules (app/v2_billing.py). Today and later are left for nightly billing.
Safe to run again: done steps are skipped, so a re-run after fixing a missing monthly
amount just bills the remaining days and posts new payments.
"""
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ledger import Ledger
from app.models.user import User
from app.models.v2 import Delivery, Dog, OpeningBalance, PaymentReceived
from app.v2_billing import IST, bill_delivery, month_of, today_ist

ZERO = Decimal("0.00")


def money(x) -> float:
    return float(Decimal(x).quantize(Decimal("0.01")))


async def run_opening(db: AsyncSession, admin_id: int, user_ids: Optional[list[int]] = None) -> dict:
    """Does the work in the caller's transaction. The caller commits or rolls back."""
    today = today_ist()
    month, first, _ = month_of(today)
    label = first.strftime("%B")
    opening_at = datetime.combine(first, time(0, 0), IST) - timedelta(seconds=1)
    now = datetime.now(timezone.utc)

    # Customers: anyone with an active dog in this month's sheet, or with dues entered.
    with_deliveries = select(Dog.user_id).join(Delivery, Delivery.dog_id == Dog.id).where(
        Dog.active.is_(True), Delivery.delivery_date >= first)
    q = select(User).where(User.is_admin.is_(False), (
        User.id.in_(with_deliveries) | User.id.in_(select(OpeningBalance.user_id))))
    if user_ids:
        q = q.where(User.id.in_(user_ids))
    users = (await db.execute(q.order_by(User.name).with_for_update())).scalars().all()

    openings = {o.user_id: o for o in (await db.execute(
        select(OpeningBalance).where(OpeningBalance.user_id.in_([u.id for u in users] or [0])))).scalars()}

    out = []
    for u in users:
        row: dict = {"user_id": u.id, "name": u.name, "wa_phone": u.wa_phone}

        # 1. opening balance
        o = openings.get(u.id)
        if o is not None and o.applied_at is not None:
            row["opening"] = {"already_applied": True, "dues": money(o.dues),
                              "applied_at": o.applied_at.isoformat()}
        else:
            if o is None:
                o = OpeningBalance(user_id=u.id, month=month, dues=ZERO)
                db.add(o)
            dues = Decimal(o.dues)
            old = Decimal(u.current_balance)
            target = -dues
            change = target - old
            entry = None
            if change != 0:
                what = (f"Dues from before {label}" if dues > 0
                        else f"Advance from before {label}" if dues < 0
                        else f"Balance at the start of {label}")
                entry = Ledger(user_id=u.id, type="credit" if change > 0 else "debit", amount=abs(change),
                               mode_of_payment="opening" if change > 0 else None, status="completed",
                               description=what, created_at=opening_at)
                db.add(entry)
                await db.flush()
            u.current_balance = target
            o.month, o.old_balance, o.applied_at, o.applied_by = month, old, now, admin_id
            o.ledger_id = entry.id if entry else None
            row["opening"] = {"already_applied": False, "dues": money(dues), "old_balance": money(old),
                              "opening_balance": money(target), "ledger_change": money(change)}

        # 2. payments received
        received, problems = [], []
        for pr in (await db.execute(select(PaymentReceived).where(
                PaymentReceived.user_id == u.id, PaymentReceived.posted_at.is_(None))
                .order_by(PaymentReceived.paid_on, PaymentReceived.id))).scalars():
            if pr.paid_on < first:
                problems.append({"what": "payment_before_this_month", "date": pr.paid_on.isoformat(),
                                 "amount": money(pr.amount),
                                 "fix": "count it inside Previous dues and remove it from Received"})
                continue
            entry = Ledger(user_id=u.id, type="credit", amount=Decimal(pr.amount), status="completed",
                           mode_of_payment=pr.mode, transaction_id=pr.reference,
                           description="Payment received",
                           created_at=datetime.combine(pr.paid_on, time(12, 0), IST))
            db.add(entry)
            await db.flush()
            u.current_balance = Decimal(u.current_balance) + Decimal(pr.amount)
            pr.ledger_id, pr.posted_at = entry.id, now
            received.append({"date": pr.paid_on.isoformat(), "amount": money(pr.amount), "mode": pr.mode})
        row["received"] = received

        # 3. this month's days before today
        rows = (await db.execute(
            select(Delivery, Dog).join(Dog, Dog.id == Delivery.dog_id)
            .where(Dog.user_id == u.id, Dog.active.is_(True), Delivery.billed_at.is_(None),
                   Delivery.delivery_date >= first, Delivery.delivery_date < today,
                   Delivery.billing.in_(("charge", "credit", "hold")))
            .order_by(Delivery.delivery_date, Delivery.id).with_for_update(of=Delivery)
        )).all()
        charged, credited, n_charged, n_credited = ZERO, ZERO, 0, 0
        for d, dog in rows:
            if d.billing == "hold":
                problems.append({"what": "sheet_error", "dog": dog.name, "date": d.delivery_date.isoformat(),
                                 "value": d.raw_value})
                continue
            r = await bill_delivery(db, d, dog, now)
            if r["result"] == "charged":
                charged += Decimal(str(r["amount"]))
                n_charged += 1
            elif r["result"] == "credited":
                credited += Decimal(str(r["amount"]))
                n_credited += 1
            else:
                problems.append({"what": r["result"], "dog": dog.name, "date": d.delivery_date.isoformat()})
        row["past_days"] = {"meals": n_charged, "charged": money(charged),
                            "paused": n_credited, "kept_as_credit": money(credited)}
        row["problems"] = problems
        row["balance_after"] = money(u.current_balance)
        out.append(row)

    await db.flush()
    return {
        "month": month,
        "opening_dated": opening_at.isoformat(),
        "billed_up_to": (today - timedelta(days=1)).isoformat(),
        "customers": out,
        "totals": {
            "customers": len(out),
            "openings_applied": sum(1 for r in out if not r["opening"]["already_applied"]),
            "payments_posted": sum(len(r["received"]) for r in out),
            "charged": money(sum((Decimal(str(r["past_days"]["charged"])) for r in out), ZERO)),
            "balance_after": money(sum((Decimal(str(r["balance_after"])) for r in out), ZERO)),
            "customers_with_problems": sum(1 for r in out if r["problems"]),
        },
    }
