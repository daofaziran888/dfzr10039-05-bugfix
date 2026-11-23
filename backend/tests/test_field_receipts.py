"""字段校验回执与批次版本仲裁测试：0/False 合法、缺失溯源、并发确认唯一版本。"""
from __future__ import annotations

import copy
import threading
import uuid

import pytest

from app.integration import _apply_clean_rules, recover_failed_batch
from app.models import (
    CleanRule, CleanType, CleanedContent, RawExtractedContent, StorageBatch,
    is_missing, legacy_fields_digest, type_name,
)
from app.receipts import (
    CANDIDATE, CONFIRMED, CONFLICT, LEGACY_BATCH_ID, NOT_STORABLE, SUPERSEDED,
    BatchReceiptRegistry, build_receipt, registry, validate_manifest_checksum,
)
from app.services.batch_store import BatchStoreService
from app.services.field_clean import FieldCleanService
from app.store import store


# ---------------------------------------------------------------------------
# 值语义：0 / False 不能当成缺失
# ---------------------------------------------------------------------------

def test_is_missing_distinguishes_falsey_valid_values() -> None:
    assert is_missing(None) is True
    assert is_missing("") is True
    assert is_missing("   ") is True
    assert is_missing([]) is True
    assert is_missing({}) is True

    assert is_missing(0) is False
    assert is_missing(0.0) is False
    assert is_missing(False) is False
    assert is_missing("0") is False


def test_cleaned_content_valid_with_zero_and_false() -> None:
    record = CleanedContent(
        source_url="u1", extract_rule_id="R1",
        fields={"金额": 0, "上架标志": False, "备注": "ok"},
    )
    assert record.compute_missing_fields() == []
    assert record.is_valid() is True

    empty = CleanedContent(
        source_url="u2", extract_rule_id="R1", fields={"金额": "", "上架标志": False},
    )
    assert empty.compute_missing_fields() == ["金额"]
    assert empty.is_valid() is False


def test_type_preserved_by_cast() -> None:
    rules = [
        CleanRule(rule_id="R-A", target_field="金额", clean_type=CleanType.TYPE_CAST,
                  expression="float"),
        CleanRule(rule_id="R-B", target_field="上架标志", clean_type=CleanType.TYPE_CAST,
                  expression="bool"),
    ]
    cleaned = _apply_clean_rules(
        RawExtractedContent(
            source_url="u", extract_rule_id="E1",
            raw_fields={"金额": "0", "上架标志": "false"},
        ),
        rules,
    )
    assert cleaned.fields["金额"] == 0.0
    assert isinstance(cleaned.fields["金额"], float)
    assert cleaned.fields["上架标志"] is False
    assert cleaned.is_valid() is True
    assert cleaned.raw_fields == {"金额": "0", "上架标志": "false"}


def test_missing_field_keeps_raw_input_and_source() -> None:
    cleaned = _apply_clean_rules(
        RawExtractedContent(
            source_url="u", extract_rule_id="E9",
            raw_fields={"金额": "", "上架标志": "false"},
        ),
        [CleanRule(rule_id="R-A", target_field="金额", clean_type=CleanType.TRIM)],
    )
    assert cleaned.missing_fields == ["金额"]
    assert cleaned.raw_fields["金额"] == ""
    assert cleaned.field_sources["金额"].startswith("raw:")
    assert cleaned.field_sources["上架标志"].startswith("raw:")


# ---------------------------------------------------------------------------
# 字段清洗页面复核
# ---------------------------------------------------------------------------

def test_field_review_three_rows() -> None:
    service = FieldCleanService()
    result = service.review_inputs([
        {"金额": 0, "上架标志": False, "备注": "零元赠品"},
        {"金额": 12.5, "上架标志": "false", "备注": "常规商品"},
        {"金额": "", "上架标志": True, "备注": "待补价商品"},
    ])
    rows = result["rows"]
    assert rows[0]["valid"] is True
    assert rows[0]["fields"]["金额"] == {"value": 0.0, "value_type": "float"}
    assert rows[0]["fields"]["上架标志"]["value"] is False
    assert rows[0]["fields"]["上架标志"]["value_type"] == "bool"

    assert rows[1]["valid"] is True
    assert rows[1]["fields"]["上架标志"]["value"] is False

    assert rows[2]["valid"] is False
    assert rows[2]["missing_fields"] == ["金额"]
    assert rows[2]["raw_fields"]["金额"] == ""
    assert rows[2]["input"] == {"金额": "", "上架标志": True, "备注": "待补价商品"}

    assert result["valid_rows"] == 2
    assert result["missing_row_count"] == 1
    assert result["storable"] is False


