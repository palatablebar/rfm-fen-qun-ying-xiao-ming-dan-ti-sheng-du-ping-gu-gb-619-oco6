"""退款核对单测：幂等重试、重复回传、异常隔离、核对门禁与逐项对账。"""
from __future__ import annotations

from datetime import date, datetime

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from src.models import Base
from src.models.audience import Audience
from src.models.campaign import Campaign
from src.models.member import Member
from src.models.order import Order
from src.models.reconciliation import ReconciliationRun
from src.models.refund import RefundRecord
from src.services import (
    audience_service,
    export_service,
    reconciliation_service,
    segment_service,
)
from src.utils.errors import AppError

AS_OF = datetime(2026, 11, 14, 12, 0)


@pytest.fixture()
def db(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, class_=Session)
    session = factory()
    monkeypatch.setattr(
        segment_service.rfm_service,
        "as_of_from_orders",
        lambda _db: AS_OF.date(),
    )
    _seed(session)
    yield session
    session.close()


def _seed(db: Session) -> None:
    members = []
    for index in range(4):
        members.append(
            Member(
                member_no=f"M{index + 1:03d}",
                name=f"会员{index + 1}",
                phone=f"1380000{index:04d}",
                city="上海",
                tier="gold",
                joined_on=date(2025, 1, 1),
                purchase_rate=[0.09, 0.06, 0.02, 0.01][index],
                avg_ticket=30.0,
            )
        )
    db.add_all(members)
    db.flush()
    # M001/M002 近期高频高额 → 高分群；M003/M004 低频 → 低分群。
    # 每人的第一笔订单固定 100（O11/O21/O31/O41），其余 50，便于精确断言退款口径。
    order_counts = [5, 4, 2, 1]
    for index, member in enumerate(members):
        for seq in range(order_counts[index]):
            amount = 100.0 if seq == 0 else 50.0
            db.add(
                Order(
                    order_no=f"O{index + 1}{seq + 1}",
                    member_id=member.id,
                    store_code="MY-S01",
                    channel="store",
                    amount=amount,
                    items=1,
                    paid_at=datetime(2026, 11, max(1, 12 - seq), 10, 0),
                )
            )
    db.flush()
    segment_service.recompute(db, weights=(0.3, 0.3, 0.4), window_days=365, allow_unreconciled=True)
    campaign = Campaign(
        code="autumn-member-day",
        name="秋日会员日券",
        channel="小程序订阅消息",
        budget=500.0,
        status="active",
        starts_on=date(2026, 11, 18),
        ends_on=date(2026, 11, 24),
        operator_email="seed@rfmseg.io",
    )
    db.add(campaign)
    db.flush()
    audience_service.generate(
        db,
        campaign=campaign,
        budget=None,
        operator_email="seed@rfmseg.io",
        seed_key="seed-audience-1",
    )
    db.flush()


def _batch(batch_key: str, records: list[dict]) -> list[dict]:
    return records


def test_batch_import_is_idempotent_and_does_not_double_deduct(db: Session):
    records = [
        {"order_no": "O11", "refund_no": "RF-1", "amount": 30.0, "refunded_at": "2026-11-12 09:00:00"},
    ]
    run, created = reconciliation_service.import_batch(
        db, batch_key="wk46", records=records, operator_email="analyst@rfmseg.io"
    )
    assert created is True
    assert run.record_count == 1
    assert run.refund_amount == pytest.approx(30.0)

    again, created_again = reconciliation_service.import_batch(
        db, batch_key="wk46", records=records, operator_email="analyst@rfmseg.io"
    )
    assert created_again is False
    assert again.id == run.id
    assert again.refund_amount == pytest.approx(30.0)
    assert db.scalar(select(RefundRecord).where(RefundRecord.batch_key == "wk46")).amount == pytest.approx(30.0)

    # 同一批确认两次也只冲减一次
    reconciliation_service.confirm_run(db, again, actor="analyst@rfmseg.io")
    reconciliation_service.confirm_run(db, again, actor="analyst@rfmseg.io")
    applied = reconciliation_service.applied_refund_by_order(db)
    assert applied[1] == pytest.approx(30.0)

    member = db.scalar(select(Member).where(Member.member_no == "M001"))
    assert member is not None
    assert member.refund_window == pytest.approx(30.0)
    assert member.amount_window == pytest.approx(270.0)
    assert member.gross_window == pytest.approx(300.0)


