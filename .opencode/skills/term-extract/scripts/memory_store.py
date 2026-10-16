"""本地经验库：不可变修订、人工发布、冻结任务快照和交付回执。

人工身份来自操作权限与人工维护流程；reviewer/confirmed_by 是审计记录，
不是身份认证。SQLite 与快照哈希用于一致性检查，不是抵抗本机管理员篡改的签名。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class MemoryError(ValueError):
    """可供工具与人工 CLI 处理的经验库错误。"""


ISSUE_TYPES = {"missing_product", "boundary", "quantity", "association"}
PAYLOAD_FIELDS = {"title", "issue_type", "triggers", "applies_when", "rule", "exclusions", "correction_ids"}
SUMMARY_FIELDS = {"fix_improvements", "regressions", "independent_regression_cases", "counterexample_cases", "unfixed_target_cases"}
SNAPSHOT_NAME = "memory_snapshot.json"
RECEIPTS_NAME = "memory_receipts.jsonl"
NOTICE = "词语命中只表示相关建议，不能据此认定适用；请核对适用条件、不适用条件及原文。交付不等于已采用。"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS corrections (
 correction_id TEXT PRIMARY KEY, record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
 memory_id TEXT NOT NULL, revision INTEGER NOT NULL, payload_json TEXT NOT NULL,
 content_sha256 TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(memory_id, revision)
);
CREATE TABLE IF NOT EXISTS evaluations (
 evaluation_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, revision INTEGER NOT NULL,
 report_json TEXT NOT NULL, report_sha256 TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS active (
 memory_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, evaluation_id TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS used_evaluations (evaluation_id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS audit (
 event_id TEXT PRIMARY KEY, memory_id TEXT NOT NULL, revision INTEGER NOT NULL,
 action TEXT NOT NULL, record_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learning_runs (
 run_id TEXT PRIMARY KEY, run_key TEXT NOT NULL UNIQUE, record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learning_feedback (
 feedback_id TEXT PRIMARY KEY, feedback_key TEXT NOT NULL UNIQUE, record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learning_sources (
 source_id TEXT PRIMARY KEY, record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learning_confirmations (
 source_id TEXT PRIMARY KEY, record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS learning_events (
 event_key TEXT PRIMARY KEY, record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise MemoryError("内容不是合法的 UTF-8 JSON。") from exc


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_hash(path: Path) -> str:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError as exc:
        raise MemoryError(f"无法读取文件以核验哈希：{path}") from exc


def _string(value: Any, name: str, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise MemoryError(f"{name} 必须是非空字符串，最多 {maximum} 个字符。")
    return value.strip()


def _integer(value: Any, name: str, minimum: int = 1, maximum: int = 2**31 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise MemoryError(f"{name} 必须是 {minimum} 到 {maximum} 的整数，不能是布尔值。")
    return value


def _pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise MemoryError(f"JSON 字段重复：{key}")
        result[key] = value
    return result


def _parse(text: str) -> Any:
    try:
        return json.loads(text, object_pairs_hook=_pairs,
                          parse_constant=lambda value: (_ for _ in ()).throw(MemoryError(f"非法 JSON 常量：{value}")))
    except (ValueError, TypeError) as exc:
        raise MemoryError(f"JSON 解析失败：{exc}") from exc


def _read_json(path: Path, maximum: int = 8 * 1024 * 1024) -> Any:
    try:
        if path.stat().st_size > maximum:
            raise MemoryError(f"JSON 文件超过允许大小：{path}")
        return _parse(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError) as exc:
        raise MemoryError(f"无法读取 UTF-8 JSON 文件：{path}") from exc


def resolve_store_dir(project_root: Path) -> Path:
    root = Path(project_root).resolve()
    configured = os.environ.get("TERM_EXTRACT_MEMORY_DIR", "").strip()
    selected = Path(os.path.expandvars(configured)).expanduser() if configured else root / ".term-memory"
    return (root / selected).resolve() if not selected.is_absolute() else selected.resolve()


@contextmanager
def _db(root: Path, *, write: bool = False):
    file = resolve_store_dir(root) / "memory.sqlite3"
    if not write and not file.exists():
        yield None
        return
    connection = None
    try:
        if write:
            file.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(file), timeout=15, isolation_level=None)
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(_SCHEMA)
            # 内容表仅允许追加；发布状态用独立活动指针与审计事件表示。
            for table in ("corrections", "revisions", "evaluations", "audit", "used_evaluations",
                          "learning_runs", "learning_feedback", "learning_sources", "learning_confirmations", "learning_events"):
                for operation in ("UPDATE", "DELETE"):
                    connection.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable record'); END")
            connection.execute("BEGIN IMMEDIATE")
        else:
            connection = sqlite3.connect(file.as_uri() + "?mode=ro", uri=True, timeout=15, isolation_level=None)
            connection.execute("BEGIN")
        connection.row_factory = sqlite3.Row
        yield connection
        connection.commit()
    except sqlite3.Error as exc:
        if connection:
            connection.rollback()
        raise MemoryError(f"经验数据库操作失败：{exc}") from exc
    except Exception:
        if connection:
            connection.rollback()
        raise
    finally:
        if connection:
            connection.close()


def _payload(payload: Any) -> dict:
    if not isinstance(payload, dict) or not PAYLOAD_FIELDS <= set(payload) or set(payload) - PAYLOAD_FIELDS - {"source_ids"}:
        raise MemoryError("经验 payload 必须含原七字段，仅可额外含 source_ids。")
    kind = payload["issue_type"]
    if not isinstance(kind, str) or kind not in ISSUE_TYPES:
        raise MemoryError("issue_type 不在支持范围内。")
    result = {"title": _string(payload["title"], "title", 160), "issue_type": kind,
              "applies_when": _string(payload["applies_when"], "applies_when", 1000),
              "rule": _string(payload["rule"], "rule", 2400),
              "exclusions": _string(payload["exclusions"], "exclusions", 1200)}
    lists = [("triggers", 12, 100), ("correction_ids", 50, 128)]
    if "source_ids" in payload:
        lists.append(("source_ids", 50, 128))
    for key, count, size in lists:
        values = payload[key]
        if not isinstance(values, list) or len(values) > count or (key == "triggers" and not values):
            raise MemoryError(f"{key} 必须为字符串数组，最多 {count} 项；triggers 不能为空。")
        result[key] = [_string(value, key, size) for value in values]
        if len(set(result[key])) != len(result[key]):
            raise MemoryError(f"{key} 不允许重复项。")
    if len(_canonical(result)) > 4096:
        raise MemoryError("单条经验 payload 超过 4096 字节，请缩小适用范围并精简，以便完整交付适用与不适用条件。")
    return result


def _correction(connection, correction_id: str) -> dict:
    row = connection.execute("SELECT * FROM corrections WHERE correction_id=?", (correction_id,)).fetchone() if connection else None
    if row is None:
        raise MemoryError(f"纠错记录不存在：{correction_id}")
    record = _parse(row["record_json"])
    if _hash(record) != row["record_sha256"]:
        raise MemoryError("纠错存档哈希不一致。")
    return record


def _candidate(connection, memory_id: str, revision: int) -> dict:
    row = connection.execute("SELECT * FROM revisions WHERE memory_id=? AND revision=?", (memory_id, revision)).fetchone() if connection else None
    if row is None:
        raise MemoryError("指定经验修订不存在。")
    payload = _payload(_parse(row["payload_json"]))
    if _hash(payload) != row["content_sha256"]:
        raise MemoryError("经验修订内容哈希不一致。")
    active = connection.execute("SELECT revision FROM active WHERE memory_id=?", (memory_id,)).fetchone()
    event = connection.execute("SELECT action FROM audit WHERE memory_id=? AND revision=? ORDER BY rowid DESC LIMIT 1", (memory_id, revision)).fetchone()
    state = "published" if active and active["revision"] == revision else ("withdrawn" if event and event["action"] == "withdraw" else "superseded" if event else "candidate")
    if state == "candidate" and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='evoz_decisions'").fetchone():
        from evolution_store import _decision, _candidate_review_status
        decision = _decision(connection, memory_id, revision)
        if decision:
            state = decision["status"]
        review_status = _candidate_review_status(connection, memory_id, revision)
        if review_status["blocking"]:
            state = review_status["state"]
    return {"memory_id": memory_id, "revision": revision, "content_sha256": row["content_sha256"],
            "payload": payload, "state": state, "created_at": row["created_at"]}


def _published(connection) -> list[dict]:
    if connection is None:
        return []
    rows = connection.execute("SELECT memory_id,revision FROM active ORDER BY memory_id").fetchall()
    return [{key: item[key] for key in ("memory_id", "revision", "content_sha256", "payload")}
            for item in (_candidate(connection, row["memory_id"], row["revision"]) for row in rows)]


def _release(entries: list[dict]) -> str:
    return _hash([[entry["memory_id"], entry["revision"], entry["content_sha256"]]
                  for entry in sorted(entries, key=lambda entry: entry["memory_id"])])


def import_correction(root: Path, input_path: Path, before_path: Path, after_path: Path,
                      reason: str, confirmed_by: str, family_id: str) -> dict:
    reason = _string(reason, "reason", 4000)
    confirmed_by = _string(confirmed_by, "confirmed_by（人工确认者审计名）", 160)
    family_id = _string(family_id, "family_id", 200)
    paths = {"input_path": Path(input_path).resolve(), "before_path": Path(before_path).resolve(), "after_path": Path(after_path).resolve()}
    hashes = {"source_sha256": _file_hash(paths["input_path"]), "before_sha256": _file_hash(paths["before_path"]), "after_sha256": _file_hash(paths["after_path"])}
    after = _read_json(paths["after_path"], 2 * 1024 * 1024)
    if not isinstance(after, list):
        raise MemoryError("人工确认的 after 必须为结果 JSON 数组。")
    try:
        before = _read_json(paths["before_path"], 2 * 1024 * 1024)
    except MemoryError:
        before = None  # 格式损坏的 badcase 仅留路径与哈希，不复制原始文本。
    record = {"correction_id": "corr_" + uuid.uuid4().hex, "created_at": _now(),
              "reason": reason, "confirmed_by": confirmed_by, "family_id": family_id,
              **{key: str(value) for key, value in paths.items()}, **hashes,
              "before_json": before, "after_json": after}
    _check_correction_files(record)
    with _db(root, write=True) as connection:
        connection.execute("INSERT INTO corrections VALUES (?,?,?)", (record["correction_id"], _canonical(record).decode(), _hash(record)))
    return {key: value for key, value in record.items() if key not in ("before_json", "after_json")}


def get_correction(root: Path, correction_id: str) -> dict:
    with _db(root) as connection:
        return _correction(connection, _string(correction_id, "correction_id", 128))


def _check_correction_files(record: dict) -> None:
    _string(record.get("confirmed_by"), "人工确认者", 160)
    _string(record.get("reason"), "人工纠错理由", 4000)
    for path_key, hash_key in (("input_path", "source_sha256"), ("before_path", "before_sha256"), ("after_path", "after_sha256")):
        if _file_hash(Path(record[path_key])) != record[hash_key]:
            raise MemoryError(f"纠错依据文件已变化：{path_key}；请重新人工导入并评测。")


def _insert_candidate(connection, payload: dict, memory_id=None) -> dict:
    payload = _payload(payload)
    selected_id = _string(memory_id, "memory_id", 128) if memory_id is not None else "mem_" + uuid.uuid4().hex
    for correction_id in payload["correction_ids"]:
        _correction(connection, correction_id)
    if payload.get("source_ids"):
        from memory_learning import _source
        for source_id in payload["source_ids"]:
            if source_id.startswith("case_"):
                from evolution_store import _case, _check_case
                case = _case(connection, source_id)
                _check_case(case)
                if case["split"] != "development":
                    raise MemoryError("回归/保留集标签不能作为经验归纳来源。")
            else:
                _source(connection, source_id)
    latest = connection.execute("SELECT MAX(revision) FROM revisions WHERE memory_id=?", (selected_id,)).fetchone()[0]
    if memory_id is not None and latest is None:
        raise MemoryError("修订目标不存在；新经验请省略 memory_id。")
    revision = (latest or 0) + 1
    sha = _hash(payload)
    connection.execute("INSERT INTO revisions VALUES (?,?,?,?,?)", (selected_id, revision, _canonical(payload).decode(), sha, _now()))
    return {"memory_id": selected_id, "revision": revision, "content_sha256": sha, "state": "candidate"}


def propose(root: Path, payload: dict, memory_id=None) -> dict:
    with _db(root, write=True) as connection:
        return _insert_candidate(connection, payload, memory_id)


def load_candidate(root: Path, memory_id, revision) -> dict:
    with _db(root) as connection:
        return _candidate(connection, _string(memory_id, "memory_id", 128), _integer(revision, "revision"))


def release_digest(root: Path) -> str:
    with _db(root) as connection:
        return _release(_published(connection))


def _validate_report(report: Any, candidate: dict, base_release: str) -> dict:
    if not isinstance(report, dict) or report.get("candidate") != {key: candidate[key] for key in ("memory_id", "revision", "content_sha256")}:
        raise MemoryError("评测报告未精确绑定指定候选修订。")
    _integer(report["candidate"].get("revision"), "candidate.revision")
    if report.get("base_release_sha256") != base_release:
        raise MemoryError("评测基线 release 已变化，必须重新评测。")
    if report.get("status") not in ("passed", "failed"):
        raise MemoryError("评测 status 只能为 passed/failed。")
    summary = report.get("summary")
    mode = report.get("evaluation_mode", "correction")
    if mode not in ("correction", "success", "paired_labeled_v1"):
        raise MemoryError("evaluation_mode 不在支持范围。")
    expected = SUMMARY_FIELDS | ({"success_cases"} if "evaluation_mode" in report else set())
    if not isinstance(summary, dict) or set(summary) != expected:
        raise MemoryError("评测 summary 字段不完整或存在额外字段。")
    for key in expected:
        _integer(summary[key], "summary." + key, 0)
    if len(_canonical(report)) > 1024 * 1024:
        raise MemoryError("评测报告超过 1 MiB。")
    return report


def _check_learning_sources(connection, payload: dict, report: dict) -> None:
    if report.get("evaluation_mode") == "paired_labeled_v1":
        from evolution_store import check_sealed_report
        check_sealed_report(connection, payload, report)
        return
    if not payload.get("source_ids"):
        if report.get("evaluation_mode", "correction") != "correction":
            raise MemoryError("成功经验评测必须绑定人工确认的成功来源。")
        return
    from memory_learning import _get_sources
    sources = _get_sources(connection, payload, require_confirmed=True)
    mode = "correction" if any(item["kind"] == "correction" for item in sources) else "success"
    if report.get("evaluation_mode") != mode:
        raise MemoryError("评测模式与已确认来源类型不一致，不能绕过纠错门槛。")
    expected = sorted([{"source_id": item["source_id"], "confirmation_sha256": item["confirmation_sha256"]}
                       for item in sources if item["source_id"] in payload["source_ids"]], key=lambda item: item["source_id"])
    actual = report.get("source_confirmations")
    if not isinstance(actual, list) or any(not isinstance(item, dict) or set(item) != {"source_id", "confirmation_sha256"} for item in actual):
        raise MemoryError("评测必须绑定全部新来源的人工确认记录哈希。")
    if sorted(actual, key=lambda item: str(item["source_id"])) != expected:
        raise MemoryError("评测来源确认记录与当前候选不一致。")


def register_evaluation(root: Path, memory_id, revision, report: dict) -> dict:
    # 仅供离线评测模块使用；没有任意 report 导入 CLI 或自动发布工具。
    if isinstance(report, dict) and report.get("evaluation_mode") == "paired_labeled_v1":
        raise MemoryError("自动实验报告只能由内部运行器注册，不能通过旧评测入口重复导入。")
    memory_id, revision = _string(memory_id, "memory_id", 128), _integer(revision, "revision")
    with _db(root, write=True) as connection:
        candidate = _candidate(connection, _string(memory_id, "memory_id", 128), _integer(revision, "revision"))
        report = _validate_report(report, candidate, _release(_published(connection)))
        _check_learning_sources(connection, candidate["payload"], report)
        evaluation_id, sha = "eval_" + uuid.uuid4().hex, _hash(report)
        connection.execute("INSERT INTO evaluations VALUES (?,?,?,?,?,?)", (evaluation_id, memory_id, revision, _canonical(report).decode(), sha, _now()))
    return {"evaluation_id": evaluation_id, "report_sha256": sha, "status": report["status"], "summary": report["summary"]}


def _evaluation(connection, evaluation_id: str) -> dict:
    row = connection.execute("SELECT * FROM evaluations WHERE evaluation_id=?", (evaluation_id,)).fetchone() if connection else None
    if row is None:
        raise MemoryError("评测记录不存在；不能用外部报告文件替代内部注册的评测。")
    report = _parse(row["report_json"])
    if _hash(report) != row["report_sha256"]:
        raise MemoryError("内部评测报告哈希不一致。")
    return {"evaluation_id": evaluation_id, "report_sha256": row["report_sha256"], "report": report, "memory_id": row["memory_id"], "revision": row["revision"]}


def _check_artifacts(report: dict) -> None:
    artifacts = report.get("artifacts", [])
    if not isinstance(artifacts, list):
        raise MemoryError("report.artifacts 必须为 path/sha256 对象数组。")
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str) or not isinstance(artifact.get("sha256"), str):
            raise MemoryError("评测 artifact 必须具有 path 和 sha256。")
        path = Path(artifact["path"])
        if not path.is_absolute() or _file_hash(path) != artifact["sha256"]:
            raise MemoryError("评测外部依据文件已变化、缺失或路径非绝对路径，必须重新评测。")


def _audit(connection, memory_id, revision, action, reviewer, note, details) -> str:
    event_id = "event_" + uuid.uuid4().hex
    record = {"reviewer": reviewer, "note": note, **details}
    connection.execute("INSERT INTO audit VALUES (?,?,?,?,?,?)", (event_id, memory_id, revision, action, _canonical(record).decode(), _now()))
    return event_id


def publish(root: Path, memory_id, revision, evaluation_id, reviewer: str, note: str) -> dict:
    reviewer, note = _string(reviewer, "reviewer（人工审阅者审计名）", 160), _string(note, "发布说明", 2000)
    memory_id, revision = _string(memory_id, "memory_id", 128), _integer(revision, "revision")
    evaluation_id = _string(evaluation_id, "evaluation_id", 128)
    with _db(root, write=True) as connection:
        candidate = _candidate(connection, _string(memory_id, "memory_id", 128), _integer(revision, "revision"))
        if candidate["state"] == "published":
            raise MemoryError("该修订已发布；不要重复发布。")
        if candidate["state"] in {"rejected", "merged", "needs_review"}:
            raise MemoryError("该候选被拒绝、合并或仍待复核，不能发布。")
        evaluation = _evaluation(connection, _string(evaluation_id, "evaluation_id", 128))
        if connection.execute("SELECT 1 FROM used_evaluations WHERE evaluation_id=?", (evaluation_id,)).fetchone():
            raise MemoryError("该评测已用于发布，不能再次使用；撤回后的重新发布需要重新评测。")
        if evaluation["memory_id"] != memory_id or evaluation["revision"] != revision:
            raise MemoryError("评测记录属于其他候选修订。")
        report = _validate_report(evaluation["report"], candidate, _release(_published(connection)))
        _check_learning_sources(connection, candidate["payload"], report)
        if report.get("evaluation_mode") == "paired_labeled_v1":
            from evolution_store import _find
            experiment = _find(connection, "evoz_experiments", "experiment_id", report["automation"]["experiment_id"])
            if experiment["evaluation_id"] != evaluation_id:
                raise MemoryError("自动发布必须使用原内部实验的唯一评测记录。")
        summary = report["summary"]
        target_passed = summary.get("success_cases", 0) >= 1 if report.get("evaluation_mode") in {"success", "paired_labeled_v1"} else summary["fix_improvements"] >= 1
        if not (report["status"] == "passed" and target_passed and summary["regressions"] == 0
                and summary["independent_regression_cases"] >= 1 and summary["counterexample_cases"] >= 1 and summary["unfixed_target_cases"] == 0):
            raise MemoryError("发布门槛未通过：需有修复改善或成功案例、独立回归和反例，且无退化或未通过目标。")
        if not candidate["payload"]["correction_ids"] and not candidate["payload"].get("source_ids"):
            raise MemoryError("发布必须有人工确认的来源。")
        for correction_id in candidate["payload"]["correction_ids"]:
            _check_correction_files(_correction(connection, correction_id))
        _check_artifacts(report)
        event_id = _audit(connection, memory_id, revision, "publish", reviewer, note,
                          {"content_sha256": candidate["content_sha256"], "payload": candidate["payload"],
                           "evaluation_id": evaluation_id, "report_sha256": evaluation["report_sha256"]})
        connection.execute("INSERT INTO active VALUES (?,?,?) ON CONFLICT(memory_id) DO UPDATE SET revision=excluded.revision,evaluation_id=excluded.evaluation_id", (memory_id, revision, evaluation_id))
        connection.execute("INSERT INTO used_evaluations VALUES (?)", (evaluation_id,))
        digest = _release(_published(connection))
    return {"memory_id": memory_id, "revision": revision, "state": "published", "content_sha256": candidate["content_sha256"], "evaluation_id": evaluation_id, "report_sha256": evaluation["report_sha256"], "release_sha256": digest, "event_id": event_id}


def withdraw(root: Path, memory_id, reviewer: str, note: str, expected_revision=None) -> dict:
    reviewer, note = _string(reviewer, "reviewer（人工审阅者审计名）", 160), _string(note, "撤回说明", 2000)
    memory_id = _string(memory_id, "memory_id", 128)
    with _db(root, write=True) as connection:
        row = connection.execute("SELECT revision FROM active WHERE memory_id=?", (_string(memory_id, "memory_id", 128),)).fetchone()
        if row is None:
            raise MemoryError("该经验没有当前已发布修订。")
        if expected_revision is not None and row["revision"] != _integer(expected_revision, "expected_revision"):
            raise MemoryError("活动修订已变化，请刷新页面后再撤回。")
        candidate = _candidate(connection, memory_id, row["revision"])
        event_id = _audit(connection, memory_id, row["revision"], "withdraw", reviewer, note, {"content_sha256": candidate["content_sha256"]})
        connection.execute("DELETE FROM active WHERE memory_id=?", (memory_id,))
        digest = _release(_published(connection))
    return {"memory_id": memory_id, "revision": row["revision"], "state": "withdrawn", "release_sha256": digest, "event_id": event_id}


def list_memories(root: Path) -> dict:
    with _db(root) as connection:
        entries = _published(connection)
        result = []
        if connection:
            for row in connection.execute("SELECT memory_id,MAX(revision) AS revision FROM revisions GROUP BY memory_id ORDER BY memory_id"):
                latest = _candidate(connection, row["memory_id"], row["revision"])
                active = next((item for item in entries if item["memory_id"] == row["memory_id"]), None)
                result.append({"memory_id": row["memory_id"], "latest_revision": row["revision"], "active_revision": active["revision"] if active else None,
                               "state": latest["state"], "title": latest["payload"]["title"], "issue_type": latest["payload"]["issue_type"], "content_sha256": latest["content_sha256"]})
        return {"memories": result, "release_sha256": _release(entries)}


def _snapshot(index_path: Path) -> dict:
    path = index_path.parent / SNAPSHOT_NAME
    snapshot = _read_json(path)
    if not isinstance(snapshot, dict) or type(snapshot.get("schema_version")) is not int or snapshot.get("schema_version") != 1:
        raise MemoryError("经验快照格式或版本错误。")
    claimed = snapshot.get("snapshot_sha256")
    body = {key: value for key, value in snapshot.items() if key != "snapshot_sha256"}
    if _hash(body) != claimed:
        raise MemoryError("经验快照内容哈希不一致。")
    if snapshot.get("index_sha256") != _file_hash(index_path):
        raise MemoryError("经验快照不属于当前 index。")
    entries, seen = snapshot.get("memories"), set()
    if not isinstance(entries, list):
        raise MemoryError("经验快照 memories 必须为数组。")
    for entry in entries:
        if not isinstance(entry, dict):
            raise MemoryError("经验快照条目必须为对象。")
        memory_id = _string(entry.get("memory_id"), "memory_id", 128)
        _integer(entry.get("revision"), "revision")
        if memory_id in seen or _hash(_payload(entry.get("payload"))) != entry.get("content_sha256"):
            raise MemoryError("经验快照存在重复条目或规则内容哈希不一致。")
        seen.add(memory_id)
    if _release(entries) != snapshot.get("release_sha256"):
        raise MemoryError("经验快照 release 哈希不一致。")
    return snapshot


def create_snapshot(root: Path, index_path: Path) -> dict:
    index_path = Path(index_path).resolve()
    index_hash = _file_hash(index_path)
    target = index_path.parent / SNAPSHOT_NAME
    if not target.exists():
        with _db(root) as connection:
            entries = _published(connection)
            body = {"schema_version": 1, "created_at": _now(), "index_sha256": index_hash,
                    "release_sha256": _release(entries), "memories": entries}
        body["snapshot_sha256"] = _hash(body)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".memory-snapshot-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(_canonical(body) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            # 原子首次发布；并发调用不得覆盖已经冻结的任务快照。
            try:
                os.link(temporary, target)
            except FileExistsError:
                pass
        except OSError as exc:
            raise MemoryError("无法原子保存经验快照。") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    snapshot = _snapshot(index_path)
    return {"snapshot_path": str(target), "snapshot_sha256": snapshot["snapshot_sha256"], "index_sha256": snapshot["index_sha256"],
            "release_sha256": snapshot["release_sha256"], "memory_count": len(snapshot["memories"])}


def _terms(text: str) -> set[str]:
    from memory_lexical import terms
    return terms(text)


def recall(root: Path, index_path: Path, query: str, limit: int = 3, max_bytes: int = 6144,
           issue_type=None, record_receipt: bool = True) -> dict:
    query = _string(query, "query", 512)
    _integer(limit, "limit", 1, 5)
    _integer(max_bytes, "max_bytes", 512, 24576)
    if issue_type is not None and (not isinstance(issue_type, str) or issue_type not in ISSUE_TYPES):
        raise MemoryError("issue_type 不在支持范围内。")
    if type(record_receipt) is not bool:
        raise MemoryError("record_receipt 必须是布尔值。")
    index_path = Path(index_path).resolve()
    snapshot = _snapshot(index_path)
    query_terms = _terms(query)
    ranked = []
    if query_terms:
        for entry in snapshot["memories"]:
            payload = entry["payload"]
            if issue_type is not None and payload["issue_type"] != issue_type:
                continue
            trigger_terms = _terms(" ".join(payload["triggers"]))
            text_terms = _terms(" ".join([payload["title"], payload["applies_when"], payload["rule"]]))
            score = len(query_terms & trigger_terms) * 3 + len(query_terms & text_terms)
            if score:
                ranked.append((score, entry))
    ranked.sort(key=lambda pair: (-pair[0], pair[1]["memory_id"], pair[1]["revision"]))
    response = {"schema_version": 1, "snapshot_sha256": snapshot["snapshot_sha256"], "release_sha256": snapshot["release_sha256"],
                "index_sha256": snapshot["index_sha256"], "memories": [], "notice": NOTICE, "budget_omitted": 0}
    if len(_canonical(response)) > max_bytes:
        raise MemoryError("max_bytes 不足以交付必要的快照身份信息。")
    for _, entry in ranked[:limit]:
        response["memories"].append(entry)
        if len(_canonical(response)) > max_bytes:
            response["memories"].pop()
            response["budget_omitted"] += 1
    if len(_canonical(response)) > max_bytes:
        raise MemoryError("max_bytes 不足以交付响应。")
    if record_receipt:
        record_delivery(index_path, response)
    return response


def _validate_delivery(snapshot: dict, response: Any) -> list[dict]:
    if not isinstance(response, dict) or type(response.get("schema_version")) is not int or response.get("schema_version") != 1 or any(response.get(key) != snapshot[key] for key in ("snapshot_sha256", "index_sha256", "release_sha256")):
        raise MemoryError("经验交付记录未绑定当前任务快照。")
    entries = response.get("memories")
    if not isinstance(entries, list) or len(entries) > 5:
        raise MemoryError("经验交付条目必须为最多五项的数组。")
    lookup = {entry["memory_id"]: entry for entry in snapshot["memories"]}
    delivered, seen = [], set()
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("memory_id"), str):
            raise MemoryError("经验交付条目格式错误。")
        _integer(entry.get("revision"), "交付 revision")
        original = lookup.get(entry["memory_id"])
        if original is None or entry != original or entry["memory_id"] in seen:
            raise MemoryError("经验交付条目不是快照中的完整原始规则，或条目重复。")
        seen.add(entry["memory_id"])
        delivered.append({key: entry[key] for key in ("memory_id", "revision", "content_sha256")})
    return delivered


def record_delivery(index_path: Path, response: dict) -> None:
    index_path = Path(index_path).resolve()
    snapshot = _snapshot(index_path)
    delivered = _validate_delivery(snapshot, response)
    receipt = {"schema_version": 1, "time": _now(), "snapshot_sha256": snapshot["snapshot_sha256"],
               "release_sha256": snapshot["release_sha256"], "index_sha256": snapshot["index_sha256"], "delivered": delivered}
    try:
        data = _canonical(receipt) + b"\n"
        fd = os.open(index_path.parent / RECEIPTS_NAME, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            if os.write(fd, data) != len(data):
                raise MemoryError("经验回执写入不完整。")
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise MemoryError("无法保存经验交付回执。") from exc


def run_memory_record(index_path: Path) -> dict:
    index_path = Path(index_path).resolve()
    path = index_path.parent / RECEIPTS_NAME
    if not (index_path.parent / SNAPSHOT_NAME).exists():
        if path.exists() and path.stat().st_size:
            raise MemoryError("存在经验回执但缺少任务快照，不能验证交付。")
        return {"status": "legacy_no_snapshot", "snapshot_sha256": None, "release_sha256": None, "delivered": [], "notice": "旧任务缺少经验快照，不能推断其使用过任何经验。"}
    snapshot = _snapshot(index_path)
    delivered = {}
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise MemoryError("无法读取经验回执。") from exc
        lookup = {entry["memory_id"]: entry for entry in snapshot["memories"]}
        for line in lines:
            if not line.strip():
                continue
            receipt = _parse(line)
            if not isinstance(receipt, dict) or type(receipt.get("schema_version")) is not int or receipt.get("schema_version") != 1 or any(receipt.get(key) != snapshot[key] for key in ("snapshot_sha256", "index_sha256", "release_sha256")):
                raise MemoryError("经验回执快照身份不一致或格式错误。")
            items = receipt.get("delivered")
            if not isinstance(items, list) or len(items) > 5:
                raise MemoryError("经验回执 delivered 格式错误。")
            seen = set()
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("memory_id"), str):
                    raise MemoryError("经验回执条目格式错误。")
                _integer(item.get("revision"), "回执 revision")
                original = lookup.get(item["memory_id"])
                if original is None or item != {key: original[key] for key in ("memory_id", "revision", "content_sha256")} or item["memory_id"] in seen:
                    raise MemoryError("经验回执引用了快照之外或哈希不匹配的规则。")
                seen.add(item["memory_id"])
                delivered[item["memory_id"]] = item
    return {"status": "frozen_snapshot", "snapshot_sha256": snapshot["snapshot_sha256"], "release_sha256": snapshot["release_sha256"],
            "index_sha256": snapshot["index_sha256"], "delivered": [delivered[key] for key in sorted(delivered)], "notice": "记录的是实际交付条目，不能据此认定已采用或适用。"}


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise MemoryError("命令参数错误：" + message)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    try:
        parser = _Parser(description="本地经验修订、人工发布及冻结快照。身份字段为审计记录，不替代操作系统访问权限。")
        parser.add_argument("--project-root", type=Path, default=Path.cwd())
        sub = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
        action = sub.add_parser("import-correction")
        for name in ("input", "before", "after", "reason", "confirmed-by", "family-id"):
            action.add_argument("--" + name, required=True)
        action = sub.add_parser("propose")
        action.add_argument("--payload-json", required=True)
        action.add_argument("--memory-id")
        sub.add_parser("list")
        action = sub.add_parser("show")
        action.add_argument("--memory-id", required=True)
        action.add_argument("--revision", required=True, type=int)
        action = sub.add_parser("show-correction")
        action.add_argument("--correction-id", required=True)
        action = sub.add_parser("show-evaluation")
        action.add_argument("--evaluation-id", required=True)
        action = sub.add_parser("publish")
        for name in ("memory-id", "evaluation-id", "reviewer", "note"):
            action.add_argument("--" + name, required=True)
        action.add_argument("--revision", required=True, type=int)
        action = sub.add_parser("withdraw")
        for name in ("memory-id", "reviewer", "note"):
            action.add_argument("--" + name, required=True)
        action = sub.add_parser("create-snapshot")
        action.add_argument("--index", required=True, type=Path)
        action = sub.add_parser("recall")
        action.add_argument("--index", required=True, type=Path)
        action.add_argument("--query", required=True)
        action.add_argument("--limit", type=int, default=3)
        action.add_argument("--max-bytes", type=int, default=6144)
        action.add_argument("--issue-type", choices=sorted(ISSUE_TYPES))
        action.add_argument("--defer-receipt", action="store_true")
        action = sub.add_parser("record-delivery")
        action.add_argument("--index", required=True, type=Path)
        action.add_argument("--response-json", required=True)
        args = parser.parse_args(argv)
        root = args.project_root.resolve()
        if args.command == "import-correction":
            result = import_correction(root, Path(args.input), Path(args.before), Path(args.after), args.reason, args.confirmed_by, args.family_id)
        elif args.command == "propose":
            result = propose(root, _parse(args.payload_json), args.memory_id)
        elif args.command == "list":
            result = list_memories(root)
        elif args.command == "show":
            result = load_candidate(root, args.memory_id, args.revision)
        elif args.command == "show-correction":
            result = {key: value for key, value in get_correction(root, args.correction_id).items() if key not in ("before_json", "after_json")}
        elif args.command == "show-evaluation":
            with _db(root) as connection:
                result = _evaluation(connection, args.evaluation_id)
        elif args.command == "publish":
            result = publish(root, args.memory_id, args.revision, args.evaluation_id, args.reviewer, args.note)
        elif args.command == "withdraw":
            result = withdraw(root, args.memory_id, args.reviewer, args.note)
        elif args.command == "create-snapshot":
            result = create_snapshot(root, args.index)
        elif args.command == "recall":
            result = recall(root, args.index, args.query, args.limit, args.max_bytes, args.issue_type, not args.defer_receipt)
        else:
            record_delivery(args.index, _parse(args.response_json))
            result = {"recorded": True}
        print(_canonical(result).decode())
        return 0
    except (MemoryError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
