"""幂等 seed：3 账号 / 8,000 会员 / 30,000 订单 / 8 分群 / 2 活动 / 2 份名单 / 2 批退款。

自然键 upsert：
  users.email / members.member_no / orders.order_no
  segments.code（只更新快照字段，不动运营改过的券面额）
  campaigns.code / audiences.seed_key
  reconciliation_runs.batch_key / refund_records.record_key
  audit_logs.seed_key

重复执行不新增、不覆盖已确认名单；SEED_SUMMARY 两次逐字一致。
退款批次：wk45 已核对（历史冲减），wk46 晚到待核对（含重复 / 无法归属 / 累计超额），
seed 完成后页面停在「退款核对 · 待核对」，正式分群与导出被冻结。
数量口径（与 batch-plan 的 seed 段对应）：
  sources = 会员数、runs = 订单数、findings = 分群数、pending = 待确认名单数。
"""
from __future__ import annotations

import random
from datetime import datetime, time, timedelta

from sqlalchemy import func, insert, select

from src.config.database import session_scope
from src.models.audit import AuditLog
from src.models.campaign import Campaign
from src.models.member import Member
from src.models.order import Order
from src.models.refund import RefundRecord
from src.models.segment import Segment
from src.models.user import User
from src.services import audience_service, segment_service
from src.constants.limits import RFM_WINDOW_DAYS
from src.utils.security import hash_password

from scripts.demo_config import (
    ARCHETYPES,
    CAMPAIGNS,
    CHANNELS,
    CITIES,
    DEMO_PASSWORD,
    DEMO_USERS,
    GIVEN,
    MEMBER_TARGET,
    ORDER_TARGET,
    ORG,
    RANDOM_SEED,
    REFUND_CONFIRMED_BATCH,
    REFUND_PENDING_BATCH,
    SEED_ANCHOR,
    SEED_AUDIENCES,
    SEGMENT_COUPON,
    STORES,
    SURNAMES,
)

REFUND_BASE_TIME = datetime.combine(SEED_ANCHOR, time(10, 0))


def _pick_weighted(rng: random.Random, items: list[tuple[str, float]]) -> str:
    point = rng.random()
    cumulative = 0.0
    for key, weight in items:
        cumulative += weight
        if point <= cumulative:
            return key
    return items[-1][0]


