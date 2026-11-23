"""统一资源接口：查询、导出、登记和动作按模块 key 分派到领域服务。

除通用 CRUD 外，这里承载字段清洗复核与批次存储回执/确认的专用入口：
- POST /api/field_clean/review           字段清洗页面复核输入
- GET  /api/field_clean/review/history   复核历史
- GET  /api/batch_store/{batch_id}/detail  批次存储详情（清单+回执+历史）
- POST /api/batch_store/{batch_id}/revalidate  重验
- POST /api/batch_store/{batch_id}/corrections 来源更正
- POST /api/batch_store/{batch_id}/confirm     确认唯一可入库版本
- POST /api/batch_store/{batch_id}/verify      脱离批次独立校验摘要
"""
from __future__ import annotations

import csv
import io
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.schemas import ActionPayload, ActionResult, EntryPayload, PageResult
from app.services import service_for
from app.services.batch_store import BatchStoreService
from app.services.field_clean import FieldCleanService

router = APIRouter(prefix="/api", tags=["业务资源"])


class ReviewPayload(BaseModel):
    inputs: list[dict[str, Any]] = Field(default_factory=list)
    extract_rule_id: str = "CONT-REVIEW-001"
    source_url: str = "https://field-clean.example.com/review"
    logic: str = "soft"


class CorrectionPayload(BaseModel):
    field: str
    value: Any = None
    value_type: str = "str"
    source_revision: str = ""
    source_url: str = ""
    row_index: int | None = None
    note: str = ""
    idempotency_key: str = ""


class RevalidatePayload(BaseModel):
    note: str = ""
    idempotency_key: str = ""


class ConfirmPayload(BaseModel):
    expected_base_version: int | None = None
    idempotency_key: str = ""
    operator: str = "值班员"


class VerifyPayload(BaseModel):
    manifest: list[dict[str, Any]] | None = None
    checksum: str = ""
    version: int | None = None


def require_service(module: str):
    service = service_for(module)
    if service is None:
        raise HTTPException(status_code=404, detail=f"未知业务模块：{module}")
    return service


def _field_clean_service() -> FieldCleanService:
    service = service_for("field_clean")
    if not isinstance(service, FieldCleanService):
        raise HTTPException(status_code=500, detail="字段清洗服务未正确注册")
    return service


def _batch_service() -> BatchStoreService:
    service = service_for("batch_store")
    if not isinstance(service, BatchStoreService):
        raise HTTPException(status_code=500, detail="批次存储服务未正确注册")
    return service


# ---------------------------------------------------------------------------
# 专用路由必须注册在 /{module}/{entry_id} 之前，避免被动态路径吞掉
# ---------------------------------------------------------------------------

@router.post("/field_clean/review", response_model=dict)
def review_field_inputs(payload: ReviewPayload) -> dict[str, Any]:
    """字段清洗页面复核：返回每行的回执（类型、来源、缺失字段、原始输入）。"""
    service = _field_clean_service()
    if not payload.inputs:
        raise HTTPException(status_code=400, detail="复核输入不能为空")
    return service.review_inputs(
        payload.inputs,
        extract_rule_id=payload.extract_rule_id,
        source_url=payload.source_url,
        logic=payload.logic,
    )


@router.get("/field_clean/review/history", response_model=dict)
def field_review_history() -> dict[str, Any]:
    service = _field_clean_service()
    items = service.review_history()
    return {"items": items, "total": len(items)}


@router.get("/batch_store/{batch_id}/detail", response_model=dict)
def batch_detail(batch_id: str) -> dict[str, Any]:
    service = _batch_service()
    try:
        return service.batch_detail(batch_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"批次 {batch_id} 不存在")


@router.get("/batch_store/{batch_id}/receipts", response_model=dict)
def batch_receipts(batch_id: str) -> dict[str, Any]:
    service = _batch_service()
    return service.receipt_history(batch_id)


@router.get("/batch_store/{batch_id}/receipts/{version}", response_model=dict)
def batch_receipt(batch_id: str, version: int) -> dict[str, Any]:
    service = _batch_service()
    try:
        return service.receipt(batch_id, version)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"回执 {batch_id} V{version} 不存在")