def test_duplicate_across_batches_is_listed_not_deducted(db: Session):
    first = [{"order_no": "O11", "refund_no": "RF-DUP", "amount": 20.0, "refunded_at": "2026-11-12 09:00:00"}]
    run, _ = reconciliation_service.import_batch(
        db, batch_key="wk45", records=first, operator_email="analyst@rfmseg.io"
    )
    reconciliation_service.confirm_run(db, run, actor="analyst@rfmseg.io")

    second = [
        {"order_no": "O11", "refund_no": "RF-DUP", "amount": 20.0, "refunded_at": "2026-11-13 09:00:00"},
        {"order_no": "O12", "refund_no": "RF-NEW", "amount": 10.0, "refunded_at": "2026-11-13 10:00:00"},
    ]
    pending, _ = reconciliation_service.import_batch(
        db, batch_key="wk46", records=second, operator_email="analyst@rfmseg.io"
    )
    assert pending.duplicate_count == 1
    assert pending.applied_count == 1  # RF-NEW 将冲减；RF-DUP 重复不冲减
    assert pending.refund_amount == pytest.approx(10.0)
    duplicates = [row for row in pending.exceptions or [] if row["status"] == "duplicate"]
    assert duplicates == []

    records = reconciliation_service.run_detail(db, pending.id)["records"]
    duplicate = next(row for row in records if row["refund_no"] == "RF-DUP")
    assert duplicate["status"] == "duplicate"
    assert duplicate["applied_amount"] == 0.0


def test_unattributable_and_over_refund_are_isolated(db: Session):
    records = [
        {"order_no": "O11", "refund_no": "RF-A", "amount": 80.0, "refunded_at": "2026-11-12 09:00:00"},
        {"order_no": "O11", "refund_no": "RF-B", "amount": 50.0, "refunded_at": "2026-11-12 10:00:00"},
        {"order_no": "NOPE", "refund_no": "RF-C", "amount": 15.0, "refunded_at": "2026-11-12 11:00:00"},
        {"order_no": "O21", "refund_no": "RF-D", "amount": 0.0, "refunded_at": "2026-11-12 12:00:00"},
    ]
    run, _ = reconciliation_service.import_batch(
        db, batch_key="wk47", records=records, operator_email="analyst@rfmseg.io"
    )
    detail = reconciliation_service.run_detail(db, run.id)
    statuses = {row["refund_no"]: row for row in detail["records"]}
    # 待核对期间：校验通过的计划冲减为 pending，确认后才落为 applied
    assert statuses["RF-A"]["status"] == "pending"
    assert statuses["RF-A"]["applied_amount"] == pytest.approx(80.0)
    # 原单 100，已退 80，再退 50 只能冲 20，超 30 列异常
    assert statuses["RF-B"]["status"] == "over_refund"
    assert statuses["RF-B"]["applied_amount"] == pytest.approx(20.0)
    assert statuses["RF-B"]["exception_amount"] == pytest.approx(30.0)
    assert statuses["RF-C"]["status"] == "unattributable"
    assert statuses["RF-D"]["status"] == "unattributable"
    assert detail["exception_count"] == 3
    assert detail["exception_amount"] == pytest.approx(45.0)
    assert len(detail["exceptions"]) == 3

    # 未逐条确认异常时不允许核对
    with pytest.raises(AppError) as exc:
        reconciliation_service.confirm_run(db, run, actor="analyst@rfmseg.io")
    assert exc.value.status_code == 422

    reconciliation_service.confirm_run(db, run, actor="analyst@rfmseg.io", acknowledge_exceptions=True)
    confirmed = {row["refund_no"]: row for row in reconciliation_service.run_detail(db, run.id)["records"]}
    assert confirmed["RF-A"]["status"] == "applied"
    applied = reconciliation_service.applied_refund_by_order(db)
    assert applied[1] == pytest.approx(100.0)  # 原单最多冲减到 0


def test_pending_batch_blocks_formal_segments_audiences_and_exports(db: Session):
    records = [{"order_no": "O11", "refund_no": "RF-X", "amount": 10.0, "refunded_at": "2026-11-12 09:00:00"}]
    run, _ = reconciliation_service.import_batch(
        db, batch_key="wk48", records=records, operator_email="analyst@rfmseg.io"
    )
    with pytest.raises(AppError) as exc:
        segment_service.recompute(db)
    assert exc.value.code == "REFUND_UNRECONCILED"
    with pytest.raises(AppError):
        segment_service.list_segments(db) and reconciliation_service.require_reconciled(db)
    campaign = db.scalar(select(Campaign))
    with pytest.raises(AppError):
        audience_service.preview_allocation(db, campaign)
    with pytest.raises(AppError):
        audience_service.confirm(db, db.scalar(select(Audience)), status="confirmed", note=None, actor="x")
    with pytest.raises(AppError):
        reconciliation_service.require_reconciled(db)

    # 已核对后恢复
    reconciliation_service.confirm_run(db, run, actor="analyst@rfmseg.io")
    assert reconciliation_service.pending_run(db) is None
    result = segment_service.recompute(db)
    assert result["status"] == "success"


def test_confirm_recomputes_member_net_and_gmv(db: Session):
    before = db.scalar(select(Member).where(Member.member_no == "M001"))
    before_gmv = db.scalar(select(Audience).where(Audience.seed_key == "seed-audience-1")).inc_gmv
    records = [
        {"order_no": "O11", "refund_no": "RF-N1", "amount": 100.0, "refunded_at": "2026-11-12 09:00:00"},
    ]
    run, _ = reconciliation_service.import_batch(
        db, batch_key="wk49", records=records, operator_email="analyst@rfmseg.io"
    )
    reconciliation_service.confirm_run(db, run, actor="analyst@rfmseg.io")
    db.refresh(before)
    assert before.gross_window == pytest.approx(300.0)
    assert before.refund_window == pytest.approx(100.0)
    assert before.amount_window == pytest.approx(200.0)

    after = db.scalar(select(Audience).where(Audience.seed_key == "seed-audience-1"))
    assert after.inc_gmv <= before_gmv
    assert after.gross_amount > 0
    assert after.net_amount == pytest.approx(after.gross_amount - after.refund_amount)