def _build_dataset() -> tuple[list[dict], list[dict], dict[str, dict]]:
    """生成会员与订单（纯内存、纯确定性），返回 (member_rows, order_rows, stats)。"""
    rng = random.Random(RANDOM_SEED)
    archetype_by_key = {item["key"]: item for item in ARCHETYPES}
    weights = [(item["key"], item["weight"]) for item in ARCHETYPES]

    plans: list[dict] = []
    for index in range(MEMBER_TARGET):
        key = _pick_weighted(rng, weights)
        profile = archetype_by_key[key]
        base = profile["orders"]
        count = int(round(base + rng.uniform(-1.2, 1.2)))
        if key == "hibernating":
            count = max(0, count)
        else:
            count = max(1, count)
        plans.append({"index": index, "archetype": key, "profile": profile, "orders": count})

    total = sum(item["orders"] for item in plans)
    # 调整到精确 30,000 单：先削多、再补少（顺序由固定 rng 决定，可重放）
    rng.shuffle(plans)
    while total > ORDER_TARGET:
        for item in plans:
            if total <= ORDER_TARGET:
                break
            if item["orders"] > 0:
                item["orders"] -= 1
                total -= 1
    if total < ORDER_TARGET:
        candidates = [item for item in plans if item["archetype"] != "new"]
        cursor = 0
        while total < ORDER_TARGET and candidates:
            item = candidates[cursor % len(candidates)]
            item["orders"] += 1
            total += 1
            cursor += 1
    plans.sort(key=lambda item: item["index"])

    member_rows: list[dict] = []
    order_rows: list[dict] = []
    order_seq = 0
    for plan in plans:
        index = plan["index"]
        profile = plan["profile"]
        member_no = f"MY{index + 1:05d}"
        low, high = profile["recent"]
        joined_on = SEED_ANCHOR - timedelta(days=rng.randint(420, 1500))
        paid_days: list[int] = []
        for _ in range(plan["orders"]):
            span = rng.randint(low, high)
            paid_days.append(max(0, min(520, span + rng.randint(-18, 18))))
        paid_days.sort(reverse=True)
        amounts: list[float] = []
        last_paid: datetime | None = None
        for order_index, days in enumerate(paid_days):
            order_seq += 1
            items = rng.choice([1, 1, 1, 2, 2, 3])
            ticket = profile["ticket"] * rng.uniform(0.78, 1.32)
            amount = round(ticket * (1 + (items - 1) * 0.42), 2)
            amounts.append(amount)
            paid_at = datetime.combine(SEED_ANCHOR - timedelta(days=days), time(hour=rng.randint(8, 21), minute=rng.randint(0, 59)))
            if last_paid is None or paid_at > last_paid:
                last_paid = paid_at
            channel = _pick_weighted(rng, CHANNELS)
            order_rows.append(
                {
                    "order_no": f"MYO{order_seq:06d}",
                    "member_no": member_no,
                    "store_code": rng.choice(STORES),
                    "channel": channel,
                    "amount": amount,
                    "items": items,
                    "coupon_used": rng.random() < profile["rate"],
                    "paid_at": paid_at,
                    "created_at": paid_at,
                }
            )
        in_window = [amount for days, amount in zip(paid_days, amounts) if days <= RFM_WINDOW_DAYS]
        orders_window = len(in_window)
        amount_window = round(sum(in_window), 2)
        avg_ticket = round(sum(amounts) / len(amounts), 2) if amounts else 0.0
        rate = round(profile["rate"] * rng.uniform(0.82, 1.18), 6)
        tier = profile["tier"]
        if tier == "silver" and rng.random() < 0.18:
            tier = "gold"
        elif tier == "gold" and rng.random() < 0.12:
            tier = "black"
        member_rows.append(
            {
                "member_no": member_no,
                "name": rng.choice(SURNAMES) + rng.choice(GIVEN),
                "phone": "1" + rng.choice("35789") + "".join(str(rng.randint(0, 9)) for _ in range(9)),
                "city": rng.choice(CITIES),
                "tier": tier,
                "joined_on": joined_on,
                "last_order_at": last_paid,
                "orders_window": orders_window,
                "amount_window": amount_window,
                "purchase_rate": rate,
                "avg_ticket": avg_ticket,
            }
        )
    return member_rows, order_rows, {"members": len(member_rows), "orders": len(order_rows)}


def ensure_users(db) -> int:
    for email, display_name, role in DEMO_USERS:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            db.add(
                User(
                    email=email,
                    password_hash=hash_password(DEMO_PASSWORD),
                    display_name=display_name,
                    role=role,
                    org=ORG,
                )
            )
    db.flush()
    return db.scalar(select(func.count()).select_from(User)) or 0


def ensure_members_and_orders(db) -> tuple[int, int]:
    members_total = db.scalar(select(func.count()).select_from(Member)) or 0
    orders_total = db.scalar(select(func.count()).select_from(Order)) or 0
    if members_total and orders_total:
        return members_total, orders_total

    member_rows, order_rows, _stats = _build_dataset()
    exist_members = {row for (row,) in db.execute(select(Member.member_no)).all()}
    new_members = [row for row in member_rows if row["member_no"] not in exist_members]
    for offset in range(0, len(new_members), 1000):
        db.execute(insert(Member), new_members[offset : offset + 1000])
    db.flush()

    id_by_no = {no: pk for no, pk in db.execute(select(Member.member_no, Member.id)).all()}
    exist_orders = {row for (row,) in db.execute(select(Order.order_no)).all()}
    payload = []
    for row in order_rows:
        if row["order_no"] in exist_orders:
            continue
        payload.append(
            {
                "order_no": row["order_no"],
                "member_id": id_by_no[row["member_no"]],
                "store_code": row["store_code"],
                "channel": row["channel"],
                "amount": row["amount"],
                "items": row["items"],
                "coupon_used": row["coupon_used"],
                "paid_at": row["paid_at"],
                "created_at": row["created_at"],
            }
        )
    for offset in range(0, len(payload), 2000):
        db.execute(insert(Order), payload[offset : offset + 2000])
    db.flush()
    members_total = db.scalar(select(func.count()).select_from(Member)) or 0
    orders_total = db.scalar(select(func.count()).select_from(Order)) or 0
    return members_total, orders_total