@router.post("/batch_store/{batch_id}/revalidate", response_model=dict)
def batch_revalidate(batch_id: str, payload: RevalidatePayload | None = None) -> dict[str, Any]:
    service = _batch_service()
    body = payload or RevalidatePayload()
    try:
        return service.revalidate(
            batch_id, note=body.note, idempotency_key=body.idempotency_key)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"批次 {batch_id} 不存在")


@router.post("/batch_store/{batch_id}/corrections", response_model=dict)
def batch_source_correction(batch_id: str, payload: CorrectionPayload) -> dict[str, Any]:
    service = _batch_service()
    try:
        return service.source_correction(
            batch_id,
            field=payload.field,
            value=payload.value,
            value_type=payload.value_type,
            source_revision=payload.source_revision,
            source_url=payload.source_url,
            row_index=payload.row_index,
            note=payload.note,
            idempotency_key=payload.idempotency_key,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"批次 {batch_id} 不存在")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/batch_store/{batch_id}/confirm", response_model=dict)
def batch_confirm(batch_id: str, payload: ConfirmPayload) -> dict[str, Any]:
    return _confirm(batch_id, payload)


def _confirm(batch_id: str, payload: ConfirmPayload, version: int | None = None) -> dict[str, Any]:
    service = _batch_service()
    return service.confirm(
        batch_id,
        version if version is not None else _latest_candidate_version(batch_id),
        expected_base_version=payload.expected_base_version,
        idempotency_key=payload.idempotency_key,
        operator=payload.operator,
    )


def _latest_candidate_version(batch_id: str) -> int:
    service = _batch_service()
    history = service.receipt_history(batch_id)
    candidates = [item["version"] for item in history["receipts"]
                  if item["status"] in ("候选", "不可入库", "确认冲突")]
    if not candidates:
        raise HTTPException(status_code=400, detail="没有可确认的候选回执")
    return max(candidates)


@router.post("/batch_store/{batch_id}/receipts/{version}/confirm", response_model=dict)
def batch_confirm_version(batch_id: str, version: int,
                          payload: ConfirmPayload | None = None) -> dict[str, Any]:
    return _confirm(batch_id, payload or ConfirmPayload(), version)


@router.post("/batch_store/{batch_id}/verify", response_model=dict)
def batch_verify(batch_id: str, payload: VerifyPayload | None = None) -> dict[str, Any]:
    service = _batch_service()
    body = payload or VerifyPayload()
    try:
        return service.verify_manifest(
            batch_id,
            manifest=body.manifest,
            checksum=body.checksum,
            version=body.version,
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"批次 {batch_id} 不存在")


@router.get("/{module}", response_model=PageResult[dict])
def list_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> PageResult[dict]:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, total = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{module}/export")
def export_entries(
    module: str,
    request: Request,
    keyword: str | None = Query(default=None),
    status: str | None = Query(default=None),
) -> StreamingResponse:
    service = require_service(module)
    filters = {
        key: value.strip()
        for key, value in request.query_params.items()
        if key in service.LIST_FIELDS and value.strip()
    }
    items, _ = service.list_entries(
        filters=filters, keyword=keyword, status=status, page=1, size=1_000_000)
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(["id", *service.LIST_FIELDS, "status"])
    for item in items:
        values = [item.get("id"), *(item.get(field, "") for field in service.LIST_FIELDS),
                  item.get("status", "")]
        writer.writerow([
            "'" + str(value) if str(value or "").startswith(("=", "+", "-", "@")) else value
            for value in values
        ])
    return StreamingResponse(
        iter([output.getvalue()]), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{module}.csv"'},
    )


@router.get("/{module}/{entry_id}", response_model=dict)
def get_entry(module: str, entry_id: int) -> dict[str, Any]:
    service = require_service(module)
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"{service.ENTITY} {entry_id} 不存在或已归档")
    return entry


@router.post("/{module}", response_model=ActionResult)
def create_entry(module: str, payload: EntryPayload) -> ActionResult:
    service = require_service(module)
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message=f"{service.ENTITY}已登记", entry=entry)


@router.post("/{module}/{entry_id}/actions", response_model=ActionResult)
def run_action(module: str, entry_id: int, payload: ActionPayload) -> ActionResult:
    service = require_service(module)
    entry, message = service.run_action(entry_id, payload.action.strip())
    return ActionResult(ok=entry is not None, message=message, entry=entry)
