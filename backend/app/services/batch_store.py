"""批次存储领域服务：存储清单、字段校验回执、重验/来源更正与唯一版本确认。"""
from __future__ import annotations

import copy
from typing import Any

from app.models import (
    CleanedContent, CorrectionRecord, StorageBatch, is_missing, type_name,
)
from app.receipts import (
    CONFIRMED, LEGACY_BATCH_ID, NOT_STORABLE, SUPERSEDED,
    build_row_receipts, registry, validate_manifest_checksum,
)
from app.services.base import BaseModuleService
from app.store import store


def _coerce(raw_value: Any, value_type: str) -> Any:
    """按声明的字段类型还原值：0 / false 不会退化成字符串或空值。"""
    value_type = (value_type or "str").lower()
    if value_type in ("int", "integer"):
        if isinstance(raw_value, bool):
            return int(raw_value)
        if isinstance(raw_value, (int, float)):
            return int(raw_value)
        text = str(raw_value).strip()
        return int(float(text)) if text not in ("", "None") else ""
    if value_type in ("float", "number", "decimal"):
        if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
            return float(raw_value)
        text = str(raw_value).strip()
        return float(text) if text not in ("", "None") else ""
    if value_type in ("bool", "boolean"):
        if isinstance(raw_value, bool):
            return raw_value
        text = str(raw_value).strip().lower()
        if text in ("true", "1", "yes", "是"):
            return True
        if text in ("false", "0", "no", "否"):
            return False
        return raw_value
    if value_type == "null":
        return None
    return raw_value