def ensure_segments(db) -> int:
    if db.scalar(select(func.count()).select_from(Segment)) == 0:
        segment_service.recompute(db)
        for code, config in SEGMENT_COUPON.items():
            segment = db.scalar(select(Segment).where(Segment.code == code))
            if segment is not None:
                segment.coupon_face = config["coupon_face"]
                segment.core_share = config["core_share"]
        db.flush()
    return db.scalar(select(func.count()).select_from(Segment)) or 0


def ensure_campaigns(db) -> int:
    for spec in CAMPAIGNS:
        exists = db.scalar(select(Campaign).where(Campaign.code == spec["code"]))
        if exists is not None:
            continue
        audience_service.create_campaign(
            db,
            {
                "code": spec["code"],
                "name": spec["name"],
                "channel": spec["channel"],
                "budget": spec["budget"],
                "status": spec["status"],
                "starts_on": spec["starts_on"],
                "ends_on": spec["ends_on"],
                "note": spec["note"],
            },
            operator_email="seed@rfmseg.io",
            seed_key=spec["seed_key"],
        )
        db.flush()
    return db.scalar(select(func.count()).select_from(Campaign)) or 0


def ensure_audiences(db) -> int:
    from src.models.audience import Audience

    campaigns = {item.code: item for item in db.scalars(select(Campaign)).all()}
    for seed_key, campaign_code, note in SEED_AUDIENCES:
        campaign = campaigns.get(campaign_code)
        if campaign is None:
            continue
        audience_service.generate(
            db,
            campaign=campaign,
            budget=None,
            operator_email="seed@rfmseg.io",
            note=note,
            seed_key=seed_key,
        )
    db.flush()
    return db.scalar(select(func.count()).select_from(Audience)) or 0


def _refund_rows(orders: list[Order], spec: dict) -> list[dict]:
    """按固定顺序抽订单造退款行；同一批再叠加「重复回传」与「无法归属」两类脏数据。"""
    rows: list[dict] = []
    ordered = sorted(orders, key=lambda item: item.order_no)
    if not ordered:
        return rows
    picked = ordered[spec["order_offset"] : spec["order_offset"] + spec["count"]]
    low, high = spec["days_ago"]
    for index, order in enumerate(picked):
        span = low + (index % max(1, high - low + 1))
        rows.append(
            {
                "order_no": order.order_no,
                "refund_no": f"RF-{spec['batch_key'][-6:]}-{index + 1:03d}",
                "member_no": None,
                "amount": round(float(order.amount) * spec["ratio"], 2),
                "refunded_at": (REFUND_BASE_TIME - timedelta(days=span)).isoformat(sep=" "),
            }
        )
    return rows


