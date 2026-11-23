"""字段校验回执与批次版本仲裁。

这里承载三类真实业务约束：

1. 值语义：``0`` 与 ``False`` 是合法值，``None`` / 空字符串才是真实空项
   （判定函数在 :mod:`app.models` 的 ``is_missing``）。
2. 可追溯：每条回执都记录每个字段的来源（原始抽取规则或清洗规则），真实
   空项要列在 ``missing_fields`` 里，并保留原始输入 ``raw_fields``。
3. 一次只确认一个可入库版本：重验与来源更正会各自生成候选回执，确认动作
   在锁内完成「幂等 / 乐观基版本 / 唯一有效版本」三重校验，失败的一方保留
   在历史里，仍可查询冲突原因。
"""
from __future__ import annotations

import copy
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.models import (
    CleanedContent, CorrectionRecord, FieldReceipt, RowReceipt,
    ValidationReceipt, is_missing, legacy_fields_digest, manifest_digest,
    type_name,
)

CHECKSUM_ALGORITHM = "manifest-v2/sha256(含字段类型标签,可独立校验)"
VALIDITY_RULE = "0/False 视为合法值；None/空字符串/空容器视为真实缺失"

# 确认结果枚举
CONFIRMED = "已确认"
CANDIDATE = "候选"
NOT_STORABLE = "不可入库"
SUPERSEDED = "被取代"
CONFLICT = "确认冲突"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _receipt_id(batch_id: str, version: int) -> str:
    return f"RCPT-{batch_id}-V{version}"


@dataclass
class BatchRecordSet:
    """一个批次的当前记录与来源信息，供重验 / 更正反复取数。"""
    batch_id: str
    source_job_id: str = ""
    records: list[CleanedContent] = field(default_factory=list)
    storage_path: str = ""
    schema_version: str = "v1"
    stored_legacy_checksum: str = ""
    source_revision: str = ""
    legacy_valid: bool | None = None
    created_at: str = field(default_factory=_now)
    # 首次登记时的原始记录快照，用于核对历史批次已确认的摘要
    legacy_records: list[CleanedContent] = field(default_factory=list)


def build_row_receipts(records: list[CleanedContent]) -> list[RowReceipt]:
    """按字段校验回执逐行核查有效性、失败原因与缺失字段来源。"""
    rows: list[RowReceipt] = []
    for index, record in enumerate(records):
        missing = list(dict.fromkeys(
            record.missing_fields or record.compute_missing_fields()))
        errors = list(record.clean_errors)
        field_receipts: list[FieldReceipt] = []
        for name, value in record.fields.items():
            source = record.field_sources.get(name) or f"raw:{record.extract_rule_id}"
            if name in missing:
                status = "missing"
                reason = f"字段 {name} 缺失（来源 {source}）"
            else:
                field_errors = [item for item in errors
                                if item.startswith(f"字段 {name} ")]
                status = "invalid" if field_errors else "valid"
                reason = field_errors[0] if field_errors else ""
            field_receipts.append(FieldReceipt(
                field=name,
                value=value,
                value_type=type_name(value),
                status=status,
                source=source,
                reason=reason,
            ))
        # 清洗错误可能不指向具体字段，单独列出
        unattached = [item for item in errors
                      if not any(item.startswith(f"字段 {name}") for name in record.fields)]
        reasons = list(dict.fromkeys([*record.invalid_reasons(), *unattached]))
        rows.append(RowReceipt(
            row_index=index + 1,
            source_url=record.source_url,
            extract_rule_id=record.extract_rule_id,
            valid=record.is_valid(),
            fields=dict(record.fields),
            raw_fields=dict(record.raw_fields),
            missing_fields=missing,
            reasons=reasons,
            field_receipts=field_receipts,
            digest=record.row_digest(),
            field_sources=dict(record.field_sources),
        ))
    return rows


def _judgement_scope(batch_id: str, rows: list[RowReceipt], source_revision: str,
                     base_version: int) -> list[str]:
    fields: list[str] = []
    for row in rows:
        for field_receipt in row.field_receipts:
            if field_receipt.field not in fields:
                fields.append(field_receipt.field)
    scope = [
        f"batch:{batch_id}",
        f"rows:1-{len(rows)}" if rows else "rows:0",
        f"fields:{','.join(fields)}" if fields else "fields:",
        f"validity:{VALIDITY_RULE}",
        f"checksum:{CHECKSUM_ALGORITHM}",
        f"legacy_checksum:sha256(原始fields)/16位前缀",
        f"base_version:{base_version}",
    ]
    if source_revision:
        scope.append(f"source_revision:{source_revision}")
    return scope