class BatchStoreService(BaseModuleService):
    MODULE = "batch_store"
    ENTITY = "采集批次"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["批次编号", "来源作业", "数据行数", "存储路径", "压缩格式", "Schema版本", "校验摘要", "入库时间"]
    REQUIRED_FIELDS = ["批次编号", "来源作业", "数据行数"]
    STATUS_ORDER = ["采集中", "已落盘", "校验通过", "校验失败"]
    ACTION_RULES = {"触发校验": "已落盘", "标记通过": "校验通过", "标记失败": "校验失败"}
    NEGATIVE_ACTIONS = []

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def _row_by_batch_id(self, batch_id: str) -> dict[str, Any] | None:
        for row in store.rows(self.MODULE):
            if str(row.get("批次编号")) == batch_id:
                return row
        return None

    def batch_detail(self, batch_id: str, *, history: bool = True) -> dict[str, Any]:
        """批次存储详情：存储清单 + 当前有效回执 + 全部历史版本/更正。"""
        row = self._row_by_batch_id(batch_id)
        if row is None:
            raise KeyError(batch_id)
        record_set = registry.record_set(batch_id)
        records = registry.records(batch_id)
        batch = StorageBatch.from_seed_row(row)
        batch.records = records
        # 存储清单按字段校验回执产出：字段值带类型、缺失字段带来源
        manifest = [item.to_dict() for item in build_row_receipts(records)]
        # 历史批次已确认的摘要按首次登记时的原始记录核对，后续更正不覆盖
        legacy_records = record_set.legacy_records if record_set else records
        stored_historical = str(
            row.get("历史校验摘要") or row.get("校验摘要") or "")
        historical_checksum = StorageBatch(
            batch_id=batch_id, source_job_id=batch.source_job_id,
            records=list(legacy_records),
        ).compute_legacy_checksum()
        active = registry.active_receipt(batch_id)
        latest = registry.latest_receipt(batch_id)
        effective = active or latest
        payload: dict[str, Any] = {
            "batch": row,
            "source_job_id": row.get("来源作业"),
            "stored_checksum": row.get("校验摘要"),
            "historical_checksum": stored_historical,
            "storage_path": row.get("存储路径", ""),
            "manifest": manifest,
            "manifest_checksum": batch.compute_checksum(),
            "legacy_checksum": historical_checksum,
            "legacy_checksum_matches": bool(stored_historical)
            and historical_checksum.startswith(stored_historical),
            "historical_checksum_matches": bool(stored_historical)
            and historical_checksum.startswith(stored_historical),
            "record_count": len(records),
            "invalid_rows": sum(1 for item in manifest if not item["valid"]),
            "active_receipt": active.to_dict() if active else None,
            "latest_receipt": latest.to_dict() if latest else None,
            "effective_receipt": effective.to_dict() if effective else None,
            "source_revision": record_set.source_revision if record_set else "",
            "acceptance_ready": bool(active),
            "acceptance_receipt_id": active.receipt_id if active else "",
            "acceptance_checksum": active.checksum if active else "",
        }
        if history:
            payload["receipts"] = [item.to_dict() for item in registry.receipts(batch_id)]
            payload["corrections"] = [
                item.to_dict() for item in registry.corrections(batch_id)
            ]
        return payload

    def receipt(self, batch_id: str, version: int) -> dict[str, Any]:
        record = registry.receipt(batch_id, version)
        if record is None:
            raise KeyError(f"{batch_id}:V{version}")
        return record.to_dict()

    def receipt_history(self, batch_id: str) -> dict[str, Any]:
        return {
            "batch_id": batch_id,
            "active_version": registry.active_version(batch_id),
            "receipts": [item.to_dict() for item in registry.receipts(batch_id)],
            "corrections": [item.to_dict() for item in registry.corrections(batch_id)],
        }

    # ------------------------------------------------------------------
    # 复核（重验）与来源更正
    # ------------------------------------------------------------------
    def revalidate(self, batch_id: str, *, note: str = "",
                   idempotency_key: str = "") -> dict[str, Any]:
        """按新值语义重新核对批次，生成候选回执，不自动确认。"""
        if registry.record_set(batch_id) is None:
            raise KeyError(batch_id)
        records = copy.deepcopy(registry.records(batch_id))
        active_version = registry.active_version(batch_id)
        receipt = registry.create_receipt(
            batch_id, "revalidate", records,
            base_version=active_version,
            note=note or "按 0/False 合法、空值才算缺失的规则重验",
            idempotency_key=idempotency_key,
        )
        return receipt.to_dict()

    def source_correction(
        self,
        batch_id: str,
        *,
        field: str,
        value: Any,
        value_type: str = "str",
        source_revision: str = "",
        source_url: str = "",
        row_index: int | None = None,
        note: str = "",
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """接收来源修订，生成候选回执；同一字段的新值按声明类型保存。"""
        if registry.record_set(batch_id) is None:
            raise KeyError(batch_id)
        base_version = registry.active_version(batch_id)
        records = copy.deepcopy(registry.records(batch_id))
        coerced = _coerce(value, value_type)
        corrected: list[CleanedContent] = []
        pending_corrections: list[dict[str, Any]] = []
        hit = False
        for index, record in enumerate(records):
            if row_index is not None and index + 1 != row_index:
                corrected.append(record)
                continue
            if source_url and record.source_url != source_url:
                corrected.append(record)
                continue
            old_value = record.fields.get(field)
            record.fields[field] = coerced
            record.field_sources[field] = (
                f"correction:{source_revision or 'unspecified'}")
            if is_missing(coerced):
                if field not in record.missing_fields:
                    record.missing_fields.append(field)
            else:
                record.missing_fields = [name for name in record.missing_fields
                                         if name != field]
            # 来源修订覆盖了该字段，移除指向该字段的旧清洗错误
            record.clean_errors = [
                item for item in record.clean_errors if field not in item
            ]
            hit = True
            corrected.append(record)
            pending_corrections.append({
                "field": field,
                "old_value": old_value,
                "new_value": coerced,
                "value_type": type_name(coerced),
                "source_revision": source_revision,
                "source_url": record.source_url,
                "note": note,
            })
        if not hit:
            raise ValueError("来源更正未匹配到任何行")
        receipt = registry.create_receipt(
            batch_id, "correction", corrected,
            base_version=base_version,
            source_revision=source_revision,
            corrected_fields=[field],
            note=note or f"来源修订 {source_revision} 更正字段 {field}",
            idempotency_key=idempotency_key,
        )
        for sequence, payload in enumerate(pending_corrections, start=1):
            registry.add_correction(CorrectionRecord(
                correction_id=f"CORR-{batch_id}-V{receipt.version}-{field}-{sequence}",
                batch_id=batch_id,
                version=receipt.version,
                field=payload["field"],
                old_value=payload["old_value"],
                new_value=payload["new_value"],
                value_type=payload["value_type"],
                source_revision=payload["source_revision"],
                source_url=payload["source_url"],
                note=payload["note"],
            ))
        return receipt.to_dict()

    def confirm(
        self,
        batch_id: str,
        version: int,
        *,
        expected_base_version: int | None = None,
        idempotency_key: str = "",
        operator: str = "值班员",
    ) -> dict[str, Any]:
        """确认唯一可入库版本；冲突方保留在历史中并带出判断范围与摘要。"""
        ok, receipt, message = registry.confirm(
            batch_id, version,
            expected_base_version=expected_base_version,
            idempotency_key=idempotency_key,
            operator=operator,
        )
        if receipt is not None and not ok and receipt.valid:
            registry.mark_conflict(receipt, message)
        if ok and receipt is not None:
            self._activate(receipt)
        return {
            "ok": ok,
            "message": message,
            "batch_id": batch_id,
            "version": version,
            "active_version": registry.active_version(batch_id),
            "receipt": receipt.to_dict() if receipt else None,
            "acceptance_ready": ok,
        }

    def _activate(self, receipt: Any) -> None:
        """把已确认版本同步到批次存储主表与存储清单。"""
        row = self._row_by_batch_id(receipt.batch_id)
        if row is None:
            return
        # 先固化历史摘要，再写入新摘要
        if not row.get("历史校验摘要"):
            row["历史校验摘要"] = row.get("校验摘要", "")
        row["校验摘要"] = receipt.checksum[:16]
        row["数据行数"] = len(receipt.rows)
        row["status"] = "校验通过"
        row["pending"] = False
        row["abnormal"] = False
        registry.replace_records(receipt.batch_id, [
            CleanedContent(
                source_url=row_receipt.source_url,
                extract_rule_id=row_receipt.extract_rule_id,
                fields=dict(row_receipt.fields),
                raw_fields=dict(row_receipt.raw_fields),
                missing_fields=list(row_receipt.missing_fields),
                field_sources=dict(row_receipt.field_sources),
            )
            for row_receipt in receipt.rows
        ])

    # ------------------------------------------------------------------
    # 独立校验
    # ------------------------------------------------------------------
    def verify_manifest(
        self, batch_id: str,
        *,
        manifest: list[dict[str, Any]] | None = None,
        checksum: str = "",
        version: int | None = None,
    ) -> dict[str, Any]:
        """脱离批次上下文，按传入清单重算摘要并核对。"""
        if manifest is None or not checksum:
            if version is not None:
                receipt = registry.receipt(batch_id, version)
            else:
                receipt = registry.active_receipt(batch_id) or registry.latest_receipt(batch_id)
            if receipt is None:
                raise KeyError(batch_id)
            if manifest is None:
                manifest = [
                    {
                        "source_url": row.source_url,
                        "extract_rule_id": row.extract_rule_id,
                        "fields": row.fields,
                        "valid": row.valid,
                        "missing_fields": row.missing_fields,
                    }
                    for row in receipt.rows
                ]
            if not checksum:
                checksum = receipt.checksum
        matches = validate_manifest_checksum(manifest, checksum)
        return {
            "ok": matches,
            "batch_id": batch_id,
            "checksum": checksum,
            "manifest_row_count": len(manifest),
            "message": "摘要与清单一致，可用于验收" if matches else "摘要与清单不一致",
        }

    def manifest_export(self, batch_id: str, version: int | None = None) -> dict[str, Any]:
        receipt = (
            registry.receipt(batch_id, version) if version is not None
            else (registry.active_receipt(batch_id) or registry.latest_receipt(batch_id))
        )
        if receipt is None:
            raise KeyError(batch_id)
        return {
            "batch_id": batch_id,
            "version": receipt.version,
            "status": receipt.status,
            "checksum": receipt.checksum,
            "judgement_scope": receipt.judgement_scope,
            "rows": [
                {
                    "source_url": row.source_url,
                    "extract_rule_id": row.extract_rule_id,
                    "fields": row.fields,
                    "valid": row.valid,
                    "missing_fields": row.missing_fields,
                    "raw_fields": row.raw_fields,
                    "field_sources": row.field_sources,
                }
                for row in receipt.rows
            ],
        }

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return None, message
        batch_id = str(entry.get("批次编号", ""))
        if action == "触发校验" and batch_id:
            receipt = self.revalidate(batch_id, note="从批次列表触发校验")
            entry["校验摘要"] = receipt["checksum"][:16]
            return entry, f"已生成候选回执 {receipt['receipt_id']}，待确认"
        return entry, message


__all__ = ["BatchStoreService", "LEGACY_BATCH_ID", "NOT_STORABLE", "SUPERSEDED",
           "CONFIRMED"]
