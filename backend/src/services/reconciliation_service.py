"""退款核对：晚到回传去重、异常隔离、核对前后口径隔离。

业务口径（README §10.3 与「退款核对」页同步展示）：

1. **幂等**：批次以 ``batch_key`` 为自然键，同一批重试导入直接返回原批次，
   不新增退款记录、不重复冲减；确认动作本身也幂等。
2. **重复回传**：退款笔以 ``refund_no``（缺省时用订单号+金额+日期）为去重键。
   同一笔在不同批次再次出现时记为 ``duplicate``，``applied_amount=0``，不冲减净额。
3. **异常单独列出**：
   - 无法归属：订单号不存在、会员号与订单不一致、金额非法 → ``unattributable``；
   - 累计超过原单：同一订单历史已核对退款 + 本笔 > 订单毛额 → ``over_refund``，
     只把剩余可退部分计入冲减，超出部分记入 ``exception_amount``。
4. **核对门禁**：存在 ``pending`` 批次时，分群重算、名单生成/确认、分群与名单导出
   一律 409 ``REFUND_UNRECONCILED``，未核对结果不能进入正式分群。
5. **确认**：把批次从「待核对」推进到「已核对」，随后按当前快照权重/窗口重算
   R/F/M（M 与增量 GMV 一律使用净额），并刷新仍处于待确认状态的名单口径。
"""
from __future__ import annotations

import csv
import io
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from dateutil import parser as date_parser
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from src.constants.enums import (
    RECONCILIATION_STATUS_LABELS,
    REFUND_STATUS_LABELS,
    ReconciliationStatus,
    RefundStatus,
)
from src.constants.limits import DEFAULT_RFM_WEIGHTS, RFM_WINDOW_DAYS
from src.models.audience import Audience
from src.models.member import Member
from src.models.order import Order
from src.models.reconciliation import ReconciliationRun
from src.models.refund import RefundRecord
from src.models.segment import Segment
from src.services import rfm_service
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_utc, local_now, utcnow

MONEY_TOLERANCE = 0.01
MAX_BATCH_RECORDS = 5000


# ---------------------------------------------------------------- 门禁


def pending_run(db: Session) -> ReconciliationRun | None:
    return db.scalar(
        select(ReconciliationRun)
        .where(ReconciliationRun.status == ReconciliationStatus.PENDING.value)
        .order_by(ReconciliationRun.id.desc())
        .limit(1)
    )


def latest_confirmed_run(db: Session) -> ReconciliationRun | None:
    return db.scalar(
        select(ReconciliationRun)
        .where(ReconciliationRun.status == ReconciliationStatus.CONFIRMED.value)
        .order_by(ReconciliationRun.id.desc())
        .limit(1)
    )


def require_reconciled(db: Session) -> None:
    """未核对批次存在时，正式分群 / 名单 / 导出全部拒绝。"""
    run = pending_run(db)
    if run is not None:
        raise AppError(
            "REFUND_UNRECONCILED",
            409,
            {
                "reason": "存在未核对的退款批次，请先完成退款核对",
                "run_no": run.run_no,
                "batch_key": run.batch_key,
                "status": run.status,
            },
        )


# ---------------------------------------------------------------- 读取辅助


def _basis(db: Session) -> tuple[date, int]:
    snapshot = db.scalar(select(Segment.snapshot).where(Segment.code == "champion"))
    if not snapshot:
        snapshot = db.scalar(select(Segment.snapshot).where(Segment.snapshot.is_not(None)))
    window_days = int((snapshot or {}).get("window_days") or RFM_WINDOW_DAYS)
    return rfm_service.as_of_from_orders(db), window_days


def applied_refund_by_order(db: Session) -> dict[int, float]:
    """只统计「已核对」批次里真正冲减的金额（超额退款只算可退部分）。"""
    rows = db.execute(
        select(RefundRecord.order_id, func.sum(RefundRecord.applied_amount))
        .join(ReconciliationRun, RefundRecord.run_id == ReconciliationRun.id)
        .where(ReconciliationRun.status == ReconciliationStatus.CONFIRMED.value)
        .where(RefundRecord.order_id.is_not(None))
        .where(RefundRecord.applied_amount > 0)
        .group_by(RefundRecord.order_id)
    ).all()
    return {int(order_id): round(float(amount or 0.0), 2) for order_id, amount in rows}


