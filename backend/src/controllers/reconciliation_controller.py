"""退款核对控制器：导入幂等、异常确认、核对落账都记审计。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, reconciliation_service


def summary(db: Session) -> dict:
    return reconciliation_service.summary(db)


def list_runs(db: Session, *, limit: int = 20) -> dict:
    return reconciliation_service.list_runs(db, limit=limit)


def get_run(db: Session, run_id: int) -> dict:
    return reconciliation_service.run_detail(db, run_id)


def checklist(db: Session) -> dict:
    return reconciliation_service.checklist(db)


def import_batch(db: Session, payload: dict, principal: dict, request: Request) -> dict:
    run, created = reconciliation_service.import_batch(
        db,
        batch_key=payload["batch_key"],
        records=payload["records"],
        operator_email=principal["email"],
        note=payload.get("note"),
    )
    if created:
        audit_service.record(
            db,
            actor_email=principal["email"],
            actor_role=principal["role"],
            action="refund.import",
            entity="reconciliation_run",
            entity_id=run.id,
            detail={
                "run_no": run.run_no,
                "batch_key": run.batch_key,
                "records": run.record_count,
                "applied": run.applied_count,
                "duplicate": run.duplicate_count,
                "exceptions": run.exception_count,
                "refund_amount": run.refund_amount,
            },
            ip=request.client.host if request.client else None,
        )
        db.commit()
        db.refresh(run)
    detail = reconciliation_service.run_detail(db, run.id)
    detail["created"] = created
    return detail


def confirm(db: Session, run_id: int, payload: dict, principal: dict, request: Request) -> dict:
    run = reconciliation_service.get_run(db, run_id)
    reconciliation_service.confirm_run(
        db,
        run,
        actor=principal["email"],
        acknowledge_exceptions=bool(payload.get("acknowledge_exceptions")),
    )
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="refund.confirm",
        entity="reconciliation_run",
        entity_id=run.id,
        detail={
            "run_no": run.run_no,
            "batch_key": run.batch_key,
            "exceptions_acknowledged": bool(payload.get("acknowledge_exceptions")),
            "exception_count": run.exception_count,
            "refund_amount": run.refund_amount,
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return reconciliation_service.run_detail(db, run.id)