def ensure_reconciliations(db) -> tuple[int, int]:
    """两批退款回传：wk45 已核对（历史冲减），wk46 晚到待核对（含重复 / 异常）。"""
    from src.models.audience import Audience
    from src.models.reconciliation import ReconciliationRun
    from src.services import reconciliation_service

    if db.scalar(select(func.count()).select_from(ReconciliationRun)) > 0:
        return 0, 0

    orders = db.scalars(select(Order).order_by(Order.order_no)).all()
    applied = 0

    # 1) 已核对批次：真实冲减历史退款（确认时会重算分群并刷新待确认名单）
    confirmed_rows = _refund_rows(orders, REFUND_CONFIRMED_BATCH)
    run, _created = reconciliation_service.import_batch(
        db,
        batch_key=REFUND_CONFIRMED_BATCH["batch_key"],
        records=confirmed_rows,
        operator_email="seed@rfmseg.io",
        note=REFUND_CONFIRMED_BATCH["note"],
    )
    reconciliation_service.confirm_run(db, run, actor="seed@rfmseg.io", recompute=True)
    applied = run.record_count

    # 2) 待核对批次：在真实退款基础上叠加重试重复与无法归属，页面停在「待核对」
    pending_rows = _refund_rows(orders, REFUND_PENDING_BATCH)
    duplicate_source = db.scalars(
        select(RefundRecord).where(RefundRecord.batch_key == REFUND_CONFIRMED_BATCH["batch_key"]).limit(2)
    ).all()
    for record in duplicate_source:
        pending_rows.append(
            {
                "order_no": record.order_no,
                "refund_no": record.refund_no,  # 同退款笔再次回传 → duplicate
                "amount": record.amount,
                "refunded_at": (REFUND_BASE_TIME - timedelta(days=3)).isoformat(sep=" "),
            }
        )
    pending_rows.append(
        {
            "order_no": REFUND_PENDING_BATCH["unknown_order_no"],  # 订单号不存在 → unattributable
            "refund_no": "RF-WK46-UNKNOWN-1",
            "amount": 26.0,
            "refunded_at": (REFUND_BASE_TIME - timedelta(days=2)).isoformat(sep=" "),
        }
    )
    # 累计超过原单：对已核销订单再退一笔远超剩余可退金额
    over_order = next(
        (order for order in sorted(orders, key=lambda item: item.order_no) if order.order_no not in {row["order_no"] for row in pending_rows}),
        None,
    )
    if over_order is not None:
        pending_rows.append(
            {
                "order_no": over_order.order_no,
                "refund_no": "RF-WK46-OVER-1",
                "amount": round(float(over_order.amount) + 40.0, 2),  # 超出原单 → over_refund
                "refunded_at": (REFUND_BASE_TIME - timedelta(days=1)).isoformat(sep=" "),
            }
        )
    reconciliation_service.import_batch(
        db,
        batch_key=REFUND_PENDING_BATCH["batch_key"],
        records=pending_rows,
        operator_email="seed@rfmseg.io",
        note=REFUND_PENDING_BATCH["note"],
    )
    db.flush()
    pending = db.scalar(select(func.count()).select_from(Audience).where(Audience.status == "pending")) or 0
    return applied, pending


def ensure_audit(db) -> None:
    seeds = [
        ("seed-audit-1", "admin@rfmseg.io", "admin", "segment.recompute", "segment", {"weights": [0.3, 0.3, 0.4]}),
        ("seed-audit-2", "analyst@rfmseg.io", "analyst", "export.members", "member", {"rows": 8000}),
    ]
    for seed_key, email, role, action, entity, detail in seeds:
        exists = db.scalar(select(AuditLog).where(AuditLog.seed_key == seed_key))
        if exists is None:
            db.add(
                AuditLog(
                    actor_email=email,
                    actor_role=role,
                    action=action,
                    entity=entity,
                    detail=detail,
                    layer="seed",
                    seed_key=seed_key,
                )
            )
    db.flush()


def seed_all() -> dict:
    from src.models.audience import Audience

    with session_scope() as db:
        users = ensure_users(db)
        members, orders = ensure_members_and_orders(db)
        segments = ensure_segments(db)
        ensure_campaigns(db)
        ensure_audiences(db)
        ensure_reconciliations(db)
        ensure_audit(db)
        audiences = db.scalar(select(func.count()).select_from(Audience)) or 0
        pending = db.scalar(select(func.count()).select_from(Audience).where(Audience.status == "pending")) or 0
        summary = (
            f"SEED_SUMMARY brand=rfmseg users={users} sources={members} "
            f"runs={orders} findings={segments} pending={pending}"
        )
        print(summary, flush=True)
        return {
            "users": users,
            "members": members,
            "orders": orders,
            "segments": segments,
            "audiences": audiences,
            "pending": pending,
        }


if __name__ == "__main__":
    seed_all()