def applied_refund_by_member(db: Session, *, as_of: date, window_days: int) -> dict[int, float]:
    since = as_of - timedelta(days=window_days)
    rows = db.execute(
        select(Order.member_id, func.sum(RefundRecord.applied_amount))
        .join(RefundRecord, RefundRecord.order_id == Order.id)
        .join(ReconciliationRun, RefundRecord.run_id == ReconciliationRun.id)
        .where(ReconciliationRun.status == ReconciliationStatus.CONFIRMED.value)
        .where(RefundRecord.applied_amount > 0)
        .where(Order.paid_at >= datetime.combine(since, time.min))
        .where(Order.paid_at <= datetime.combine(as_of, time.max))
        .group_by(Order.member_id)
    ).all()
    return {int(member_id): round(float(amount or 0.0), 2) for member_id, amount in rows}


def applied_refund_total(db: Session, *, as_of: date, window_days: int) -> float:
    return round(sum(applied_refund_by_member(db, as_of=as_of, window_days=window_days).values()), 2)


# ---------------------------------------------------------------- 导入


def _dedupe_key(row: dict) -> str:
    refund_no = (row.get("refund_no") or "").strip()
    if refund_no:
        return f"NO:{refund_no}"
    stamp = str(row.get("refunded_at") or "")[:10]
    return f"OA:{row['order_no']}:{float(row['amount']):.2f}:{stamp}"


def _parse_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value
    text = str(value or "").strip()
    if not text:
        return datetime.combine(date.today(), datetime.min.time())
    try:
        return date_parser.parse(text).replace(tzinfo=None)
    except (ValueError, OverflowError):
        return datetime.combine(date.today(), datetime.min.time())


def _next_run_no(db: Session) -> str:
    prefix = f"R{local_now():%Y%m%d}"
    count = db.scalar(
        select(func.count()).select_from(ReconciliationRun).where(ReconciliationRun.run_no.like(f"{prefix}%"))
    ) or 0
    return f"{prefix}{count + 1:03d}"


