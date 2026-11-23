"""字段清洗领域服务：清洗规则配置 + 输入复核。

复核入口让值班员按真实输入验证规则：0 与 false 是合法值，会按字段类型保存；
空字符串才是真实空项，回执会列出缺失字段、原始输入与所用抽取规则。
"""
from __future__ import annotations

from typing import Any

from app.integration import _apply_review_rules
from app.models import CleanRule, CleanType
from app.receipts import build_row_receipts, make_uuid, registry
from app.services.base import BaseModuleService
from app.store import store

SAMPLE_SOURCE_URL = "https://field-clean.example.com/review"


class FieldCleanService(BaseModuleService):
    MODULE = "field_clean"
    ENTITY = "清洗规则"
    OPERATION_TERM = "采集"
    LIST_FIELDS = ["规则编号", "目标字段", "清洗类型", "处理表达式", "空值策略", "校验范围", "清洗顺序", "规则状态"]
    REQUIRED_FIELDS = ["规则编号", "目标字段", "清洗类型"]
    STATUS_ORDER = ["草稿", "生效中", "清洗失败", "已停用"]
    ACTION_RULES = {"启效规则": "生效中", "标记失败": "清洗失败", "停用规则": "已停用"}
    NEGATIVE_ACTIONS = ["停用规则"]

    def review_inputs(self, inputs: list[dict[str, Any]],
                      *, extract_rule_id: str = "CONT-REVIEW-001",
                      source_url: str = SAMPLE_SOURCE_URL,
                      logic: str = "soft") -> dict[str, Any]:
        """在字段清洗页面复核输入。

        ``inputs`` 每项形如 ``{"字段": 值}``；``logic`` 可选 ``strict`` / ``soft``，
        错配的规则只影响所属字段，不会被 0/False 触发。
        """
        rules = self._active_rules()
        cleaned_records = []
        raw_inputs: list[dict[str, Any]] = []
        for index, raw_input in enumerate(inputs, start=1):
            raw_fields = {str(key): value for key, value in raw_input.items()}
            cleaned_records.append(_apply_review_rules(
                raw_fields, rules,
                source_url=f"{source_url}#row-{index}",
                extract_rule_id=extract_rule_id,
            ))
            raw_inputs.append(raw_fields)

        rows: list[dict[str, Any]] = []
        for row_receipt, raw_fields in zip(
                build_row_receipts(cleaned_records), raw_inputs):
            row = row_receipt.to_dict()
            row["input"] = raw_fields
            rows.append(row)

        review = {
            "review_id": f"REVIEW-{make_uuid()}",
            "module": self.MODULE,
            "extract_rule_id": extract_rule_id,
            "source_url": source_url,
            "logic": logic,
            "rule_ids": [rule.rule_id for rule in rules],
            "validity_rule": "0/False 视为合法值；空字符串/None 才视为真实空项",
            "rows": rows,
            "valid_rows": sum(1 for row in rows if row["valid"]),
            "missing_row_count": sum(1 for row in rows if row["missing_fields"]),
            "storable": all(row["valid"] for row in rows) and bool(rows),
        }
        registry.add_field_review(review)
        return review

    def review_history(self) -> list[dict[str, Any]]:
        return registry.field_reviews()

    def _active_rules(self) -> list[CleanRule]:
        """取生效中的清洗规则；没有配置时用内置的安全默认规则。

        复核页面只关心能按类型保存的字段，因此只在「生效中」规则里挑出
        字段名明确、类型可识别的规则；其余字段按原始输入原样入清单。
        """
        rules: list[CleanRule] = []
        for row in store.rows(self.MODULE):
            if row.get("status") != "生效中":
                continue
            try:
                rule = CleanRule.from_seed_row(row)
            except (ValueError, TypeError):
                continue
            if not rule.target_field or rule.target_field.startswith("字段清洗样例"):
                continue
            rules.append(rule)
        if rules:
            return rules
        return list(DEFAULT_REVIEW_RULES)


# 复核页面的内置规则：覆盖金额（数值）、上架标志（布尔）、备注（文本）三类字段
DEFAULT_REVIEW_RULES: list[CleanRule] = [
    CleanRule(rule_id="FIEL-REVIEW-AMOUNT", target_field="金额",
              clean_type=CleanType.TYPE_CAST, expression="float",
              null_strategy="skip", validation_range=r"^\d+(\.\d+)?$", clean_order=1),
    CleanRule(rule_id="FIEL-REVIEW-FLAG", target_field="上架标志",
              clean_type=CleanType.TYPE_CAST, expression="bool",
              null_strategy="skip", clean_order=2),
    CleanRule(rule_id="FIEL-REVIEW-NOTE", target_field="备注",
              clean_type=CleanType.TRIM, expression="",
              null_strategy="skip", clean_order=3),
]


__all__ = ["FieldCleanService", "DEFAULT_REVIEW_RULES"]