def test_field_review_history_appends() -> None:
    service = FieldCleanService()
    before = len(service.review_history())
    service.review_inputs([{"金额": 0}])
    assert len(service.review_history()) == before + 1


# ---------------------------------------------------------------------------
# 回执摘要：0 与 False 不碰撞，真实缺失会改变摘要
# ---------------------------------------------------------------------------

def test_manifest_checksum_is_independently_verifiable() -> None:
    rows = [
        {"source_url": "u1", "extract_rule_id": "R1",
         "fields": {"金额": 0, "上架标志": False}, "valid": True, "missing_fields": []},
        {"source_url": "u2", "extract_rule_id": "R1",
         "fields": {"金额": "", "上架标志": True}, "valid": False, "missing_fields": ["金额"]},
    ]
    receipt = build_receipt("BATCH-X", 1, "review", [
        CleanedContent(source_url=row["source_url"], extract_rule_id=row["extract_rule_id"],
                       fields=row["fields"], missing_fields=row["missing_fields"])
        for row in rows
    ])
    assert validate_manifest_checksum(rows, receipt.checksum) is True

    tampered = copy.deepcopy(rows)
    tampered[0]["fields"]["上架标志"] = "False"
    assert validate_manifest_checksum(tampered, receipt.checksum) is False

    tampered_zero = copy.deepcopy(rows)
    tampered_zero[0]["fields"]["金额"] = ""
    assert validate_manifest_checksum(tampered_zero, receipt.checksum) is False


def test_checksum_differs_for_zero_versus_empty() -> None:
    zero = build_receipt("B", 1, "review", [CleanedContent(
        source_url="u", extract_rule_id="R", fields={"金额": 0})])
    empty = build_receipt("B", 1, "review", [CleanedContent(
        source_url="u", extract_rule_id="R", fields={"金额": ""},
        missing_fields=["金额"])])
    assert zero.checksum != empty.checksum


def test_legacy_checksum_still_verifiable() -> None:
    records = [CleanedContent(source_url="u", extract_rule_id="R",
                              fields={"金额": 0, "上架标志": False})]
    batch = StorageBatch(batch_id="B", source_job_id="J", records=records)
    legacy = legacy_fields_digest([{"fields": r.fields} for r in records])
    assert batch.compute_legacy_checksum() == legacy
    assert batch.compute_checksum() != legacy


# ---------------------------------------------------------------------------
# 恢复失败批次：只补真实空项
# ---------------------------------------------------------------------------

def test_recover_keeps_zero_and_false() -> None:
    batch = StorageBatch(
        batch_id="BATCH-REC-2", source_job_id="J", row_count=2, status="校验失败",
        records=[
            CleanedContent(source_url="u1", extract_rule_id="R",
                           fields={"金额": 0, "上架标志": False, "备注": "ok"}),
            CleanedContent(source_url="u2", extract_rule_id="R",
                           fields={"金额": "", "上架标志": True}, missing_fields=["金额"]),
        ],
    )
    recovered = recover_failed_batch(batch)
    assert recovered is not None
    assert recovered.status == "校验通过"
    assert recovered.records[0].fields["金额"] == 0
    assert recovered.records[0].fields["上架标志"] is False
    assert recovered.records[1].fields["金额"] == "recovered:金额"
    assert all(record.is_valid() for record in recovered.records)


# ---------------------------------------------------------------------------
# 历史失败批次 + 重验 / 来源更正 并发确认
# ---------------------------------------------------------------------------

@pytest.fixture()
def service() -> BatchStoreService:
    return BatchStoreService()