def import_batch(
    db: Session,
    *,
    batch_key: str,
    records: list[dict],
    operator_email: str,
    note: str | None = None,
    seed_key: str | None = None,
) -> tuple[ReconciliationRun, bool]:
    """导入一批晚到退款。返回 (批次, 是否新建)；同批次重试返回原批次，绝不重复冲减。"""
    batch_key = (batch_key or "").strip()
    if not batch_key:
        raise AppError("VALIDATION", 422, {"reason": "batch_key 不能为空"})
    existing = db.scalar(select(ReconciliationRun).where(ReconciliationRun.batch_key == batch_key))
    if existing is not None:
        return existing, False
    if seed_key:
        seeded = db.scalar(select(ReconciliationRun).where(ReconciliationRun.seed_key == seed_key))
        if seeded is not None:
            return seeded, False
    if pending_run(db) is not None:
        raise AppError(
            "CONFLICT",
            409,
            {"reason": "已有待核对的退款批次，请先完成核对再导入下一批", "batch_key": batch_key},
        )
    if not records:
        raise AppError("VALIDATION", 422, {"reason": "退款批次没有记录"})
    if len(records) > MAX_BATCH_RECORDS:
        raise AppError("VALIDATION", 422, {"reason": f"单批次最多 {MAX_BATCH_RECORDS} 条退款记录"})

    prepared = []
    for index, raw in enumerate(records):
        row = dict(raw)
        row["order_no"] = str(row.get("order_no") or "").strip()
        row["refund_no"] = str(row.get("refund_no") or "").strip()
        row["member_no"] = str(row.get("member_no") or "").strip() or None
        row["amount"] = round(float(row.get("amount") or 0.0), 2)
        row["refunded_at"] = _parse_dt(row.get("refunded_at"))
        row["_index"] = index
        row["_dedupe"] = _dedupe_key(row)
        prepared.append(row)
    prepared.sort(key=lambda item: (item["refunded_at"], item["order_no"], item["refund_no"], item["_index"]))

    order_nos = {row["order_no"] for row in prepared if row["order_no"]}
    orders = {
        row.order_no: row for row in db.scalars(select(Order).where(Order.order_no.in_(order_nos))).all()
    }
    member_nos = {member_id: member_no for member_id, member_no in db.execute(select(Member.id, Member.member_no)).all()}
    confirmed_applied = applied_refund_by_order(db)
    confirmed_keys = {
        key
        for (key,) in db.execute(
            select(RefundRecord.dedupe_key)
            .join(ReconciliationRun, RefundRecord.run_id == ReconciliationRun.id)
            .where(ReconciliationRun.status == ReconciliationStatus.CONFIRMED.value)
            .where(RefundRecord.applied_amount > 0)
        ).all()
        if key
    }

    run = ReconciliationRun(
        run_no=_next_run_no(db),
        batch_key=batch_key,
        status=ReconciliationStatus.PENDING.value,
        note=note,
        operator_email=operator_email,
        seed_key=seed_key,
    )
    db.add(run)
    db.flush()

    reserved: dict[int, float] = defaultdict(float)  # 本批次内同一订单已占用的可退金额
    seen_keys: set[str] = set()
    seen_record_keys: set[str] = set()
    touched_orders: dict[int, Order] = {}
    applied_total = 0.0
    exception_amount = 0.0
    counts: dict[str, int] = defaultdict(int)
    exceptions: list[dict] = []

    for row in prepared:
        record_key = f"{batch_key}:{row['refund_no']}" if row["refund_no"] else f"{batch_key}:{row['_dedupe']}"
        if record_key in seen_record_keys:
            continue  # 同批次同退款笔重复行，直接跳过
        seen_record_keys.add(record_key)

        order = orders.get(row["order_no"])
        status = RefundStatus.PENDING.value
        applied = 0.0
        exception = 0.0
        reason = None

        if order is not None:
            touched_orders[order.id] = order

        if row["amount"] <= 0:
            status = RefundStatus.UNATTRIBUTABLE.value
            exception = max(0.0, row["amount"])
            reason = "退款金额必须大于 0"
        elif order is None:
            status = RefundStatus.UNATTRIBUTABLE.value
            exception = row["amount"]
            reason = "订单号不存在，无法归属会员"
        elif row["member_no"] and member_nos.get(order.member_id) != row["member_no"]:
            status = RefundStatus.UNATTRIBUTABLE.value
            exception = row["amount"]
            reason = "会员号与原单不一致"
        elif row["_dedupe"] in confirmed_keys or row["_dedupe"] in seen_keys:
            status = RefundStatus.DUPLICATE.value
            reason = "同一退款笔已回传并冲减过，本次不重复冲减"
        else:
            already = round(confirmed_applied.get(order.id, 0.0) + reserved[order.id], 2)
            remaining = round(float(order.amount) - already, 2)
            if remaining <= 0.005:
                status = RefundStatus.OVER_REFUND.value
                exception = row["amount"]
                reason = "原单已全额退款，累计超过原单"
            elif row["amount"] > remaining + 0.005:
                status = RefundStatus.OVER_REFUND.value
                applied = round(remaining, 2)
                exception = round(row["amount"] - remaining, 2)
                reason = f"累计退款超过原单 ¥{order.amount:.2f}，超出部分列异常"
            else:
                applied = row["amount"]

        if applied > 0:
            reserved[order.id] += applied  # type: ignore[union-attr]
            seen_keys.add(row["_dedupe"])
            applied_total += applied
        exception_amount += exception
        counts[status] += 1
        record = RefundRecord(
            record_key=record_key,
            batch_key=batch_key,
            refund_no=row["refund_no"] or f"{batch_key}-{row['_index'] + 1:03d}",
            order_no=row["order_no"],
            member_no=row["member_no"] or (member_nos.get(order.member_id) if order else None),
            amount=row["amount"],
            refunded_at=row["refunded_at"],
            status=status,
            applied_amount=round(applied, 2),
            exception_amount=round(exception, 2),
            exception_reason=reason,
            order_id=order.id if order else None,
            member_id=order.member_id if order else None,
            run_id=run.id,
            dedupe_key=row["_dedupe"],
            raw={
                "refund_no": row["refund_no"],
                "order_no": row["order_no"],
                "member_no": row["member_no"],
                "amount": row["amount"],
                "refunded_at": row["refunded_at"].isoformat(),
            },
        )
        db.add(record)
        if status in (RefundStatus.UNATTRIBUTABLE.value, RefundStatus.OVER_REFUND.value):
            exceptions.append(
                {
                    "refund_no": record.refund_no,
                    "order_no": record.order_no,
                    "member_no": record.member_no,
                    "amount": record.amount,
                    "applied_amount": record.applied_amount,
                    "exception_amount": record.exception_amount,
                    "status": status,
                    "status_label": REFUND_STATUS_LABELS[status],
                    "reason": reason,
                    "refunded_at": iso_utc(record.refunded_at),
                }
            )

    gross = round(sum(float(order.amount) for order in touched_orders.values()), 2)
    prior = round(sum(confirmed_applied.get(order.id, 0.0) for order in touched_orders.values()), 2)
    run.record_count = len(prepared)
    run.gross_amount = gross
    run.refund_amount = round(applied_total, 2)
    run.net_amount = round(gross - prior - applied_total, 2)
    run.applied_count = sum(
        count for status, count in counts.items() if status in (RefundStatus.PENDING.value, RefundStatus.APPLIED.value)
    )
    run.duplicate_count = counts[RefundStatus.DUPLICATE.value]
    run.unattributable_count = counts[RefundStatus.UNATTRIBUTABLE.value]
    run.over_refund_count = counts[RefundStatus.OVER_REFUND.value]
    run.exception_count = run.unattributable_count + run.over_refund_count
    run.exception_amount = round(exception_amount, 2)
    run.exceptions = exceptions
    run.per_order = {
        order.order_no: {
            "order_no": order.order_no,
            "member_no": member_nos.get(order.member_id),
            "gross": round(float(order.amount), 2),
            "prior_refund": round(confirmed_applied.get(order.id, 0.0), 2),
            "batch_refund": round(reserved.get(order.id, 0.0), 2),
            "net": round(
                float(order.amount) - confirmed_applied.get(order.id, 0.0) - reserved.get(order.id, 0.0), 2
            ),
        }
        for order in touched_orders.values()
    }
    db.flush()
    return run, True


