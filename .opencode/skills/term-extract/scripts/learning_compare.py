#!/usr/bin/env python3
"""可信标签对照：按采购对象比较，并把无法由定位证明的语义差异留给局部复核。

这里不运行模型。标签范围不是逐字关键参数标注，范围变化不会直接判定业务错误。
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from typing import Any

import validate_result as result_validator


SUMMARY_KEYS = ("missing_objects", "extra_objects", "parameter_omissions", "parameter_pollution",
                "quantity_errors", "association_errors", "ambiguous_differences", "harmless_differences")
MAX_CONTEXT_CHARS = 24000
MAX_SNIPPET_CHARS = 600
MAX_DIFFERENCE_TAGS = 80


class ComparisonError(ValueError):
    """输入或局部复核结果不合法。"""


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _normalize_ids(records: Any, name: str) -> list[dict]:
    if not isinstance(records, list):
        raise ComparisonError(f"{name} 必须是条款数组。")
    ids = [record.get("id") if isinstance(record, dict) else None for record in records]
    if any(type(value) is not int for value in ids) or len(set(ids)) != len(ids):
        raise ComparisonError(f"{name} 的条款 id 必须是互不重复的整数。")
    mapped = dict(zip(ids, range(len(ids))))
    result = copy.deepcopy(records)
    for record in result:
        record["id"] = mapped[record["id"]]
        if isinstance(record.get("相关条款"), list):
            if any(type(value) is not int or value not in mapped for value in record["相关条款"]):
                raise ComparisonError(f"{name} 包含不存在或非法的相关条款 id。")
            record["相关条款"] = [mapped[value] for value in record["相关条款"]]
    return result


def _validated(index: dict, records: Any, name: str) -> list[dict]:
    result = _normalize_ids(records, name)
    report = result_validator.validate_result(index, result)
    if not report["ok"]:
        errors = "; ".join(f"{item['code']}: {item['message']}" for item in report["errors"][:5])
        raise ComparisonError(f"{name} 结构不合法：{errors}")
    return result


def _groups(records: list[dict], order: dict[str, int], tags: list[str], prefix: str) -> tuple[list[dict], dict[int, set[str]]]:
    by_id = {record["id"]: record for record in records}
    spans = {key: set(tags[order[value["定位标识"][0]]:order[value["定位标识"][1]] + 1])
             for key, value in by_id.items()}
    objects = {key: value for key, value in by_id.items() if value["条款分类"] != "general"}
    adjacent = {key: set() for key in objects}
    for key, value in objects.items():
        if value["条款分类"] == "separate":
            for target in value["相关条款"]:
                if target in objects:
                    adjacent[key].add(target)
                    adjacent[target].add(key)
    remaining, groups = set(objects), []
    while remaining:
        pending, component = [min(remaining)], set()
        while pending:
            key = pending.pop()
            if key not in component:
                component.add(key)
                pending.extend(adjacent[key] - component)
        remaining -= component
        general = {key for key, value in by_id.items()
                   if value["条款分类"] == "general" and set(value["相关条款"]) & component}
        own = set().union(*(spans[key] for key in component))
        effective = own | set().union(set(), *(spans[key] for key in general))
        key = prefix + "_" + _sha(sorted(own, key=order.get))[:16]
        if any(group["key"] == key for group in groups):
            key += "_duplicate_" + str(sum(1 for group in groups if group["own"] == own))
        groups.append({"key": key, "ids": sorted(component), "general_ids": sorted(general),
                       "own": own, "effective": effective,
                       "records": [by_id[value] for value in sorted(component)],
                       "general_records": [by_id[value] for value in sorted(general)]})
    return groups, spans


def _public_group(group: dict, order: dict) -> dict:
    return {"object_key": group["key"], "clause_ids": group["ids"], "general_clause_ids": group["general_ids"],
            "titles": [record["条款标题"][:160] for record in group["records"]],
            "quantities": [record["产品数量"][:160] for record in group["records"]],
            "own_tags": sorted(group["own"], key=order.get),
            "effective_tags": sorted(group["effective"], key=order.get)}


def _refresh(report: dict) -> dict:
    summary = dict.fromkeys(SUMMARY_KEYS, 0)
    for item in report["differences"]:
        summary[item["kind"]] += 1
    report["summary"] = summary
    report["status"] = ("failed" if any(item["severity"] == "error" for item in report["differences"])
                        else "needs_review" if any(item["severity"] == "review" for item in report["differences"])
                        else "passed")
    return report


def _quantity(value: str) -> tuple[str, str] | None:
    value = unicodedata.normalize("NFKC", value).strip()
    match = re.fullmatch(r"([0-9]+)\s*(台|套|个|件|只|支|块|组|路|对|张|枚|本|根)?", value)
    return (str(int(match[1])), match[2] or "") if match else None


def _quantities(group: dict) -> set[str]:
    return {unicodedata.normalize("NFKC", record["产品数量"]).strip()
            for record in group["records"] if record["产品数量"].strip() != "未提及"}


def _title_hint(first: dict, second: dict) -> bool:
    """同名只用于提出对齐疑点，绝不据此认定两个采购项相同。"""
    normalize = lambda value: re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", value).casefold())
    left = [normalize(record["条款标题"]) for record in first["records"]]
    right = [normalize(record["条款标题"]) for record in second["records"]]
    return any(a and b and (a == b or (min(len(a), len(b)) >= 4 and (a in b or b in a)))
               for a in left for b in right)


def compare_results(index: dict, prediction: list, labels: list, *, decisions=None) -> dict:
    """对完整采购对象集合进行闭合检查；定位范围差异保守地提交语义复核。"""
    predicted = _validated(index, prediction, "prediction")
    gold = _validated(index, labels, "labels")
    units = sorted(index["units"], key=lambda value: value["order"])
    tags = [unit.get("tag") or (unit.get("tags") or [None])[0] for unit in units]
    tags = [tag for tag in tags if tag is not None]
    order = {tag: position for position, tag in enumerate(tags)}
    unit_by_tag = {unit.get("tag") or (unit.get("tags") or [None])[0]: unit for unit in units}
    pgroups, pspans = _groups(predicted, order, tags, "prediction")
    ggroups, gspans = _groups(gold, order, tags, "label")
    report = {"schema_version": 1, "status": "passed", "summary": {}, "objects": [], "differences": [],
              "prediction_sha256": _sha(prediction), "labels_sha256": _sha(labels),
              "notice": "标签区间不等于逐字关键参数标注；非精确范围差异须经局部语义复核。产品分类不计分。",
              "context_budget_chars": MAX_CONTEXT_CHARS, "context_truncated": False}
    budget = [MAX_CONTEXT_CHARS]

    def add(kind, severity, message, object_key, changed_tags=(), context=None, question=""):
        ordered = sorted(set(changed_tags), key=order.get)
        identity = {"kind": kind, "object_key": object_key, "tags": ordered,
                    "issue": (context or {}).get("issue", ""), "message": message}
        local = copy.deepcopy(context or {})
        sample = ordered if len(ordered) <= 8 else ordered[:4] + ordered[-4:]
        neighboring = set()
        for tag in sample:
            position = order[tag]
            neighboring.update(tags[max(0, position - 1):position + 2])
        snippets, truncated = [], len(ordered) > MAX_DIFFERENCE_TAGS or len(sample) < len(ordered)
        for tag in sorted(neighboring, key=order.get):
            if budget[0] <= 0:
                truncated = True
                break
            unit = unit_by_tag[tag]
            text = unit.get("text", "")
            limit = min(MAX_SNIPPET_CHARS, budget[0])
            excerpt = text[:limit]
            budget[0] -= len(excerpt)
            cut = len(text) > len(excerpt)
            truncated = truncated or cut
            snippets.append({"tag": tag, "text": excerpt, "truncated": cut,
                             "block_index": unit.get("block_index"), "row": unit.get("row"), "col": unit.get("col")})
        local.update({"snippets": snippets, "context_truncated": truncated,
                      "difference_tag_count": len(ordered)})
        report["context_truncated"] = report["context_truncated"] or truncated
        report["differences"].append({"difference_id": "diff_" + _sha(identity)[:24], "kind": kind,
            "severity": severity, "message": message, "object_key": object_key,
            "tags": ordered[:MAX_DIFFERENCE_TAGS], "context": local, "question": question,
            "choices": ["harmless", "error", "uncertain"] if severity == "review" else []})

    # 用设备本体范围对齐，不能用共享 general 把不同设备连接为同一对象。
    p_hits = {p["key"]: [g for g in ggroups if p["own"] & g["own"]] for p in pgroups}
    g_hits = {g["key"]: [p for p in pgroups if p["own"] & g["own"]] for g in ggroups}
    # 两边均孤立但有同名线索时，可能只是选了同一设备的另一处片段。
    # 先交局部复核，避免把无法定位对齐的情况机械记为确定的漏项和多抽。
    fallback = {g["key"]: [p for p in pgroups if not p_hits[p["key"]] and _title_hint(p, g)]
                for g in ggroups if not g_hits[g["key"]]}
    possible_prediction = {p["key"] for values in fallback.values() for p in values}
    paired = {}
    for group in ggroups:
        hits = g_hits[group["key"]]
        if len(hits) == 1 and len(p_hits[hits[0]["key"]]) == 1:
            paired[hits[0]["key"]] = group["key"]
        possible = fallback.get(group["key"], [])
        if not hits and possible:
            add("ambiguous_differences", "review", "预测与标签的范围不相交，但存在同名采购项线索；对象对齐尚不确定。",
                group["key"], group["own"] | set().union(*(p["own"] for p in possible)),
                {"issue": "object_alignment", "error_kind": "association_errors", "label": _public_group(group, order),
                 "predictions": [_public_group(p, order) for p in possible]},
                "这些记录是同一设备的不同片段，还是存在目标设备漏抽及额外误抽？标题相同本身不足以确认。")
        elif not hits:
            add("missing_objects", "error", "标签中的采购对象没有对应抽取记录。", group["key"], group["own"],
                {"issue": "missing_object", "label": _public_group(group, order)})
        report["objects"].append({"object_key": group["key"], "label": _public_group(group, order),
                                  "prediction": [_public_group(p, order) for p in hits or possible],
                                  "alignment": "matched" if len(hits) == 1 and hits[0]["key"] in paired
                                               else "missing" if not hits and not possible else "ambiguous"})
    for group in pgroups:
        hits = p_hits[group["key"]]
        if not hits and group["key"] not in possible_prediction:
            add("extra_objects", "error", "抽取结果包含标签对象集合之外的采购对象。", group["key"], group["own"],
                {"issue": "extra_object", "prediction": _public_group(group, order)})
            report["objects"].append({"object_key": group["key"], "label": None,
                                      "prediction": [_public_group(group, order)], "alignment": "extra"})
        elif len(hits) > 1:
            member_hits = [{g["key"] for g in hits if pspans[r["id"]] & g["own"]} for r in group["records"]]
            independent = set().union(set(), *(values for values in member_hits if len(values) == 1))
            hard = len(independent) > 1
            add("association_errors" if hard else "ambiguous_differences", "error" if hard else "review",
                "separate 关联把多个标签采购对象连接成一个对象。" if hard else "一条对象范围与多个标签对象重叠，需要核对是否误合并或边界冗余。",
                group["key"], group["own"], {"issue": "merged_objects", "error_kind": "association_errors",
                    "prediction": _public_group(group, order), "labels": [_public_group(g, order) for g in hits]},
                "这些范围是否把不同采购对象错误合并，还是仅包含无害的边界描述？")
    for group in ggroups:
        hits = g_hits[group["key"]]
        if len(hits) > 1:
            add("ambiguous_differences", "review", "同一标签对象对应多个未关联的抽取对象，需要核对拆分语义。",
                group["key"], group["own"], {"issue": "split_object", "error_kind": "association_errors",
                    "label": _public_group(group, order), "predictions": [_public_group(p, order) for p in hits]},
                "这些记录是否属于同一不可拆分设备或系统，需要合并或补充 separate 关联？")

    # general 可拆成多段或部分内联；按共同 general 原文实际约束的对象集合比较。
    # 不递归 general -> general，也不因重复 general 行改变有效语义就机械报错。
    ggeneral = set().union(set(), *(gspans[r["id"]] for r in gold if r["条款分类"] == "general"))
    pgeneral = set().union(set(), *(pspans[r["id"]] for r in predicted if r["条款分类"] == "general"))
    changed_scopes = {}
    for tag in sorted(ggeneral & pgeneral, key=order.get):
        targets = [p for p in pgroups if tag in p["effective"]]
        if any(p["key"] not in paired for p in targets):
            continue
        expected = frozenset(g["key"] for g in ggroups if tag in g["effective"])
        actual = frozenset(paired[p["key"]] for p in targets)
        if expected != actual:
            changed_scopes.setdefault((expected, actual), set()).add(tag)
    for (expected, actual), changed in changed_scopes.items():
        add("association_errors", "error", "同一 general 原文的作用对象与标签不符。",
            "general_" + _sha(sorted(changed, key=order.get))[:16], changed,
            {"issue": "general_scope", "expected_objects": sorted(expected), "actual_objects": sorted(actual)})

    gold_by_key = {g["key"]: g for g in ggroups}
    for p in pgroups:
        if p["key"] not in paired:
            continue
        g = gold_by_key[paired[p["key"]]]
        for difference, issue, error_kind, message in [
            (g["effective"] - p["effective"], "range_omission", "parameter_omissions", "抽取范围少于标签；需要判断是否遗漏关键选型参数。"),
            (p["effective"] - g["effective"], "range_addition", "parameter_pollution", "抽取范围超出标签；需要判断是否混入其他设备参数或仅为无害描述。")]:
            if difference:
                add("ambiguous_differences", "review", message, g["key"], difference,
                    {"issue": issue, "error_kind": error_kind, "label": _public_group(g, order),
                     "prediction": _public_group(p, order),
                     "other_label_objects": [_public_group(other, order) for other in ggroups
                                             if other["key"] != g["key"] and other["effective"] & difference]},
                    "这部分范围差异会影响本设备选型配置吗？请选择无害、业务错误或证据不足，并说明依据。")
        expected, actual = _quantities(g), _quantities(p)
        if expected == actual:
            continue
        context = {"issue": "quantity", "error_kind": "quantity_errors", "expected": sorted(expected) or ["未提及"],
                   "actual": sorted(actual) or ["未提及"], "label": _public_group(g, order),
                   "prediction": _public_group(p, order)}
        hard, equivalent = False, False
        if len(expected) <= 1 and len(actual) <= 1:
            e = _quantity(next(iter(expected))) if expected else None
            a = _quantity(next(iter(actual))) if actual else None
            if expected and not actual and e:
                hard = True
            elif e and a:
                if e[0] != a[0] and e[1] == a[1]:
                    hard = True
                elif e[0] == a[0]:
                    if e[1] == a[1]:
                        equivalent = True
                    elif not e[1] or not a[1]:
                        unit = e[1] or a[1]
                        pattern = re.compile(r"(?<![0-9])" + re.escape(e[0]) + r"\s*" + re.escape(unit))
                        equivalent = any(pattern.search(unicodedata.normalize("NFKC", unit_by_tag[tag]["text"]))
                                         for tag in g["effective"])
        if not equivalent:
            add("quantity_errors" if hard else "ambiguous_differences", "error" if hard else "review",
                "采购数量与可信标签不符。" if hard else "数量表达或计量对象有差异，需要结合原文核对。",
                g["key"], g["effective"], context, "两种数量表达是否指同一计量对象和数量？台与套不能自动等同。")
    _refresh(report)
    if decisions is not None:
        if isinstance(decisions, dict):
            if any(not isinstance(value, dict) for value in decisions.values()):
                raise ComparisonError("decisions 映射值必须是 judgment 对象。")
            decisions = [{"difference_id": key, **value} for key, value in decisions.items()]
        report = apply_judgments(report, decisions)
    return report


def apply_judgments(report: dict, judgments: list) -> dict:
    """仅解决原本未定的语义差异；硬错误不允许通过模型或人工判断覆盖。"""
    if not isinstance(report, dict) or report.get("schema_version") != 1 or not isinstance(report.get("differences"), list):
        raise ComparisonError("comparison report 结构不合法。")
    if not isinstance(judgments, list):
        raise ComparisonError("judgments 必须是数组。")
    result = copy.deepcopy(report)
    by_id = {item["difference_id"]: item for item in result["differences"]}
    seen = set()
    for judgment in judgments:
        if not isinstance(judgment, dict):
            raise ComparisonError("每项 judgment 必须是对象。")
        key = judgment.get("difference_id")
        if key not in by_id or key in seen:
            raise ComparisonError("judgment 引用未知或重复的 difference_id。")
        seen.add(key)
        item = by_id[key]
        if item["severity"] != "review":
            raise ComparisonError("judgment 只能处理待复核差异，不能覆盖硬错误或已解决项。")
        decision, reason = judgment.get("decision"), judgment.get("reason")
        if decision not in ("harmless", "error", "uncertain") or not isinstance(reason, str) or not reason.strip():
            raise ComparisonError("judgment 需要 harmless/error/uncertain 决策和非空 reason。")
        if len(reason) > 4000:
            raise ComparisonError("judgment.reason 超过 4000 字符。")
        item["judgment"] = {"decision": decision, "reason": reason.strip()}
        item.setdefault("original_kind", item["kind"])
        if decision == "harmless":
            item["kind"], item["severity"] = "harmless_differences", "info"
        elif decision == "error":
            target = item.get("context", {}).get("error_kind", "association_errors")
            if target not in SUMMARY_KEYS or target in ("ambiguous_differences", "harmless_differences"):
                raise ComparisonError("差异的 error_kind 不合法。")
            item["kind"], item["severity"] = target, "error"
    return _refresh(result)