def _fresh_legacy_copy() -> str:
    """复制历史批次到独立 batch_id，避免测试之间互相影响。"""
    template = registry.record_set(LEGACY_BATCH_ID)
    assert template is not None
    batch_id = f"BATC-TEST-{uuid.uuid4().hex[:8]}"
    records = copy.deepcopy(template.records)
    registry.register_batch(
        batch_id, records,
        source_job_id=template.source_job_id,
        storage_path=template.storage_path,
        stored_legacy_checksum=template.stored_legacy_checksum,
        source_revision=template.source_revision,
    )
    store.rows("batch_store").append({
        "id": 9000 + len(store.rows("batch_store")),
        "批次编号": batch_id,
        "来源作业": template.source_job_id,
        "数据行数": len(records),
        "存储路径": template.storage_path,
        "压缩格式": "gzip",
        "Schema版本": "v1",
        "校验摘要": template.stored_legacy_checksum,
        "历史校验摘要": template.stored_legacy_checksum,
        "入库时间": "2026-11-01T08:00:00+00:00",
        "status": "校验失败",
        "pending": True,
        "abnormal": True,
    })
    registry.add_receipt(build_receipt(
        batch_id, 1, "legacy", records,
        stored_legacy_checksum=template.stored_legacy_checksum,
        source_revision=template.source_revision,
    ))
    return batch_id


def test_legacy_batch_detail_reports_missing_with_source(service: BatchStoreService) -> None:
    detail = service.batch_detail(LEGACY_BATCH_ID)
    assert detail["historical_checksum_matches"] is True
    assert detail["invalid_rows"] == 1
    row2 = detail["manifest"][1]
    assert row2["missing_fields"] == ["金额"]
    assert row2["raw_fields"]["金额"] == ""
    assert row2["field_sources"]["金额"].startswith("raw:")
    assert detail["manifest"][0]["fields"]["金额"]["value"] == 0
    assert detail["manifest"][0]["fields"]["上架标志"]["value"] is False
    assert detail["acceptance_ready"] is False


def test_revalidate_zero_is_legal_but_real_missing_blocks(service: BatchStoreService) -> None:
    batch_id = _fresh_legacy_copy()
    receipt = service.revalidate(batch_id)
    assert receipt["valid"] is False
    assert receipt["status"] != CANDIDATE
    assert receipt["rows"][0]["valid"] is True
    assert receipt["rows"][1]["missing_fields"] == ["金额"]
    confirm = service.confirm(batch_id, receipt["version"])
    assert confirm["ok"] is False
    assert "真实空项" in confirm["message"]


