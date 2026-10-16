"""按标书校验结果：可编辑工作副本、追加修订、显式确认与待学习队列。

不修改 learning_runs 内容地址快照，不把整篇丢弃当成负例；GET 完全只读。
SQLite 写事务串行化网页/代理操作，文件哈希 CAS 防止覆盖用户的外部修改。
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import uuid

import memory_learning as learning
import memory_store as store
from validate_result import validate_result

MemoryError = store.MemoryError
PAGE_CHARS = 20000
MAX_RESULT = 2 * 1024 * 1024
_FILE_JOURNAL = ContextVar("document_review_file_journal", default=None)
_SCHEMA = """
CREATE TABLE IF NOT EXISTS review_revisions (
 run_id TEXT NOT NULL, revision INTEGER NOT NULL, record_json TEXT NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,revision));
CREATE TABLE IF NOT EXISTS review_documents (
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, document_id TEXT NOT NULL,
 record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_learning (
 run_id TEXT NOT NULL, revision INTEGER NOT NULL, record_json TEXT NOT NULL,
 record_sha256 TEXT NOT NULL, PRIMARY KEY(run_id,revision));
"""


@contextmanager
def _db(root: Path, write=False):
    path = store.resolve_store_dir(root) / "document-review.sqlite3"
    if not write and not path.exists():
        yield None
        return
    connection = None
    journal = [] if write else None
    journal_token = _FILE_JOURNAL.set(journal) if write else None
    try:
        if write:
            path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(path), timeout=15, isolation_level=None)
            connection.executescript(_SCHEMA)
            for table in ("review_revisions", "review_documents", "review_learning"):
                for operation in ("UPDATE", "DELETE"):
                    connection.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable record'); END")
            connection.execute("BEGIN IMMEDIATE")
        else:
            connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15, isolation_level=None)
            connection.execute("BEGIN")
        connection.row_factory = sqlite3.Row
        yield connection
        connection.commit()
    except Exception as error:
        if connection:
            connection.rollback()
        if journal_token is not None:
            _FILE_JOURNAL.reset(journal_token)
            journal_token = None
        recovery_errors = []
        for item in reversed(journal or []):
            path, before, written_hash = item
            try:
                if not path.exists() or store._file_hash(path) != written_hash:
                    # 外部编辑优先，不覆盖操作失败期间用户写入的新内容。
                    recovery_errors.append(f"工作文件再次被外部修改，未覆盖：{path}")
                elif before is None:
                    path.unlink()
                else:
                    _atomic(path, before, written_hash)
            except (ValueError, OSError) as recovery_error:
                recovery_errors.append(str(recovery_error))
        if recovery_errors:
            raise MemoryError(f"操作失败且部分工作文件需手动恢复：{'；'.join(recovery_errors)}；原错误：{error}") from error
        if isinstance(error, sqlite3.Error):
            raise MemoryError(f"结果校验数据库操作失败：{error}") from error
        raise
    finally:
        if journal_token is not None:
            _FILE_JOURNAL.reset(journal_token)
        if connection:
            connection.close()


def _decode(row):
    if row is None:
        return None
    value = store._parse(row["record_json"])
    if store._hash(value) != row["record_sha256"]:
        raise MemoryError("结果校验记录哈希不一致。")
    return value


def _latest(connection, run_id):
    return _decode(connection.execute("SELECT record_json,record_sha256 FROM review_revisions WHERE run_id=? ORDER BY revision DESC LIMIT 1", (run_id,)).fetchone()) if connection else None


def _doc_state(connection, document_id):
    return _decode(connection.execute("SELECT record_json,record_sha256 FROM review_documents WHERE document_id=? ORDER BY event_id DESC LIMIT 1", (document_id,)).fetchone()) if connection else None


def _learned(connection, run_id, revision):
    return _decode(connection.execute("SELECT record_json,record_sha256 FROM review_learning WHERE run_id=? AND revision=?", (run_id, revision)).fetchone()) if connection else None


def _run(root, run_id):
    with store._db(root) as connection:
        return learning._run(connection, run_id)


def _runs(root):
    with store._db(root) as connection:
        if connection is None or not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='learning_runs'").fetchone():
            return []
        return [learning._run(connection, row["run_id"]) for row in connection.execute("SELECT run_id FROM learning_runs ORDER BY rowid DESC")]


def _docid(run):
    return "doc_" + run["source_sha256"][:32]


def _result(clauses, discarded=False):
    if discarded:
        return []
    active = [item for item in clauses if item["state"] == "active"]
    ids = {item["clause_key"]: i for i, item in enumerate(active)}
    result = []
    for i, item in enumerate(active):
        clause = copy.deepcopy(item["clause"])
        clause["id"] = i
        clause["相关条款"] = [ids[key] for key in item["related_keys"] if key in ids]
        result.append(clause)
    return result


def _clauses(run_id, result):
    if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
        raise MemoryError("工作结果必须是条款对象组成的 JSON 数组。")
    keys = {item.get("id"): "clause_" + store._hash([run_id, i])[:24] for i, item in enumerate(result)}
    return [{"clause_key": keys[item.get("id")], "state": "active", "clause": copy.deepcopy(item),
             "related_keys": [keys[cid] for cid in item.get("相关条款", []) if cid in keys], "reason": ""}
            for item in result]


def _base(run):
    work = (Path(run["index_path"]).resolve().parent / "review" / run["run_id"] / "result.json").resolve()
    if work == Path(run["result_path"]).resolve() or work == Path(run["input_path"]).resolve():
        raise MemoryError("校验工作文件不能覆盖原始输入或不可变结果。")
    return {"run_id": run["run_id"], "document_id": _docid(run), "revision": 0,
            "status": "pending", "created_at": run["created_at"], "action": "pending",
            "current_result_path": str(work), "original_result_path": run["result_path"],
            "clauses": _clauses(run["run_id"], run["result_json"]), "operations": [],
            "reviewer": "", "family_id": "", "note": "", "working_sha256": None}


def _record(connection, run):
    return _latest(connection, run["run_id"]) or _base(run)


def _sync(run, record):
    blocked = []
    try:
        learning._check_record_files(run)
    except (ValueError, OSError) as error:
        blocked.append(str(error))
    work = Path(record["current_result_path"])
    digest = None
    dirty = False
    external = None
    if work.exists():
        try:
            digest = store._file_hash(work)
            external = store._read_json(work, MAX_RESULT)
            if not isinstance(external, list) or any(not isinstance(item, dict) for item in external):
                raise MemoryError("工作结果必须是条款对象组成的 JSON 数组。")
            dirty = digest != record.get("working_sha256")
        except (ValueError, OSError) as error:
            blocked.append(f"本地工作结果无法读取：{error}")
    elif record.get("working_sha256") is not None:
        blocked.append("本地校验工作结果已缺失；请恢复该文件后再同步。")
    return {"dirty": dirty, "blocked": bool(blocked), "reason": "；".join(blocked),
            "current_sha256": digest, "external_result": external}


def _validation(run, result):
    try:
        index = store._read_json(Path(run["index_path"]), 64 * 1024 * 1024)
        return validate_result(index, result)
    except (ValueError, OSError) as error:
        return {"ok": False, "errors": [{"code": "SOURCE_UNAVAILABLE", "message": str(error), "path": "index"}], "warnings": []}


def _summary(root, run, record, connection):
    docstate = _doc_state(connection, _docid(run)) or {}
    sync = _sync(run, record)
    discarded = bool(docstate.get("discarded"))
    status = "discarded" if discarded else "modified" if sync["dirty"] or sync["blocked"] else record["status"]
    learned = _learned(connection, run["run_id"], record["revision"])
    return {"document_id": _docid(run), "run_id": run["run_id"], "revision": record["revision"],
            "title": Path(run["input_path"]).stem, "input_path": run["input_path"],
            "created_at": run["created_at"], "updated_at": record["created_at"], "status": status,
            "clause_count": len(_result(record["clauses"], discarded)),
            "current_result_path": record["current_result_path"],
            "learning_status": "learned" if learned and status == "verified" and not sync["blocked"] else "pending" if status == "verified" and not sync["blocked"] else "not_ready",
            "sync": {key: value for key, value in sync.items() if key != "external_result"}}


def _review(root, run, record, connection):
    value = _summary(root, run, record, connection)
    discarded = value["status"] == "discarded"
    # 外部文件未显式同步前只显示已记录的可信草稿，禁止以漂移内容确认或学习。
    result = _result(record["clauses"], discarded)
    history = []
    if connection:
        for row in connection.execute("SELECT record_json,record_sha256 FROM review_revisions WHERE run_id=? ORDER BY revision DESC", (run["run_id"],)):
            item = _decode(row)
            history.append({key: item.get(key) for key in ("revision", "action", "created_at", "reviewer", "note", "operations", "before_result", "after_result")})
    learned = _learned(connection, run["run_id"], record["revision"])
    value.update(original_result_path=run["result_path"], source_sha256=run["source_sha256"],
                 result=result, clauses=copy.deepcopy(record["clauses"]), history=history,
                 validation=_validation(run, result), reviewer=record["reviewer"], family_id=record["family_id"], note=record["note"],
                 learning={"status": value["learning_status"], "record": learned},
                 next_action="结果尚未完成校验，请检查条款后点击已校验。" if value["status"] in ("pending", "modified") else "")
    return value


def get_review(root: Path, run_id: str) -> dict:
    run = _run(root, run_id)
    with _db(root) as connection:
        return _review(root, run, _record(connection, run), connection)


def _atomic(path, data, expected_hash):
    path = Path(path)
    before = path.read_bytes() if path.exists() else None
    if path.exists():
        if expected_hash is None or store._file_hash(path) != expected_hash:
            raise MemoryError("本地工作结果在操作期间已变化，禁止覆盖；请刷新并同步。")
    elif expected_hash is not None:
        raise MemoryError("本地工作结果已缺失，禁止覆盖。")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        # 再检查一次：外部编辑不能被较早读取的界面悄悄覆盖。
        if (path.exists() and (expected_hash is None or store._file_hash(path) != expected_hash)) or (not path.exists() and expected_hash is not None):
            raise MemoryError("本地工作结果在操作期间已变化，禁止覆盖；请刷新并同步。")
        os.replace(temporary, path)
        journal = _FILE_JOURNAL.get()
        if journal is not None:
            journal.append((path, before, hashlib.sha256(data).hexdigest()))
    finally:
        if temporary and temporary.exists():
            temporary.unlink()


def _save(connection, run, before, after, action, operations, expected_hash, *, write_working=True):
    after = copy.deepcopy(after)
    after.update(revision=before["revision"] + 1, action=action, created_at=store._now(), operations=operations)
    discarded = after["status"] == "discarded"
    after["before_result"] = _result(before["clauses"], before["status"] == "discarded")
    after["after_result"] = _result(after["clauses"], discarded)
    data = json.dumps(after["after_result"], ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8") + b"\n"
    if write_working:
        after["working_sha256"] = hashlib.sha256(data).hexdigest()
    normalized = {clause["id"]: clause for clause in after["after_result"]}
    if not discarded:
        for ordinal, item in enumerate(entry for entry in after["clauses"] if entry["state"] == "active"):
            item["clause"] = copy.deepcopy(normalized[ordinal])
    # 每一修订自己保留内容；已校验标签另存内容地址文件供代理和案例学习。
    if write_working:
        _atomic(Path(after["current_result_path"]), data, expected_hash)
    connection.execute("INSERT INTO review_revisions VALUES (?,?,?,?)", (run["run_id"], after["revision"], store._canonical(after).decode(), store._hash(after)))
    return after


def ensure_run(root: Path, run_id: str) -> dict:
    run = _run(root, run_id)
    with _db(root, True) as connection:
        record = _latest(connection, run_id)
        if record is None:
            record = _base(run)
            sync = _sync(run, record)
            if sync["blocked"] or sync["dirty"]:
                raise MemoryError(sync["reason"] or "已存在未登记的本地工作结果，请在网页显式同步。")
            if (_doc_state(connection, _docid(run)) or {}).get("discarded"):
                record["status"] = "discarded"
            record = _save(connection, run, record, record, "initialize", [], None)
        value = _summary(root, run, record, connection)
    return {key: value[key] for key in ("document_id", "run_id", "revision", "status", "current_result_path")} | {
        "review_url": f"/#documents?run_id={run_id}",
        "next_action": "请用户在网页检查条款并主动点击已校验；未校验结果不作为可信标签。"}


def _expected(body, record):
    value = body.get("expected_revision")
    if type(value) is not int or value != record["revision"]:
        raise MemoryError("结果修订已变化，请刷新后重试（expected_revision 不一致）。")


def _external_clauses(run, record, external):
    """外部数组优先以不含 id 的内容匹配，其次按旧 id；内部 key 不进入七字段文件。"""
    if len(external) > 10000:
        raise MemoryError("单篇条款超过10000条。")
    ids = [clause.get("id") for clause in external]
    if any(type(cid) is not int for cid in ids) or len(set(ids)) != len(ids):
        raise MemoryError("本地结果的 id 必须是不重复的整数，无法安全识别条款关联；请先修复本地文件。")
    for clause in external:
        links = clause.get("相关条款")
        if not isinstance(links, list) or any(type(cid) is not int or cid not in ids for cid in links):
            raise MemoryError("本地结果包含无效或不存在的相关条款 id，禁止静默丢弃关联；请先修复本地文件。")
    previous = copy.deepcopy(record["clauses"])
    available = [item for item in previous if item["state"] == "active"]
    def signature(clause):
        return store._hash({key: value for key, value in clause.items() if key not in {"id", "相关条款"}})
    chosen, used = [], set()
    for ordinal, clause in enumerate(external):
        candidates = [item for item in available if item["clause_key"] not in used and signature(item["clause"]) == signature(clause)]
        if len(candidates) != 1:
            candidates = [item for item in available if item["clause_key"] not in used and item["clause"].get("id") == clause.get("id")]
        item = candidates[0] if len(candidates) == 1 else {"clause_key": "clause_" + uuid.uuid4().hex[:24], "state": "active", "reason": "", "related_keys": []}
        used.add(item["clause_key"])
        item["clause"] = copy.deepcopy(clause)
        chosen.append(item)
    keys = {item["clause"].get("id"): item["clause_key"] for item in chosen}
    for item in chosen:
        links = item["clause"].get("相关条款", [])
        if not isinstance(links, list):
            raise MemoryError("本地工作结果的相关条款必须为数组；请先修复该 JSON。")
        item["related_keys"] = [keys[cid] for cid in links if type(cid) is int and cid in keys]
    removed = [item for item in previous if item["clause_key"] not in used]
    for item in removed:
        if item["state"] == "active":
            item.update(state="deleted", reason="本地结果文件删除了此条款")
    return chosen + removed


def _edit(run, record, body):
    action = body.get("action")
    clauses = record["clauses"]
    key = body.get("clause_key")
    item = next((item for item in clauses if item["clause_key"] == key), None)
    if action == "add":
        clause = body.get("clause")
        if not isinstance(clause, dict):
            raise MemoryError("新增条款需要 clause 对象。")
        clause = copy.deepcopy(clause)
        clause.setdefault("id", len(clauses))
        clause.setdefault("相关条款", [])
        item = {"clause_key": "clause_" + uuid.uuid4().hex[:24], "state": "active", "clause": clause,
                "related_keys": [], "reason": ""}
        clauses.append(item)
    elif item is None:
        raise MemoryError("条款 clause_key 不存在。")
    elif action == "update":
        patch = body.get("patch")
        allowed = {"条款标题", "条款分类", "产品分类", "定位标识", "产品数量"}
        if not isinstance(patch, dict) or set(patch) - allowed:
            raise MemoryError("patch 只能修改标题、分类、定位标识和数量；关联请用 related_keys。")
        item["clause"].update(copy.deepcopy(patch))
    elif action in {"non_target", "delete"}:
        item["state"] = "non_target" if action == "non_target" else "deleted"
        item["reason"] = store._string(body.get("reason") or ("非目标条款" if action == "non_target" else "用户删除条款"), "reason", 2000)
    elif action == "restore":
        item.update(state="active", reason="")
    else:
        raise MemoryError("不支持的条款编辑 action。")
    if "related_keys" in body:
        links = body["related_keys"]
        known = {entry["clause_key"] for entry in clauses}
        if not isinstance(links, list) or any(not isinstance(value, str) or value not in known or value == item["clause_key"] for value in links) or len(set(links)) != len(links):
            raise MemoryError("related_keys 必须是不重复且存在的其他条款 key 数组。")
        item["related_keys"] = list(links)
    if len(store._canonical(clauses)) > MAX_RESULT:
        raise MemoryError("条款草稿超过2 MiB。")
    record["status"] = "modified"
    return [{"action": action, "clause_key": item["clause_key"], "patch": body.get("patch"), "reason": item["reason"],
             "note": str(body.get("note") or "").strip()[:4000], "related_keys": item["related_keys"]}]


def _mutate(root, document_id, endpoint, body):
    run = _run(root, store._string(body.get("run_id"), "run_id", 128))
    if _docid(run) != document_id:
        raise MemoryError("document_id 与 run_id 不对应。")
    with _db(root, True) as connection:
        before = _record(connection, run)
        _expected(body, before)
        sync = _sync(run, before)
        discarded = bool((_doc_state(connection, document_id) or {}).get("discarded"))
        whole_document = endpoint in {"discard", "restore"}
        if sync["blocked"] and not whole_document:
            raise MemoryError(sync["reason"])
        if sync["dirty"] and endpoint != "sync" and not whole_document:
            raise MemoryError("本地结果已被外部修改，请先点击同步本地结果，再执行校验或编辑。")
        if discarded and endpoint != "restore":
            raise MemoryError("整篇结果已丢弃；请先恢复，丢弃不作为修正经验。")
        after = copy.deepcopy(before)
        if endpoint == "edit":
            operations = _edit(run, after, body)
        elif endpoint == "sync":
            if not sync["dirty"]:
                return _review(root, run, before, connection)
            after["clauses"] = _external_clauses(run, before, sync["external_result"])
            after["status"] = "modified"
            operations = [{"action": "external_sync", "source_sha256": sync["current_sha256"]}]
        elif endpoint == "verify":
            if body.get("acknowledged") is not True:
                raise MemoryError("请主动确认整篇结果已经人工校验（acknowledged=true）。")
            validation = _validation(run, _result(after["clauses"]))
            if not validation["ok"]:
                raise MemoryError("条款结构或定位尚未通过校验，不能标为已校验：" + "；".join(item["message"] for item in validation["errors"][:5]))
            after.update(status="verified", reviewer=store._string(body.get("reviewer"), "reviewer", 160),
                         family_id=store._string(body.get("family_id"), "family_id", 200),
                         note=str(body.get("note") or "").strip()[:4000])
            operations = [{"action": "verify", "acknowledged": True}]
            after["labels_artifact"] = learning._artifact(root, _result(after["clauses"]))
            after["verified_sha256"] = store._hash({"run_id": run["run_id"], "revision": before["revision"] + 1,
                "labels_sha256": after["labels_artifact"]["sha256"], "reviewer": after["reviewer"], "family_id": after["family_id"]})
        elif endpoint in {"discard", "restore"}:
            event = {"document_id": document_id, "discarded": endpoint == "discard", "created_at": store._now(),
                     "run_id": run["run_id"], "note": str(body.get("note") or "")[:4000], "action": endpoint}
            # 坏来源或外部编辑不应阻止清理整篇；这类文件只隐藏，不覆写。
            changes = []
            for sibling in _runs(root):
                if _docid(sibling) != document_id:
                    continue
                old = _record(connection, sibling)
                state = _sync(sibling, old)
                changes.append((sibling, old, state))
            connection.execute("INSERT INTO review_documents(document_id,record_json,record_sha256) VALUES(?,?,?)", (document_id, store._canonical(event).decode(), store._hash(event)))
            # 同一标书所有运行同步隐藏/恢复；原快照始终保留。
            for sibling, old, state in changes:
                new = copy.deepcopy(old)
                new["status"] = "discarded" if endpoint == "discard" else "modified" if any(item.get("action") not in {"initialize", "verify", "discard", "restore"} for item in _history(connection, sibling["run_id"])) else "pending"
                new["note"] = event["note"]
                for field in ("labels_artifact", "verified_sha256"):
                    new.pop(field, None)
                new.update(reviewer="", family_id="")
                preserve = state["blocked"] or state["dirty"]
                operation = {"action": endpoint, "learning_feedback": False, "working_file_preserved": preserve,
                             "reason": state["reason"] or ("存在尚未导入的本地修改，保留原文件" if state["dirty"] else "")}
                _save(connection, sibling, old, new, endpoint, [operation], state["current_sha256"], write_working=not preserve)
            return _review(root, run, _record(connection, run), connection)
        else:
            raise MemoryError("不支持的标书结果操作。")
        # 修改后旧的校验身份不能误当作当前版本确认。
        if endpoint != "verify":
            for field in ("labels_artifact", "verified_sha256"):
                after.pop(field, None)
            after.update(reviewer="", family_id="", note=str(body.get("note") or "").strip()[:4000])
        saved = _save(connection, run, before, after, endpoint, operations, sync["current_sha256"])
        return _review(root, run, saved, connection)


def _history(connection, run_id):
    return [_decode(row) for row in connection.execute("SELECT record_json,record_sha256 FROM review_revisions WHERE run_id=? ORDER BY revision", (run_id,))] if connection else []


def list_documents(root: Path) -> dict:
    documents = {}
    with _db(root) as connection:
        for run in _runs(root):
            summary = _summary(root, run, _record(connection, run), connection)
            document = documents.setdefault(_docid(run), {**summary, "runs": []})
            document["runs"].append(summary)
    values = list(documents.values())
    for value in values:
        value["run_count"] = len(value["runs"])
    return {"documents": values, "counts": {status: sum(value["status"] == status for value in values) for status in ("pending", "modified", "verified", "discarded")}}


def _select(root, document_id, query):
    candidates = [run for run in _runs(root) if _docid(run) == document_id]
    if not candidates:
        raise MemoryError("标书不存在。")
    requested = query.get("run_id")
    selected = next((run for run in candidates if run["run_id"] == requested), None) if requested else candidates[0]
    if selected is None:
        raise MemoryError("该运行不属于当前标书。")
    return selected


def _source(root, run, query):
    if store._file_hash(Path(run["index_path"])) != run["index_sha256"]:
        raise MemoryError("原文 index 已变化，无法保证展示内容与该次抽取一致；请恢复索引或重新注册。")
    index = store._read_json(Path(run["index_path"]), 64 * 1024 * 1024)
    if "source_document" not in index:
        raise MemoryError("该运行索引没有保存原文快照，无法展示格式化 JSON。")
    # 保留完整源 JSON；先 rows 再 merges，跳转优先命中正文而非布局元数据。
    def reorder(value):
        if isinstance(value, dict):
            ordered = sorted(value, key=lambda key: 2 if key == "merges" else 0 if key == "rows" else 1)
            return {key: reorder(value[key]) for key in ordered}
        return [reorder(item) for item in value] if isinstance(value, list) else value
    text = json.dumps(reorder(index["source_document"]), ensure_ascii=False, indent=2)
    tag = query.get("tag")
    offset = text.find(tag) if isinstance(tag, str) and re.fullmatch(r"<C[0-9]+>", tag) else -1
    if tag and offset < 0:
        raise MemoryError("原文中不存在该 C 标签。")
    try:
        cursor = max(0, offset - 400) if offset >= 0 else int(query.get("cursor", "0"))
    except (ValueError, TypeError):
        raise MemoryError("cursor 必须为非负整数。")
    if not 0 <= cursor <= len(text):
        raise MemoryError("cursor 超出原文范围。")
    # 避免页边界切断标签，便于前端高亮完整锚点。
    for match in re.finditer(r"<C[0-9]+>", text[max(0, cursor - 30):cursor + 30]):
        start, end = max(0, cursor - 30) + match.start(), max(0, cursor - 30) + match.end()
        if start < cursor < end:
            cursor = start
            break
    stop = min(len(text), cursor + PAGE_CHARS)
    for match in re.finditer(r"<C[0-9]+>", text[max(cursor, stop - 30):stop + 30]):
        start, end = max(cursor, stop - 30) + match.start(), max(cursor, stop - 30) + match.end()
        if start < stop < end:
            stop = end
            break
    return {"document_id": _docid(run), "run_id": run["run_id"], "text": text[cursor:stop], "start_cursor": cursor,
            "next_cursor": stop if stop < len(text) else None, "total_chars": len(text), "tag": tag,
            "tag_offset": offset if offset >= 0 else None, "archived_source": True}


def handle_get(root: Path, parts: list[str], query: dict) -> dict:
    if not parts:
        return list_documents(root)
    run = _select(root, parts[0], query)
    if len(parts) == 1:
        return get_review(root, run["run_id"])
    if len(parts) == 2 and parts[1] == "source":
        return _source(root, run, query)
    raise MemoryError("结果校验接口不存在。")


def handle_post(root: Path, parts: list[str], body: dict) -> dict:
    if len(parts) != 2 or parts[1] not in {"edit", "verify", "sync", "discard", "restore"}:
        raise MemoryError("结果校验操作接口不存在。")
    return _mutate(root, parts[0], parts[1], body)


def verified_for_learning(root: Path, run_id: str, revision: int) -> dict:
    run = _run(root, run_id)
    with _db(root) as connection:
        record = _record(connection, run)
        if record["revision"] != revision or record["status"] != "verified" or (_doc_state(connection, _docid(run)) or {}).get("discarded"):
            raise MemoryError("当前修订尚未人工校验、已被修改或已整篇丢弃。")
        sync = _sync(run, record)
        if sync["blocked"] or sync["dirty"]:
            raise MemoryError(sync["reason"] or "本地工作结果已变化，请同步并重新人工校验。")
        artifact = record["labels_artifact"]
        if store._file_hash(Path(artifact["path"])) != artifact["sha256"]:
            raise MemoryError("已校验标签快照发生变化。")
        operations = [operation for item in _history(connection, run_id) for operation in item["operations"]
                      if operation.get("action") not in {"verify", "discard", "restore"}]
        return {"document_id": _docid(run), "run_id": run_id, "revision": revision,
                "input_path": run["input_path"], "index_path": run["index_path"], "source_sha256": run["source_sha256"],
                "labels_path": artifact["path"], "original_result_path": run["result_path"], "current_result_path": record["current_result_path"],
                "before_result": run["result_json"], "after_result": record["after_result"], "operations": operations,
                "family_id": record["family_id"], "reviewer": record["reviewer"], "note": record["note"],
                "verified_sha256": record["verified_sha256"], "learning": _learned(connection, run_id, revision)}


def list_verified(root: Path) -> dict:
    values = []
    for run in _runs(root):
        with _db(root) as connection:
            record = _record(connection, run)
            if record["status"] != "verified" or _learned(connection, run["run_id"], record["revision"]):
                continue
        try:
            value = verified_for_learning(root, run["run_id"], record["revision"])
        except MemoryError:
            continue
        values.append({key: value[key] for key in ("document_id", "run_id", "revision", "input_path", "family_id", "reviewer", "verified_sha256")} | {"operation_count": len(value["operations"])})
    return {"reviews": values}


def record_learning(root: Path, run_id: str, revision: int, candidates: list, execution_context: dict, no_experience_reason: str = "") -> dict:
    # 再次核验版本、原文和工作文件，禁止消费过期或已丢弃修订。
    value = verified_for_learning(root, run_id, revision)
    if not isinstance(candidates, list) or not isinstance(execution_context, dict):
        raise MemoryError("candidates 必须为数组，execution_context 必须为对象。")
    if not candidates and not str(no_experience_reason).strip():
        raise MemoryError("无候选经验时必须解释 no_experience_reason。")
    record = {"run_id": run_id, "revision": revision, "status": "learned", "verified_sha256": value["verified_sha256"],
              "candidates": candidates, "execution_context": execution_context,
              "no_experience_reason": str(no_experience_reason).strip()[:4000], "created_at": store._now()}
    with _db(root, True) as connection:
        current = _record(connection, _run(root, run_id))
        if current["revision"] != revision or current["status"] != "verified" or (_doc_state(connection, value["document_id"]) or {}).get("discarded"):
            raise MemoryError("记录学习时校验修订已变化，请重新读取。")
        current_sync = _sync(_run(root, run_id), current)
        if current_sync["blocked"] or current_sync["dirty"]:
            raise MemoryError(current_sync["reason"] or "本地工作结果已变化，禁止记录过期学习结论。")
        old = _learned(connection, run_id, revision)
        if old:
            if any(old[key] != record[key] for key in ("candidates", "execution_context", "no_experience_reason", "verified_sha256")):
                raise MemoryError("同一校验修订已记录不同学习结论，禁止覆盖。")
            return old
        connection.execute("INSERT INTO review_learning VALUES (?,?,?,?)", (run_id, revision, store._canonical(record).decode(), store._hash(record)))
    return record
