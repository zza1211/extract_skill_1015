"""运行归档、机器复盘与用户反馈；候选不代表事实确认或发布。

只使用本地 SQLite 与文件，不调用模型。人工确认 CLI 依赖操作权限，姓名是审计名。
结果保存在内容地址私有工件与不可变数据库记录中，源标书仅保留路径和哈希。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

import memory_store as store

MemoryError = store.MemoryError
INSIGHT_FIELDS = {"title", "issue_type", "triggers", "applies_when", "rule", "exclusions", "observation", "evidence_tags"}


def _table_record(connection, table: str, key: str, value: str) -> dict:
    exists = connection and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
    row = connection.execute(f"SELECT record_json,record_sha256 FROM {table} WHERE {key}=?", (value,)).fetchone() if exists else None
    if row is None:
        raise MemoryError(f"学习记录不存在：{value}")
    record = store._parse(row["record_json"])
    if store._hash(record) != row["record_sha256"]:
        raise MemoryError("学习记录内容哈希不一致。")
    return record


def _insert(connection, table: str, key: str, value: str, record: dict) -> None:
    connection.execute(f"INSERT INTO {table} ({key},record_json,record_sha256) VALUES (?,?,?)",
                       (value, store._canonical(record).decode(), store._hash(record)))


def _artifact(root: Path, value) -> dict:
    data = store._canonical(value) + b"\n"
    digest = hashlib.sha256(data).hexdigest()
    directory = store.resolve_store_dir(root) / "learning-artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / (digest + ".json")
    if not target.exists():
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=directory, suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if temporary and temporary.exists():
                temporary.unlink()
    if store._file_hash(target) != digest:
        raise MemoryError("私有学习工件已变化，禁止覆盖。")
    return {"path": str(target), "sha256": digest}


def _index(path: Path, expected_hash=None) -> tuple[dict, str]:
    from document_io import load_index
    path = Path(path).resolve()
    index, fingerprint = load_index(path)
    if index.get("demo") is not False or index.get("production_submittable") is not True:
        raise MemoryError("demo 或非生产输入不得进入学习来源。")
    if expected_hash is not None and fingerprint != expected_hash:
        raise MemoryError("来源 index 已变化，请重新登记并确认。")
    source = Path(store._string(index.get("source_path"), "source_path", 4096)).resolve()
    if store._file_hash(source) != index.get("source_sha256"):
        raise MemoryError("源标书已变化或缺失，请重新注册。")
    return index, fingerprint


def validate_reflection(reflection: dict, index: dict) -> dict:
    if not isinstance(reflection, dict) or set(reflection) != {"insights", "no_experience_reason"}:
        raise MemoryError("reflection 只能含 insights 和 no_experience_reason。")
    insights = reflection["insights"]
    reason = reflection["no_experience_reason"]
    if not isinstance(insights, list) or len(insights) > 3:
        raise MemoryError("insights 必须为数组，最多三条。")
    if not isinstance(reason, str) or len(reason) > 1000:
        raise MemoryError("no_experience_reason 必须为最多1000字符的字符串。")
    if not insights and not reason.strip():
        raise MemoryError("无经验时必须说明 no_experience_reason。")
    tags = {unit.get("tag") for unit in index.get("units", []) if isinstance(unit, dict) and unit.get("tag")}
    normalized = []
    for insight in insights:
        if not isinstance(insight, dict) or set(insight) != INSIGHT_FIELDS:
            raise MemoryError("每条 insight 必须含六个规则字段、observation 和 evidence_tags。")
        payload = store._payload({key: value for key, value in insight.items() if key not in {"observation", "evidence_tags"}} | {"correction_ids": []})
        # 预留实际机器来源编号，避免提交前通过、落候选时才超预算。
        store._payload({**payload, "source_ids": ["feedback_" + "0" * 32]})
        evidence_tags = insight["evidence_tags"]
        if not isinstance(evidence_tags, list) or not 1 <= len(evidence_tags) <= 30:
            raise MemoryError("经验观察需有1到30个 evidence_tags，不能以猜测作为来源。")
        if any(not isinstance(tag, str) or tag not in tags for tag in evidence_tags) or len(set(evidence_tags)) != len(evidence_tags):
            raise MemoryError("evidence_tags 必须为本次原文中存在且不重复的 C 标签。")
        normalized.append({key: value for key, value in payload.items() if key != "correction_ids"} |
                          {"observation": store._string(insight["observation"], "observation", 1000), "evidence_tags": evidence_tags})
    result = {"insights": normalized, "no_experience_reason": reason.strip()}
    if len(store._canonical(result)) > 16384:
        raise MemoryError("reflection 超过16 KiB，请精简。")
    return result


def _run(connection, run_id: str) -> dict:
    return _table_record(connection, "learning_runs", "run_id", store._string(run_id, "run_id", 128))


def _check_record_files(record: dict) -> None:
    index, _ = _index(Path(record["index_path"]), record["index_sha256"])
    if index["source_sha256"] != record["source_sha256"] or str(Path(index["source_path"]).resolve()) != record["input_path"]:
        raise MemoryError("学习来源与原文索引绑定不一致。")
    for artifact in record["artifacts"]:
        if store._file_hash(Path(artifact["path"])) != artifact["sha256"]:
            raise MemoryError("不可变学习工件已缺失或变化。")


def register_run(root: Path, index_path: Path, result_path: Path, evidence_path: Path, run_path: Path) -> dict:
    index_path = Path(index_path).resolve()
    index, fingerprint = _index(index_path)
    result = store._read_json(Path(result_path), 2 * 1024 * 1024)
    evidence = store._read_json(Path(evidence_path), 2 * 1024 * 1024)
    run = store._read_json(Path(run_path))
    if isinstance(run, dict) and run.get("trial_only") is True:
        raise MemoryError("独立试验结果不能登记为学习来源。")
    from validate_result import validate_result
    validation = validate_result(index, result, evidence)
    if not validation["ok"] or not isinstance(evidence, dict) or evidence.get("unresolved"):
        raise MemoryError("只能登记结构通过且未解决事项为空的生产结果；仍不代表语义正确。")
    if not isinstance(run, dict) or run.get("status") != "submitted" or run.get("production_submittable") is not True:
        raise MemoryError("学习登记需要已成功提交的真实运行。")
    if run.get("source_sha256") != index["source_sha256"] or run.get("coverage", {}).get("complete") is not True or run["coverage"].get("index_sha256") != fingerprint:
        raise MemoryError("run 的源文件、全文覆盖或 index 绑定不一致。")
    for field, file in (("result_sha256", result_path), ("evidence_sha256", evidence_path)):
        if field in run and run[field] != store._file_hash(Path(file)):
            raise MemoryError("提交结果/证据文件与 run 哈希不一致。")
    generation = {field: run[field] for field in ("model_id", "skill_version", "skill_sha256", "reference_sha256", "memory", "execution_context") if field in run}
    key = store._hash([fingerprint, store._hash(result), store._hash(evidence), generation])
    run_id = "run_" + key[:32]
    # run.json 的 learning 摘要会在登记后更新，不参与身份或外部漂移检查。
    archived_run = {key: value for key, value in run.items() if key != "learning"}
    snapshots = {"result": _artifact(root, result), "evidence": _artifact(root, evidence), "run": _artifact(root, archived_run)}
    record = {"run_id": run_id, "source_id": run_id, "created_at": store._now(),
              "input_path": str(Path(index["source_path"]).resolve()), "source_sha256": index["source_sha256"],
              "index_path": str(index_path), "index_sha256": fingerprint,
              "result_path": snapshots["result"]["path"], "result_sha256": snapshots["result"]["sha256"],
              "evidence_path": snapshots["evidence"]["path"], "evidence_sha256": snapshots["evidence"]["sha256"],
              "run_snapshot_path": snapshots["run"]["path"], "artifacts": list(snapshots.values()),
              "generation": generation,
              "result_json": result, "evidence_json": evidence, "run_json": archived_run}
    with store._db(root, write=True) as connection:
        existing = connection.execute("SELECT run_id FROM learning_runs WHERE run_key=?", (key,)).fetchone()
        if not existing:
            connection.execute("INSERT INTO learning_runs VALUES (?,?,?,?)", (run_id, key, store._canonical(record).decode(), store._hash(record)))
            _insert(connection, "learning_sources", "source_id", run_id, {"source_id": run_id, "kind": "success", "run_id": run_id})
        else:
            _check_record_files(_run(connection, existing["run_id"]))
    return get_run(root, run_id)


def _source(connection, source_id: str) -> dict:
    return _table_record(connection, "learning_sources", "source_id", store._string(source_id, "source_id", 128))


def _confirmation(connection, source_id: str):
    row = connection.execute("SELECT record_json,record_sha256 FROM learning_confirmations WHERE source_id=?", (source_id,)).fetchone() if connection else None
    if row is None:
        return None
    record = store._parse(row["record_json"])
    if store._hash(record) != row["record_sha256"]:
        raise MemoryError("人工确认记录哈希不一致。")
    return record


def _has_confirmed_correction(connection, run: dict) -> bool:
    for row in connection.execute("SELECT f.feedback_id FROM learning_feedback f JOIN learning_confirmations c ON c.source_id=f.feedback_id"):
        feedback = _table_record(connection, "learning_feedback", "feedback_id", row["feedback_id"])
        baseline = _run(connection, feedback["run_id"])
        same_output = baseline["source_sha256"] == run["source_sha256"] and baseline["result_sha256"] == run["result_sha256"]
        if same_output and feedback["after_json"] is not None and feedback["after_json"] != run["result_json"]:
            _confirmation(connection, feedback["feedback_id"])
            return True
    return False


def _reflection(connection, run: dict, source_id: str, reflection: dict) -> dict:
    event_key = "reflection:" + source_id
    prior = connection.execute("SELECT record_json,record_sha256 FROM learning_events WHERE event_key=?", (event_key,)).fetchone()
    if prior:
        event = _table_record(connection, "learning_events", "event_key", event_key)
        if event["reflection"] != reflection:
            raise MemoryError("该来源已有不同复盘；请保留原记录并通过新的反馈或候选修订补充，不能静默覆盖。")
        return event
    candidates = []
    for insight in reflection["insights"]:
        payload = {key: value for key, value in insight.items() if key not in {"observation", "evidence_tags"}}
        payload.update(correction_ids=[], source_ids=[source_id])
        candidates.append(store._insert_candidate(connection, payload))
    event = {"source_id": source_id, "run_id": run["run_id"], "reflection": reflection,
             "candidates": candidates, "created_at": store._now(), "status": "unverified_candidates" if candidates else "no_experience"}
    _insert(connection, "learning_events", "event_key", event_key, event)
    return event


def record_reflection(root: Path, run_id: str, reflection: dict) -> dict:
    with store._db(root, write=True) as connection:
        run = _run(connection, run_id)
        _check_record_files(run)
        index, _ = _index(Path(run["index_path"]), run["index_sha256"])
        return _reflection(connection, run, run_id, validate_reflection(reflection, index))


def record_feedback(root: Path, run_id: str, feedback_text: str, after_result_or_None=None, reflection=None) -> dict:
    text = store._string(feedback_text, "用户反馈原话", 8000)
    # 保留原话，包括首尾空白；校验不修改审计内容。
    text = feedback_text
    with store._db(root, write=True) as connection:
        run = _run(connection, run_id)
        _check_record_files(run)
        index, _ = _index(Path(run["index_path"]), run["index_sha256"])
        if isinstance(after_result_or_None, (str, Path)):
            after = store._read_json(Path(after_result_or_None), 2 * 1024 * 1024)
        else:
            after = after_result_or_None
        if after is not None:
            from validate_result import validate_result
            if not validate_result(index, after)["ok"]:
                raise MemoryError("反馈的正确结果未通过七字段和 C 标签结构校验。")
        normalized = validate_reflection(reflection if reflection is not None else
                                         {"insights": [], "no_experience_reason": "仅记录用户反馈，尚未提供可迁移经验。"}, index)
        key = store._hash([run_id, text, after])
        feedback_id = "feedback_" + key[:32]
        prior = connection.execute("SELECT feedback_id FROM learning_feedback WHERE feedback_key=?", (key,)).fetchone()
        if prior:
            record = _table_record(connection, "learning_feedback", "feedback_id", feedback_id)
        else:
            artifact = _artifact(root, after) if after is not None else None
            record = {"feedback_id": feedback_id, "source_id": feedback_id, "run_id": run_id,
                      "feedback_text": text, "created_at": store._now(), "after_json": after,
                      "after_path": artifact["path"] if artifact else None,
                      "after_sha256": artifact["sha256"] if artifact else None}
            connection.execute("INSERT INTO learning_feedback VALUES (?,?,?,?)", (feedback_id, key, store._canonical(record).decode(), store._hash(record)))
            _insert(connection, "learning_sources", "source_id", feedback_id, {"source_id": feedback_id, "kind": "correction", "run_id": run_id, "feedback_id": feedback_id})
        event = _reflection(connection, run, feedback_id, normalized)
        return {"feedback_id": feedback_id, "source_id": feedback_id, "run_id": run_id,
                "confirmed": _confirmation(connection, feedback_id) is not None, "candidates": event["candidates"], "status": "pending_confirmation"}


def _get_sources(connection, payload: dict, *, require_confirmed=True) -> list[dict]:
    sources = []
    for cid in payload.get("correction_ids", []):
        record = store._correction(connection, cid)
        store._check_correction_files(record)
        sources.append({**{key: value for key, value in record.items() if key not in {"before_json", "after_json"}},
                        "source_id": cid, "kind": "correction", "confirmed": True,
                        "artifacts": [{"path": record[p], "sha256": record[h]} for p, h in
                                      (("input_path", "source_sha256"), ("before_path", "before_sha256"), ("after_path", "after_sha256"))]})
    for source_id in payload.get("source_ids", []):
        source = _source(connection, source_id)
        run = _run(connection, source["run_id"])
        _check_record_files(run)
        confirmation = _confirmation(connection, source_id)
        invalidated = source["kind"] == "success" and _has_confirmed_correction(connection, run)
        if invalidated and require_confirmed:
            raise MemoryError("该运行已有人工确认纠错，原成功来源失效，不能继续用于评测或发布。")
        if require_confirmed and confirmation is None:
            raise MemoryError(f"来源尚未经人工确认，不得评测或发布：{source_id}")
        result = {key: run[key] for key in ("input_path", "source_sha256", "index_path", "index_sha256", "result_path", "result_sha256")}
        result.update(source_id=source_id, kind=source["kind"], confirmed=confirmation is not None,
                      invalidated=invalidated,
                      confirmation=confirmation, confirmation_sha256=store._hash(confirmation) if confirmation else None,
                      family_id=confirmation["family_id"] if confirmation else None,
                      artifacts=[*run["artifacts"], {"path": run["index_path"], "sha256": run["index_sha256"]},
                                 {"path": run["input_path"], "sha256": run["source_sha256"]}])
        if source["kind"] == "correction":
            feedback = _table_record(connection, "learning_feedback", "feedback_id", source["feedback_id"])
            result.update(feedback_id=feedback["feedback_id"], feedback_text=feedback["feedback_text"],
                          before_path=run["result_path"], before_sha256=run["result_sha256"],
                          after_path=feedback["after_path"], after_sha256=feedback["after_sha256"])
            if feedback["after_path"]:
                if store._file_hash(Path(feedback["after_path"])) != feedback["after_sha256"]:
                    raise MemoryError("反馈正确结果快照已变化。")
                result["artifacts"].append({"path": feedback["after_path"], "sha256": feedback["after_sha256"]})
            elif require_confirmed:
                raise MemoryError("纠错来源尚无确认的正确结果。")
        sources.append(result)
    return sources


def get_sources(root: Path, payload: dict, require_confirmed=True) -> list[dict]:
    payload = store._payload(payload)
    with store._db(root) as connection:
        return _get_sources(connection, payload, require_confirmed=require_confirmed)


evaluation_sources = get_sources


def show_source(root: Path, source_id: str) -> dict:
    with store._db(root) as connection:
        source = _source(connection, source_id)
        payload = {"correction_ids": [], "source_ids": [source_id]}
        result = _get_sources(connection, payload, require_confirmed=False)[0]
        event_key = "reflection:" + source_id
        row = connection.execute("SELECT record_sha256 FROM learning_events WHERE event_key=?", (event_key,)).fetchone()
        result["reflection_event"] = _table_record(connection, "learning_events", "event_key", event_key) if row else None
        result["reflection_sha256"] = row["record_sha256"] if row else None
        return result


def _confirm(root: Path, source_id: str, kind: str, confirmed_by: str, family_id: str, note: str) -> dict:
    fields = {"confirmed_by": store._string(confirmed_by, "人工确认人审计名", 160),
              "family_id": store._string(family_id, "family_id", 200), "note": store._string(note, "人工确认说明", 4000)}
    with store._db(root, write=True) as connection:
        source = _source(connection, source_id)
        if source["kind"] != kind:
            raise MemoryError("确认入口与来源类型不一致。")
        run = _run(connection, source["run_id"])
        _check_record_files(run)
        if kind == "success" and _has_confirmed_correction(connection, run):
            raise MemoryError("该运行已有人工确认纠错，不能再确认原结果为成功案例。")
        if kind == "correction":
            feedback = _table_record(connection, "learning_feedback", "feedback_id", source_id)
            if feedback["after_path"] is None or feedback["after_json"] == run["result_json"]:
                raise MemoryError("确认纠错需要实际不同的正确结果；仅用户原话不能冒充完整正确标签。")
            if store._file_hash(Path(feedback["after_path"])) != feedback["after_sha256"]:
                raise MemoryError("正确结果快照已变化。")
        prior = _confirmation(connection, source_id)
        if prior:
            if any(prior[key] != value for key, value in fields.items()):
                raise MemoryError("该来源已被确认，不允许覆盖确认依据；请登记新的反馈。")
            record = prior
        else:
            record = {"source_id": source_id, "kind": kind, **fields, "confirmed_at": store._now()}
            _insert(connection, "learning_confirmations", "source_id", source_id, record)
        return {"source_id": source_id, "confirmed": True, "confirmation": record, "confirmation_sha256": store._hash(record)}


def confirm_run(root: Path, run_id: str, confirmed_by: str, family_id: str, note: str) -> dict:
    return _confirm(root, run_id, "success", confirmed_by, family_id, note)


def confirm_feedback(root: Path, feedback_id: str, confirmed_by: str, family_id: str, note: str) -> dict:
    return _confirm(root, feedback_id, "correction", confirmed_by, family_id, note)


def _run_summary(connection, run: dict) -> dict:
    result = {key: value for key, value in run.items() if key not in {"result_json", "evidence_json", "run_json", "artifacts"}}
    event = connection.execute("SELECT record_json FROM learning_events WHERE event_key=?", ("reflection:" + run["run_id"],)).fetchone()
    reflection = _table_record(connection, "learning_events", "event_key", "reflection:" + run["run_id"]) if event else None
    confirmation = _confirmation(connection, run["run_id"])
    result.update(confirmed=confirmation is not None, confirmation=confirmation,
                  status=reflection["status"] if reflection else "pending_reflection",
                  candidates=reflection["candidates"] if reflection else [])
    return result


def get_run(root: Path, run_id: str) -> dict:
    with store._db(root) as connection:
        return _run_summary(connection, _run(connection, run_id))


def list_learning(root: Path, limit: int = 20) -> dict:
    store._integer(limit, "limit", 1, 50)
    with store._db(root) as connection:
        if connection is None or not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_runs'").fetchone():
            return {"runs": [], "feedback": []}
        runs = [_run_summary(connection, _run(connection, row["run_id"])) for row in
                connection.execute("SELECT run_id FROM learning_runs ORDER BY rowid DESC LIMIT ?", (limit,))]
        feedback = []
        for row in connection.execute("SELECT feedback_id FROM learning_feedback ORDER BY rowid DESC LIMIT ?", (limit,)):
            record = _table_record(connection, "learning_feedback", "feedback_id", row["feedback_id"])
            event = _table_record(connection, "learning_events", "event_key", "reflection:" + record["feedback_id"])
            feedback.append({"feedback_id": record["feedback_id"], "source_id": record["source_id"], "run_id": record["run_id"],
                             "feedback_preview": record["feedback_text"][:160], "has_corrected_result": record["after_path"] is not None,
                             "confirmed": _confirmation(connection, record["feedback_id"]) is not None, "candidates": event["candidates"]})
        return {"runs": runs, "feedback": feedback}


def main(argv=None) -> int:
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise MemoryError(message)
    try:
        parser = Parser(description="运行复盘与人工确认；不自动确认或发布经验")
        parser.add_argument("--project-root", required=True)
        commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
        action = commands.add_parser("register-run")
        for name in ("index", "result", "evidence", "run"):
            action.add_argument("--" + name, required=True)
        action = commands.add_parser("reflect")
        action.add_argument("--run-id", required=True)
        exclusive = action.add_mutually_exclusive_group(required=True)
        exclusive.add_argument("--reflection-json")
        exclusive.add_argument("--reflection-file")
        action = commands.add_parser("feedback")
        action.add_argument("--run-id", required=True)
        action.add_argument("--request-file")
        action.add_argument("--text")
        exclusive = action.add_mutually_exclusive_group()
        exclusive.add_argument("--after-result")
        exclusive.add_argument("--after-result-json")
        action.add_argument("--reflection-json")
        action = commands.add_parser("list")
        action.add_argument("--limit", type=int, default=20)
        action = commands.add_parser("show-run")
        action.add_argument("--run-id", required=True)
        action = commands.add_parser("show-source")
        action.add_argument("--source-id", required=True)
        for name, key in (("confirm-run", "run-id"), ("confirm-feedback", "feedback-id")):
            action = commands.add_parser(name)
            for field in (key, "confirmed-by", "family-id", "note"):
                action.add_argument("--" + field, required=True)
        args = parser.parse_args(argv)
        root = Path(args.project_root).resolve()
        if args.command == "register-run":
            result = register_run(root, Path(args.index), Path(args.result), Path(args.evidence), Path(args.run))
        elif args.command == "reflect":
            reflection = (store._read_json(Path(args.reflection_file), 3 * 1024 * 1024)
                          if args.reflection_file is not None else store._parse(args.reflection_json))
            result = record_reflection(root, args.run_id, reflection)
        elif args.command == "feedback":
            if args.request_file is not None:
                if any(value is not None for value in (args.text, args.after_result, args.after_result_json, args.reflection_json)):
                    raise MemoryError("--request-file 不能与 --text/--after-result/--after-result-json/--reflection-json 混用。")
                request = store._read_json(Path(args.request_file), 3 * 1024 * 1024)
                if not isinstance(request, dict) or set(request) != {"feedback_text", "after_result", "reflection"}:
                    raise MemoryError("反馈请求文件必须且只能含 feedback_text/after_result/reflection。")
                if (not isinstance(request["feedback_text"], str)
                        or (request["after_result"] is not None and not isinstance(request["after_result"], list))
                        or not isinstance(request["reflection"], dict)):
                    raise MemoryError("反馈请求需为字符串 feedback_text、数组或null的 after_result、对象 reflection。")
                text, after, reflection = request["feedback_text"], request["after_result"], request["reflection"]
            else:
                if args.text is None:
                    raise MemoryError("未使用 --request-file 时必须提供 --text。")
                text = args.text
                after = store._parse(args.after_result_json) if args.after_result_json else Path(args.after_result) if args.after_result else None
                reflection = store._parse(args.reflection_json) if args.reflection_json else None
            result = record_feedback(root, args.run_id, text, after, reflection)
        elif args.command == "list":
            result = list_learning(root, args.limit)
        elif args.command == "show-run":
            result = get_run(root, args.run_id)
        elif args.command == "show-source":
            result = show_source(root, args.source_id)
        elif args.command == "confirm-run":
            result = confirm_run(root, args.run_id, args.confirmed_by, args.family_id, args.note)
        else:
            result = confirm_feedback(root, args.feedback_id, args.confirmed_by, args.family_id, args.note)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
