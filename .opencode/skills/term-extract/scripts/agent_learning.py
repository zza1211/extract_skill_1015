"""OpenCode 主 agent 的学习原子工具：归档、冻结、比较和候选，不调用模型。

会话信息由 TypeScript 工具上下文注入。它是本机运行溯源，不是防本机管理员
伪造的身份系统；标签只在同一会话提交完整抽取并冻结后交付。
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sys

import evolution_store as evolution
import memory_learning as learning
import memory_store as store
from learning_compare import apply_judgments, compare_results

MemoryError = store.MemoryError
TABLES = ("agent_studies", "agent_learning_events", "agent_label_disclosures")
MAIN_AGENT = "term-extractor"


@contextmanager
def _db(root, *, write=False):
    with store._db(Path(root), write=write) as connection:
        if connection is not None and write:
            for table in TABLES:
                connection.execute(f"CREATE TABLE IF NOT EXISTS {table} (record_id TEXT PRIMARY KEY, record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)")
                for operation in ("UPDATE", "DELETE"):
                    connection.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable record'); END")
        yield connection


def _context(value):
    if not isinstance(value, dict) or set(value) != {"session_id", "message_id", "agent"}:
        raise MemoryError("需要 OpenCode 工具注入 execution_context，不能由模型填写会话身份。")
    result = {key: store._string(value[key], "execution_context." + key, 256) for key in value}
    if result["agent"] != MAIN_AGENT:
        raise MemoryError("仅 term-extractor 主 agent 可以处理标签学习；盲抽试验 agent 不可读取标签。")
    return result


def _records(connection, table):
    return evolution._rows(connection, table)


def _study(connection, study_id):
    result = next((item for item in _records(connection, "agent_studies") if item["study_id"] == study_id), None)
    if result is None:
        raise MemoryError("标签学习记录不存在。")
    return result


def _events(connection, study_id):
    return [item for item in _records(connection, "agent_learning_events") if item["study_id"] == study_id]


def _owned(study, context):
    context = _context(context)
    if study["execution_context"]["session_id"] != context["session_id"]:
        raise MemoryError("标签学习只能在创建它的 OpenCode 会话中继续；其他会话不能提前查看标签。")
    return context


def _assert_blind_context(connection, context, source_type, source_key):
    """同一会话一旦见过其他来源标签，就不能再提交新的盲抽成绩。"""
    for item in _records(connection, "agent_label_disclosures"):
        if item["session_id"] == context["session_id"] and (item["source_type"], item["source_key"]) != (source_type, source_key):
            raise MemoryError("本会话已查看其他标书或验证案例的标签；请新建 OpenCode 会话开始下一篇盲抽学习，避免答案历史泄漏。")
    # 兼容尚无统一 disclosure 记录的既有冻结任务。
    for previous in _records(connection, "agent_studies"):
        if previous["session_id"] == context["session_id"] and _frozen(_events(connection, previous["study_id"])):
            if ("study", previous["study_id"]) != (source_type, source_key):
                raise MemoryError("本会话已揭示其他学习标签；请新建 OpenCode 会话开始下一篇盲抽学习。")


def assert_blind_context(root, execution_context, *, source_type="study", source_key=None):
    context = _context(execution_context)
    with _db(root) as connection:
        _assert_blind_context(connection, context, source_type, source_key)
    return {"blind_context": True}


def _disclose(connection, context, source_type, source_key):
    source_type = store._string(source_type, "source_type", 80)
    source_key = store._string(source_key, "source_key", 256)
    identity = {"session_id": context["session_id"], "source_type": source_type, "source_key": source_key}
    disclosure_id = "disclosure_" + store._hash(identity)[:32]
    old = next((item for item in _records(connection, "agent_label_disclosures") if item["disclosure_id"] == disclosure_id), None)
    if not old:
        evolution._put(connection, "agent_label_disclosures", disclosure_id, {"disclosure_id": disclosure_id, **identity,
                       "execution_context": context, "created_at": store._now()})
    return {"disclosure_id": disclosure_id, "recorded": True, "replayed": old is not None}


def record_label_disclosure(root, execution_context, *, source_type, source_key):
    """共享给试验查看工具，在交付任何标签差异前记录；不发起模型调用。"""
    context = _context(execution_context)
    with _db(root, write=True) as connection:
        return _disclose(connection, context, source_type, source_key)


def _event(connection, study_id, kind, payload, context):
    identity = {"study_id": study_id, "kind": kind, "payload": payload}
    event_id = "study_event_" + store._hash(identity)[:32]
    previous = next((item for item in _events(connection, study_id) if item["event_id"] == event_id), None)
    if previous:
        return previous
    event = {"event_id": event_id, **identity, "execution_context": context, "created_at": store._now()}
    evolution._put(connection, "agent_learning_events", event_id, event)
    return event


def _load(root, study_id, execution_context=None, *, owner=False):
    with _db(root) as connection:
        study, events = _study(connection, study_id), _events(connection, study_id)
    if owner:
        _owned(study, execution_context)
    return study, events


def _validate_study(root, study):
    index, fingerprint = learning._index(Path(study["index_path"]), study["index_sha256"])
    trusted = evolution.get_case(root, study["case_id"])
    if trusted["source_sha256"] != index["source_sha256"] or trusted["policy_sha256"] != evolution.policy_digest(root):
        raise MemoryError("学习输入或业务口径已变化，请重新建立标签学习任务。")
    if trusted["split"] != "development":
        raise MemoryError("只有 development 案例可用于归纳经验，回归及保留集不能用于调参。")
    return index, trusted


def _frozen(events):
    return next((item["payload"] for item in events if item["kind"] == "prediction_frozen"), None)


def _summary(study, events, current_report=None):
    frozen = _frozen(events)
    comparisons = [item["payload"]["comparison"] for item in events if item["kind"] == "judged"]
    report = current_report or (comparisons[-1] if comparisons else frozen["comparison"] if frozen else None)
    reflected = next((item["payload"] for item in events if item["kind"] == "reflected"), None)
    candidates = reflected["candidates"] if reflected else []
    reviews = list(dict.fromkeys(review for event in events for review in event["payload"].get("review_ids", [])))
    state = "reflected" if reflected else "needs_review" if report and report["status"] == "needs_review" else "compared" if frozen else "awaiting_blind_extraction"
    return {key: study[key] for key in ("study_id", "case_id", "family_id", "input_path", "index_path", "created_at")} | {
        "state": state, "run_id": frozen["run_id"] if frozen else None, "candidates": candidates, "review_ids": reviews,
        "comparison_summary": {"status": report["status"], "summary": report["summary"]} if report else None,
        "next_action": "使用 term_document 和 term_result 完成全文抽取并提交，再 compare 冻结预测后查看标签。" if not frozen else
                       "处理待确认局部差异后继续复盘。" if state == "needs_review" else
                       "可为候选准备独立盲抽试验；未通过验证的候选不进入正式召回。" if reflected else
                       "核对差异；仅对 review 项作有原文依据的判断，再提交可迁移经验或无经验原因。",
        "notice": "本接口不返回标签或标签文件位置。自检和结构通过不代表真实业务正确。"}


def begin_study(root, *, input_path, labels_path, family_id, index_path, execution_context, declared_by="用户提供"):
    context = _context(execution_context)
    index_path, input_path = Path(index_path).resolve(), Path(input_path).resolve()
    index, fingerprint = learning._index(index_path)
    if Path(index["source_path"]).resolve() != input_path or store._file_hash(input_path) != index["source_sha256"]:
        raise MemoryError("学习输入与 term_document_prepare 的索引不匹配。")
    if not index_path.with_name("memory_snapshot.json").is_file():
        raise MemoryError("缺少本任务正式经验快照，请先使用 term_document_prepare。")
    # 另一个标书必须使用新会话，避免已揭示的学习/验证标签留在盲抽上下文中。
    with _db(root) as connection:
        session_studies = [item for item in _records(connection, "agent_studies") if item["session_id"] == context["session_id"]]
        matches = [item["study_id"] for item in session_studies if item["index_sha256"] == fingerprint and item["index_path"] == str(index_path)]
        _assert_blind_context(connection, context, "study", matches[-1] if matches else None)
    # import_case 在私有库校验标签，但不向 agent 返回内容或标签路径。
    trusted = evolution.import_case(root, input_path, labels_path, family_id, "development", declared_by)
    identity = {"case_id": trusted["case_id"], "index_sha256": fingerprint, "index_path": str(index_path), "session_id": context["session_id"]}
    study_id = "study_" + store._hash(identity)[:32]
    with _db(root, write=True) as connection:
        _assert_blind_context(connection, context, "study", study_id)
        existing = next((item for item in _records(connection, "agent_studies") if item["study_id"] == study_id), None)
        if not existing:
            record = {"study_id": study_id, **identity, "family_id": trusted["family_id"], "input_path": str(input_path),
                      "source_sha256": index["source_sha256"], "policy_sha256": trusted["policy_sha256"],
                      "execution_context": context, "created_at": store._now()}
            evolution._put(connection, "agent_studies", study_id, record)
    return {**get_study(root, study_id), "replayed": existing is not None}


def get_study(root, study_id, execution_context=None):
    study, events = _load(root, study_id, execution_context)
    return _summary(study, events, _current_report(root, study, events) if _frozen(events) else None)


def list_studies(root, execution_context=None):
    with _db(root) as connection:
        studies = [(study, _events(connection, study["study_id"])) for study in _records(connection, "agent_studies")]
    items = [_summary(study, events, _current_report(root, study, events) if _frozen(events) else None) for study, events in studies]
    return {"items": list(reversed(items)), "total": len(items)}


def _check_committed_run(root, study, run_id, context):
    from commit_result import coverage_report
    with store._db(Path(root)) as connection:
        run = learning._run(connection, run_id)
    learning._check_record_files(run)
    if run["index_sha256"] != study["index_sha256"] or Path(run["index_path"]).resolve() != Path(study["index_path"]).resolve() or run["source_sha256"] != study["source_sha256"]:
        raise MemoryError("预测运行不是本学习任务的同一索引和原文。")
    archived = run["run_json"]
    generated = _context(archived.get("execution_context"))
    if generated["session_id"] != context["session_id"] or run.get("generation", {}).get("execution_context") != generated:
        raise MemoryError("预测提交必须来自当前同一主 agent 会话。")
    if run["created_at"] < study["created_at"]:
        raise MemoryError("不能复用学习任务创建前的历史预测；请在本任务中独立抽取并提交。")
    if archived.get("status") != "submitted" or archived.get("production_submittable") is not True or archived.get("coverage", {}).get("complete") is not True:
        raise MemoryError("必须先提交结构通过且全文交付完整的生产抽取。")
    receipt_path = Path(study["index_path"]).with_name("read_receipts.jsonl")
    report = coverage_report(Path(study["index_path"]), receipt_path)
    if report.get("complete") is not True:
        raise MemoryError("原文读取回执不完整，不能冻结学习预测。")
    receipts = [json.loads(line) for line in receipt_path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if not receipts or any(_context(receipt.get("execution_context"))["session_id"] != context["session_id"] for receipt in receipts):
        raise MemoryError("全文读取回执必须来自本学习任务的同一主 agent 会话。")
    return run, learning._artifact(Path(root), receipts)


def compare_study(root, study_id, run_id, execution_context, **page_options):
    study, events = _load(root, study_id, execution_context, owner=True)
    context = _owned(study, execution_context)
    index, trusted = _validate_study(root, study)
    frozen = _frozen(events)
    if frozen:
        if frozen["run_id"] != run_id:
            raise MemoryError("该学习任务已冻结其他预测，禁止看过标签后替换盲抽结果。")
        return get_comparison(root, study_id, execution_context, **page_options)
    assert_blind_context(root, context, source_type="study", source_key=study_id)
    run, receipts = _check_committed_run(root, study, run_id, context)
    labels = store._read_json(Path(trusted["labels_archive_path"]), 2 * 1024 * 1024)
    comparison = compare_results(index, run["result_json"], labels)
    payload = {"run_id": run_id, "prediction_sha256": store._hash(run["result_json"]), "labels_sha256": store._hash(labels),
               "result_path": run["result_path"], "result_sha256": run["result_sha256"], "receipts": receipts, "comparison": comparison}
    with _db(root, write=True) as connection:
        current = _frozen(_events(connection, study_id))
        if current and current["run_id"] != run_id:
            raise MemoryError("其他调用已冻结不同预测，请重新读取状态。")
        if not current:
            _assert_blind_context(connection, context, "study", study_id)
            _event(connection, study_id, "prediction_frozen", payload, context)
            _disclose(connection, context, "study", study_id)
    return get_comparison(root, study_id, execution_context, **page_options)


def _current_report(root, study, events):
    frozen = _frozen(events)
    if frozen is None:
        raise MemoryError("预测尚未冻结；先完成抽取、提交并 compare 后才可查看标签或学习。")
    if store._file_hash(Path(frozen["result_path"])) != frozen["result_sha256"]:
        raise MemoryError("已冻结的预测工件发生变化。")
    report = frozen["comparison"]
    for event in events:
        if event["kind"] == "judged":
            report = event["payload"]["comparison"]
    review_ids = list(dict.fromkeys(review for event in events for review in event["payload"].get("review_ids", [])))
    judgments = []
    unresolved = {item["difference_id"] for item in report["differences"] if item["severity"] == "review"}
    for review_id in review_ids:
        review = evolution.get_review(root, review_id)
        decision = review.get("resolution")
        difference_id = review["subject"].get("difference_id")
        if decision and difference_id in unresolved:
            judgments.append({"difference_id": difference_id, "decision": decision["decision"],
                              "reason": decision.get("answer") or "人工已针对本案例局部差异作出判断。"})
    return apply_judgments(report, judgments) if judgments else report


def _page(items, cursor, budget):
    if type(cursor) is not int or not 0 <= cursor <= len(items):
        raise MemoryError("分页 cursor 超出范围。")
    page, size, position = [], 2, cursor
    while position < len(items) and len(page) < 50:
        item = items[position]
        item_size = len(store._canonical(item)) + 1
        if size + item_size > budget:
            if not page:
                # 单条差异上下文可能较长；仅截原文邻域，保留明确截断信号和问题。
                if isinstance(item, dict) and "context" in item:
                    item = {**item, "context": {"issue": item["context"].get("issue"), "error_kind": item["context"].get("error_kind"),
                                               "context_truncated": True, "notice": "单条上下文超过预算，请按 tags 使用原文工具局部核对。"}}
                    item_size = len(store._canonical(item)) + 1
                if size + item_size > budget:
                    raise MemoryError("单条标签/差异超过分页预算，请增大 max_bytes。")
            else:
                break
        page.append(item)
        size += item_size
        position += 1
    return page, position if position < len(items) else None


def get_comparison(root, study_id, execution_context, label_cursor=0, difference_cursor=0, max_bytes=65536):
    study, events = _load(root, study_id, execution_context, owner=True)
    _, trusted = _validate_study(root, study)
    if type(max_bytes) is not int or not 8192 <= max_bytes <= 98304:
        raise MemoryError("max_bytes 必须是 8192 到 98304 的整数。")
    report = _current_report(root, study, events)
    labels = store._read_json(Path(trusted["labels_archive_path"]), 2 * 1024 * 1024)
    label_page, next_label = _page(labels, label_cursor, max_bytes // 3)
    differences, next_difference = _page(report["differences"], difference_cursor, max_bytes // 2)
    result = {"study_id": study_id, "state": "needs_review" if report["status"] == "needs_review" else "compared",
              "prediction_frozen": True, "run_id": _frozen(events)["run_id"],
              "labels_page": label_page, "label_count": len(labels), "next_label_cursor": next_label,
              "comparison": {key: value for key, value in report.items() if key not in {"differences", "objects"}} | {"differences": differences},
              "difference_count": len(report["differences"]), "next_difference_cursor": next_difference,
              "notice": "标签仅用于已冻结预测的学习；不得将修正结果替换为盲抽成绩。分页未结束时继续取页，局部上下文不足时回读原文。"}
    if len(store._canonical(result)) > max_bytes:
        raise MemoryError("比较视图超过 max_bytes，请增加预算或减少局部内容。")
    return result


def judge_study(root, study_id, judgments, execution_context):
    study, events = _load(root, study_id, execution_context, owner=True)
    context = _owned(study, execution_context)
    _validate_study(root, study)
    if not isinstance(judgments, list) or len(judgments) > 50 or len(store._canonical(judgments)) > 128 * 1024:
        raise MemoryError("一次最多复核50个局部差异，总计不得超过128 KiB。")
    identity = store._hash(judgments)
    previous = next((item for item in events if item["kind"] == "judged" and item["payload"].get("judgments_sha256") == identity), None)
    if previous:
        return {**get_study(root, study_id), "replayed": True}
    report = _current_report(root, study, events)
    judged = apply_judgments(report, judgments)
    review_ids = []
    by_id = {item["difference_id"]: item for item in judged["differences"]}
    for judgment in judgments:
        if judgment["decision"] != "uncertain":
            continue
        difference = by_id[judgment["difference_id"]]
        review = evolution.create_review(root, kind="learning_difference", title="标注学习：局部差异需确认", question=difference["question"] or difference["message"],
            context={"difference": difference, "reason": judgment["reason"]}, choices=["harmless", "error", "uncertain"],
            subject={"study_id": study_id, "case_id": study["case_id"], "phase": "learning", "difference_id": difference["difference_id"],
                     "prediction_sha256": judged["prediction_sha256"], "labels_sha256": judged["labels_sha256"]},
            scope="case", dedupe_key={"study_id": study_id, "difference_id": difference["difference_id"]})
        review_ids.append(review["review_id"])
    with _db(root, write=True) as connection:
        # 拒绝并发覆盖，而不是在另一组判断基础上悄悄写回旧报告。
        if [item["event_id"] for item in _events(connection, study_id)] != [item["event_id"] for item in events]:
            raise MemoryError("学习记录已变化，请刷新后重试局部复核。")
        _event(connection, study_id, "judged", {"judgments": judgments, "judgments_sha256": identity, "comparison": judged, "review_ids": review_ids}, context)
    return {**get_study(root, study_id), "comparison_summary": {"status": judged["status"], "summary": judged["summary"]}, "replayed": False}


def reflect_study(root, study_id, reflection, execution_context):
    study, events = _load(root, study_id, execution_context, owner=True)
    context = _owned(study, execution_context)
    index, trusted = _validate_study(root, study)
    report = _current_report(root, study, events)
    normalized = learning.validate_reflection(reflection, index)
    previous = next((item for item in events if item["kind"] == "reflected"), None)
    if previous:
        if previous["payload"]["reflection"] != normalized:
            raise MemoryError("本次标签学习已有不同复盘，禁止覆盖历史；新证据应建立新的学习记录。")
        return {**get_study(root, study_id), "replayed": True}
    if report["status"] == "needs_review":
        raise MemoryError("仍有未确定的局部差异，请先完成语义核对或处理人工待办，再归纳经验。")
    candidates = []
    for insight in normalized["insights"]:
        payload = {key: value for key, value in insight.items() if key not in {"observation", "evidence_tags"}}
        payload.update(source_ids=[trusted["case_id"]], correction_ids=[])
        candidate = evolution.propose_candidate(root, payload,
            origin={"kind": "agent_label_learning", "study_id": study_id, "run_id": _frozen(events)["run_id"], "case_id": trusted["case_id"],
                    "comparison_status": report["status"], "execution_context": context},
            evidence={"observation": insight["observation"], "evidence_tags": insight["evidence_tags"],
                      "prediction_sha256": report["prediction_sha256"], "labels_sha256": report["labels_sha256"]})
        candidates.append({key: candidate[key] for key in ("memory_id", "revision", "content_sha256", "state", "deduplicated")})
    with _db(root, write=True) as connection:
        current = next((item for item in _events(connection, study_id) if item["kind"] == "reflected"), None)
        if current and current["payload"]["reflection"] != normalized:
            raise MemoryError("其他调用已提交不同复盘，请刷新记录。")
        if not current:
            _event(connection, study_id, "reflected", {"reflection": normalized, "comparison_summary": {"status": report["status"], "summary": report["summary"]}, "candidates": candidates}, context)
    return {**get_study(root, study_id), "replayed": False}


def _case_summary(case):
    return {key: case[key] for key in ("case_id", "family_id", "split", "input_path", "source_sha256", "policy_sha256", "trust", "created_at") if key in case} | {"valid": case.get("valid", True)}


def import_case(root, *, input_path, labels_path, family_id, split, execution_context, declared_by="用户提供", note=""):
    _context(execution_context)
    case = evolution.import_case(root, input_path, labels_path, family_id, split, declared_by, note)
    return _case_summary(case) | {"replayed": case["replayed"], "notice": "已登记用户提供的当前口径标签；标签内容与归档位置不进入抽取会话。"}


def list_cases(root, execution_context=None, q="", cursor=0, limit=50):
    items = [_case_summary(item) for item in evolution.list_cases(root)["items"]]
    if q:
        needle = str(q).casefold().strip()
        items = [item for item in items if needle in (item["input_path"] + " " + item["family_id"]).casefold()]
    if type(cursor) is not int or not 0 <= cursor <= len(items) or type(limit) is not int or not 1 <= limit <= 100:
        raise MemoryError("案例分页 cursor 非法或 limit 不在1到100之间。")
    position = min(len(items), cursor + limit)
    return {"items": items[cursor:position], "total": len(items), "next_cursor": position if position < len(items) else None,
            "notice": "仅列来源与用途；标签与标签归档路径不会进入抽取上下文。"}


def case_preview(root, case_id, execution_context, max_chars=6000, q=""):
    _context(execution_context)
    if type(max_chars) is not int or not 500 <= max_chars <= 12000:
        raise MemoryError("max_chars 必须是500到12000的整数。")
    case = evolution.get_case(root, case_id)
    if case["split"] == "holdout":
        raise MemoryError("holdout 保留集不能用于归纳经验或挑选调参试验。")
    index = store._read_json(Path(case["index_path"]), 48 * 1024 * 1024)
    units = index.get("units", [])
    if q:
        needle = str(q).casefold()
        matched = [i for i, unit in enumerate(units) if needle in unit.get("text", "").casefold()]
        positions = sorted({i + delta for i in matched for delta in (-1, 0, 1) if 0 <= i + delta < len(units)})
        selected = [units[i] for i in positions]
    else:
        selected = units
    excerpts, left, complete = [], max_chars, True
    for unit in selected:
        if left <= 0:
            complete = False
            break
        original = unit.get("source_text", unit.get("text", ""))
        text = original[:left]
        excerpts.append({key: unit.get(key) for key in ("tag", "block_index", "table_id", "row", "col")} | {"text": text, "truncated": len(text) < len(original)})
        left -= len(text)
        complete = complete and len(text) == len(original)
    return {**_case_summary(case), "excerpts": excerpts, "truncated": not complete, "matched_query": q,
            "notice": "这是用于挑选独立回归/反例的原文预览，未提供标签；预览不算试验抽取全文交付。"}


def reply_review(root, review_id, decision, raw_user_reply, execution_context, expected_revision=1):
    context = _context(execution_context)
    reply = store._string(raw_user_reply, "raw_user_reply", 8000)
    result = evolution.resolve_review(root, review_id, decision, reply, actor="用户对话回复", channel="conversation",
                                      source_ref="OpenCode:" + context["session_id"] + "/" + context["message_id"], expected_revision=expected_revision)
    record_label_disclosure(root, context, source_type="review", source_key=review_id)
    return _review_summary(result) | {"replied": True, "replayed": result["replayed"],
                                     "notice": "已保存用户局部答复；不确认整篇或全局规则。新盲抽请使用新会话。"}


def _review_summary(review):
    allowed_ids = {"study_id", "case_id", "memory_id", "revision", "phase", "difference_id", "prediction_sha256", "labels_sha256"}
    subject = {key: value for key, value in review["subject"].items() if key in allowed_ids and
               (type(value) is int or isinstance(value, str) and len(value) <= 256)}
    return {key: review[key] for key in ("review_id", "kind", "state", "scope", "created_at", "revision")} | {"subject": subject}


def list_reviews(root, execution_context, state="open", cursor=0, limit=20, include_context=False, max_bytes=32768):
    """默认只给无答案待办目录；读取问题/依据属于显式解封，会污染后续盲抽。"""
    context = _context(execution_context)
    if type(include_context) is not bool or type(max_bytes) is not int or not 4096 <= max_bytes <= 32768:
        raise MemoryError("include_context 必须为布尔值，max_bytes 必须在4096到32768之间。")
    reviews = list(reversed(evolution.list_reviews(root, state)["items"]))
    if type(cursor) is not int or not 0 <= cursor <= len(reviews) or type(limit) is not int or not 1 <= limit <= 100:
        raise MemoryError("待办分页 cursor 非法或 limit 不在1到100之间。")
    selected, records, position = [], [], cursor
    response = {"items": records, "total": len(reviews), "next_cursor": None, "include_context": include_context,
                "notice": "默认仅列待办编号和状态，不交付答案依据。需要问题详情时显式 include_context=true；之后新盲抽请使用新会话。"}
    while position < len(reviews) and len(records) < limit:
        review = reviews[position]
        item = review if include_context else _review_summary(review)
        # 单项过大时不能以截断上下文假装完整；保留目录并明确引导网页查看。
        if len(store._canonical(item)) > max_bytes - 2048:
            item = _review_summary(review) | {"context_omitted": True, "notice": "单项待办详情超过交付预算，本次仅返回目录；请在网页查看完整问题和依据。"}
        records.append(item)
        if len(store._canonical(response)) > max_bytes - 512:
            records.pop()
            if not records:
                raise MemoryError("单项待办目录仍超过预算，请在网页查看。")
            break
        selected.append(review["review_id"])
        position += 1
    response["next_cursor"] = position if position < len(reviews) else None
    if include_context and selected:
        with _db(root, write=True) as connection:
            for review_id in selected:
                _disclose(connection, context, "review", review_id)
        response["notice"] = "已显式读取待办问题/依据，并登记本会话接触标签的记录；已冻结学习仍可继续，新盲抽请新建会话。"
    if len(store._canonical(response)) > max_bytes:
        raise MemoryError("待办视图超过预算，请减少 limit。")
    return response


def main(argv=None):
    parser = argparse.ArgumentParser(description="OpenCode 自主编排的学习原子工具，不调用模型")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[4])
    parser.add_argument("action", choices=("begin", "compare", "comparison", "judge", "reflect", "status", "studies", "import-case", "cases", "preview", "reviews", "reply"))
    parser.add_argument("--request-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        request = store._read_json(args.request_file, 512 * 1024)
        if not isinstance(request, dict):
            raise MemoryError("request 必须为对象。")
        root = args.project_root
        actions = {"begin": begin_study, "compare": compare_study, "comparison": get_comparison, "judge": judge_study,
                   "reflect": reflect_study, "status": get_study, "studies": list_studies, "cases": list_cases, "preview": case_preview,
                   "reply": reply_review, "import-case": import_case, "reviews": list_reviews}
        result = actions[args.action](root, **request)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (MemoryError, ValueError, OSError, TypeError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