def build_receipt(
    batch_id: str,
    version: int,
    kind: str,
    records: list[CleanedContent],
    *,
    stored_legacy_checksum: str = "",
    source_revision: str = "",
    base_version: int = 0,
    corrected_fields: list[str] | None = None,
    note: str = "",
    idempotency_key: str = "",
) -> ValidationReceipt:
    """构造一份候选回执；摘要与判断范围都由同一批记录计算得出。"""
    rows = build_row_receipts(records)
    checksum = manifest_digest([
        {
            "source_url": row.source_url,
            "extract_rule_id": row.extract_rule_id,
            "fields": row.fields,
            "valid": row.valid,
            "missing_fields": row.missing_fields,
        }
        for row in rows
    ])
    legacy = legacy_fields_digest([{"fields": row.fields} for row in rows])
    valid = all(row.valid for row in rows)
    status = CANDIDATE if valid else NOT_STORABLE
    return ValidationReceipt(
        receipt_id=_receipt_id(batch_id, version),
        batch_id=batch_id,
        version=version,
        kind=kind,
        status=status,
        valid=valid,
        checksum=checksum,
        legacy_checksum=legacy,
        stored_legacy_checksum=stored_legacy_checksum,
        judgement_scope=_judgement_scope(batch_id, rows, source_revision, base_version),
        rows=rows,
        source_revision=source_revision,
        idempotency_key=idempotency_key,
        base_version=base_version,
        corrected_fields=list(corrected_fields or []),
        note=note,
    )


