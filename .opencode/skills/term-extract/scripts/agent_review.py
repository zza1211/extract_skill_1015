"""读取网页显式确认的条款修订并归纳候选；不调用模型、不代人确认。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import time

import agent_learning as agent
import document_review as reviews
import evolution_store as evolution
import memory_learning as learning
import memory_store as store

MemoryError = store.MemoryError
TABLE = "agent_document_review_events"
MAX_BYTES = 48 * 1024


@contextmanager
def _db(root, *, write=False):
    with store._db(Path(root), write=write) as connection:
        if connection is not None and write:
            connection.execute(f"CREATE TABLE IF NOT EXISTS {TABLE} (record_id TEXT PRIMARY KEY, record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)")
            for operation in ("UPDATE", "DELETE"):
                connection.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{TABLE}_{operation.lower()} BEFORE {operation} ON {TABLE} BEGIN SELECT RAISE(ABORT, 'immutable record'); END")
        yield connection


def _events(root, run_id, revision, context):
    with _db(root) as connection:
        return [item for item in evolution._rows(connection, TABLE)
                if item["run_id"] == run_id and item["revision"] == revision
                and item["session_id"] == context["session_id"]]


def _event(root, run_id, revision, context, kind, payload):
    identity = {"run_id": run_id, "revision": revision, "session_id": context["session_id"], "kind": kind, "payload": payload}
    event_id = "document_review_event_" + store._hash(identity)[:32]
    with _db(root, write=True) as connection:
        if not evolution._find(connection, TABLE, "event_id", event_id):
            evolution._put(connection, TABLE, event_id, {"event_id": event_id, **identity,
                          "execution_context": context, "created_at": store._now()})


def _identity(run_id, revision):
    run_id = store._string(run_id, "run_id", 128)
    if type(revision) is not int or revision < 1:
        raise MemoryError("revision 必须为当前已校验修订的正整数。")
    return run_id, revision


def pending_reviews(root, execution_context, cursor=0, limit=20):
    agent._context(execution_context)
    records = reviews.list_verified(root)["reviews"]
    if type(cursor) is not int or not 0 <= cursor <= len(records) or type(limit) is not int or not 1 <= limit <= 100:
        raise MemoryError("待学习分页 cursor 非法或 limit 不在1到100之间。")
    # 白名单防止将未来新增的标签、操作或私有标签路径带入盲抽会话。
    fields = {"run_id", "document_id", "revision", "input_path", "family_id", "reviewer", "verified_sha256", "operation_count",
              "verified_at", "document_name", "title", "learning_status"}
    response = {"reviews": [], "total": len(records), "next_cursor": None,
                "notice": "只列用户已在网页显式完成校验的待学习修订；不返回标签。用户未校验、修改后待重校验或整篇弃用的结果不能学习。"}
    position = cursor
    while position < len(records) and len(response["reviews"]) < limit:
        response["reviews"].append({key: value for key, value in records[position].items() if key in fields})
        if len(store._canonical(response)) > MAX_BYTES - 256:
            response["reviews"].pop()
            break
        position += 1
    response["next_cursor"] = position if position < len(records) else None
    return response


def _stream(record):
    return ([{"kind": "before", "position": i, "clause": clause} for i, clause in enumerate(record["before_result"])] +
            [{"kind": "after", "position": i, "clause": clause} for i, clause in enumerate(record["after_result"])] +
            [{"kind": "operation", "position": i, "operation": operation} for i, operation in enumerate(record["operations"])])


def read_review(root, run_id, revision, execution_context, cursor=0, max_bytes=32768):
    context = agent._context(execution_context)
    run_id, revision = _identity(run_id, revision)
    if type(max_bytes) is not int or not 4096 <= max_bytes <= MAX_BYTES:
        raise MemoryError("max_bytes 必须在4096到49152之间。")
    verified = reviews.verified_for_learning(root, run_id, revision)
    stream = _stream(verified)
    if type(cursor) is not int or not 0 <= cursor <= len(stream):
        raise MemoryError("校验结果 cursor 非法。")
    result = {"run_id": run_id, "revision": revision, "verified_sha256": verified["verified_sha256"],
              "input_path": verified["input_path"], "index_path": verified["index_path"], "family_id": verified["family_id"],
              "reviewer": verified["reviewer"], "note": verified.get("note", ""), "items": [],
              "before_count": len(verified["before_result"]), "after_count": len(verified["after_result"]),
              "operation_count": len(verified["operations"]), "total": len(stream), "next_cursor": None,
              "notice": "用户已明确校验本修订；按 next_cursor 读完 before/after/操作，结合原文核验后归纳候选。已登记本会话见过标签；下一篇盲抽请新建会话。"}
    position = cursor
    while position < len(stream):
        result["items"].append(stream[position])
        if len(store._canonical(result)) > max_bytes - 256:
            result["items"].pop()
            if not result["items"]:
                raise MemoryError("单项校验结果超过交付预算；请增大 max_bytes，仍超限时在网页核查，不能截断后宣称已读。")
            break
        position += 1
    result["next_cursor"] = position if position < len(stream) else None
    if len(store._canonical(result)) > max_bytes:
        raise MemoryError("校验结果目录超过预算，请在网页核查过长备注。")
    # 任何答案交付前先写统一 disclosure，阻止本会话之后提交新盲抽成绩。
    agent.record_label_disclosure(root, context, source_type="document_review", source_key=f"{run_id}:{revision}")
    _event(root, run_id, revision, context, "read", {"verified_sha256": verified["verified_sha256"],
           "start": cursor, "end": position, "total": len(stream)})
    return result


def _assert_read(events, verified):
    total = len(_stream(verified))
    receipts = [item["payload"] for item in events if item["kind"] == "read"
                and item["payload"]["verified_sha256"] == verified["verified_sha256"]]
    position = 0
    for item in sorted(receipts, key=lambda receipt: receipt["start"]):
        if item["start"] <= position:
            position = max(position, item["end"])
    if not receipts or position < total:
        raise MemoryError("请先在当前主 agent 会话中用 term_review_read 完整读取当前已校验修订的全部分页，再总结经验。")


@contextmanager
def _reflection_lock(root, run_id, revision, timeout=15):
    """覆盖整个归纳写入过程；操作系统在崩溃/退出时释放，文件本身不能删除。"""
    directory = store.resolve_store_dir(Path(root)) / "review-learning-locks"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (store._hash([run_id, revision]) + ".lock")
    deadline = time.monotonic() + timeout
    stream = os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), "r+b", buffering=0)
    acquired = False
    try:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"\0")
        while not acquired:
            stream.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (BlockingIOError, OSError) as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                    raise MemoryError("无法建立校验修订学习锁，请检查私有目录的文件权限。") from exc
                if time.monotonic() >= deadline:
                    raise MemoryError("此校验修订正在被另一个学习调用处理，请稍后重试；未创建本次候选。") from exc
                time.sleep(0.05)
        yield
    finally:
        try:
            if acquired:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()


def reflect_review(root, run_id, revision, reflection, execution_context):
    context = agent._context(execution_context)
    run_id, revision = _identity(run_id, revision)
    # 锁内重新检查当前快照与已学习记录，防止两个会话先各自写候选，再争抢最终标记。
    with _reflection_lock(root, run_id, revision):
        return _reflect_locked(root, run_id, revision, reflection, context)


def _reflect_locked(root, run_id, revision, reflection, context):
    verified = reviews.verified_for_learning(root, run_id, revision)
    events = _events(root, run_id, revision, context)
    _assert_read(events, verified)
    index, _ = learning._index(Path(verified["index_path"]))
    normalized = learning.validate_reflection(reflection, index)
    previous = next((item["payload"] for item in events if item["kind"] == "reflected"), None)
    existing = verified.get("learning")
    if existing:
        if previous and previous["reflection"] != normalized:
            raise MemoryError("本会话已保存不同复盘，不能覆盖历史校验学习；有新修改应在网页重新校验新修订。")
        return {**existing, "case_id": previous.get("case_id") if previous else None, "replayed": True,
                "notice": "本校验修订已学习，返回既有结论；未重复创建候选。"}
    if previous:
        if previous["reflection"] != normalized or previous["verified_sha256"] != verified["verified_sha256"]:
            raise MemoryError("本会话已保存不同复盘，不能覆盖历史校验学习；有新修改应在网页重新校验新修订。")
        result = reviews.record_learning(root, run_id, revision, previous["candidates"], context, normalized["no_experience_reason"])
        return {**result, "case_id": previous.get("case_id"), "replayed": True}
    # 必须引用不可变已校验工件；不能用 review/result.json 可编辑稿当标签。
    trusted = evolution.import_case(root, verified["input_path"], verified["labels_path"], verified["family_id"], "development",
        declared_by=verified["reviewer"], note=f"用户在网页明确完成整篇校验；run_id={run_id} revision={revision} verified_sha256={verified['verified_sha256']}")
    candidates = []
    for insight in normalized["insights"]:
        payload = {key: value for key, value in insight.items() if key not in {"observation", "evidence_tags"}}
        payload.update(source_ids=[trusted["case_id"]], correction_ids=[])
        candidate = evolution.propose_candidate(root, payload,
            origin={"kind": "document_review_learning", "run_id": run_id, "review_revision": revision,
                    "verified_sha256": verified["verified_sha256"], "case_id": trusted["case_id"], "execution_context": context},
            evidence={"observation": insight["observation"], "evidence_tags": insight["evidence_tags"],
                      "before_sha256": store._hash(verified["before_result"]), "labels_sha256": store._hash(verified["after_result"])})
        candidates.append({key: candidate[key] for key in ("memory_id", "revision", "content_sha256", "state", "deduplicated")})
    # 保存可重放结果；record_learning 再检查当前修订与所有文件绑定，禁止把旧修订冒充最新已学习。
    _event(root, run_id, revision, context, "reflected", {"reflection": normalized, "verified_sha256": verified["verified_sha256"],
           "case_id": trusted["case_id"], "candidates": candidates})
    result = reviews.record_learning(root, run_id, revision, candidates, context, normalized["no_experience_reason"])
    return {**result, "case_id": trusted["case_id"], "replayed": False,
            "notice": "已记录本修订的学习与候选；没有直接发布。继续沿独立评测或人工审核发布路径。"}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[4])
    parser.add_argument("action", choices=("pending", "read", "reflect"))
    parser.add_argument("--request-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        request = store._read_json(args.request_file, 256 * 1024)
        if not isinstance(request, dict):
            raise MemoryError("request 必须为对象。")
        actions = {"pending": pending_reviews, "read": read_review, "reflect": reflect_review}
        print(json.dumps(actions[args.action](args.project_root, **request), ensure_ascii=False))
        return 0
    except (MemoryError, ValueError, TypeError, KeyError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
