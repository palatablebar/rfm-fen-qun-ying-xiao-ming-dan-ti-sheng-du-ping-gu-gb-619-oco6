"""退款核对批次：一次核对把「待核对」推进到「已核对」，未核对前分群口径冻结。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class ReconciliationRun(Base):
    __tablename__ = "reconciliation_runs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    # 同一批次重试导入返回同一条核对记录，不重复冲减
    batch_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # pending=待核对 / confirmed=已核对
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    record_count: Mapped[int] = mapped_column(Integer, default=0)

    gross_amount: Mapped[float] = mapped_column(Float, default=0.0)
    refund_amount: Mapped[float] = mapped_column(Float, default=0.0)
    net_amount: Mapped[float] = mapped_column(Float, default=0.0)
    applied_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    unattributable_count: Mapped[int] = mapped_column(Integer, default=0)
    over_refund_count: Mapped[int] = mapped_column(Integer, default=0)
    exception_count: Mapped[int] = mapped_column(Integer, default=0)
    exception_amount: Mapped[float] = mapped_column(Float, default=0.0)
    exceptions: Mapped[list] = mapped_column(JSON, default=list)
    per_order: Mapped[dict] = mapped_column(JSON, default=dict)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    operator_email: Mapped[str] = mapped_column(String(160), default="system")
    confirmed_by: Mapped[str | None] = mapped_column(String(160), nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    acknowledged_exceptions: Mapped[bool] = mapped_column(Boolean, default=False)
    seed_key: Mapped[str | None] = mapped_column(String(120), unique=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