# ---------------------------------------------------------------- 确认


def confirm_run(
    db: Session,
    run: ReconciliationRun,
    *,
    actor: str,
    acknowledge_exceptions: bool = False,
    recompute: bool = True,
) -> ReconciliationRun:
    if run.status == ReconciliationStatus.CONFIRMED.value:
        return run
    if run.exception_count and not acknowledge_exceptions:
        raise AppError(
            "VALIDATION",
            422,
            {
                "reason": "存在无法归属或累计超过原单的异常记录，确认前需要逐条知晓",
                "exception_count": run.exception_count,
                "exception_amount": run.exception_amount,
            },
        )
    run.status = ReconciliationStatus.CONFIRMED.value
    run.confirmed_by = actor
    run.confirmed_at = utcnow()
    run.acknowledged_exceptions = bool(acknowledge_exceptions)
    db.flush()
    # 待核对期间为「计划冲减」，确认后落为「已冲减」；异常与重复行状态保持不变
    db.execute(
        update(RefundRecord)
        .where(RefundRecord.run_id == run.id)
        .where(RefundRecord.status == RefundStatus.PENDING.value)
        .where(RefundRecord.applied_amount > 0)
        .values(status=RefundStatus.APPLIED.value)
    )
    db.flush()

    if recompute:
        from src.services import audience_service, segment_service

        snapshot = db.scalar(select(Segment.snapshot).where(Segment.code == "champion")) or {}
        weights = tuple(snapshot.get("weights") or DEFAULT_RFM_WEIGHTS)
        window_days = int(snapshot.get("window_days") or RFM_WINDOW_DAYS)
        try:
            segment_service.recompute(db, weights=weights, window_days=window_days)
        except AppError:
            # 会员表为空等场景不阻塞核对落账，分群留待管理员手动重算
            pass
        audience_service.refresh_pending_audiences(db)
    db.flush()
    return run


