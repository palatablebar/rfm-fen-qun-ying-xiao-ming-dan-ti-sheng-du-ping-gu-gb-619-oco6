from __future__ import annotations

from pydantic import BaseModel, Field


class RefundRecordIn(BaseModel):
    order_no: str = Field(min_length=1, max_length=32)
    refund_no: str | None = Field(default=None, max_length=48)
    member_no: str | None = Field(default=None, max_length=24)
    amount: float = Field(ge=-1_000_000, le=1_000_000)
    refunded_at: str | None = Field(default=None, max_length=32)


class RefundBatchIn(BaseModel):
    """导入一批晚到退款。同 batch_key 重试视为同一批，不会重复冲减。"""

    batch_key: str = Field(min_length=4, max_length=64)
    records: list[RefundRecordIn] = Field(min_length=1, max_length=5000)
    note: str | None = Field(default=None, max_length=1000)


class ReconciliationConfirmIn(BaseModel):
    acknowledge_exceptions: bool = False
    note: str | None = Field(default=None, max_length=1000)