def test_checklist_amounts_reconcile_across_page_segments_and_export(db: Session):
    records = [{"order_no": "O11", "refund_no": "RF-C1", "amount": 40.0, "refunded_at": "2026-11-12 09:00:00"}]
    run, _ = reconciliation_service.import_batch(
        db, batch_key="wk50", records=records, operator_email="analyst@rfmseg.io"
    )
    reconciliation_service.confirm_run(db, run, actor="analyst@rfmseg.io")

    data = reconciliation_service.checklist(db)
    assert data["status"] == "confirmed"
    assert data["all_ok"] is True, [item for item in data["items"] if not item["ok"]]
    keys = {item["key"] for item in data["items"]}
    assert {"gross", "refund", "net", "members", "projected_gmv", "audience_gross", "audience_net"} <= keys

    # 导出毛额 / 退款额 / 净额与页面逐行一致
    raw = export_service.audience_csv(db).decode("utf-8-sig")
    import csv
    import io

    rows = list(csv.DictReader(io.StringIO(raw)))
    assert rows
    assert round(sum(float(row["订单毛额"]) for row in rows), 2) == pytest.approx(
        round(sum(float(row["订单净额"]) for row in rows), 2) + round(sum(float(row["订单退款额"]) for row in rows), 2),
        2,
    )


def test_member_net_affects_m_score_and_prevents_inflated_segments(db: Session):
    member = db.scalar(select(Member).where(Member.member_no == "M004"))
    # 给低价值会员一笔大额订单再全额退款：毛额高但净额为 0，不应凭成交额进高分群
    db.add(
        Order(
            order_no="O-BIG",
            member_id=member.id,
            store_code="MY-S01",
            channel="store",
            amount=5000.0,
            items=1,
            paid_at=datetime(2026, 11, 11, 10, 0),
        )
    )
    db.flush()
    records = [{"order_no": "O-BIG", "refund_no": "RF-BIG", "amount": 5000.0, "refunded_at": "2026-11-12 09:00:00"}]
    run, _ = reconciliation_service.import_batch(
        db, batch_key="wk51", records=records, operator_email="analyst@rfmseg.io"
    )
    reconciliation_service.confirm_run(db, run, actor="analyst@rfmseg.io")
    db.refresh(member)
    assert member.gross_window > 5000
    assert member.amount_window == pytest.approx(member.gross_window - member.refund_window)
    assert member.m_score is not None and member.m_score <= 2


def test_second_batch_while_pending_is_rejected(db: Session):
    records = [{"order_no": "O11", "refund_no": "RF-W1", "amount": 5.0, "refunded_at": "2026-11-12 09:00:00"}]
    reconciliation_service.import_batch(db, batch_key="wk60", records=records, operator_email="analyst@rfmseg.io")
    with pytest.raises(AppError) as exc:
        reconciliation_service.import_batch(
            db, batch_key="wk61", records=records, operator_email="analyst@rfmseg.io"
        )
    assert exc.value.status_code == 409
    assert db.scalar(select(ReconciliationRun).where(ReconciliationRun.batch_key == "wk61")) is None


def test_same_refund_no_twice_in_one_batch_is_deducted_once(db: Session):
    records = [
        {"order_no": "O11", "refund_no": "RF-SAME", "amount": 30.0, "refunded_at": "2026-11-12 09:00:00"},
        {"order_no": "O11", "refund_no": "RF-SAME", "amount": 30.0, "refunded_at": "2026-11-12 09:05:00"},
    ]
    run, _ = reconciliation_service.import_batch(
        db, batch_key="wk62", records=records, operator_email="analyst@rfmseg.io"
    )
    reconciliation_service.confirm_run(db, run, actor="analyst@rfmseg.io")
    db.refresh(run)
    assert run.record_count == 2
    assert run.refund_amount == pytest.approx(30.0)
    assert db.scalar(select(func.count()).select_from(RefundRecord)) == 1


def test_refund_for_unknown_member_goes_to_exceptions_and_blocks_missing_ack(db: Session):
    records = [
        {"order_no": "O11", "refund_no": "RF-M", "member_no": "M999", "amount": 10.0, "refunded_at": "2026-11-12 09:00:00"}
    ]
    run, _ = reconciliation_service.import_batch(
        db, batch_key="wk63", records=records, operator_email="analyst@rfmseg.io"
    )
    assert run.unattributable_count == 1
    with pytest.raises(AppError):
        reconciliation_service.confirm_run(db, run, actor="analyst@rfmseg.io")
    reconciliation_service.confirm_run(db, run, actor="analyst@rfmseg.io", acknowledge_exceptions=True)
    assert reconciliation_service.pending_run(db) is None