# ---------------------------------------------------------------- 序列化


def serialize_run(run: ReconciliationRun) -> dict:
    return {
        "id": run.id,
        "run_no": run.run_no,
        "batch_key": run.batch_key,
        "status": run.status,
        "status_label": RECONCILIATION_STATUS_LABELS.get(run.status, run.status),
        "record_count": run.record_count,
        "gross_amount": round(run.gross_amount or 0.0, 2),
        "refund_amount": round(run.refund_amount or 0.0, 2),
        "net_amount": round(run.net_amount or 0.0, 2),
        "applied_count": run.applied_count,
        "duplicate_count": run.duplicate_count,
        "unattributable_count": run.unattributable_count,
        "over_refund_count": run.over_refund_count,
        "exception_count": run.exception_count,
        "exception_amount": round(run.exception_amount or 0.0, 2),
        "acknowledged_exceptions": bool(run.acknowledged_exceptions),
        "note": run.note,
        "operator_email": run.operator_email,
        "confirmed_by": run.confirmed_by,
        "confirmed_at": iso_utc(run.confirmed_at),
        "created_at": iso_utc(run.created_at),
    }


def serialize_record(record: RefundRecord) -> dict:
    return {
        "id": record.id,
        "refund_no": record.refund_no,
        "order_no": record.order_no,
        "member_no": record.member_no,
        "amount": round(record.amount or 0.0, 2),
        "applied_amount": round(record.applied_amount or 0.0, 2),
        "exception_amount": round(record.exception_amount or 0.0, 2),
        "status": record.status,
        "status_label": REFUND_STATUS_LABELS.get(record.status, record.status),
        "exception_reason": record.exception_reason,
        "refunded_at": iso_utc(record.refunded_at),
    }


def list_runs(db: Session, *, limit: int = 20) -> dict:
    rows = db.scalars(select(ReconciliationRun).order_by(ReconciliationRun.id.desc()).limit(limit)).all()
    return {"items": [serialize_run(row) for row in rows], "total": len(rows)}


def get_run(db: Session, run_id: int) -> ReconciliationRun:
    run = db.get(ReconciliationRun, run_id)
    if run is None:
        raise not_found("reconciliation_run", run_id)
    return run


def run_detail(db: Session, run_id: int) -> dict:
    run = get_run(db, run_id)
    records = db.scalars(
        select(RefundRecord).where(RefundRecord.run_id == run.id).order_by(RefundRecord.id)
    ).all()
    payload = serialize_run(run)
    payload["records"] = [serialize_record(row) for row in records]
    payload["duplicates"] = [item for item in payload["records"] if item["status"] == RefundStatus.DUPLICATE.value]
    payload["exceptions"] = [
        item
        for item in payload["records"]
        if item["status"] in (RefundStatus.UNATTRIBUTABLE.value, RefundStatus.OVER_REFUND.value)
    ]
    payload["per_order"] = run.per_order or {}
    return payload


def summary(db: Session) -> dict:
    as_of, window_days = _basis(db)
    since = as_of - timedelta(days=window_days)
    gross = db.scalar(
        select(func.sum(Order.amount))
        .where(Order.paid_at >= datetime.combine(since, time.min))
        .where(Order.paid_at <= datetime.combine(as_of, time.max))
    ) or 0.0
    total_gross = round(float(gross), 2)
    refund_total = applied_refund_total(db, as_of=as_of, window_days=window_days)
    pending = pending_run(db)
    latest = db.scalar(select(ReconciliationRun).order_by(ReconciliationRun.id.desc()).limit(1))
    return {
        "status": "pending" if pending else "confirmed",
        "status_label": "待核对" if pending else "已核对",
        "pending_run": serialize_run(pending) if pending else None,
        "latest_run": serialize_run(latest) if latest else None,
        "as_of": as_of.isoformat(),
        "window_days": window_days,
        "dataset_gross": total_gross,
        "dataset_refund": refund_total,
        "dataset_net": round(total_gross - refund_total, 2),
        "can_enter_formal": pending is None,
    }


# ---------------------------------------------------------------- 逐项核对清单


