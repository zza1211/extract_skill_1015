"""条款抽取疑点引擎 v1：确定性风险信号与主 agent 疑点账本。

本模块不调用模型、不判断产品类别，也不把风险信号当成错误结论。
它只依据 index、草稿和 evidence 生成可定位的检查提示，供 term-extractor
形成结构化疑点并记录处理结果。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
import uuid
from typing import Any

import document_io
import memory_store as store

MemoryError = store.MemoryError
VERSION = 1
ISSUE_TYPES = {"missing_object", "coverage_gap", "boundary", "parameter_pollution",
               "association_error", "scope_uncertain", "evidence_gap"}
IMPACTS = {"high", "medium", "low"}
CONFIDENCE = {"C0", "C1", "C2", "C3"}
STATUSES = {"open", "resolved", "dismissed"}
HINT_RE = re.compile(
    r"(?:交换机|路由器|防火墙|无线|接入点|控制器|存储|服务器|摄像|光模块|OLT|ONU|OTN|SDH|"
    r"switch|router|firewall|access\s*point|controller|storage|server|camera|module|"
    r"采购|设备|系统|技术要求|技术参数|规格|接口|支持|数量|型号)", re.I,
)
TITLE_RE = re.compile(r"(?:^|[\n：:])\s*(?:\d+(?:\.\d+)*[.)]?\s*)?[A-Za-z][^\n]{2,80}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _context(value: Any) -> dict:
    if not isinstance(value, dict) or set(value) != {"session_id", "message_id", "agent"}:
        raise MemoryError("疑点工具需要真实 OpenCode 执行上下文。")
    if value.get("agent") != "term-extractor":
        raise MemoryError("疑点工具仅允许 term-extractor 主 agent 使用。")
    if any(not isinstance(value.get(key), str) or not value[key].strip() for key in value):
        raise MemoryError("疑点执行上下文不完整。")
    return dict(value)


def _read(path: Path, limit: int = 16 * 1024 * 1024) -> Any:
    return store._read_json(path, limit)


def _write(path: Path, value: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = store._canonical(value) + b"\n"
    path.write_bytes(content)
    return str(path.resolve())


def _hash(path: Path) -> str:
    return store._file_hash(path)


def _index(path: Path) -> tuple[dict, str]:
    index, digest = document_io.load_index(path)
    return index, digest


def _tag_order(index: dict) -> dict[str, int]:
    return {tag: unit["order"] for tag, unit_id in index["tag_to_unit"].items()
            for unit in index["units"] if unit.get("unit_id") == unit_id}


def _known_tags(index: dict, tags: Any, name: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(tags, list) or (not allow_empty and not tags) or len(tags) > 40:
        raise MemoryError(f"{name} 必须是最多40项的标签数组。")
    order = _tag_order(index)
    result = []
    for tag in tags:
        if not isinstance(tag, str) or tag not in order or tag in result:
            raise MemoryError(f"{name} 含未知或重复标签：{tag!r}。")
        result.append(tag)
    return result


def _hint(unit: dict) -> bool:
    text = str(unit.get("source_text", unit.get("text", "")))
    return bool(text.strip() and HINT_RE.search(text))


def _text(unit: dict, limit: int = 180) -> str:
    value = re.sub(r"\s+", " ", str(unit.get("source_text", unit.get("text", ""))).strip())
    return value if len(value) <= limit else value[:limit - 1] + "…"


def _span_orders(index: dict, result: list[dict]) -> tuple[dict[int, tuple[int, int]], dict[int, dict]]:
    order = _tag_order(index)
    spans, records = {}, {}
    for item in result:
        if not isinstance(item, dict) or type(item.get("id")) is not int:
            continue
        records[item["id"]] = item
        span = item.get("定位标识")
        if isinstance(span, list) and len(span) == 2 and span[0] in order and span[1] in order:
            spans[item["id"]] = (order[span[0]], order[span[1]])
    return spans, records


def _tags_between(index: dict, start: int, end: int, limit: int = 12) -> list[str]:
    selected = [unit.get("tag") for unit in index["units"]
                if start <= unit.get("order", -1) <= end and unit.get("tag")]
    return list(dict.fromkeys(selected))[:limit]


def _scan(index: dict, result: list[dict] | None, evidence: dict | None) -> list[dict]:
    order = _tag_order(index)
    spans, records = _span_orders(index, result or [])
    signals: list[dict] = []

    def add(kind: str, impact: str, confidence: str, tags: list[str], clause_ids: list[int],
            title: str, trigger: str, action: str, details: str) -> None:
        if not tags and kind != "coverage_gap":
            return
        signals.append({
            "id": f"s{len(signals) + 1:04d}", "type": kind, "impact": impact,
            "confidence": confidence, "evidence_tags": list(dict.fromkeys(tags))[:20],
            "clause_ids": list(dict.fromkeys(clause_ids)), "title": title,
            "trigger": trigger, "suggested_action": action, "details": details,
        })

    if result is not None and not result and index.get("units"):
        units = [unit for unit in index["units"] if unit.get("tag")]
        add("coverage_gap", "high", "C1", [units[0]["tag"], units[-1]["tag"]] if units else [], [],
            "结果为空", "输入存在带定位标签的原文，但结果没有条款。",
            "核对全文是否确实没有目标设备；若有目标设备，先补充候选。", "空结果可能是合法空集，也可能是整篇漏抽。")

    if result is None:
        hinted = [unit for unit in index.get("units", []) if unit.get("tag") and _hint(unit)]
        for unit in hinted[:80]:
            add("coverage_gap", "low", "C0", [unit["tag"]], [], "待映射的设备/技术线索",
                "原文含设备、采购或技术要求线索，但尚未与草稿建立覆盖关系。",
                "阅读上下文后将其归入候选、general、系统组成或明确排除项。", _text(unit))

    if result is not None:
        covered: set[int] = set()
        for start, end in spans.values():
            covered.update(range(start, end + 1))
        units = index.get("units", [])
        gap_start = None
        gap_units: list[dict] = []
        for unit in units + [{"order": None}]:
            current = unit.get("order")
            interesting = current is not None and current not in covered and _hint(unit)
            if interesting and gap_start is None:
                gap_start, gap_units = current, [unit]
            elif interesting and gap_units and current == gap_units[-1].get("order", -2) + 1:
                gap_units.append(unit)
            elif gap_units:
                first, last = gap_units[0], gap_units[-1]
                add("coverage_gap", "high" if len(gap_units) > 2 else "medium", "C1",
                    [first.get("tag"), last.get("tag")], [], "未覆盖的技术/采购线索",
                    "原文单元未落入任何当前条款，且包含设备、采购或技术线索。",
                    "读取该区段及邻域，判断是否为新设备、系统组成或明确排除项。",
                    "程序只发现未覆盖线索，不能自行判定它必须抽取。")
                gap_start, gap_units = None, []

        # 仅提示非 general 重叠；general 的共享范围是合法场景。
        items = list(spans.items())
        for pos, (left_id, (left_start, left_end)) in enumerate(items):
            for right_id, (right_start, right_end) in items[pos + 1:]:
                if records.get(left_id, {}).get("条款分类") == "general" or records.get(right_id, {}).get("条款分类") == "general":
                    continue
                if max(left_start, right_start) <= min(left_end, right_end):
                    add("parameter_pollution", "high", "C2",
                        _tags_between(index, max(left_start, right_start), min(left_end, right_end)),
                        [left_id, right_id], "非通用条款区段重叠", "两个非 general 条款覆盖了同一原文区段。",
                        "展开重叠区段，确认是重复抽取、共享要求还是其他设备参数混入。",
                        "重叠本身不等于错误。")

        for cid, item in records.items():
            ctype = item.get("条款分类")
            links = item.get("相关条款") if isinstance(item.get("相关条款"), list) else []
            if ctype == "general" and not links:
                span = spans.get(cid)
                add("association_error", "medium", "C2", _tags_between(index, *span) if span else [], [cid],
                    "general 未关联对象", "general 条款没有相关条款。", "核对适用对象和章节范围，补充或取消 general。",
                    "程序只检查链接缺失，不判断它是否确实通用。")
            if ctype == "separate" and links and all(
                isinstance(target, int) and target in records and records[target].get("条款分类") == "general" for target in links
            ):
                span = spans.get(cid)
                add("association_error", "medium", "C1", _tags_between(index, *span) if span else [], [cid] + [x for x in links if isinstance(x, int)],
                    "separate 只关联 general", "分散条款没有直接关联 normal 或 separate 对象。",
                    "核对是否确实属于某个设备，不能只用 general 作为同一对象证据。",
                    "可能是合法的通用补充，也可能遗漏主设备关联。")

            span = spans.get(cid)
            if span:
                end_order = span[1]
                following = next((unit for unit in index["units"] if unit.get("order") == end_order + 1), None)
                if following and _hint(following) and following.get("tag"):
                    add("boundary", "medium", "C1", [item["定位标识"][1], following["tag"]], [cid],
                        "条款终止位置邻近技术线索", "条款终止标签之后紧接着仍有设备或技术线索。",
                        "读取终止标签后的邻域，确认当前条款是否截断或下一设备已开始。",
                        _text(following))

    if evidence and isinstance(evidence.get("unresolved"), list):
        for unresolved in evidence["unresolved"][:30]:
            text = unresolved if isinstance(unresolved, str) else json.dumps(unresolved, ensure_ascii=False)
            add("scope_uncertain", "medium", "C2", [], [], "已有待确认问题", "evidence.unresolved 已记录待确认事项。",
                "逐项处理该问题，并把结论写回疑点账本。", text[:300])
    return signals[:200]


def scan(root: Path, request: dict) -> dict:
    _context(request.get("execution_context"))
    index_path = Path(request["index_path"]).resolve()
    index, index_sha = _index(index_path)
    result = _read(Path(request["result_path"]).resolve()) if request.get("result_path") else None
    evidence = _read(Path(request["evidence_path"]).resolve()) if request.get("evidence_path") else None
    if result is not None and not isinstance(result, list):
        raise MemoryError("result 必须是 JSON 数组。")
    signals = _scan(index, result, evidence)
    path = Path(request.get("signals_path") or index_path.parent / "draft" / "doubt-signals.json").resolve()
    body = {"schema_version": VERSION, "kind": "doubt_signals", "created_at": _now(),
            "index_path": str(index_path), "index_sha256": index_sha, "result_path": str(Path(request["result_path"]).resolve()) if request.get("result_path") else None,
            "signal_count": len(signals), "signals": signals}
    body["signals_path"] = str(path)
    _write(path, body)
    counts = {kind: sum(item["type"] == kind for item in signals) for kind in sorted(ISSUE_TYPES)}
    return {"ok": True, "signals_path": body["signals_path"], "index_sha256": index_sha,
            "signal_count": len(signals), "counts": counts,
            "high_impact_count": sum(item["impact"] == "high" for item in signals),
            "signals": signals[:50], "notice": "风险信号不是业务错误结论，需要主 agent 结合原文形成疑点并处理。"}


def _load_signals(index_path: Path, request: dict) -> dict:
    path = Path(request.get("signals_path") or index_path.parent / "draft" / "doubt-signals.json").resolve()
    body = _read(path)
    if body.get("index_sha256") != _hash(index_path) or body.get("kind") != "doubt_signals":
        raise MemoryError("疑点信号与当前 index 不匹配，请重新 scan。")
    return body


def _normalize_doubts(index: dict, result: list[dict] | None, signals: list[dict], doubts: Any) -> list[dict]:
    if not isinstance(doubts, list) or len(doubts) > 200:
        raise MemoryError("doubts 必须是最多200项的数组。")
    known_signal_ids = {item["id"] for item in signals}
    known_tags = _tag_order(index)
    records = {item["id"]: item for item in (result or []) if isinstance(item, dict) and type(item.get("id")) is int}
    normalized = []
    for number, item in enumerate(doubts, 1):
        if not isinstance(item, dict):
            raise MemoryError("每条疑点必须是对象。")
        issue_type = item.get("type")
        if issue_type not in ISSUE_TYPES:
            raise MemoryError(f"疑点类型无效：{issue_type!r}。")
        clause_ids = item.get("clause_ids", [])
        if not isinstance(clause_ids, list) or any(type(cid) is not int or cid not in records for cid in clause_ids):
            raise MemoryError("clause_ids 必须引用当前结果中的有效 id；漏抽疑点可为空。")
        signal_ids = item.get("signal_ids", [])
        if not isinstance(signal_ids, list) or any(not isinstance(sid, str) or sid not in known_signal_ids for sid in signal_ids):
            raise MemoryError("signal_ids 必须引用本次 scan 产生的信号 id。")
        tags = item.get("evidence_tags", [])
        if not isinstance(tags, list) or any(tag not in known_tags for tag in tags):
            raise MemoryError("evidence_tags 必须是当前原文真实标签。")
        impact, confidence, status = item.get("impact", "medium"), item.get("confidence", "C1"), item.get("status", "open")
        if impact not in IMPACTS or confidence not in CONFIDENCE or status not in STATUSES:
            raise MemoryError("impact、confidence 或 status 无效。")
        for key, maximum in (("hypothesis", 1200), ("next_action", 600), ("reason", 800)):
            if not isinstance(item.get(key, ""), str) or len(item.get(key, "")) > maximum:
                raise MemoryError(f"{key} 必须是最多{maximum}字的字符串。")
        if not item.get("hypothesis", "").strip() or not item.get("next_action", "").strip():
            raise MemoryError("疑点必须包含 hypothesis 和 next_action。")
        if status != "open" and not item.get("reason", "").strip():
            raise MemoryError("已 resolved 或 dismissed 的疑点必须提供 reason。")
        triggers = item.get("triggers", [])
        if not isinstance(triggers, list) or not 1 <= len(triggers) <= 12 or any(not isinstance(v, str) or not v.strip() for v in triggers):
            raise MemoryError("triggers 必须是1到12项非空字符串。")
        normalized.append({"id": f"d{number:04d}", "type": issue_type, "signal_ids": list(dict.fromkeys(signal_ids)),
                           "clause_ids": list(dict.fromkeys(clause_ids)), "evidence_tags": list(dict.fromkeys(tags)),
                           "hypothesis": item["hypothesis"].strip(), "triggers": list(dict.fromkeys(triggers)),
                           "impact": impact, "confidence": confidence, "next_action": item["next_action"].strip(),
                           "status": status, "reason": item.get("reason", "").strip()})
    return normalized


def record(root: Path, request: dict) -> dict:
    context = _context(request.get("execution_context"))
    index_path = Path(request["index_path"]).resolve()
    index, index_sha = _index(index_path)
    signals_body = _load_signals(index_path, request)
    result_path = Path(request["result_path"]).resolve() if request.get("result_path") else None
    result = _read(result_path) if result_path else None
    if result is not None and not isinstance(result, list):
        raise MemoryError("result 必须是 JSON 数组。")
    doubts_input = request.get("doubts")
    if request.get("doubts_file"):
        doubts_input = _read(Path(request["doubts_file"]).resolve(), 512 * 1024)
    doubts = _normalize_doubts(index, result, signals_body["signals"], doubts_input)
    path = Path(request.get("ledger_path") or index_path.parent / "draft" / "doubt-ledger.json").resolve()
    previous_hash = _hash(path) if path.exists() else None
    previous_events: list[dict] = []
    if path.exists():
        previous = _read(path)
        if previous.get("index_sha256") == index_sha and isinstance(previous.get("events"), list):
            previous_events = previous["events"]
    body = {"schema_version": VERSION, "kind": "doubt_ledger", "created_at": _now(), "updated_at": _now(),
            "index_path": str(index_path), "index_sha256": index_sha, "signals_path": signals_body["signals_path"],
            "signals_sha256": _hash(Path(signals_body["signals_path"])), "doubts": doubts, "events": previous_events,
            "execution_context": context, "previous_ledger_sha256": previous_hash}
    body["ledger_path"] = _write(path, body)
    return {"ok": True, "ledger_path": body["ledger_path"], "doubt_count": len(doubts),
            "open_count": sum(item["status"] == "open" for item in doubts),
            "high_open_count": sum(item["status"] == "open" and item["impact"] == "high" for item in doubts),
            "doubts": doubts, "notice": "疑点由主 agent 提出；程序只校验来源、结构和状态，不证明业务判断正确。"}


def inspect(root: Path, request: dict) -> dict:
    _context(request.get("execution_context"))
    index_path = Path(request["index_path"]).resolve()
    path = Path(request.get("ledger_path") or index_path.parent / "draft" / "doubt-ledger.json").resolve()
    body = _read(path)
    if body.get("index_sha256") != _hash(index_path) or body.get("kind") != "doubt_ledger":
        raise MemoryError("疑点账本与当前 index 不匹配。")
    limit = request.get("limit", 20)
    cursor = request.get("cursor", "0")
    if type(limit) is not int or not 1 <= limit <= 50 or not isinstance(cursor, str) or not cursor.isdigit():
        raise MemoryError("limit 或 cursor 无效。")
    items = body["doubts"]
    start = int(cursor)
    page = items[start:start + limit]
    next_cursor = str(start + len(page)) if start + len(page) < len(items) else None
    return {"ok": True, "ledger_path": str(path), "doubt_count": len(items), "items": page,
            "cursor": cursor, "next_cursor": next_cursor, "has_more": next_cursor is not None,
            "open_count": sum(item["status"] == "open" for item in items)}


def resolve(root: Path, request: dict) -> dict:
    context = _context(request.get("execution_context"))
    index_path = Path(request["index_path"]).resolve()
    path = Path(request.get("ledger_path") or index_path.parent / "draft" / "doubt-ledger.json").resolve()
    body = _read(path)
    if body.get("index_sha256") != _hash(index_path) or body.get("kind") != "doubt_ledger":
        raise MemoryError("疑点账本与当前 index 不匹配。")
    updates = request.get("updates")
    if request.get("updates_file"):
        updates = _read(Path(request["updates_file"]).resolve(), 256 * 1024)
    if not isinstance(updates, list) or not updates:
        raise MemoryError("updates 必须是非空数组。")
    lookup = {item["id"]: item for item in body["doubts"]}
    event_items = []
    for update in updates:
        if not isinstance(update, dict) or update.get("doubt_id") not in lookup:
            raise MemoryError("updates 含未知 doubt_id。")
        status = update.get("status")
        if status not in {"resolved", "dismissed", "open"}:
            raise MemoryError("处理状态只能是 resolved、dismissed 或 open。")
        reason = update.get("reason", "")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 1200:
            raise MemoryError("处理疑点必须提供最多1200字的 reason。")
        tags = _known_tags(_index(index_path)[0], update.get("evidence_tags", []), "evidence_tags")
        if status != "open" and not tags:
            raise MemoryError("resolved 或 dismissed 必须提供至少一个处置证据标签。")
        item = lookup[update["doubt_id"]]
        item.update(status=status, reason=reason.strip(), resolution_evidence_tags=tags, resolved_at=_now())
        event_items.append({"doubt_id": item["id"], "status": status, "reason": reason.strip(), "evidence_tags": tags})
    body["updated_at"] = _now()
    body["execution_context"] = context
    body["events"].append({"event_id": uuid.uuid4().hex, "time": _now(), "updates": event_items, "execution_context": context})
    body["ledger_path"] = str(path)
    _write(path, body)
    return {"ok": True, "ledger_path": str(path), "doubt_count": len(body["doubts"]),
            "open_count": sum(item["status"] == "open" for item in body["doubts"]),
            "high_open_count": sum(item["status"] == "open" and item["impact"] == "high" for item in body["doubts"]),
            "updated": event_items}


def dispatch(root: Path, request: dict) -> dict:
    if not isinstance(request, dict):
        raise MemoryError("疑点请求必须是对象。")
    action = request.get("action")
    if action == "scan":
        return scan(root, request)
    if action == "record":
        return record(root, request)
    if action == "inspect":
        return inspect(root, request)
    if action == "resolve":
        return resolve(root, request)
    raise MemoryError("未知疑点动作。")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--request-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = dispatch(args.project_root, _read(args.request_file, 2 * 1024 * 1024))
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
