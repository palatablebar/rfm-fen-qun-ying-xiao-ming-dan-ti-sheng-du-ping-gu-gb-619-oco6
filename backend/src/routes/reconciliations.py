from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import RECONCILIATION_ROLES
from src.controllers import reconciliation_controller
from src.middlewares.rbac import READ_ROLES, require_roles
from src.schemas.reconciliation import ReconciliationConfirmIn, RefundBatchIn

router = APIRouter(prefix="/reconciliations", tags=["reconciliations"])


@router.get("")
def list_runs(
    limit: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return reconciliation_controller.list_runs(db, limit=limit)


@router.get("/summary")
def summary(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return reconciliation_controller.summary(db)


@router.get("/checklist")
def checklist(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return reconciliation_controller.checklist(db)


@router.get("/{run_id}")
def get_run(
    run_id: int,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return reconciliation_controller.get_run(db, run_id)


@router.post("/batches", status_code=201)
def import_batch(
    payload: RefundBatchIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*RECONCILIATION_ROLES)),
) -> dict:
    return reconciliation_controller.import_batch(db, payload.model_dump(), principal, request)


@router.post("/{run_id}/confirm")
def confirm(
    run_id: int,
    payload: ReconciliationConfirmIn,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*RECONCILIATION_ROLES)),
) -> dict:
    return reconciliation_controller.confirm(db, run_id, payload.model_dump(), principal, request)