def _item(key: str, label: str, values: list[dict], *, digits: int = 2, note: str = "") -> dict:
    normalized = [round(float(item["value"]), digits) for item in values]
    delta = round(max(normalized) - min(normalized), digits) if normalized else 0
    tolerance = MONEY_TOLERANCE if digits else 0
    return {
        "key": key,
        "label": label,
        "values": [
            {**item, "value": round(float(item["value"]), digits)}
            for item in values
        ],
        "ok": bool(delta <= tolerance),
        "delta": delta,
        "note": note,
    }


def checklist(db: Session) -> dict:
    pending = pending_run(db)
    as_of, window_days = _basis(db)
    since = as_of - timedelta(days=window_days)
    orders = db.scalars(
        select(Order)
        .where(Order.paid_at >= datetime.combine(since, time.min))
        .where(Order.paid_at <= datetime.combine(as_of, time.max))
    ).all()
    order_gross = round(sum(float(row.amount) for row in orders), 2)
    order_ids = {row.id for row in orders}
    refund_by_order = applied_refund_by_order(db)
    order_refund = round(sum(amount for order_id, amount in refund_by_order.items() if order_id in order_ids), 2)

    members = db.scalars(select(Member)).all()
    member_gross = round(sum(float(row.gross_window or 0.0) for row in members), 2)
    member_refund = round(sum(float(row.refund_window or 0.0) for row in members), 2)
    member_net = round(sum(float(row.amount_window or 0.0) for row in members), 2)
    member_count = len(members)

    segments = db.scalars(select(Segment).order_by(Segment.sort_order)).all()
    seg_gross = round(sum(float(row.gross_amount or 0.0) for row in segments), 2)
    seg_refund = round(sum(float(row.refund_amount or 0.0) for row in segments), 2)
    seg_net = round(sum(float(row.net_amount or 0.0) for row in segments), 2)
    seg_count = sum(int(row.member_count or 0) for row in segments)

    audiences = db.scalars(select(Audience).order_by(Audience.id)).all()
    aud_members = sum(int(row.member_count or 0) for row in audiences)
    aud_allocated = sum(
        int(row.get("allocated") or 0)
        for audience in audiences
        for row in (audience.allocation or [])
        if row.get("status") == "allocated"
    )
    aud_inc_gmv = round(sum(float(row.inc_gmv or 0.0) for row in audiences), 2)
    aud_gross = round(sum(float(row.gross_amount or 0.0) for row in audiences), 2)
    aud_refund = round(sum(float(row.refund_amount or 0.0) for row in audiences), 2)
    aud_net = round(sum(float(row.net_amount or 0.0) for row in audiences), 2)
    allocation_inc_gmv = round(
        sum(
            float(row.get("inc_gmv") or 0.0)
            for audience in audiences
            for row in (audience.allocation or [])
            if row.get("status") == "allocated"
        ),
        2,
    )

    selected_ids = [
        int(member_id)
        for audience in audiences
        for row in (audience.allocation or [])
        if row.get("status") == "allocated"
        for member_id in (row.get("member_ids") or [])
    ]
    selected_members = (
        {row.id: row for row in db.scalars(select(Member).where(Member.id.in_(set(selected_ids)))).all()}
        if selected_ids
        else {}
    )
    selected_gross = round(sum(float(selected_members[i].gross_window or 0.0) for i in selected_ids if i in selected_members), 2)
    selected_refund = round(sum(float(selected_members[i].refund_window or 0.0) for i in selected_ids if i in selected_members), 2)
    selected_net = round(sum(float(selected_members[i].amount_window or 0.0) for i in selected_ids if i in selected_members), 2)

    # 导出层：真实生成 CSV 后按行校验，保证「页面 / 分群 / 导出」同源
    from src.services import export_service

    export_info = {"rows": 0, "gross": 0.0, "refund": 0.0, "net": 0.0, "error": None}
    if pending is None:
        try:
            raw = export_service.audience_csv(db)
            reader = csv.reader(io.StringIO(raw.decode("utf-8-sig")))
            header = next(reader, [])
            index = {name: position for position, name in enumerate(header)}
            rows = [row for row in reader if row]
            export_info["rows"] = len(rows)

            def _sum(column: str) -> float:
                position = index.get(column)
                if position is None:
                    return 0.0
                return round(sum(float(row[position] or 0) for row in rows), 2)

            export_info["gross"] = _sum("订单毛额")
            export_info["refund"] = _sum("订单退款额")
            export_info["net"] = _sum("订单净额")
        except Exception as exc:  # noqa: BLE001 - 导出校验失败要在清单里显式暴露
            export_info["error"] = str(exc)

    export_rows = export_info["rows"]
    export_gated = pending is not None
    export_note = "待核对期间导出已冻结，核对完成后自动校验" if export_gated else "导出与页面/分群同源"

    def _sources(*pairs: tuple[str, float]) -> list[dict]:
        return [{"source": name, "value": value} for name, value in pairs]

    def _sources_or_gated(*pairs: tuple[str, float], gated: tuple[str, float]) -> list[dict]:
        if export_gated:
            return _sources(*pairs)
        return _sources(*pairs, gated)

    items = [
        _item(
            "gross",
            "订单毛额",
            _sources(
                ("订单事实", order_gross),
                ("会员窗口汇总", member_gross),
                ("分群快照", seg_gross),
            ),
            note=f"窗口 {window_days} 天，基准日 {as_of.isoformat()}",
        ),
        _item(
            "refund",
            "退款额（已核对）",
            _sources(
                ("退款流水", order_refund),
                ("会员窗口汇总", member_refund),
                ("分群快照", seg_refund),
            ),
            note="只统计「已核对」批次，重复回传不冲减",
        ),
        _item(
            "net",
            "净额（毛额 - 退款）",
            _sources(
                ("订单事实", round(order_gross - order_refund, 2)),
                ("会员窗口汇总", member_net),
                ("分群快照", seg_net),
            ),
            note="M 分与增量 GMV 一律使用净额",
        ),
        _item(
            "members",
            "分群人数",
            _sources(("会员表", member_count), ("八类分群", seg_count)),
            digits=0,
        ),
        _item(
            "audience_members",
            "名单人数",
            _sources_or_gated(
                ("名单登记人数", aud_members),
                ("分群配额合计", aud_allocated),
                gated=("导出明细行数", export_rows),
            ),
            digits=0,
            note=export_note if export_gated else "导出按会员级逐行展开，一行一位会员配额",
        ),
        _item(
            "audience_gross",
            "名单订单毛额",
            _sources_or_gated(
                ("名单登记毛额", aud_gross),
                ("名单覆盖会员", selected_gross),
                gated=("导出逐行毛额", export_info["gross"]),
            ),
            note=export_note if export_gated else "名单按覆盖会员的窗口毛额登记",
        ),
        _item(
            "audience_refund",
            "名单退款额",
            _sources_or_gated(
                ("名单登记退款额", aud_refund),
                ("名单覆盖会员", selected_refund),
                gated=("导出逐行退款额", export_info["refund"]),
            ),
            note=export_note if export_gated else "",
        ),
        _item(
            "audience_net",
            "名单净额",
            _sources_or_gated(
                ("名单登记净额", aud_net),
                ("名单覆盖会员", selected_net),
                gated=("导出逐行净额", export_info["net"]),
            ),
            note=export_note if export_gated else "",
        ),
        _item(
            "projected_gmv",
            "活动预计 GMV",
            _sources(("名单预计 GMV", aud_inc_gmv), ("分群分配明细合计", allocation_inc_gmv)),
            note="按净额客单价推导，页面 / 分群 / 导出一致",
        ),
    ]
    if export_info["error"]:
        for item in items:
            if item["key"].startswith("audience_"):
                item["ok"] = False
                item["note"] = f"导出校验失败：{export_info['error']}"
    return {
        "ready": pending is None,
        "status": "confirmed" if pending is None else "pending",
        "status_label": "已核对" if pending is None else "待核对",
        "as_of": as_of.isoformat(),
        "window_days": window_days,
        "items": items,
        "all_ok": all(item["ok"] for item in items),
        "export_gated": export_gated,
        "export_rows": export_rows,
        "generated_at": iso_utc(utcnow()),
    }
