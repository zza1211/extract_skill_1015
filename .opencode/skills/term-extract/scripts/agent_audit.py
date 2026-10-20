"""独立语义核验的票据、隔离读取和提交门槛；不调用模型、不确认正确标签。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
import uuid

import document_io
import evolution_store as evo
import memory_store as store
from validate_result import validate_result

MemoryError = store.MemoryError
TABLES = ("agent_audit_plans", "agent_audit_claims", "agent_audit_inventories",
          "agent_audit_deliveries", "agent_audit_reports", "agent_audit_resolutions")
FINDING_TYPES = {"missing_object", "boundary_truncated", "parameter_pollution",
                 "association_error", "scope_uncertain"}
NOTICE = "独立模型核验和主代理处置记录，不是人工正确标签，也不证明没有遗漏。"


@contextmanager
def _db(root, *, write=False):
    with store._db(Path(root), write=write) as connection:
        if connection is not None and write:
            for table in TABLES:
                connection.execute(f"CREATE TABLE IF NOT EXISTS {table} (record_id TEXT PRIMARY KEY, record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)")
                for op in ("UPDATE", "DELETE"):
                    connection.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable record'); END")
        yield connection


def _context(value, role):
    if not isinstance(value, dict) or set(value) != {"session_id", "message_id", "agent"}:
        raise MemoryError("独立核验需要工具注入的真实 OpenCode 执行上下文。")
    for key in value:
        store._string(value[key], "execution_context." + key, 200)
    if value["agent"] != role:
        raise MemoryError(f"此操作仅允许 {role}。")
    return dict(value)


def _put(connection, table, key, body):
    record = {**body, "created_at": store._now()}
    evo._put(connection, table, key, record)
    return record


def _one(connection, table, audit_id):
    return evo._find(connection, table, "audit_id", audit_id)


def _plan(connection, audit_id):
    store._string(audit_id, "audit_id", 100)
    plan = _one(connection, "agent_audit_plans", audit_id)
    if plan is None:
        raise MemoryError("独立核验票不存在。")
    return plan


def _owner(plan, value):
    context = _context(value, "term-extractor")
    if context["session_id"] != plan["controller"]["session_id"]:
        raise MemoryError("仅创建核验票的主会话可以查看和处置报告。")
    return context


def _worker(connection, plan, value):
    context = _context(value, "term-auditor")
    claim = _one(connection, "agent_audit_claims", plan["audit_id"])
    if claim is None or claim["execution_context"]["session_id"] != context["session_id"]:
        raise MemoryError("请先领取本票；不能使用其他核验会话的票。")
    return context


def _read(path):
    return store._read_json(Path(path), 16 * 1024 * 1024)


def _capture(path):
    raw = Path(path).read_bytes()
    return store._parse(raw.decode("utf-8-sig")), hashlib.sha256(raw).hexdigest()


def _write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = store._canonical(data) + b"\n"
    if path.exists() and path.read_bytes() != raw:
        raise MemoryError("核验冻结工件不可覆盖。")
    if not path.exists():
        path.write_bytes(raw)
    return str(path.resolve())


def _refs(root):
    directory = Path(root) / ".opencode/skills/term-extract/references"
    return {name: (directory / name).read_text(encoding="utf-8-sig")
            for name in ("business-rules.md", "output-format.md") if (directory / name).exists()}


def _frozen(root, plan):
    for field, digest in (("index_path", "index_sha256"), ("source_path", "source_sha256"),
                          ("snapshot_path", "snapshot_sha256"), ("worker_index_path", "index_sha256"),
                          ("frozen_result_path", "result_sha256"), ("frozen_evidence_path", "evidence_sha256")):
        if store._file_hash(Path(plan[field])) != plan[digest]:
            raise MemoryError(f"核验依据已变化：{field}；请准备新核验票。")
    if store._hash(_refs(root)) != plan["references_sha256"]:
        raise MemoryError("业务口径或输出协议已变化，请重新准备核验票。")


def _index(plan):
    index, _ = document_io.load_index(Path(plan["worker_index_path"]))
    return index


def _tags(index, value, name, *, allow_empty=False):
    if (not isinstance(value, list) or len(value) > 40 or (not value and not allow_empty)
            or any(not isinstance(tag, str) or tag not in index["tag_to_unit"] for tag in value)
            or len(set(value)) != len(value)):
        raise MemoryError(f"{name} 必须是本篇真实且不重复的证据标签数组。")
    return list(value)


def _receipts(path):
    path = Path(path)
    if not path.exists():
        return []
    return [store._parse(line) for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def _fully_read_tags(index_path, receipts_path, context, offset=0):
    """只承认当前角色/会话的完整字符交付，搜索不计入。"""
    index, _ = document_io.load_index(Path(index_path))
    digest = store._file_hash(Path(index_path))
    spans = {unit["unit_id"]: [] for unit in index["units"]}
    units = {unit["unit_id"]: unit for unit in index["units"]}
    for receipt in _receipts(receipts_path)[offset:]:
        actual = receipt.get("execution_context", {})
        if actual.get("agent") != context["agent"] or actual.get("session_id") != context["session_id"]:
            continue
        if receipt.get("index_sha256") != digest or receipt.get("coverage_kind") != "full_original":
            raise MemoryError("核验读取回执与当前原文版本不一致。")
        for part in receipt.get("parts", []):
            key = part.get("unit_id")
            if key not in units:
                raise MemoryError("核验回执含未知原文单元。")
            start, end, length = part.get("part_start"), part.get("part_end"), units[key]["source_length"]
            if (type(start) is not int or type(end) is not int or not 0 <= start <= end <= length
                    or part.get("total_length") != length):
                raise MemoryError("核验原文读取范围不合法。")
            spans[key].append((start, end))
    complete = set()
    for key, segments in spans.items():
        end = 0
        for start, stop in sorted(segments):
            if start > end:
                break
            end = max(end, stop)
        if segments and end == units[key]["source_length"]:
            complete.add(key)
    tags = {tag for tag, key in index["tag_to_unit"].items() if key in complete}
    return tags, len(complete) == len(units)


def _worker_complete(plan, context):
    tags, complete = _fully_read_tags(plan["worker_index_path"], plan["receipts_path"], context)
    if not complete:
        raise MemoryError("核验者必须先完整读取全文，不能用主代理回执或搜索替代。")
    return tags


def _page(items, cursor, limit, max_bytes, base):
    if not isinstance(cursor, str) or not cursor.isdigit():
        raise MemoryError("cursor 必须为非负整数字符串。")
    start = int(cursor)
    if start > len(items) or type(limit) is not int or not 1 <= limit <= 20:
        raise MemoryError("分页位置越界或 limit 不在 1 到 20。")
    if type(max_bytes) is not int or not 2048 <= max_bytes <= 24576:
        raise MemoryError("max_bytes 必须在 2048 到 24576。")
    for end in range(min(start + limit, len(items)), start - 1, -1):
        if end == start and start < len(items):
            break
        response = {**base, "cursor": cursor, "items": items[start:end],
                    "next_cursor": str(end) if end < len(items) else None, "has_more": end < len(items)}
        if len(store._canonical(response)) <= max_bytes:
            return response, list(range(start, end))
    raise MemoryError("单条核验材料超过预算，请提高 max_bytes；未交付部分内容。")


def _delivered(connection, audit_id, kind):
    selected, empty = set(), False
    for event in evo._rows(connection, "agent_audit_deliveries"):
        if event["audit_id"] == audit_id and event["kind"] == kind:
            selected.update(event["positions"])
            empty |= event.get("empty", False)
    return selected, empty


def prepare(root, request):
    context = _context(request.get("execution_context"), "term-extractor")
    index_path, result_path, evidence_path = [Path(request[key]).resolve() for key in ("index_path", "result_path", "evidence_path")]
    index, index_sha = document_io.load_index(index_path)
    (result, original_result_sha), (evidence, original_evidence_sha) = _capture(result_path), _capture(evidence_path)
    validation = validate_result(index, result, evidence, allow_demo=bool(index.get("demo")))
    if not validation["ok"]:
        raise MemoryError("先用 term_result_validate 修复草稿结构和证据格式，再准备核验。")
    snapshot = index_path.with_name("memory_snapshot.json")
    store.run_memory_record(index_path)
    audit_id = "audit_" + uuid.uuid4().hex
    for item in result:
        try:
            _page([item], "0", 1, 24576, {"audit_id": audit_id, "draft_sha256": "0" * 64, "notice": NOTICE})
        except MemoryError as exc:
            raise MemoryError("单条草稿超过核验最大交付预算（24 KiB）；请精简过长标题或结果字段，不要把原文参数复述到七字段中。") from exc
    directory = store.resolve_store_dir(Path(root)) / "agent-audits" / audit_id
    directory.mkdir(parents=True)
    worker_index = directory / "source/index.json"
    worker_index.parent.mkdir()
    worker_index.write_bytes(index_path.read_bytes())
    if store._file_hash(worker_index) != index_sha:
        raise MemoryError("准备核验时索引发生变化，请重新准备。")
    receipts = directory / "source/read_receipts.jsonl"
    receipts.write_text("", encoding="utf-8")
    references = _refs(root)
    plan = {"audit_id": audit_id, "controller": context, "index_path": str(index_path),
            "index_sha256": index_sha, "source_path": index["source_path"],
            "source_sha256": index["source_sha256"], "snapshot_path": str(snapshot),
            "snapshot_sha256": store._file_hash(snapshot), "worker_index_path": str(worker_index),
            "receipts_path": str(receipts), "directory": str(directory),
            "frozen_result_path": _write(directory / "draft/result.json", result),
            "frozen_evidence_path": _write(directory / "draft/evidence.json", evidence),
            "result_sha256": store._file_hash(directory / "draft/result.json"),
            "evidence_sha256": store._file_hash(directory / "draft/evidence.json"),
            "reference_pack": references, "references_sha256": store._hash(references)}
    # 冻结副本规范化序列化，原草稿身份另存；不把格式差异当业务修改。
    plan["original_result_sha256"] = original_result_sha
    plan["original_evidence_sha256"] = original_evidence_sha
    with _db(root, write=True) as connection:
        _put(connection, "agent_audit_plans", audit_id, plan)
    return {"audit_id": audit_id, "status": "prepared", "result_sha256": plan["original_result_sha256"],
            "next_action": "用 task 新建 term-auditor 子会话，prompt 严格只传 audit_id=" + audit_id,
            "notice": NOTICE}


def dispatch(root, request):
    if not isinstance(request, dict):
        raise MemoryError("核验请求必须是对象。")
    action = request.get("action")
    if action == "prepare":
        return prepare(root, request)
    with _db(root, write=True) as connection:
        plan = _plan(connection, request.get("audit_id"))
        _frozen(root, plan)
        audit_id = plan["audit_id"]
        if action in {"inspect", "resolve"}:
            context = _owner(plan, request.get("execution_context"))
        elif action == "open":
            context = _context(request.get("execution_context"), "term-auditor")
            if context["session_id"] == plan["controller"]["session_id"]:
                raise MemoryError("核验必须使用独立子会话。")
            previous = [claim for claim in evo._rows(connection, "agent_audit_claims")
                        if claim["execution_context"]["session_id"] == context["session_id"]]
            if any(claim["audit_id"] != audit_id for claim in previous):
                raise MemoryError("一个核验子会话只能处理一张票，新票需要新会话。")
            claim = _one(connection, "agent_audit_claims", audit_id)
            if claim and claim["execution_context"]["session_id"] != context["session_id"]:
                raise MemoryError("本票已被其他核验会话领取。")
            response = {"audit_id": audit_id, "reference_pack": plan["reference_pack"],
                        "unit_count": len(_index(plan)["units"]), "notice": NOTICE,
                        "next_action": "先独立全文 read，保存 inventory 后才能读取 draft；不接收主会话推理、标签或旧答案。"}
            if len(store._canonical(response)) > 48 * 1024:
                raise MemoryError("核验口径超过工具交付预算。")
            if not claim:
                _put(connection, "agent_audit_claims", audit_id, {"audit_id": audit_id, "execution_context": context})
            return response
        else:
            context = _worker(connection, plan, request.get("execution_context"))
        report = _one(connection, "agent_audit_reports", audit_id)
        inventory = _one(connection, "agent_audit_inventories", audit_id)
        if action in {"read", "search"}:
            if report:
                raise MemoryError("核验报告已冻结，不能继续修改本票的原文交付。")
            budget = request.get("max_bytes", 24576)
            if type(budget) is not int or not 2048 <= budget <= 24576:
                raise MemoryError("max_bytes 必须在 2048 到 24576。")
            if action == "read":
                page = document_io.read_document(Path(plan["worker_index_path"]), request.get("cursor", "0"), budget,
                                                request.get("start_tag"), request.get("end_tag"), "compact")
                receipt = {"index_sha256": plan["index_sha256"], "coverage_kind": page["coverage_kind"],
                           "parts": page["parts"], "execution_context": context, "time": store._now(),
                           "cursor": request.get("cursor", "0"), "next_cursor": page["next_cursor"]}
                with Path(plan["receipts_path"]).open("a", encoding="utf-8") as stream:
                    stream.write(store._canonical(receipt).decode() + "\n")
            else:
                limit = request.get("limit", 20)
                if type(limit) is not int or not 1 <= limit <= 50:
                    raise MemoryError("搜索 limit 必须在 1 到 50。")
                page = document_io.search_document(Path(plan["worker_index_path"]), request.get("query", ""),
                                                   request.get("cursor", "0"), budget, limit, view="compact")
            return {"model_view": page["model_view"]}
        if action == "inventory":
            _worker_complete(plan, context)
            objects = request.get("objects")
            if not isinstance(objects, list) or len(objects) > 1000:
                raise MemoryError("objects 必须是独立设备清点数组，最多 1000 项。")
            normalized = [{"title": store._string(item.get("title"), "title", 300),
                           "evidence_tags": _tags(_index(plan), item.get("evidence_tags"), "evidence_tags")}
                          for item in objects if isinstance(item, dict)]
            if len(normalized) != len(objects):
                raise MemoryError("设备清点条目必须是对象。")
            if inventory and inventory["objects"] != normalized:
                raise MemoryError("独立设备清点已冻结，看到草稿后不能反改清点。")
            if report:
                raise MemoryError("报告已冻结。")
            if not inventory:
                _put(connection, "agent_audit_inventories", audit_id,
                     {"audit_id": audit_id, "objects": normalized, "execution_context": context})
            return {"audit_id": audit_id, "inventory_count": len(normalized), "status": "inventory_frozen"}
        if action == "draft":
            if not inventory:
                raise MemoryError("先完整读原文并固定独立设备清点 inventory，再读取主代理草稿。")
            if report:
                raise MemoryError("报告已冻结。")
            response, positions = _page(_read(plan["frozen_result_path"]), request.get("cursor", "0"),
                request.get("limit", 10), request.get("max_bytes", 8192),
                {"audit_id": audit_id, "draft_sha256": plan["original_result_sha256"], "notice": NOTICE})
            _put(connection, "agent_audit_deliveries", uuid.uuid4().hex,
                 {"audit_id": audit_id, "kind": "draft", "positions": positions,
                  "empty": not _read(plan["frozen_result_path"]), "execution_context": context})
            return response
        if action == "finish":
            if not inventory:
                raise MemoryError("尚未完成独立全文读取和 inventory。")
            _worker_complete(plan, context)
            draft = _read(plan["frozen_result_path"])
            delivered, empty = _delivered(connection, audit_id, "draft")
            if set(range(len(draft))) - delivered or (not draft and not empty):
                raise MemoryError("先按分页完整读取主代理草稿，再完成核验报告。")
            findings = request.get("findings")
            if not isinstance(findings, list) or len(findings) > 100:
                raise MemoryError("findings 必须是至多 100 项的问题数组。")
            valid_ids = {item["id"] for item in draft}
            normalized = []
            for i, item in enumerate(findings):
                if not isinstance(item, dict) or item.get("type") not in FINDING_TYPES:
                    raise MemoryError("核验问题类型无效。")
                ids = item.get("clause_ids")
                if (not isinstance(ids, list) or any(type(cid) is not int or cid not in valid_ids for cid in ids)
                        or len(set(ids)) != len(ids)):
                    raise MemoryError("clause_ids 必须引用冻结草稿中的有效条款；漏项可为空数组。")
                if item.get("certainty") not in {"confirmed", "uncertain"}:
                    raise MemoryError("certainty 必须为 confirmed 或 uncertain。")
                normalized.append({"finding_id": f"f{i + 1:04d}", "type": item["type"], "clause_ids": ids,
                    "evidence_tags": _tags(_index(plan), item.get("evidence_tags"), "evidence_tags"),
                    "finding": store._string(item.get("finding"), "finding", 1200),
                    "suggested_action": store._string(item.get("suggested_action"), "suggested_action", 800),
                    "certainty": item["certainty"]})
            summary = request.get("summary", "")
            if not isinstance(summary, str) or len(summary) > 2000 or (not normalized and not summary.strip()):
                raise MemoryError("summary 必须是至多 2000 字的说明；无发现时须说明检查结果。")
            body = {"audit_id": audit_id, "findings": normalized, "summary": summary,
                    "execution_context": context, "primary_receipt_offset": len(_receipts(Path(plan["index_path"]).with_name("read_receipts.jsonl"))),
                    "draft_sha256": plan["original_result_sha256"], "notice": NOTICE}
            if report:
                if report["findings"] != normalized or report["summary"] != summary:
                    raise MemoryError("已冻结的核验报告不能覆盖。")
            else:
                body["report_path"] = _write(Path(plan["directory"]) / "report.json", body)
                report = _put(connection, "agent_audit_reports", audit_id, body)
            return {"audit_id": audit_id, "status": "reported", "finding_count": len(normalized),
                    "report_path": report["report_path"], "notice": NOTICE}
        if action == "inspect":
            if not report:
                raise MemoryError("独立核验尚未完成，请等待子任务返回。")
            response, positions = _page(report["findings"], request.get("cursor", "0"),
                request.get("limit", 10), request.get("max_bytes", 8192),
                {"audit_id": audit_id, "summary": report["summary"], "finding_count": len(report["findings"]),
                 "draft_sha256": report["draft_sha256"], "notice": NOTICE})
            _put(connection, "agent_audit_deliveries", uuid.uuid4().hex,
                 {"audit_id": audit_id, "kind": "report", "positions": positions,
                  "empty": not report["findings"], "execution_context": context})
            return response
        if action == "resolve":
            if not report:
                raise MemoryError("独立核验尚未完成。")
            delivered, empty = _delivered(connection, audit_id, "report")
            if set(range(len(report["findings"]))) - delivered or (not report["findings"] and not empty):
                raise MemoryError("先完整读取核验报告再处置。")
            decisions = request.get("decisions")
            if not isinstance(decisions, list):
                raise MemoryError("decisions 必须逐项处置核验发现。")
            mapping = {item["finding_id"]: item for item in report["findings"]}
            if (any(not isinstance(item, dict) for item in decisions)
                    or len({item.get("finding_id") for item in decisions}) != len(decisions)
                    or {item.get("finding_id") for item in decisions} != set(mapping)):
                raise MemoryError("每个 finding_id 必须且只能处置一次，不能漏项或虚构问题。")
            result_path, evidence_path = Path(request["result_path"]).resolve(), Path(request["evidence_path"]).resolve()
            (result, result_sha), (evidence, evidence_sha) = _capture(result_path), _capture(evidence_path)
            checked = validate_result(_index(plan), result, evidence, allow_demo=bool(_index(plan).get("demo")))
            if not checked["ok"]:
                raise MemoryError("处置后的草稿结构或证据格式未通过。")
            read_tags, _ = _fully_read_tags(plan["index_path"], Path(plan["index_path"]).with_name("read_receipts.jsonl"),
                                          context, report["primary_receipt_offset"])
            normalized = []
            changed = store._hash(result) != store._hash(_read(plan["frozen_result_path"]))
            previous = [item for item in evo._rows(connection, "agent_audit_resolutions") if item["audit_id"] == audit_id]
            cleared = next((item for item in reversed(previous) if item["status"] == "cleared"), None)
            if cleared and (result_sha != cleared["result_sha256"] or evidence_sha != cleared["evidence_sha256"]):
                raise MemoryError("已完成处置的草稿或证据又发生变化，请新建核验票，不能重绑旧报告。")
            if changed and not any(item.get("decision") == "fixed" for item in decisions):
                raise MemoryError("草稿内容已变化，但没有核验发现的修复；请准备新核验票。")
            for item in decisions:
                decision = item.get("decision")
                if decision not in {"fixed", "dismissed", "unresolved"}:
                    raise MemoryError("decision 必须为 fixed / dismissed / unresolved。")
                tags = _tags(_index(plan), item.get("evidence_tags"), "decision.evidence_tags")
                reason = store._string(item.get("reason"), "reason", 1600)
                required_tags = set(tags) | set(mapping[item["finding_id"]]["evidence_tags"])
                if decision != "unresolved" and required_tags - read_tags:
                    raise MemoryError("处置前主代理必须在报告完成后自行回读证据，不能复用核验者的读取回执。")
                if decision == "fixed" and not changed:
                    raise MemoryError("声明 fixed 必须实际修改抽取结果，不能只改说明或清空疑点。")
                normalized.append({"finding_id": item["finding_id"], "decision": decision, "reason": reason, "evidence_tags": tags})
            unresolved = [item["finding_id"] for item in normalized if item["decision"] == "unresolved"]
            ready = not unresolved and not evidence.get("unresolved")
            resolution = {"audit_id": audit_id, "decisions": normalized,
                          "result_sha256": result_sha, "evidence_sha256": evidence_sha,
                          "status": "cleared" if ready else "needs_review", "unresolved": unresolved,
                          "execution_context": context, "reviewed_draft_sha256": plan["original_result_sha256"],
                          "final_draft_independently_reviewed": not changed, "notice": NOTICE}
            if store._file_hash(result_path) != result_sha or store._file_hash(evidence_path) != evidence_sha:
                raise MemoryError("处置期间草稿发生变化，未绑定其他版本，请重试。")
            identity = uuid.uuid4().hex
            if not previous or any(previous[-1].get(key) != value for key, value in resolution.items()):
                resolution["resolution_path"] = _write(Path(plan["directory"]) / ("resolution-" + identity[:24] + ".json"), resolution)
                _put(connection, "agent_audit_resolutions", identity, {**resolution, "resolution_id": identity})
            return {"audit_id": audit_id, "status": resolution["status"], "unresolved": unresolved,
                    "result_sha256": resolution["result_sha256"], "evidence_sha256": resolution["evidence_sha256"],
                    "final_draft_independently_reviewed": not changed, "notice": NOTICE}
        raise MemoryError("未知核验动作。")


def assert_ready(root, index_path, result_path, evidence_path, context):
    """仅真实生产主角色使用；旧无会话 CLI 与独立盲抽试验保留原协议。"""
    context = _context(context, "term-extractor")
    index_path = Path(index_path).resolve()
    with _db(root) as connection:
        plans = [plan for plan in evo._rows(connection, "agent_audit_plans")
                 if Path(plan["index_path"]).resolve() == index_path
                 and plan["controller"]["session_id"] == context["session_id"]]
        if not plans:
            raise MemoryError("本次主会话尚未完成独立核验；请 prepare → term-auditor → inspect → resolve 后提交。")
        plan = plans[-1]
        _frozen(root, plan)
        resolutions = [item for item in evo._rows(connection, "agent_audit_resolutions") if item["audit_id"] == plan["audit_id"]]
        if not resolutions or resolutions[-1]["status"] != "cleared":
            raise MemoryError("独立核验尚有未处理发现或未完成处置，保留草稿。")
        resolution = resolutions[-1]
        if (store._file_hash(Path(result_path)) != resolution["result_sha256"]
                or store._file_hash(Path(evidence_path)) != resolution["evidence_sha256"]):
            raise MemoryError("最终草稿或证据已变化，旧核验处置不再适用；请准备新核验票。")
        report = _one(connection, "agent_audit_reports", plan["audit_id"])
        return {"audit_id": plan["audit_id"], "status": "cleared", "notice": NOTICE,
                "finding_count": len(report["findings"]), "report_path": report["report_path"],
                "resolution_path": resolution["resolution_path"],
                "decision_counts": {kind: sum(item["decision"] == kind for item in resolution["decisions"])
                                    for kind in ("fixed", "dismissed", "unresolved")},
                "reviewed_draft_sha256": resolution["reviewed_draft_sha256"],
                "final_result_sha256": resolution["result_sha256"],
                "final_evidence_sha256": resolution["evidence_sha256"],
                "index_sha256": plan["index_sha256"],
                "final_draft_independently_reviewed": resolution["final_draft_independently_reviewed"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("--request-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(dispatch(args.project_root, store._read_json(args.request_file, 1024 * 1024)), ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
