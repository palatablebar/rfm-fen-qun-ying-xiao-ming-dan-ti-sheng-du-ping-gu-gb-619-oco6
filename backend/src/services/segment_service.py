"""分群快照：读取、参数调整、触发算法 A 重算。"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import SEGMENT_LABELS, SEGMENT_ORDER, SEGMENT_RULES
from src.constants.limits import DEFAULT_RFM_WEIGHTS, DEFAULT_TAIL_FACTOR, RFM_WINDOW_DAYS
from src.models.member import Member
from src.models.order import Order
from src.models.segment import Segment
from src.services import rfm_service
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_utc


def serialize(segment: Segment) -> dict:
    return {
        "id": segment.id,
        "code": segment.code,
        "name": segment.name,
        "rule_text": segment.rule_text,
        "sort_order": segment.sort_order,
        "member_count": segment.member_count,
        "share": round(segment.share or 0.0, 4),
        "avg_r_score": round(segment.avg_r_score or 0.0, 2),
        "avg_f_score": round(segment.avg_f_score or 0.0, 2),
        "avg_m_score": round(segment.avg_m_score or 0.0, 2),
        "avg_r_days": round(segment.avg_r_days or 0.0, 1),
        "gross_amount": round(segment.gross_amount or 0.0, 2),
        "refund_amount": round(segment.refund_amount or 0.0, 2),
        "net_amount": round(segment.net_amount or 0.0, 2),
        "avg_amount_window": round(segment.avg_amount_window or 0.0, 2),
        "purchase_rate": round(segment.purchase_rate or 0.0, 6),
        "avg_ticket": round(segment.avg_ticket or 0.0, 2),
        "lift": round(segment.lift or 0.0, 4),
        "coupon_face": round(segment.coupon_face or 0.0, 2),
        "core_share": round(segment.core_share or 0.0, 4),
        "enabled": bool(segment.enabled),
        "as_of": iso_utc(segment.as_of),
        "updated_at": iso_utc(segment.updated_at),
    }


def list_segments(db: Session) -> dict:
    rows = db.scalars(select(Segment).order_by(Segment.sort_order, Segment.id)).all()
    snapshot = {}
    for row in rows:
        if row.snapshot:
            snapshot = row.snapshot
            break
    as_of = next((iso_utc(row.as_of) for row in rows if row.as_of), None)
    members_total = db.scalar(select(func.count()).select_from(Member)) or 0
    return {
        "items": [serialize(row) for row in rows],
        "members_total": members_total,
        "as_of": as_of,
        "snapshot": snapshot,
        "global_purchase_rate": snapshot.get("global_purchase_rate", 0.0),
        "weights": snapshot.get("weights", list(DEFAULT_RFM_WEIGHTS)),
        "window_days": snapshot.get("window_days", RFM_WINDOW_DAYS),
        "thresholds": snapshot.get("thresholds", {}),
        "gross_total": round(sum(float(row.gross_amount or 0.0) for row in rows), 2),
        "refund_total": round(sum(float(row.refund_amount or 0.0) for row in rows), 2),
        "net_total": round(sum(float(row.net_amount or 0.0) for row in rows), 2),
        "reconciliation_status": snapshot.get("reconciliation_status", "confirmed"),
        "reconciliation_run_no": snapshot.get("reconciliation_run_no"),
    }


def get_segment(db: Session, segment_id: int) -> Segment:
    segment = db.get(Segment, segment_id)
    if segment is None:
        raise not_found("segment", segment_id)
    return segment


def update_segment(db: Session, segment: Segment, payload: dict) -> Segment:
    if payload.get("coupon_face") is not None:
        segment.coupon_face = round(float(payload["coupon_face"]), 2)
    if payload.get("core_share") is not None:
        segment.core_share = round(float(payload["core_share"]), 4)
    if payload.get("enabled") is not None:
        segment.enabled = bool(payload["enabled"])
    db.flush()
    return segment


def _aggregates(db: Session, as_of, window_days: int) -> dict[int, tuple[int, float, float, float, datetime | None]]:
    """单人窗口事实：(单数, 毛额, 已核对退款, 净额, 最近下单)。

    退款只认「已核对」批次（``ReconciliationRun.status=confirmed``），
    未核对 / 重复回传的记录不冲减，避免同一批重试把净额越扣越少。
    """
    from src.constants.enums import ReconciliationStatus
    from src.models.reconciliation import ReconciliationRun
    from src.models.refund import RefundRecord

    since = as_of - timedelta(days=window_days)
    start = datetime.combine(since, time.min)
    end = datetime.combine(as_of, time.max)
    rows = db.execute(
        select(
            Order.member_id,
            func.count(Order.id),
            func.sum(Order.amount),
            func.max(Order.paid_at),
        )
        .where(Order.paid_at >= start)
        .where(Order.paid_at <= end)
        .group_by(Order.member_id)
    ).all()
    refund_rows = db.execute(
        select(Order.member_id, func.sum(RefundRecord.applied_amount))
        .join(RefundRecord, RefundRecord.order_id == Order.id)
        .join(ReconciliationRun, RefundRecord.run_id == ReconciliationRun.id)
        .where(ReconciliationRun.status == ReconciliationStatus.CONFIRMED.value)
        .where(RefundRecord.applied_amount > 0)
        .where(Order.paid_at >= start)
        .where(Order.paid_at <= end)
        .group_by(Order.member_id)
    ).all()
    refunds = {int(member_id): round(float(amount or 0.0), 2) for member_id, amount in refund_rows}
    result = {}
    for member_id, count, amount, latest in rows:
        gross = round(float(amount or 0.0), 2)
        refund = refunds.get(int(member_id), 0.0)
        result[int(member_id)] = (int(count or 0), gross, refund, round(gross - refund, 2), latest)
    return result


def recompute(
    db: Session,
    *,
    weights: tuple[float, float, float] = DEFAULT_RFM_WEIGHTS,
    window_days: int = RFM_WINDOW_DAYS,
    allow_unreconciled: bool = False,
) -> dict:
    """从订单事实重算 R/F/M → 打分 → 分群 → 写回会员与 8 行分群快照。

    默认拒绝在「有未核对退款批次」时重算正式分群；内部核对确认流程
    （``allow_unreconciled=True``）在批次刚落为已核对后才重算。
    """
    from src.services import reconciliation_service

    if not allow_unreconciled:
        reconciliation_service.require_reconciled(db)
    weights = rfm_service.validate_weights(tuple(weights))
    if window_days < 30 or window_days > 730:
        raise AppError("VALIDATION", 422, {"reason": "统计窗口必须在 30~730 天之间"})
    as_of = rfm_service.as_of_from_orders(db)
    aggregates = _aggregates(db, as_of, window_days)
    members = db.scalars(select(Member)).all()
    if not members:
        raise AppError("VALIDATION", 409, {"reason": "会员表为空，无法重算分群"})
    payload = []
    for member in members:
        count, gross, refund, net, latest = aggregates.get(member.id, (0, 0.0, 0.0, 0.0, None))
        payload.append(
            {
                "id": member.id,
                "member_no": member.member_no,
                "last_order_on": latest.date() if isinstance(latest, datetime) else None,
                "orders": count,
                "gross": gross,
                "refund": refund,
                "amount": net,
                "purchase_rate": member.purchase_rate,
                "avg_ticket": member.avg_ticket,
            }
        )
    result = rfm_service.compute(payload, as_of=as_of, weights=weights, window_days=window_days)
    latest_run = reconciliation_service.latest_confirmed_run(db)
    snapshot = {
        "weights": list(weights),
        "window_days": window_days,
        "thresholds": result["thresholds"],
        "global_purchase_rate": result["global_purchase_rate"],
        "as_of": result["as_of"],
        "members": result["totals"]["members"],
        "orders": result["totals"]["orders"],
        "gross": result["totals"]["gross"],
        "refund": result["totals"]["refund"],
        "net": result["totals"]["amount"],
        "tail_factor": DEFAULT_TAIL_FACTOR,
        "reconciliation_status": "confirmed",
        "reconciliation_run_no": latest_run.run_no if latest_run else None,
    }
    rfm_service.persist(db, result, window_days=window_days, as_of=as_of, snapshot=snapshot)
    result["members"] = []
    result["member_count"] = len(payload)
    return result


def segments_for_allocation(db: Session) -> list[dict]:
    """供算法 B 使用：只取启用且有人的分群。"""
    rows = db.scalars(select(Segment).where(Segment.enabled.is_(True)).order_by(Segment.sort_order)).all()
    return [
        {
            "code": row.code,
            "name": row.name,
            "member_count": row.member_count,
            "gross_amount": round(row.gross_amount or 0.0, 2),
            "refund_amount": round(row.refund_amount or 0.0, 2),
            "net_amount": round(row.net_amount or 0.0, 2),
            "purchase_rate": row.purchase_rate,
            "coupon_face": row.coupon_face,
            "core_share": row.core_share,
            "avg_ticket": row.avg_ticket,
        }
        for row in rows
        if row.member_count > 0
    ]


def ensure_segments_meta(db: Session) -> dict:
    rows = db.scalars(select(Segment).order_by(Segment.sort_order)).all()
    snapshot = next((row.snapshot for row in rows if row.snapshot), {})
    return {
        "codes": [row.code for row in rows] or SEGMENT_ORDER,
        "labels": SEGMENT_LABELS,
        "rules": SEGMENT_RULES,
        "snapshot": snapshot,
    }