class BatchReceiptRegistry:
    """批次记录 + 回执版本的内存注册表（真实项目替换为带事务的存储层）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._batches: dict[str, BatchRecordSet] = {}
        self._receipts: dict[str, dict[int, ValidationReceipt]] = {}
        self._active: dict[str, int] = {}
        self._corrections: dict[str, list[CorrectionRecord]] = {}
        self._confirmations: dict[str, str] = {}
        self._field_reviews: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 批次记录
    # ------------------------------------------------------------------
    def register_batch(
        self,
        batch_id: str,
        records: list[CleanedContent],
        *,
        source_job_id: str = "",
        storage_path: str = "",
        schema_version: str = "v1",
        stored_legacy_checksum: str = "",
        source_revision: str = "",
    ) -> BatchRecordSet:
        with self._lock:
            existing = self._batches.get(batch_id)
            if existing is None:
                existing = BatchRecordSet(
                    batch_id=batch_id,
                    source_job_id=source_job_id,
                    records=list(records),
                    storage_path=storage_path,
                    schema_version=schema_version,
                    stored_legacy_checksum=stored_legacy_checksum,
                    source_revision=source_revision,
                    legacy_records=copy.deepcopy(records),
                )
                self._batches[batch_id] = existing
            else:
                existing.records = list(records)
                existing.source_job_id = source_job_id or existing.source_job_id
                existing.storage_path = storage_path or existing.storage_path
                existing.schema_version = schema_version or existing.schema_version
                existing.stored_legacy_checksum = (
                    stored_legacy_checksum or existing.stored_legacy_checksum)
                existing.source_revision = source_revision or existing.source_revision
            return existing

    def record_set(self, batch_id: str) -> BatchRecordSet | None:
        return self._batches.get(batch_id)

    def records(self, batch_id: str) -> list[CleanedContent]:
        record_set = self._batches.get(batch_id)
        return list(record_set.records) if record_set else []

    def replace_records(self, batch_id: str, records: list[CleanedContent]) -> None:
        record_set = self._batches[batch_id]
        record_set.records = list(records)

    # ------------------------------------------------------------------
    # 回执版本
    # ------------------------------------------------------------------
    def next_version(self, batch_id: str) -> int:
        versions = self._receipts.get(batch_id, {})
        return max(versions, default=0) + 1

    def add_receipt(self, receipt: ValidationReceipt) -> ValidationReceipt:
        with self._lock:
            self._receipts.setdefault(receipt.batch_id, {})[receipt.version] = receipt
            return receipt

    def create_receipt(self, batch_id: str, kind: str, records: list[CleanedContent],
                       **kwargs: Any) -> ValidationReceipt:
        with self._lock:
            record_set = self._batches.get(batch_id)
            version = self.next_version(batch_id)
            stored_legacy = kwargs.pop(
                "stored_legacy_checksum",
                record_set.stored_legacy_checksum if record_set else "",
            )
            receipt = build_receipt(
                batch_id, version, kind, records,
                stored_legacy_checksum=stored_legacy, **kwargs)
            return self.add_receipt(receipt)

    def receipts(self, batch_id: str) -> list[ValidationReceipt]:
        versions = self._receipts.get(batch_id, {})
        return [versions[key] for key in sorted(versions)]

    def receipt(self, batch_id: str, version: int) -> ValidationReceipt | None:
        return self._receipts.get(batch_id, {}).get(version)

    def latest_receipt(self, batch_id: str) -> ValidationReceipt | None:
        versions = self._receipts.get(batch_id, {})
        if not versions:
            return None
        return versions[max(versions)]

    def active_version(self, batch_id: str) -> int:
        return self._active.get(batch_id, 0)

    def active_receipt(self, batch_id: str) -> ValidationReceipt | None:
        version = self._active.get(batch_id)
        return self.receipt(batch_id, version) if version else None

    # ------------------------------------------------------------------
    # 确认仲裁
    # ------------------------------------------------------------------
    def confirm(
        self,
        batch_id: str,
        version: int,
        *,
        expected_base_version: int | None = None,
        idempotency_key: str = "",
        operator: str = "值班员",
    ) -> tuple[bool, ValidationReceipt | None, str]:
        """在锁内确认唯一可入库版本。

        返回 ``(ok, receipt, message)``；冲突或不可入库时 ``ok=False``，
        ``receipt`` 仍返回候选回执，便于调用方带出判断范围与摘要。
        """
        with self._lock:
            target = self.receipt(batch_id, version)
            if target is None:
                return False, None, f"回执版本 V{version} 不存在"

            if idempotency_key:
                confirmed_key = self._confirmations.get(idempotency_key)
                if confirmed_key:
                    recorded_batch, recorded_version, _ = confirmed_key.split(":", 2)
                    if recorded_batch == batch_id and recorded_version == str(version):
                        return True, target, "重复确认请求，已按幂等键返回原确认结果"
                    return False, target, "幂等键已用于其它回执版本，拒绝重复确认"

            if self._active.get(batch_id) == version:
                return True, target, f"回执 V{version} 已是当前唯一有效版本，无需重复确认"

            if not target.valid:
                missing = sorted({
                    f"{row.row_index}:{name}"
                    for row in target.rows for name in row.missing_fields
                })
                detail = f"；真实空项 {', '.join(missing)}" if missing else ""
                return False, target, f"回执 V{version} 不可入库：仍存在无效行{detail}"

            active = self._active.get(batch_id, 0)
            if active and active != version:
                active_receipt = self.receipt(batch_id, active)
                return False, target, (
                    f"批次已确认 V{active}（{active_receipt.receipt_id if active_receipt else ''}），"
                    f"本次 V{version} 被拒绝：同一批次只允许一个可入库版本"
                )

            base = expected_base_version if expected_base_version is not None else target.base_version
            if active and base and base != active:
                return False, target, (
                    f"候选版本基于 V{base}，已确认版本是 V{active}，判定为过期更正"
                )

            target.status = CONFIRMED
            target.confirmed_at = _now()
            target.confirmed_by = operator
            target.superseded_by = None
            self._active[batch_id] = version
            for other_version, other in self._receipts.get(batch_id, {}).items():
                if other_version == version or other.status == CONFIRMED:
                    continue
                # 无效候选保留「不可入库」，有效但未选中的候选标记为被取代
                if other.valid:
                    other.status = SUPERSEDED
                    other.superseded_by = version
            if idempotency_key:
                self._confirmations[idempotency_key] = (
                    f"{batch_id}:{version}:{target.confirmed_at}")
            return True, target, f"已确认回执 {target.receipt_id} 为唯一可入库版本"

    def mark_conflict(self, receipt: ValidationReceipt, reason: str) -> None:
        with self._lock:
            if receipt.status != CONFIRMED:
                receipt.status = CONFLICT
            receipt.note = "；".join(filter(None, [receipt.note, reason]))

    # ------------------------------------------------------------------
    # 来源更正
    # ------------------------------------------------------------------
    def add_correction(self, correction: CorrectionRecord) -> CorrectionRecord:
        with self._lock:
            self._corrections.setdefault(correction.batch_id, []).append(correction)
            return correction

    def corrections(self, batch_id: str) -> list[CorrectionRecord]:
        return list(self._corrections.get(batch_id, []))

    # ------------------------------------------------------------------
    # 字段清洗页面复核历史
    # ------------------------------------------------------------------
    def add_field_review(self, review: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._field_reviews.append(review)
            return review

    def field_reviews(self) -> list[dict[str, Any]]:
        return list(self._field_reviews)


registry = BatchReceiptRegistry()


# ---------------------------------------------------------------------------
# 历史批次种子：旧规则把金额 0 误判缺失，批次因此整批校验失败
# ---------------------------------------------------------------------------

LEGACY_BATCH_ID = "BATC-20261101-OLD"


def seed_demo_data(store: Any) -> None:
    """注入一个「旧规则失败」的历史批次，保留原始摘要与抽取来源。"""
    records = [
        CleanedContent(
            source_url="https://shop.example.com/item/1001",
            extract_rule_id="CONT-HIST-001",
            fields={"商品名称": "零元赠品", "金额": 0, "上架标志": False},
            raw_fields={"商品名称": "零元赠品", "金额": "0", "上架标志": "false"},
            field_sources={
                "商品名称": "raw:CONT-HIST-001",
                "金额": "raw:CONT-HIST-001",
                "上架标志": "raw:CONT-HIST-001",
            },
        ),
        CleanedContent(
            source_url="https://shop.example.com/item/1002",
            extract_rule_id="CONT-HIST-001",
            fields={"商品名称": "待补价商品", "金额": "", "上架标志": True},
            raw_fields={"商品名称": "待补价商品", "金额": "", "上架标志": "true"},
            missing_fields=["金额"],
            field_sources={
                "商品名称": "raw:CONT-HIST-001",
                "金额": "raw:CONT-HIST-001",
                "上架标志": "raw:CONT-HIST-001",
            },
        ),
        CleanedContent(
            source_url="https://shop.example.com/item/1003",
            extract_rule_id="CONT-HIST-001",
            fields={"商品名称": "普通商品", "金额": 25.5, "上架标志": False},
            raw_fields={"商品名称": "普通商品", "金额": "25.5", "上架标志": "false"},
            field_sources={
                "商品名称": "raw:CONT-HIST-001",
                "金额": "raw:CONT-HIST-001",
                "上架标志": "raw:CONT-HIST-001",
            },
        ),
    ]
    legacy_checksum = legacy_fields_digest([{"fields": record.fields} for record in records])
    rows = store.rows("batch_store")
    next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
    rows.append({
        "id": next_id,
        "批次编号": LEGACY_BATCH_ID,
        "来源作业": "CRAW-JOB-HIST",
        "数据行数": len(records),
        "存储路径": "/data/batches/CRAW-JOB-HIST/",
        "压缩格式": "gzip",
        "Schema版本": "v1",
        "校验摘要": legacy_checksum[:16],
        # 历史批次按旧规则确认过的摘要，后续重验/更正不能覆盖它
        "历史校验摘要": legacy_checksum[:16],
        "入库时间": "2026-11-01T08:00:00+00:00",
        "status": "校验失败",
        "pending": True,
        "abnormal": True,
    })
    registry.register_batch(
        LEGACY_BATCH_ID,
        records,
        source_job_id="CRAW-JOB-HIST",
        storage_path="/data/batches/CRAW-JOB-HIST/",
        stored_legacy_checksum=legacy_checksum[:16],
        source_revision="rev-2026-11-01",
    )
    initial = build_receipt(
        LEGACY_BATCH_ID, 1, "legacy", records,
        stored_legacy_checksum=legacy_checksum[:16],
        source_revision="rev-2026-11-01",
        note="旧规则将金额 0 误判为缺失，整批判定失败",
    )
    initial.status = NOT_STORABLE
    registry.add_receipt(initial)


def seed_store(store: Any) -> None:
    seed_demo_data(store)


__all__ = [
    "BatchReceiptRegistry", "BatchRecordSet", "CHECKSUM_ALGORITHM",
    "CANDIDATE", "CONFIRMED", "CONFLICT", "LEGACY_BATCH_ID", "NOT_STORABLE",
    "SUPERSEDED", "VALIDITY_RULE", "build_receipt", "build_row_receipts",
    "make_uuid", "registry", "seed_demo_data", "seed_store",
    "validate_manifest_checksum",
]


def validate_manifest_checksum(manifest: list[dict[str, Any]], checksum: str) -> bool:
    """脱离批次上下文，仅凭清单重算并核对摘要。"""
    computed = manifest_digest([
        {
            "source_url": row.get("source_url", ""),
            "extract_rule_id": row.get("extract_rule_id", ""),
            "fields": row.get("fields", {}),
            "valid": bool(row.get("valid", True)),
            "missing_fields": row.get("missing_fields", []),
        }
        for row in manifest
    ])
    return computed == checksum


def make_uuid(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"