def test_correction_and_revalidate_race_only_one_confirmed(service: BatchStoreService) -> None:
    batch_id = _fresh_legacy_copy()
    revalidated = service.revalidate(batch_id)
    corrected = service.source_correction(
        batch_id, field="金额", value=0, value_type="int",
        source_revision="rev-2026-11-20", row_index=2,
    )
    assert corrected["valid"] is True
    assert corrected["rows"][1]["fields"]["金额"] == {"value": 0, "value_type": "int"}
    assert corrected["rows"][1]["missing_fields"] == []

    # 并发：重验与来源更正同时确认，只能有一个成功
    results: list[dict] = []
    barrier = threading.Barrier(2)

    def confirm(version: int) -> None:
        barrier.wait()
        results.append(service.confirm(batch_id, version, expected_base_version=0))

    threads = [
        threading.Thread(target=confirm, args=(revalidated["version"],)),
        threading.Thread(target=confirm, args=(corrected["version"],)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    ok_results = [item for item in results if item["ok"]]
    failed_results = [item for item in results if not item["ok"]]
    assert len(ok_results) == 1, results
    assert len(failed_results) == 1
    winner = ok_results[0]
    assert winner["version"] == corrected["version"]
    assert winner["receipt"]["status"] == CONFIRMED
    loser = failed_results[0]
    assert "只允许一个可入库版本" in loser["message"] or "不可入库" in loser["message"]

    history = service.receipt_history(batch_id)
    assert history["active_version"] == corrected["version"]
    statuses = {item["version"]: item["status"] for item in history["receipts"]}
    assert statuses[corrected["version"]] == CONFIRMED
    # 重验版本如果先被确认过，现在应被取代；否则保留冲突/不可入库状态，历史都可查
    assert statuses[revalidated["version"]] in (SUPERSEDED, NOT_STORABLE, CONFLICT)
    assert len(history["receipts"]) >= 3


def test_confirm_is_idempotent(service: BatchStoreService) -> None:
    batch_id = _fresh_legacy_copy()
    service.source_correction(batch_id, field="金额", value=0, value_type="int",
                              source_revision="rev-1", row_index=2)
    latest = service.receipt_history(batch_id)["receipts"][-1]
    first = service.confirm(batch_id, latest["version"], idempotency_key="idem-1")
    second = service.confirm(batch_id, latest["version"], idempotency_key="idem-1")
    assert first["ok"] is True and second["ok"] is True
    assert "幂等" in second["message"] or second["version"] == first["version"]


def test_acceptance_only_after_confirmation(service: BatchStoreService) -> None:
    batch_id = _fresh_legacy_copy()
    detail = service.batch_detail(batch_id)
    assert detail["acceptance_ready"] is False
    receipt = service.source_correction(batch_id, field="金额", value=0,
                                        value_type="int", row_index=2,
                                        source_revision="rev-2")
    detail = service.batch_detail(batch_id)
    assert detail["acceptance_ready"] is False
    confirm = service.confirm(batch_id, receipt["version"])
    assert confirm["ok"] is True
    detail = service.batch_detail(batch_id)
    assert detail["acceptance_ready"] is True
    assert detail["acceptance_receipt_id"] == receipt["receipt_id"]
    assert detail["acceptance_checksum"] == receipt["checksum"]
    verify = service.verify_manifest(batch_id)
    assert verify["ok"] is True


def test_stale_correction_rejected(service: BatchStoreService) -> None:
    batch_id = _fresh_legacy_copy()
    first = service.source_correction(batch_id, field="金额", value=0,
                                      value_type="int", row_index=2,
                                      source_revision="rev-a")
    assert service.confirm(batch_id, first["version"])["ok"] is True
    # 基于已过期基版本（0）的新候选，不得再抢占确认
    stale = service.source_correction(batch_id, field="金额", value=9,
                                      value_type="int", row_index=2,
                                      source_revision="rev-b")
    result = service.confirm(batch_id, stale["version"], expected_base_version=0)
    assert result["ok"] is False
    assert "过期" in result["message"] or "只允许一个" in result["message"]


def test_source_correction_records_traceability(service: BatchStoreService) -> None:
    batch_id = _fresh_legacy_copy()
    receipt = service.source_correction(
        batch_id, field="金额", value=0, value_type="int",
        source_revision="rev-trace", source_url="https://shop.example.com/item/1002",
        note="来源二次确认",
    )
    assert receipt["source_revision"] == "rev-trace"
    assert "correction:rev-trace" in receipt["rows"][1]["field_sources"]["金额"]
    corrections = registry.corrections(batch_id)
    assert corrections[-1].field == "金额"
    assert corrections[-1].source_revision == "rev-trace"
    assert corrections[-1].old_value == ""


def test_confirm_requires_valid_receipt(service: BatchStoreService) -> None:
    batch_id = _fresh_legacy_copy()
    receipt = service.revalidate(batch_id)
    result = service.confirm(batch_id, receipt["version"])
    assert result["ok"] is False
    assert result["acceptance_ready"] is False


def test_registry_isolation_between_batches() -> None:
    local = BatchReceiptRegistry()
    receipt = local.create_receipt(
        "B1", "review",
        [CleanedContent(source_url="u", extract_rule_id="R", fields={"a": 0})],
    )
    ok, confirmed, message = local.confirm("B1", receipt.version)
    assert ok is True
    assert confirmed is not None and confirmed.status == CONFIRMED
    assert local.active_version("B1") == receipt.version
    assert "已确认" in message


def test_two_valid_candidates_race_confirms_exactly_one() -> None:
    """两个都有效的候选（重验 / 来源更正）并发确认，只能入库一个。"""
    local = BatchReceiptRegistry()
    batch_id = "BATCH-RACE-VALID"
    local.register_batch(batch_id, [CleanedContent(
        source_url="u", extract_rule_id="R", fields={"金额": 0, "上架标志": False})])
    revalidated = local.create_receipt(
        batch_id, "revalidate",
        [CleanedContent(source_url="u", extract_rule_id="R",
                        fields={"金额": 0, "上架标志": False})],
        base_version=0,
    )
    corrected = local.create_receipt(
        batch_id, "correction",
        [CleanedContent(source_url="u", extract_rule_id="R",
                        fields={"金额": 5, "上架标志": False})],
        base_version=0, source_revision="rev-x", corrected_fields=["金额"],
    )
    assert revalidated.valid and corrected.valid

    results: list[tuple[bool, object, str]] = []
    barrier = threading.Barrier(2)

    def worker(version: int) -> None:
        barrier.wait()
        results.append(local.confirm(batch_id, version))

    threads = [threading.Thread(target=worker, args=(revalidated.version,)),
               threading.Thread(target=worker, args=(corrected.version,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [item for item in results if item[0]]
    losers = [item for item in results if not item[0]]
    assert len(winners) == 1
    assert len(losers) == 1
    active = local.active_version(batch_id)
    assert active == winners[0][1].version
    assert "只允许一个可入库版本" in losers[0][2]
    statuses = {receipt.version: receipt.status for receipt in local.receipts(batch_id)}
    assert statuses[active] == CONFIRMED
    other = revalidated.version if active == corrected.version else corrected.version
    assert statuses[other] == SUPERSEDED


# ---------------------------------------------------------------------------
# 路由层：字段清洗复核 / 批次详情 / 确认 / 独立校验
# ---------------------------------------------------------------------------

def test_router_review_and_batch_endpoints() -> None:
    from app.routers import resource

    review = resource.review_field_inputs(resource.ReviewPayload(inputs=[
        {"金额": 0, "上架标志": False},
        {"金额": "", "上架标志": True},
    ]))
    assert review["valid_rows"] == 1
    assert review["missing_row_count"] == 1
    assert review["rows"][0]["fields"]["上架标志"]["value"] is False

    batch_id = _fresh_legacy_copy()
    detail = resource.batch_detail(batch_id)
    assert detail["invalid_rows"] == 1
    assert detail["acceptance_ready"] is False

    changed = resource.batch_source_correction(
        batch_id,
        resource.CorrectionPayload(
            field="金额", value=0, value_type="int", row_index=2,
            source_revision="rev-router",
        ),
    )
    confirm = resource.batch_confirm_version(
        batch_id, changed["version"], resource.ConfirmPayload())
    assert confirm["ok"] is True
    assert confirm["receipt"]["judgement_scope"]
    assert confirm["receipt"]["checksum"]

    verify = resource.batch_verify(batch_id, resource.VerifyPayload())
    assert verify["ok"] is True

    detail = resource.batch_detail(batch_id)
    assert detail["acceptance_ready"] is True
    assert detail["historical_checksum_matches"] is True


def test_router_list_and_detail_routes_coexist() -> None:
    from fastapi import HTTPException

    from app.routers import resource

    detail = resource.batch_detail(LEGACY_BATCH_ID)
    assert detail["batch"]["批次编号"] == LEGACY_BATCH_ID

    try:
        resource.batch_detail("NO-SUCH-BATCH")
    except HTTPException as exc:
        assert exc.status_code == 404
    else:  # pragma: no cover
        raise AssertionError("不存在的批次应返回 404")


def test_trigger_validation_action_creates_candidate() -> None:
    service = BatchStoreService()
    batch_id = _fresh_legacy_copy()
    row = next(item for item in store.rows("batch_store")
               if item["批次编号"] == batch_id)
    entry, message = service.run_action(int(row["id"]), "触发校验")
    assert entry is not None
    assert "候选回执" in message
    versions = [item["version"] for item in service.receipt_history(batch_id)["receipts"]]
    assert max(versions) == 2
