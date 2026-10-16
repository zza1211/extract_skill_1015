#!/usr/bin/env python3
"""校验条款结果的结构和引用；不把结构通过当成业务语义正确。"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import document_io

FIELDS = frozenset({
    "id", "条款标题", "条款分类", "相关条款", "产品分类", "定位标识", "产品数量",
})
CLAUSE_TYPES = frozenset({"normal", "general", "separate"})
TAG_PATTERN = re.compile(r"<C[0-9]+>\Z")
BARE_TAG_PATTERN = re.compile(r"C[0-9]+\Z")
SEMANTIC_NOTICE = (
    "本报告仅检查结构、定位和引用。结构通过不等于产品无遗漏、选型参数完整、"
    "参数归属正确、数量正确或关联语义正确；warnings 和待确认问题需要复核。"
)


def _report() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "ok": False,
        "demo": False,
        "production_submittable": False,
        "semantic_verified": False,
        "notice": SEMANTIC_NOTICE,
        "errors": [],
        "warnings": [],
        "stats": {"unit_count": 0, "tag_count": 0, "clause_count": 0},
    }


def _add(report: dict, severity: str, code: str, message: str, path: str) -> None:
    report[severity].append({"code": code, "message": message, "path": path})


def _index_tag(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    if TAG_PATTERN.fullmatch(value):
        return value
    if BARE_TAG_PATTERN.fullmatch(value):
        return f"<{value}>"
    return None


def _build_tag_order(index: Any, report: dict, allow_demo: bool) -> dict[str, int]:
    if not isinstance(index, dict):
        _add(report, "errors", "INDEX_TYPE", "index 必须是 JSON 对象。", "index")
        return {}
    demo = index.get("demo", False)
    if type(demo) is not bool:
        _add(report, "errors", "INDEX_DEMO_TYPE", "index.demo 必须是布尔值。", "index.demo")
        demo = False
    report["demo"] = demo
    submittable = index.get("production_submittable", not demo)
    if type(submittable) is not bool:
        _add(report, "errors", "INDEX_PRODUCTION_TYPE", "production_submittable 必须是布尔值。", "index.production_submittable")
        submittable = False
    if demo:
        if not allow_demo:
            _add(report, "errors", "DEMO_FORBIDDEN", "合成标签 demo 禁止生产提交；仅演示校验可显式使用 --allow-demo。", "index.demo")
        else:
            _add(report, "warnings", "DEMO_ONLY", "本次仅校验 demo，结果不可作为生产提交。", "index.demo")
    elif not submittable:
        _add(report, "errors", "INDEX_NOT_SUBMITTABLE", "index 标记为不可生产提交。", "index.production_submittable")
    report["production_submittable"] = bool(submittable and not demo)
    if isinstance(index.get("source_sha256"), str):
        report["source_sha256"] = index["source_sha256"]
    # 旧的最小结构索引没有原文快照；带快照或别名声明的索引必须核验证明。
    if ("source_document" in index or "merged_cell_aliases" in index
            or "merged_cell_alias_policy" in index):
        try:
            document_io.validate_merged_aliases(index)
        except (ValueError, TypeError) as error:
            _add(report, "errors", "INDEX_MERGED_ALIAS_INVALID", str(error), "index.merged_cell_aliases")

    units = index.get("units")
    if not isinstance(units, list):
        _add(report, "errors", "INDEX_UNITS_TYPE", "index.units 必须是数组。", "index.units")
        return {}
    report["stats"]["unit_count"] = len(units)
    orders: set[int] = set()
    tag_order: dict[str, int] = {}
    for i, unit in enumerate(units):
        path = f"index.units[{i}]"
        if not isinstance(unit, dict):
            _add(report, "errors", "INDEX_UNIT_TYPE", "unit 必须是对象。", path)
            continue
        order = unit.get("order")
        if type(order) is not int or order < 0:
            _add(report, "errors", "INDEX_ORDER_TYPE", "unit.order 必须是非负整数，不能是布尔值。", path + ".order")
            continue
        if order in orders:
            _add(report, "errors", "INDEX_ORDER_DUPLICATE", "unit.order 不能重复。", path + ".order")
        orders.add(order)
        text = unit.get("text")
        if not isinstance(text, str):
            _add(report, "errors", "INDEX_TEXT_TYPE", "unit.text 必须是字符串。", path + ".text")
        tags = unit.get("tags", [])
        if not isinstance(tags, list) or len(tags) > 1:
            _add(report, "errors", "INDEX_TAGS_SHAPE", "内部定位 unit.tags 必须是空数组或单标签数组；原字符串的多个标签应先由 prepare 拆为定位单元。", path + ".tags")
            continue
        tag = unit.get("tag")
        if tag is None and tags:
            tag = tags[0]
        if tag is None:
            if isinstance(text, str) and text.strip():
                _add(report, "errors", "INDEX_MISSING_TAG", "非空 unit 必须有真实或显式 demo 标签。", path)
            continue
        canonical = _index_tag(tag)
        if canonical is None:
            _add(report, "errors", "INDEX_TAG_FORMAT", "索引标签必须为 <C数字> 或 C数字。", path + ".tag")
            continue
        if tags and _index_tag(tags[0]) != canonical:
            _add(report, "errors", "INDEX_TAG_MISMATCH", "unit.tag 与 unit.tags 不一致。", path)
        if canonical in tag_order:
            _add(report, "errors", "INDEX_TAG_DUPLICATE", f"索引标签 {canonical} 重复，无法可靠定位。", path)
        else:
            tag_order[canonical] = order
    report["stats"]["tag_count"] = len(tag_order)
    return tag_order


def _check_evidence(evidence: Any, records: dict[int, dict], tag_order: dict[str, int], report: dict) -> set[int]:
    quantity_evidenced: set[int] = set()
    if evidence is None:
        return quantity_evidenced
    if not isinstance(evidence, dict):
        _add(report, "errors", "EVIDENCE_TYPE", "证据 sidecar 必须是 JSON 对象。", "evidence")
        return quantity_evidenced
    clauses = evidence.get("clauses")
    if not isinstance(clauses, list):
        _add(report, "errors", "EVIDENCE_CLAUSES_TYPE", "evidence.clauses 必须是数组。", "evidence.clauses")
        clauses = []
    seen: set[int] = set()
    for i, item in enumerate(clauses):
        path = f"evidence.clauses[{i}]"
        if not isinstance(item, dict):
            _add(report, "errors", "EVIDENCE_CLAUSE_TYPE", "证据条目必须是对象。", path)
            continue
        cid = item.get("id")
        if type(cid) is not int:
            _add(report, "errors", "EVIDENCE_ID_TYPE", "证据 id 必须是整数，不能是布尔值。", path + ".id")
        else:
            if cid in seen:
                _add(report, "errors", "EVIDENCE_ID_DUPLICATE", "证据 id 不能重复。", path + ".id")
            seen.add(cid)
            if cid not in records:
                _add(report, "errors", "EVIDENCE_ID_UNKNOWN", "证据 id 不对应任何结果。", path + ".id")
        if not isinstance(item.get("notes"), str):
            _add(report, "errors", "EVIDENCE_NOTES_TYPE", "证据 notes 必须是字符串。", path + ".notes")
        for key in ("quantity_source_tags", "related_source_tags"):
            values = item.get(key)
            if not isinstance(values, list):
                _add(report, "errors", "EVIDENCE_TAGS_TYPE", f"{key} 必须是标签数组。", path + "." + key)
                continue
            valid_tags: set[str] = set()
            for j, value in enumerate(values):
                loc = f"{path}.{key}[{j}]"
                if not isinstance(value, str) or not TAG_PATTERN.fullmatch(value):
                    _add(report, "errors", "EVIDENCE_TAG_FORMAT", "证据标签必须为原样 <C数字> 字符串。", loc)
                elif value not in tag_order:
                    _add(report, "errors", "EVIDENCE_TAG_UNKNOWN", f"证据标签 {value} 不在输入中。", loc)
                elif value in valid_tags:
                    _add(report, "errors", "EVIDENCE_TAG_DUPLICATE", "同一证据标签数组不能重复。", loc)
                else:
                    valid_tags.add(value)
            if key == "quantity_source_tags" and valid_tags and type(cid) is int and cid in records:
                quantity_evidenced.add(cid)
    unresolved = evidence.get("unresolved")
    if not isinstance(unresolved, list):
        _add(report, "errors", "EVIDENCE_UNRESOLVED_TYPE", "evidence.unresolved 必须是数组。", "evidence.unresolved")
    elif unresolved:
        _add(report, "warnings", "UNRESOLVED_REVIEW", f"仍有 {len(unresolved)} 个待确认问题，需复核，不代表语义已验收。", "evidence.unresolved")
    return quantity_evidenced


def _table_row_spans(index: Any, intervals: list[tuple[int, int, int, str]]) -> dict[int, tuple[str, int, int]]:
    """仅识别整个定位区间都在同一原表内的行范围，不推断续表或业务归属。"""
    if not isinstance(index, dict) or not isinstance(index.get("units"), list):
        return {}
    units = [unit for unit in index["units"]
             if isinstance(unit, dict) and type(unit.get("order")) is int]
    layouts = {}
    for start, end, position, _ in intervals:
        selected = [unit for unit in units if start <= unit["order"] <= end]
        if not selected:
            continue
        table_id = selected[0].get("table_id")
        if not isinstance(table_id, str) or not table_id:
            continue
        if any(unit.get("table_id") != table_id or type(unit.get("row")) is not int
               or unit["row"] < 0 for unit in selected):
            continue
        rows = [unit["row"] for unit in selected]
        layouts[position] = (table_id, min(rows), max(rows))
    return layouts


def validate_result(index: Any, result: Any, evidence: Any = None, *, allow_demo: bool = False) -> dict[str, Any]:
    report = _report()
    tag_order = _build_tag_order(index, report, allow_demo)
    if not isinstance(result, list):
        _add(report, "errors", "RESULT_TYPE", "最终结果必须是 JSON 数组。", "result")
        report["production_submittable"] = False
        return report
    report["stats"]["clause_count"] = len(result)
    if not result:
        _add(report, "warnings", "EMPTY_RESULT_REVIEW", "结果为空；请核对是否没有目标产品或存在漏抽。", "result")
    records: dict[int, dict] = {}
    positions: dict[int, int] = {}
    intervals: list[tuple[int, int, int, str]] = []
    seen_ids: set[int] = set()
    seen_records: set[str] = set()
    for i, item in enumerate(result):
        path = f"result[{i}]"
        if not isinstance(item, dict):
            _add(report, "errors", "CLAUSE_TYPE", "每条结果必须是对象。", path)
            continue
        missing, extra = FIELDS - item.keys(), item.keys() - FIELDS
        if missing:
            _add(report, "errors", "FIELDS_MISSING", "缺少字段：" + "、".join(sorted(missing)), path)
        if extra:
            _add(report, "errors", "FIELDS_EXTRA", "七字段结果中不允许额外字段：" + "、".join(sorted(extra)), path)
        cid = item.get("id")
        if type(cid) is not int:
            _add(report, "errors", "ID_TYPE", "id 必须是整数，不能是布尔值。", path + ".id")
        else:
            if cid in seen_ids:
                _add(report, "errors", "ID_DUPLICATE", "id 不能重复。", path + ".id")
            seen_ids.add(cid)
            if cid != i:
                _add(report, "errors", "ID_SEQUENCE", "id 必须按数组顺序从 0 连续递增。", path + ".id")
            if cid not in records:
                records[cid], positions[cid] = item, i
        for key in ("条款标题", "条款分类", "产品分类", "产品数量"):
            value = item.get(key)
            if not isinstance(value, str) or not value.strip():
                _add(report, "errors", "STRING_REQUIRED", f"{key} 必须是非空字符串。", path + "." + key)
        ctype = item.get("条款分类")
        if not isinstance(ctype, str) or ctype not in CLAUSE_TYPES:
            _add(report, "errors", "CLAUSE_CATEGORY", "条款分类只能为 normal/general/separate。", path + ".条款分类")
        links = item.get("相关条款")
        if not isinstance(links, list):
            _add(report, "errors", "LINKS_TYPE", "相关条款必须是整数数组。", path + ".相关条款")
        else:
            valid_links: set[int] = set()
            for j, related in enumerate(links):
                loc = f"{path}.相关条款[{j}]"
                if type(related) is not int:
                    _add(report, "errors", "LINK_ID_TYPE", "关联 id 必须是整数，不能是布尔值。", loc)
                elif related in valid_links:
                    _add(report, "errors", "LINK_DUPLICATE", "关联 id 不能重复。", loc)
                else:
                    valid_links.add(related)
                if type(cid) is int and type(related) is int and related == cid:
                    _add(report, "errors", "LINK_SELF", "不能关联自身。", loc)
            if ctype == "normal" and links:
                _add(report, "errors", "NORMAL_LINKS", "normal 的相关条款必须为空。", path + ".相关条款")
            if ctype in ("general", "separate") and not links:
                _add(report, "errors", "LINKS_REQUIRED", "general 和 separate 必须有有效关联。", path + ".相关条款")
        span = item.get("定位标识")
        if not isinstance(span, list) or len(span) != 2:
            _add(report, "errors", "SPAN_SHAPE", "定位标识必须是恰好两个标签的数组。", path + ".定位标识")
        else:
            usable = True
            for j, tag in enumerate(span):
                loc = f"{path}.定位标识[{j}]"
                if not isinstance(tag, str) or not TAG_PATTERN.fullmatch(tag):
                    _add(report, "errors", "SPAN_TAG_FORMAT", "定位标签必须为原样 <C数字> 字符串。", loc)
                    usable = False
                elif tag not in tag_order:
                    _add(report, "errors", "SPAN_TAG_UNKNOWN", f"定位标签 {tag} 不在输入中。", loc)
                    usable = False
            if usable:
                start, end = tag_order[span[0]], tag_order[span[1]]
                if start > end:
                    _add(report, "errors", "SPAN_REVERSED", "起始标签在原文顺序上晚于终止标签；不能按标签数字排序。", path + ".定位标识")
                elif type(cid) is int and isinstance(ctype, str) and ctype in CLAUSE_TYPES:
                    intervals.append((start, end, i, ctype))
        signature = json.dumps({key: value for key, value in item.items() if key != "id"}, ensure_ascii=False, sort_keys=True)
        if signature in seen_records:
            _add(report, "errors", "CLAUSE_DUPLICATE", "同一记录内容重复，请核对重复抽取。", path)
        seen_records.add(signature)

    # 依据明确关联检查，绝不使用标题相等来推定采购对象相等。
    table_spans = _table_row_spans(index, intervals)
    for cid, item in records.items():
        links = item.get("相关条款")
        if not isinstance(links, list):
            continue
        for j, target in enumerate(links):
            if type(target) is not int:
                continue
            path = f"result[{positions[cid]}].相关条款[{j}]"
            if target not in records:
                _add(report, "errors", "LINK_UNKNOWN", f"关联 id {target} 不存在。", path)
            elif target != cid and item.get("条款分类") == "separate" and records[target].get("条款分类") == "separate":
                reverse = records[target].get("相关条款")
                if not isinstance(reverse, list) or not any(type(value) is int and value == cid for value in reverse):
                    _add(report, "errors", "SEPARATE_RECIPROCAL", "直接关联的 separate 记录应互相引用。", path)
            if target in records and target != cid and item.get("条款分类") == "separate":
                related = records[target]
                related_quantity = related.get("产品数量")
                if (item.get("产品数量") == "未提及"
                        and related.get("条款分类") in ("normal", "separate")
                        and isinstance(related_quantity, str)
                        and related_quantity.strip() not in ("", "未提及")):
                    _add(report, "warnings", "SEPARATE_QUANTITY_MISSING_REVIEW",
                         f"separate {cid} 的产品数量为未提及，而直接关联条款 {target} 已填「{related_quantity}」。"
                         "请核对同一采购对象在关联条款中的原文数量来源，并补充 quantity_source_tags；"
                         "不能只因本片没有数字就填未提及。关联数量仍须原文核验，不自动继承，也不保证其正确。",
                         f"result[{positions[cid]}].产品数量")
                source_layout = table_spans.get(positions[cid])
                target_layout = table_spans.get(positions[target])
                if source_layout and target_layout and source_layout[0] == target_layout[0]:
                    distance = max(source_layout[1] - target_layout[2], target_layout[1] - source_layout[2], 0)
                    if distance <= 1:
                        _add(report, "warnings", "SEPARATE_LOCAL_LAYOUT_REVIEW",
                             f"separate {cid} 与直接关联条款 {target} 位于同一原表 {source_layout[0]} 的相邻或重叠行。"
                             "请优先核对是否应合并连续的同一采购对象要求，或将共同性能识别为 general；"
                             "标签不相邻或夹有其他设备短段（如 AC 参数）不能直接证明分散。"
                             "若保留 separate，须在 evidence.notes 写明真实的逻辑不连续依据，不能仅凭标题或标签数字间隔判断。",
                             path)
        if item.get("条款分类") == "separate" and links and all(
            type(target) is int and target in records and records[target].get("条款分类") == "general" for target in links
        ):
            _add(report, "warnings", "SEPARATE_GENERAL_ONLY", "separate 只关联 general，需确认其同一采购对象关联是否完整。", f"result[{positions[cid]}].相关条款")
    separate = [(cid, item) for cid, item in records.items() if item.get("条款分类") == "separate"]
    for i, (a, first) in enumerate(separate):
        a_links = {value for value in first.get("相关条款", []) if type(value) is int} if isinstance(first.get("相关条款"), list) else set()
        for b, second in separate[i + 1:]:
            b_links = {value for value in second.get("相关条款", []) if type(value) is int} if isinstance(second.get("相关条款"), list) else set()
            common_normal = {value for value in a_links & b_links if value in records and records[value].get("条款分类") == "normal"}
            if common_normal and (b not in a_links or a not in b_links):
                _add(report, "warnings", "SEPARATE_SHARED_NORMAL_REVIEW", f"separate {a} 与 {b} 指向共同 normal；请核对是否为同一采购对象并补齐互联。", f"result[{positions[a]}].相关条款")

    for i, (a_start, a_end, a_pos, a_type) in enumerate(intervals):
        for b_start, b_end, b_pos, b_type in intervals[i + 1:]:
            if a_type == "general" or b_type == "general":
                continue
            if max(a_start, b_start) <= min(a_end, b_end):
                _add(report, "warnings", "SPAN_OVERLAP_REVIEW", f"结果位置 {a_pos} 与 {b_pos} 的非 general 区段重叠，请核对重复抽取或参数污染；不机械判错。", f"result[{a_pos}].定位标识")
    evidenced = _check_evidence(evidence, records, tag_order, report)
    for cid, item in records.items():
        quantity = item.get("产品数量")
        if isinstance(quantity, str) and quantity.strip() and quantity != "未提及" and cid not in evidenced:
            _add(report, "warnings", "QUANTITY_EVIDENCE_MISSING", "数量已填写但缺少有效数量来源标签；需主 agent/人工核对。", f"result[{positions[cid]}].产品数量")
    report["ok"] = not report["errors"]
    report["production_submittable"] = report["production_submittable"] and report["ok"]
    return report


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict:
    obj: dict = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError(f"JSON 字段 {key!r} 重复，不能静默覆盖。")
        obj[key] = value
    return obj


def _load_json(path: str) -> Any:
    with Path(path).open("r", encoding="utf-8-sig") as stream:
        return json.load(stream, object_pairs_hook=_reject_duplicate_keys,
                         parse_constant=_reject_nonfinite)


def _reject_nonfinite(value: str) -> Any:
    raise ValueError(f"JSON 不允许非有限数字常量 {value}。")


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        report = _report()
        _add(report, "errors", "CLI_ARGUMENTS", "命令参数错误：" + message, "cli")
        print(json.dumps(report, ensure_ascii=False))
        raise SystemExit(2)


def main(argv: list[str] | None = None) -> int:
    # Windows 的管道默认编码可能为 GBK；工具接口和报告统一使用 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = _Parser(description="校验条款 JSON 结构与引用，不验证业务语义。")
    parser.add_argument("--index", required=True, help="document_io 生成的 index.json")
    parser.add_argument("--result", required=True, help="严格七字段结果 JSON")
    parser.add_argument("--evidence", help="可选证据 sidecar JSON")
    parser.add_argument("--report", help="可选校验报告输出路径")
    parser.add_argument("--allow-demo", action="store_true", help="仅演示校验允许合成标签，仍禁止生产提交")
    args = parser.parse_args(argv)
    try:
        index = _load_json(args.index)
        result = _load_json(args.result)
        evidence = _load_json(args.evidence) if args.evidence else None
        report = validate_result(index, result, evidence, allow_demo=args.allow_demo)
    except (OSError, ValueError, UnicodeError) as exc:
        report = _report()
        _add(report, "errors", "INPUT_READ", "输入文件读取或 JSON 解析失败：" + str(exc), "input")
    report["inputs"] = {"index": args.index, "result": args.result, "evidence": args.evidence}
    if args.report:
        try:
            target = Path(args.report)
            resolved = target.resolve()
            sources = [Path(value).resolve() for value in (args.index, args.result, args.evidence) if value]
            if resolved in sources:
                raise ValueError("报告路径不能覆盖任何输入文件。")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (OSError, ValueError) as exc:
            _add(report, "errors", "REPORT_WRITE", "报告保存失败：" + str(exc), "report")
            report["ok"] = False
            report["production_submittable"] = False
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
