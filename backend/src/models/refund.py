"""退款流水：晚到回传、重试重复、无法归属与超额退款都在这里留痕。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class RefundRecord(Base):
    __tablename__ = "refund_records"
    __table_args__ = (
        Index("ix_refund_records_batch_status", "batch_key", "status"),
        Index("ix_refund_records_order_status", "order_no", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    # 幂等键：同一批次同一退款行重复导入不会新增记录
    record_key: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    batch_key: Mapped[str] = mapped_column(String(64), index=True)
    # 退款笔去重键：refund_no 优先，缺省时用「订单号+金额+日期」
    dedupe_key: Mapped[str] = mapped_column(String(160), index=True)
    refund_no: Mapped[str] = mapped_column(String(48), index=True)
    order_no: Mapped[str] = mapped_column(String(32), index=True)
    member_no: Mapped[str | None] = mapped_column(String(24), nullable=True)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    refunded_at: Mapped[datetime] = mapped_column(DateTime, index=True)

    # pending / applied / duplicate / unattributable / over_refund
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    applied_amount: Mapped[float] = mapped_column(Float, default=0.0)
    exception_amount: Mapped[float] = mapped_column(Float, default=0.0)
    exception_reason: Mapped[str | None] = mapped_column(String(160), nullable=True)

    order_id: Mapped[int | None] = mapped_column(
        ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    member_id: Mapped[int | None] = mapped_column(
        ForeignKey("members.id", ondelete="SET NULL"), nullable=True, index=True
    )
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    raw: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
