"""标签学习、局部人工待办与自动实验的追加式存储。

可信标签来自用户声明，不伪造逐篇人工审核。实验报告仅由本地运行器产生；
内容哈希保障可重放一致性，不是抵抗本机管理员的签名或身份认证。
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import memory_store as store

MemoryError = store.MemoryError
_TABLES = ("evoz_cases", "evoz_reviews", "evoz_resolutions", "evoz_decisions",
           "evoz_experiments", "evoz_conflicts", "evoz_origins", "evoz_scans")


@contextmanager
def _db(root, *, write=False):
    with store._db(Path(root), write=write) as connection:
        if connection is not None and write:
            for table in _TABLES:
                connection.execute(f"CREATE TABLE IF NOT EXISTS {table} (record_id TEXT PRIMARY KEY, record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)")
                for op in ("UPDATE", "DELETE"):
                    connection.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable record'); END")
        yield connection


def _rows(connection, table):
    if connection is None or not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return []
    result = []
    for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid"):
        record = store._parse(row["record_json"])
        if store._hash(record) != row["record_sha256"]:
            raise MemoryError("演进记录哈希不一致。")
        result.append(record)
    return result


def _put(connection, table, record_id, record):
    connection.execute(f"INSERT INTO {table} VALUES (?,?,?,?)", (record_id, store._canonical(record).decode(), store._hash(record), record.get("created_at", store._now())))


def _find(connection, table, key, value):
    return next((record for record in _rows(connection, table) if record.get(key) == value), None)


def policy_digest(root):
    path = Path(root) / ".opencode/skills/term-extract/references/business-rules.md"
    return store._file_hash(path) if path.exists() else store._hash({"policy": "missing"})


def _case(connection, case_id):
    record = _find(connection, "evoz_cases", "case_id", case_id)
    if record is None:
        raise MemoryError("可信标签案例不存在。")
    return record


def _check_case(record):
    for path_key, hash_key in (("input_path", "source_sha256"), ("labels_path", "labels_sha256"),
                               ("input_archive_path", "source_sha256"), ("labels_archive_path", "labels_sha256"),
                               ("index_path", "index_sha256")):
        if store._file_hash(Path(record[path_key])) != record[hash_key]:
            raise MemoryError(f"可信案例依据已变化：{path_key}；请检查文件并重新导入。")
    if record.get("trust") != "user_provided_label":
        raise MemoryError("案例不具有用户提供的可信标签来源。")


def import_case(root, input_path, labels_path, family_id, split="development", declared_by="用户提供", note=""):
    from document_io import prepare
    from validate_result import validate_result
    root, input_path, labels_path = Path(root), Path(input_path).resolve(), Path(labels_path).resolve()
    family_id = store._string(family_id, "family_id", 200)
    declared_by = store._string(declared_by, "declared_by", 160)
    if split not in {"development", "regression", "holdout"}:
        raise MemoryError("split 必须为 development/regression/holdout。")
    source_sha, label_sha = store._file_hash(input_path), store._file_hash(labels_path)
    identity = {"source_sha256": source_sha, "labels_sha256": label_sha, "family_id": family_id, "split": split,
                "policy_sha256": policy_digest(root)}
    case_id = "case_" + store._hash(identity)[:32]
    with _db(root) as connection:
        existing = _find(connection, "evoz_cases", "case_id", case_id)
        if existing:
            _check_case(existing)
            return {**existing, "replayed": True}
        for previous in _rows(connection, "evoz_cases"):
            if previous["source_sha256"] == source_sha and (previous["family_id"] != family_id or previous["split"] != split):
                raise MemoryError("同一输入已分配 family/split，不能改名充当独立回归或跨训练/保留集。")
    directory = store.resolve_store_dir(root) / "evolution-artifacts" / case_id
    directory.mkdir(parents=True, exist_ok=True)
    input_archive, label_archive = directory / "input.json", directory / "labels.json"
    for original, destination, sha in ((input_path, input_archive, source_sha), (labels_path, label_archive, label_sha)):
        if destination.exists() and store._file_hash(destination) != sha:
            raise MemoryError("案例私有归档已存在但内容不同，禁止覆盖。")
        if not destination.exists():
            destination.write_bytes(original.read_bytes())
    prepared = prepare(input_archive, directory / "document")
    index_path = Path(prepared["index_path"])
    labels = store._read_json(label_archive, 2 * 1024 * 1024)
    checked = validate_result(store._read_json(index_path, 48 * 1024 * 1024), labels)
    if not checked["ok"]:
        raise MemoryError("可信标签不满足定位/结构要求：" + "; ".join(item["message"] for item in checked["errors"][:5]))
    record = {"case_id": case_id, **identity, "input_path": str(input_path), "labels_path": str(labels_path),
              "input_archive_path": str(input_archive), "labels_archive_path": str(label_archive),
              "index_path": str(index_path), "index_sha256": store._file_hash(index_path),
              "trust": "user_provided_label", "declared_by": declared_by, "note": str(note)[:4000],
              "policy_sha256": policy_digest(root), "created_at": store._now()}
    _check_case(record)
    with _db(root, write=True) as connection:
        existing = _find(connection, "evoz_cases", "case_id", case_id)
        if existing:
            return {**existing, "replayed": True}
        for previous in _rows(connection, "evoz_cases"):
            if previous["source_sha256"] == source_sha and (previous["family_id"] != family_id or previous["split"] != split):
                raise MemoryError("并发导入已为同一输入分配 family/split。")
        _put(connection, "evoz_cases", case_id, record)
    return {**record, "replayed": False}


def get_case(root, case_id, include_labels=False):
    with _db(root) as connection:
        record = dict(_case(connection, case_id))
    _check_case(record)
    if include_labels:
        record["labels_json"] = store._read_json(Path(record["labels_archive_path"]), 2 * 1024 * 1024)
    return record


def list_cases(root):
    with _db(root) as connection:
        items = _rows(connection, "evoz_cases")
    for item in items:
        try:
            _check_case(item)
            item["valid"] = True
        except MemoryError as exc:
            item.update(valid=False, error=str(exc))
    return {"items": items, "total": len(items)}


def validate_case(root, case_id):
    record = get_case(root, case_id)
    return {"case_id": case_id, "valid": True, "policy_current": record["policy_sha256"] == policy_digest(root), "trust": record["trust"]}


def create_review(root, *, kind, title, question, context, choices, subject, scope="case", dedupe_key=None):
    if scope not in {"case", "memory", "policy"}:
        raise MemoryError("待办 scope 不在支持范围。")
    if not isinstance(context, dict) or not isinstance(subject, dict) or not isinstance(choices, list) or not choices:
        raise MemoryError("待办需 context/subject 对象及非空 choices。")
    values = [item.get("value", item.get("id", item.get("label"))) if isinstance(item, dict) else item for item in choices]
    if any(not isinstance(item, str) or not item.strip() for item in values) or len(set(values)) != len(values):
        raise MemoryError("choices 需互异字符串或具有 value/id/label 的选项对象。")
    body = {"kind": store._string(kind, "kind", 80), "title": store._string(title, "title", 200),
            "question": store._string(question, "question", 4000), "context": context, "choices": choices,
            "subject": subject, "scope": scope}
    if len(store._canonical(body)) > 96 * 1024:
        raise MemoryError("单项待办上下文超过 96 KiB，请仅提供相关原文。")
    key = store._hash({"dedupe_key": dedupe_key}) if dedupe_key else store._hash(body)
    review_id = "review_" + key[:32]
    with _db(root, write=True) as connection:
        old = _find(connection, "evoz_reviews", "review_id", review_id)
        if old:
            if old["content_sha256"] != store._hash(body):
                raise MemoryError("同一待办去重键对应不同问题，禁止覆盖。")
            return _review(connection, review_id)
        _put(connection, "evoz_reviews", review_id, {"review_id": review_id, **body, "content_sha256": store._hash(body), "created_at": store._now()})
        return _review(connection, review_id)


def _review(connection, review_id):
    record = _find(connection, "evoz_reviews", "review_id", review_id)
    if record is None:
        raise MemoryError("人工待办不存在。")
    resolution = _find(connection, "evoz_resolutions", "review_id", review_id)
    return {**record, "state": "resolved" if resolution else "open", "revision": 2 if resolution else 1,
            "resolution": resolution, "notice": "回答仅作用于本待办的 subject，不确认整篇结果，也不自动修改全局业务口径。"}


def list_reviews(root, state="open"):
    if state not in {"open", "resolved", "all"}:
        raise MemoryError("state 必须为 open/resolved/all。")
    with _db(root) as connection:
        items = [_review(connection, item["review_id"]) for item in _rows(connection, "evoz_reviews")]
    items = [item for item in items if state == "all" or item["state"] == state]
    return {"items": items, "total": len(items)}


def get_review(root, review_id):
    with _db(root) as connection:
        return _review(connection, review_id)


def resolve_review(root, review_id, decision, answer="", actor="维护者", channel="dashboard", source_ref="", expected_revision=1):
    if channel not in {"dashboard", "conversation"}:
        raise MemoryError("channel 必须为 dashboard/conversation。")
    actor = store._string(actor, "actor", 160)
    if type(expected_revision) is not int or expected_revision != 1:
        raise MemoryError("待办版本已变化，请刷新；局部回复不能覆盖历史。")
    with _db(root, write=True) as connection:
        current = _review(connection, review_id)
        allowed = [item.get("value", item.get("id", item.get("label"))) if isinstance(item, dict) else item for item in current["choices"]]
        if decision not in allowed:
            raise MemoryError("decision 必须来自当前待办选项。")
        record = {"review_id": review_id, "decision": decision, "answer": str(answer)[:8000], "actor": actor,
                  "channel": channel, "source_ref": str(source_ref)[:2000], "subject": current["subject"], "scope": current["scope"]}
        if current["resolution"]:
            previous = {key: value for key, value in current["resolution"].items() if key != "created_at"}
            if previous != record:
                raise MemoryError("待办已有不同回复，请刷新查看；不能覆盖已确认的局部判断。")
            return {**current, "replayed": True}
        _put(connection, "evoz_resolutions", review_id, {**record, "created_at": store._now()})
        if current["kind"] == "experience_conflict" and current["subject"].get("memory_id") and current["subject"].get("revision"):
            memory_id, revision = current["subject"]["memory_id"], current["subject"]["revision"]
            store._candidate(connection, memory_id, revision)
            status = _candidate_review_status(connection, memory_id, revision, policy_digest(root), ignore_prior_needs_review=True)
            new_state = status["state"]
            decision_record = {"decision_id": "decision_" + uuid.uuid4().hex, "memory_id": memory_id, "revision": revision,
                               "status": new_state, "reason": "局部经验冲突待办已回答：" + decision,
                               "actor": actor, "target_memory_id": None, "target_revision": None, "created_at": store._now()}
            _put(connection, "evoz_decisions", decision_record["decision_id"], decision_record)
        return {**_review(connection, review_id), "replayed": False}


def _decision(connection, memory_id, revision):
    items = [item for item in _rows(connection, "evoz_decisions") if item["memory_id"] == memory_id and item["revision"] == revision]
    return items[-1] if items else None


def get_decision(root, memory_id, revision):
    with _db(root) as connection:
        return _decision(connection, memory_id, revision)


def _candidate_review_status(connection, memory_id, revision, policy_sha=None, ignore_prior_needs_review=False):
    if policy_sha is None:
        scans = _rows(connection, "evoz_scans")
        policy_sha = scans[-1]["policy_sha256"] if scans else None
    reviews = []
    for item in _rows(connection, "evoz_reviews"):
        subject = item.get("subject", {})
        if item.get("kind") != "experience_conflict" or subject.get("memory_id") != memory_id or subject.get("revision") != revision:
            continue
        if policy_sha and subject.get("policy_sha256") and subject["policy_sha256"] != policy_sha:
            continue
        reviews.append(_review(connection, item["review_id"]))
    open_count = sum(item["state"] == "open" for item in reviews)
    decisions = [item["resolution"]["decision"] for item in reviews if item["resolution"]]
    prior = _decision(connection, memory_id, revision)
    rejected = "不采纳" in decisions or bool(prior and prior["status"] in {"rejected", "merged"})
    needs_review = (open_count > 0 or any(value in {"需要修订", "证据不足"} for value in decisions)
                    or bool(not ignore_prior_needs_review and prior and prior["status"] == "needs_review"))
    state = prior["status"] if rejected and prior and prior["status"] == "merged" else "rejected" if rejected else "needs_review" if needs_review else "candidate"
    return {"blocking": rejected or needs_review, "open_conflicts": open_count,
            "total_conflicts": len(reviews), "state": state, "reviews": reviews}


def candidate_review_status(root, memory_id, revision):
    with _db(root) as connection:
        store._candidate(connection, memory_id, revision)
        return _candidate_review_status(connection, memory_id, revision, policy_digest(root))


def record_decision(root, memory_id, revision, status, reason, actor="system", target_memory_id=None):
    if status not in {"rejected", "merged", "needs_review", "candidate"}:
        raise MemoryError("候选决策 status 不在支持范围。")
    reason = store._string(reason, "reason", 4000)
    with _db(root, write=True) as connection:
        candidate = store._candidate(connection, memory_id, revision)
        merged_revision = None
        if target_memory_id:
            target = connection.execute("SELECT 1 FROM revisions WHERE memory_id=?", (target_memory_id,)).fetchone()
            if not target or target_memory_id == memory_id:
                raise MemoryError("合并目标必须为另一个已存在经验。")
        if status == "merged":
            if not target_memory_id:
                raise MemoryError("合并必须指定目标经验，不能仅隐藏当前来源。")
            latest = connection.execute("SELECT MAX(revision) FROM revisions WHERE memory_id=?", (target_memory_id,)).fetchone()[0]
            target_candidate = store._candidate(connection, target_memory_id, latest)
            merged = dict(target_candidate["payload"])
            for key in ("source_ids", "correction_ids"):
                values = list(dict.fromkeys([*merged.get(key, []), *candidate["payload"].get(key, [])]))
                if key in merged or key in candidate["payload"]:
                    merged[key] = values
            if merged != target_candidate["payload"]:
                merged_revision = store._insert_candidate(connection, merged, target_memory_id)["revision"]
            else:
                merged_revision = latest
        record = {"decision_id": "decision_" + uuid.uuid4().hex, "memory_id": memory_id, "revision": revision,
                  "status": status, "reason": reason, "actor": store._string(actor, "actor", 160),
                  "target_memory_id": target_memory_id, "target_revision": merged_revision, "created_at": store._now()}
        _put(connection, "evoz_decisions", record["decision_id"], record)
    return record


def _rule_key(payload):
    return store._hash({key: re.sub(r"\s+", " ", payload[key]).strip() for key in ("issue_type", "applies_when", "rule", "exclusions")})


def propose_candidate(root, payload, origin=None, evidence=None):
    payload = store._payload(payload)
    with _db(root, write=True) as connection:
        selected = None
        for row in connection.execute("SELECT memory_id,MAX(revision) revision FROM revisions GROUP BY memory_id"):
            item = store._candidate(connection, row["memory_id"], row["revision"])
            if item["state"] not in {"rejected", "merged"} and _rule_key(item["payload"]) == _rule_key(payload):
                selected = item
                break
        if selected:
            merged = dict(selected["payload"])
            for key, maximum in (("source_ids", 50), ("correction_ids", 50), ("triggers", 12)):
                values = list(dict.fromkeys([*merged.get(key, []), *payload.get(key, [])]))
                if len(values) > maximum:
                    raise MemoryError("重复经验合并来源超过限额，请拆分适用范围，不能丢弃来源。")
                if key in merged or key in payload:
                    merged[key] = values
            if merged != selected["payload"]:
                result = store._insert_candidate(connection, merged, selected["memory_id"])
            else:
                result = {key: selected[key] for key in ("memory_id", "revision", "content_sha256", "state")}
            result["deduplicated"] = True
        else:
            result = store._insert_candidate(connection, payload)
            result["deduplicated"] = False
        origin_record = {"memory_id": result["memory_id"], "revision": result["revision"], "origin": origin or {}, "evidence": evidence or {}}
        origin_id = "origin_" + store._hash(origin_record)[:32]
        if not _find(connection, "evoz_origins", "origin_id", origin_id):
            _put(connection, "evoz_origins", origin_id, {"origin_id": origin_id, **origin_record, "created_at": store._now()})
    result["conflict_scan"] = scan_conflicts(root)
    status = candidate_review_status(root, result["memory_id"], result["revision"])
    result["conflict_scan"].update({key: status[key] for key in ("blocking", "open_conflicts")})
    result["state"] = store.load_candidate(root, result["memory_id"], result["revision"])["state"]
    return result


def _norm(text):
    for old, new in (("機櫃", "机柜"), ("機架", "机架"), ("布線", "布线"), ("資質", "资质"), ("條款", "条款"), ("通用", "general")):
        text = text.replace(old, new)
    return text.casefold()


def _scan_gate(root, latest, policy_sha):
    with _db(root) as connection:
        statuses = [_candidate_review_status(connection, item["memory_id"], item["revision"], policy_sha) for item in latest]
    return {"blocking": any(item["blocking"] for item in statuses), "open_conflicts": sum(item["open_conflicts"] for item in statuses)}


def scan_conflicts(root, force=False, due_days=7):
    policy_sha = policy_digest(root)
    with _db(root) as connection:
        latest = [store._candidate(connection, row["memory_id"], row["revision"]) for row in connection.execute("SELECT memory_id,MAX(revision) revision FROM revisions GROUP BY memory_id")] if connection else []
        previous = _rows(connection, "evoz_conflicts")
        scans = _rows(connection, "evoz_scans")
    signature = store._hash({"policy_sha256": policy_sha, "candidates": [[item["memory_id"], item["revision"], item["content_sha256"]] for item in latest]})
    last_scan = scans[-1] if scans else None
    if last_scan and not force and last_scan["signature"] == signature and datetime.fromisoformat(last_scan["created_at"]) + timedelta(days=due_days) > datetime.now(timezone.utc):
        return {"policy_sha256": policy_sha, "checked": len(latest), "findings": last_scan["findings"], "created": [],
                "forced": False, "due": False, "last_scan_at": last_scan["created_at"], "next_scan_at": (datetime.fromisoformat(last_scan["created_at"]) + timedelta(days=due_days)).isoformat(),
                **_scan_gate(root, latest, policy_sha)}
    findings = []
    for item in latest:
        payload = item["payload"]
        text = _norm(" ".join(str(payload.get(key, "")) for key in ("title", "applies_when", "rule")))
        negative = bool(re.search(r"不(?:单独)?(?:抽取|提取|生成)|排除|不得|不应|无需|不作为", text))
        reason = None
        if not negative and re.search(r"机柜|机架|布线|cabling", text) and re.search(r"抽|提取|separate|general", text):
            reason = "规则可能要求抽取已排除的机柜/布线；请核对适用范围和原文，关键词命中不是矛盾证明。"
        elif not negative and re.search(r"品牌|gartner|资质", text) and "general" in text:
            reason = "规则可能将纯品牌准入独立抽为 general；含选型技术条件的情形需区分，关键词命中不是矛盾证明。"
        if reason:
            findings.append({"kind": "policy_scope", "memory_id": item["memory_id"], "revision": item["revision"], "reason": reason})
    for position, left in enumerate(latest):
        for right in latest[position + 1:]:
            if left["payload"]["issue_type"] != right["payload"]["issue_type"]:
                continue
            ltext, rtext = _norm(left["payload"]["rule"]), _norm(right["payload"]["rule"])
            neg = lambda value: bool(re.search(r"不应|禁止|不得|不能|不要|不可", value))
            if neg(ltext) != neg(rtext) and difflib.SequenceMatcher(None, ltext, rtext).ratio() >= .62:
                findings.append({"kind": "possible_overlap", "memory_id": left["memory_id"], "revision": left["revision"],
                                 "other_memory_id": right["memory_id"], "other_revision": right["revision"],
                                 "reason": "规则表述相近而执行方向可能不同；需核对适用条件后判断，不自动认定冲突。"})
    created = []
    for finding in findings:
        key = "conflict_" + store._hash({**finding, "policy_sha256": policy_sha})[:32]
        if any(record.get("conflict_id") == key for record in previous):
            continue
        related = [item for item in latest if item["memory_id"] in {finding["memory_id"], finding.get("other_memory_id")}]
        related = [{"memory_id": item["memory_id"], "revision": item["revision"],
                    **{key: item["payload"][key] for key in ("title", "rule", "applies_when", "exclusions")}} for item in related]
        policy_path = Path(root) / ".opencode/skills/term-extract/references/business-rules.md"
        policy_text = policy_path.read_text(encoding="utf-8") if policy_path.exists() else ""
        policy_lines = policy_text.splitlines()
        relevant = set()
        for number, line in enumerate(policy_lines):
            if re.search(r"机柜|机架|布线|品牌|Gartner|general", line):
                relevant.update(range(max(0, number - 1), min(len(policy_lines), number + 2)))
        excerpt = "\n".join(policy_lines[number] for number in sorted(relevant))[:3600]
        review = create_review(root, kind="experience_conflict", title="经验适用范围待核对", question=finding["reason"],
                               context={"finding": finding, "memories": related, "policy_excerpt": excerpt, "policy_sha256": policy_sha,
                                        "excerpt_notice": "仅展示相关口径节选，完整规则以当前固定业务口径页面为准。"}, choices=["保留，条件不同", "需要修订", "不采纳", "证据不足"],
                               subject={"memory_id": finding["memory_id"], "revision": finding["revision"], "policy_sha256": policy_sha}, scope="memory", dedupe_key=key)
        record = {"conflict_id": key, **finding, "policy_sha256": policy_sha, "review_id": review["review_id"], "created_at": store._now()}
        with _db(root, write=True) as connection:
            if not _find(connection, "evoz_conflicts", "conflict_id", key):
                _put(connection, "evoz_conflicts", key, record)
        created.append(record)
    scanned_at = store._now()
    scan_record = {"scan_id": "scan_" + uuid.uuid4().hex, "signature": signature, "policy_sha256": policy_sha,
                   "checked": len(latest), "findings": findings, "created_at": scanned_at}
    with _db(root, write=True) as connection:
        _put(connection, "evoz_scans", scan_record["scan_id"], scan_record)
    return {"policy_sha256": policy_sha, "checked": len(latest), "findings": findings, "created": created,
            "forced": bool(force), "due": True, "last_scan_at": scanned_at,
            "next_scan_at": (datetime.fromisoformat(scanned_at) + timedelta(days=due_days)).isoformat(),
            **_scan_gate(root, latest, policy_sha)}


def list_experiments(root):
    with _db(root) as connection:
        items = _rows(connection, "evoz_experiments")
    return {"items": items, "total": len(items)}


def register_experiment(root, memory_id, revision, record):
    """接口由运行器调用；校验实现见下方，禁止 HTTP 任意报告上传。"""
    return _register_experiment(root, memory_id, revision, record)


def auto_promote(root, evaluation_id):
    with _db(root) as connection:
        evaluation = store._evaluation(connection, evaluation_id)
        report = evaluation["report"]
        if report.get("evaluation_mode") != "paired_labeled_v1":
            raise MemoryError("自动晋级仅接受内部配对标签实验。")
        latest = connection.execute("SELECT MAX(revision) FROM revisions WHERE memory_id=?", (evaluation["memory_id"],)).fetchone()[0]
        if latest != evaluation["revision"]:
            raise MemoryError("候选已有新修订，旧实验不能自动发布；请对最新修订重新实验。")
        if _candidate_review_status(connection, evaluation["memory_id"], evaluation["revision"], policy_digest(root))["blocking"]:
            raise MemoryError("经验冲突仍待处理或已被拒绝，不能自动发布。")
    return store.publish(root, evaluation["memory_id"], evaluation["revision"], evaluation_id,
                         "system:auto-evolution", "自动实验通过：可信标签、独立回归与反例均满足准入；保留审计及撤回能力。")


def _check_provenance(artifact, *, phase, candidate, generation, base_release, expected_source=None, expected_memories=None, expected_policy=None, root=None):
    """结果必须能从同一真实会话的事件重新解析，不能仅上传一个通过声明。"""
    if not isinstance(artifact, dict) or not isinstance(artifact.get("provenance"), dict):
        raise MemoryError("实验结果缺少运行来源。")
    provenance = artifact["provenance"]
    if provenance.get("kind") == "opencode_agent":
        from agent_trials import check_provenance
        return check_provenance(artifact, phase=phase, candidate=candidate, generation=generation,
                                base_release=base_release, expected_source=expected_source,
                                expected_memories=expected_memories, expected_policy=expected_policy, root=root)
    if provenance.get("kind") != "opencode":
        raise MemoryError("自动晋级仅接受真实 OpenCode 运行；fake/外部结果不能冒充实验。")
    from learning_runner import extract_event_output
    if provenance.get("phase") != phase or provenance.get("blind") is not (phase != "judge"):
        raise MemoryError("运行 phase/blind 必须与盲抽或局部复核阶段一致。")
    for key in ("model_id", "variant"):
        if provenance.get(key) != generation.get(key):
            raise MemoryError("实验 A/B 的模型和 variant 必须相同。")
    if provenance.get("base_release_sha256") != base_release:
        raise MemoryError("模型运行未绑定本轮正式经验基线。")
    candidate_ids = provenance.get("candidate_ids", [])
    expected = {key: candidate[key] for key in ("memory_id", "revision", "content_sha256")}
    if phase == "baseline" and candidate_ids:
        raise MemoryError("基线运行不能注入候选经验。")
    if phase == "candidate" and candidate_ids not in ([expected], [candidate["memory_id"]]):
        raise MemoryError("实验运行未精确绑定唯一候选经验。")
    required_files = []
    for prefix in ("events", "prompt", "result"):
        path_value = provenance.get(prefix + "_path", artifact.get("result_path") if prefix == "result" else None)
        sha = provenance.get(prefix + "_sha256", artifact.get("result_sha256") if prefix == "result" else None)
        if not isinstance(path_value, str) or not Path(path_value).is_absolute() or store._file_hash(Path(path_value)) != sha:
            raise MemoryError(f"运行来源 {prefix} 文件缺失、变化或哈希不符。")
        required_files.append({"path": path_value, "sha256": sha})
    if provenance.get("result_path", artifact["result_path"]) != artifact["result_path"] or provenance.get("result_sha256", artifact["result_sha256"]) != artifact["result_sha256"]:
        raise MemoryError("运行来源与实验所用输出不一致。")
    prompt = store._read_json(Path(provenance["prompt_path"]), 8 * 1024 * 1024)
    if phase in {"baseline", "candidate"}:
        if expected_source is not None and prompt.get("source_document") != expected_source:
            raise MemoryError("实际盲抽提示没有交付指定案例的完整原文。")
        if expected_memories is not None and prompt.get("memories") != expected_memories:
            raise MemoryError("实际盲抽提示中的经验集合与基线/候选不符。")
        if expected_policy is not None and prompt.get("references", {}).get("business-rules.md") != expected_policy:
            raise MemoryError("实际盲抽提示中的业务口径与本实验不符。")
    elif phase == "judge":
        if prompt.get("prediction_sha256") != artifact.get("prediction_sha256") or prompt.get("labels_sha256") != artifact.get("labels_sha256"):
            raise MemoryError("实际局部复核提示没有绑定本次预测与标签。")
    result_path = Path(artifact["result_path"])
    if store._file_hash(result_path) != artifact["result_sha256"]:
        raise MemoryError("实验输出已变化。")
    event_text = Path(provenance["events_path"]).read_text(encoding="utf-8")
    parsed = extract_event_output(event_text)
    session_id = store._string(provenance.get("session_id"), "session_id", 200)
    if parsed.get("session_id") != session_id:
        raise MemoryError("实际模型事件的 session_id 与运行来源不一致。")
    sessions = set()
    for line in event_text.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        sid = event.get("sessionID", event.get("session_id")) if isinstance(event, dict) else None
        if sid:
            sessions.add(sid)
    if sessions - {session_id}:
        raise MemoryError("运行事件混入其他 session，不可作为独立盲抽依据。")
    result = store._read_json(result_path, 2 * 1024 * 1024)
    output = parsed.get("output")
    actual = output if phase == "judge" else (output.get("result") if isinstance(output, dict) else output)
    if store._hash(actual) != store._hash(result):
        raise MemoryError("实验输出不能从对应模型事件中重现。")
    return result, required_files


def _review_judgments(connection, case_id, phase, comparison):
    judgments = []
    for review in _rows(connection, "evoz_reviews"):
        subject = review.get("subject", {})
        if (subject.get("case_id") != case_id or subject.get("phase") != phase
                or subject.get("prediction_sha256") != comparison["prediction_sha256"]
                or subject.get("labels_sha256") != comparison["labels_sha256"]):
            continue
        resolution = _find(connection, "evoz_resolutions", "review_id", review["review_id"])
        difference_id = subject.get("difference_id")
        if resolution and resolution["decision"] in {"harmless", "error", "uncertain"} and any(item["difference_id"] == difference_id for item in comparison["differences"]):
            judgments.append({"difference_id": difference_id, "decision": resolution["decision"],
                              "reason": resolution.get("answer") or "人工已针对该案例的局部差异作出判断。"})
    return judgments


def _validate_sources(connection, candidate, development_source_ids):
    sources = list(dict.fromkeys([*candidate["payload"].get("source_ids", []), *candidate["payload"].get("correction_ids", [])]))
    if not sources or set(sources) != set(development_source_ids):
        raise MemoryError("实验必须绑定候选的全部真实归纳来源，不能额外冒用其他开发案例。")
    source_hashes, families, artifacts = set(), set(), []
    from memory_learning import _source, _run, _check_record_files
    for source_id in sources:
        if source_id.startswith("case_"):
            item = _case(connection, source_id)
            _check_case(item)
            if item["split"] != "development":
                raise MemoryError("回归/保留集不能作为归纳来源。")
            source_hashes.add(item["source_sha256"])
            families.add(item["family_id"])
            artifacts.extend({"path": item[path], "sha256": item[sha]} for path, sha in
                             (("input_path", "source_sha256"), ("labels_path", "labels_sha256"),
                              ("input_archive_path", "source_sha256"), ("labels_archive_path", "labels_sha256"), ("index_path", "index_sha256")))
        elif source_id.startswith("corr_"):
            item = store._correction(connection, source_id)
            store._check_correction_files(item)
            source_hashes.add(item["source_sha256"])
            families.add(item["family_id"])
            artifacts.extend({"path": item[path], "sha256": item[sha]} for path, sha in
                             (("input_path", "source_sha256"), ("before_path", "before_sha256"), ("after_path", "after_sha256")))
        else:
            source = _source(connection, source_id)
            run = _run(connection, source["run_id"])
            _check_record_files(run)
            source_hashes.add(run["source_sha256"])
            artifacts.extend(run["artifacts"])
            artifacts.extend([{"path": run["index_path"], "sha256": run["index_sha256"]},
                              {"path": run["input_path"], "sha256": run["source_sha256"]}])
    return source_hashes, families, artifacts


def _register_experiment(root, memory_id, revision, record):
    from learning_compare import compare_results, apply_judgments
    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise MemoryError("自动实验 schema_version 必须为 1。")
    root = Path(root)
    memory_id, revision = store._string(memory_id, "memory_id", 128), store._integer(revision, "revision")
    with _db(root) as connection:
        candidate = store._candidate(connection, memory_id, revision)
        base_entries = store._published(connection)
        current_release = store._release(base_entries)
        if candidate["state"] in {"rejected", "merged", "needs_review", "published"}:
            raise MemoryError("当前修订状态不允许新的自动晋级实验。")
        if record.get("base_release_sha256") != current_release or record.get("policy_sha256") != policy_digest(root):
            raise MemoryError("自动实验基线或业务口径已变化，请重新运行。")
        generation = record.get("generation", {})
        if not isinstance(generation, dict):
            raise MemoryError("generation 必须为对象。")
        for key in ("model_id", "runner_version"):
            store._string(generation.get(key), "generation." + key, 200)
        if not isinstance(generation.get("variant"), str) or len(generation["variant"]) > 200:
            raise MemoryError("generation.variant 必须为字符串；默认变体可为空。")
        source_hashes, source_families, artifacts = _validate_sources(connection, candidate, record.get("development_source_ids", []))
        cases = record.get("cases")
        if not isinstance(cases, list) or not cases:
            raise MemoryError("实验 cases 必须为非空数组。")
        if any(not isinstance(item, dict) or item.get("role") not in {"development", "regression", "counterexample"} for item in cases):
            raise MemoryError("实验角色只能为 development/regression/counterexample。")
        # 未标注观察来源本身可能没有 family；所选可信开发目标也属于归纳族。
        source_families |= {_case(connection, item.get("case_id"))["family_id"] for item in cases if item["role"] == "development"}
        target_hashes = {_case(connection, item.get("case_id"))["source_sha256"] for item in cases if item["role"] == "development"}
        evaluated, blockers, seen_hashes = [], [], set()
        summary = {"fix_improvements": 0, "success_cases": 0, "regressions": 0,
                   "independent_regression_cases": 0, "counterexample_cases": 0, "unfixed_target_cases": 0}
        independent_families = set()
        target_count = 0
        for case in cases:
            if not isinstance(case, dict) or case.get("role") not in {"development", "regression", "counterexample"}:
                raise MemoryError("实验角色只能为 development/regression/counterexample。")
            trusted = _case(connection, case.get("case_id"))
            _check_case(trusted)
            if trusted["split"] == "holdout":
                raise MemoryError("保留集不得用于经验归纳、晋级调参或反复试验。")
            if trusted["policy_sha256"] != record["policy_sha256"]:
                raise MemoryError("标签案例所声明口径已过期，请重新提供当前口径确认。")
            if trusted["source_sha256"] in seen_hashes:
                raise MemoryError("同一原文不能在实验中重复充当多个角色。")
            seen_hashes.add(trusted["source_sha256"])
            if case["role"] == "development" and trusted["split"] != "development":
                raise MemoryError("开发目标必须来自 development 案例。")
            if case["role"] != "development" and (trusted["source_sha256"] in source_hashes or trusted["family_id"] in source_families):
                raise MemoryError("回归和反例必须与经验归纳来源的原文及 family 独立。")
            if case["role"] == "development" and candidate["payload"].get("source_ids") and all(s.startswith("case_") for s in candidate["payload"]["source_ids"]) and trusted["case_id"] not in candidate["payload"]["source_ids"]:
                raise MemoryError("开发目标没有绑定该候选的标签学习来源。")
            index = store._read_json(Path(trusted["index_path"]), 48 * 1024 * 1024)
            labels = store._read_json(Path(trusted["labels_archive_path"]), 2 * 1024 * 1024)
            comparisons = {}
            baseline_provenance = case.get("baseline", {}).get("provenance", {})
            baseline_memories = base_entries
            if baseline_provenance.get("kind") == "opencode_agent":
                from agent_trials import baseline_memories as agent_baseline_memories
                baseline_memories = agent_baseline_memories(root, baseline_provenance)
            if baseline_provenance.get("prompt_path"):
                baseline_prompt = store._read_json(Path(baseline_provenance["prompt_path"]), 8 * 1024 * 1024)
                baseline_memories = baseline_prompt.get("memories")
                if (not isinstance(baseline_memories, list) or any(entry not in base_entries for entry in baseline_memories)
                        or len({store._hash(entry) for entry in baseline_memories}) != len(baseline_memories)):
                    raise MemoryError("基线召回必须是内容完整、无重复的正式经验子集。")
            for phase in ("baseline", "candidate"):
                injected = [{key: candidate[key] for key in ("memory_id", "revision", "content_sha256", "payload")}] if phase == "candidate" else []
                policy_path = root / ".opencode/skills/term-extract/references/business-rules.md"
                expected_policy = policy_path.read_text(encoding="utf-8") if policy_path.exists() else ""
                result, runtime_artifacts = _check_provenance(case[phase], phase=phase, candidate=candidate, generation=generation,
                                                            base_release=current_release, expected_source=index["source_document"],
                                                            expected_memories=([entry for entry in baseline_memories if entry["memory_id"] != memory_id] + injected if phase == "candidate" else baseline_memories),
                                                            expected_policy=expected_policy, root=root)
                artifacts.extend(runtime_artifacts)
                comparison = compare_results(index, result, labels)
                judge = case.get(phase + "_judge")
                model_judgments = []
                if judge:
                    if (judge.get("prediction_sha256") != comparison["prediction_sha256"]
                            or judge.get("labels_sha256") != comparison["labels_sha256"]):
                        raise MemoryError("局部语义复核未绑定本次预测和标签。")
                    judge_result, judge_artifacts = _check_provenance(judge, phase="judge", candidate=candidate,
                                                                    generation=generation, base_release=current_release, root=root)
                    if not isinstance(judge_result, dict) or judge_result.get("judgments") != judge.get("judgments"):
                        raise MemoryError("局部语义判断与实际模型事件输出不一致。")
                    artifacts.extend(judge_artifacts)
                    # 先验证模型不能覆盖硬错误；人工对同一 review 的明确回复优先。
                    apply_judgments(comparison, judge_result["judgments"])
                    model_judgments = judge_result["judgments"]
                judgments = _review_judgments(connection, trusted["case_id"], phase, comparison)
                human_ids = {item["difference_id"] for item in judgments}
                combined = [item for item in model_judgments if item["difference_id"] not in human_ids] + judgments
                if combined:
                    comparison = apply_judgments(comparison, combined)
                comparisons[phase] = comparison
            before, after = comparisons["baseline"], comparisons["candidate"]
            if after["status"] != "passed":
                blockers.append({"code": "candidate_not_passed", "case_id": trusted["case_id"], "message": "候选结果仍有错误或待判断差异。"})
            error_keys = ("missing_objects", "extra_objects", "parameter_omissions", "parameter_pollution", "quantity_errors", "association_errors")
            if any(after["summary"].get(key, 0) > before["summary"].get(key, 0) for key in error_keys) or (before["status"] == "passed" and after["status"] != "passed"):
                summary["regressions"] += 1
            if case["role"] == "development":
                target_count += 1
                if after["status"] != "passed":
                    summary["unfixed_target_cases"] += 1
                else:
                    summary["success_cases"] += 1
                    if before["status"] == "failed":
                        summary["fix_improvements"] += 1
            elif after["status"] == "passed":
                if trusted["family_id"] in independent_families:
                    blockers.append({"code": "family_not_independent", "case_id": trusted["case_id"], "message": "独立回归与反例需要不同 family。"})
                independent_families.add(trusted["family_id"])
                if case["role"] == "regression":
                    summary["independent_regression_cases"] += 1
                elif isinstance(case.get("scope_note"), str) and case["scope_note"].strip():
                    summary["counterexample_cases"] += 1
                else:
                    blockers.append({"code": "counterexample_scope_missing", "case_id": trusted["case_id"], "message": "反例需说明触及的适用或排除边界。"})
            artifacts.extend({"path": trusted[path], "sha256": trusted[sha]} for path, sha in
                             (("input_path", "source_sha256"), ("labels_path", "labels_sha256"),
                              ("input_archive_path", "source_sha256"), ("labels_archive_path", "labels_sha256"), ("index_path", "index_sha256")))
            evaluated.append({"case_id": trusted["case_id"], "role": case["role"], "family_id": trusted["family_id"],
                              "baseline_comparison": before, "candidate_comparison": after})
        if target_count < 1 or summary["independent_regression_cases"] < 1 or summary["counterexample_cases"] < 1 or len(independent_families) < 2:
            blockers.append({"code": "coverage_insufficient", "message": "至少需要开发目标、独立回归和独立反例；回归与反例来自两个独立 family。"})
        if not source_hashes <= target_hashes:
            blockers.append({"code": "development_sources_incomplete", "message": "开发目标必须覆盖该候选的全部归纳来源。"})
        for review in _rows(connection, "evoz_reviews"):
            subject = review.get("subject", {})
            if subject.get("memory_id") == memory_id and subject.get("revision") == revision:
                resolution = _find(connection, "evoz_resolutions", "review_id", review["review_id"])
                if not resolution or resolution["decision"] in {"需要修订", "不采纳", "证据不足"}:
                    blockers.append({"code": "memory_review_open", "review_id": review["review_id"], "message": "经验适用范围仍需处理。"})
    policy_path = root / ".opencode/skills/term-extract/references/business-rules.md"
    if policy_path.exists():
        artifacts.append({"path": str(policy_path.resolve()), "sha256": record["policy_sha256"]})
    else:
        blockers.append({"code": "policy_missing", "message": "缺少已确认业务口径文件。"})
    for artifact in record.get("artifacts", []):
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str) or store._file_hash(Path(artifact["path"])) != artifact.get("sha256"):
            raise MemoryError("额外实验依据哈希不符。")
        artifacts.append(artifact)
    artifacts = list({(item["path"], item["sha256"]): item for item in artifacts}.values())
    status = "passed" if not blockers and not summary["regressions"] and not summary["unfixed_target_cases"] else "failed"
    identity = {"memory_id": memory_id, "revision": revision, "record": record, "evaluated_cases": evaluated, "summary": summary, "blockers": blockers}
    experiment_id = "experiment_" + store._hash(identity)[:32]
    evaluation_id = "eval_" + store._hash({"experiment_id": experiment_id})[:32]
    report = {"candidate": {key: candidate[key] for key in ("memory_id", "revision", "content_sha256")},
              "base_release_sha256": current_release, "status": status, "evaluation_mode": "paired_labeled_v1",
              "summary": summary, "artifacts": artifacts, "automation": {"experiment_id": experiment_id, "record_sha256": store._hash(identity)},
              "evaluator": {"mode": "paired_labeled_v1", "runner_version": generation["runner_version"]},
              "blockers": blockers, "notice": "success_cases 表示目标保持或达到正确，只有 fix_improvements 证明本实验修复改善；不保证未覆盖场景。"}
    experiment = {"experiment_id": experiment_id, "evaluation_id": evaluation_id, "memory_id": memory_id,
                  "revision": revision, **identity, "report": report, "status": status, "summary": summary, "created_at": store._now()}
    store._check_artifacts(report)
    with _db(root, write=True) as connection:
        if store._release(store._published(connection)) != current_release or policy_digest(root) != record["policy_sha256"]:
            raise MemoryError("注册过程中发布集合或口径已变化，请重新实验。")
        existing = _find(connection, "evoz_experiments", "experiment_id", experiment_id)
        if existing:
            return {"experiment_id": experiment_id, "evaluation_id": existing["evaluation_id"], "status": existing["status"], "summary": existing["summary"], "blockers": existing["report"]["blockers"], "replayed": True}
        _put(connection, "evoz_experiments", experiment_id, experiment)
        store._validate_report(report, candidate, current_release)
        check_sealed_report(connection, candidate["payload"], report)
        connection.execute("INSERT INTO evaluations VALUES (?,?,?,?,?,?)", (evaluation_id, memory_id, revision, store._canonical(report).decode(), store._hash(report), store._now()))
    return {"experiment_id": experiment_id, "evaluation_id": evaluation_id, "status": status, "summary": summary, "blockers": blockers, "replayed": False}


def check_sealed_report(connection, payload, report):
    automation = report.get("automation", {})
    experiment = _find(connection, "evoz_experiments", "experiment_id", automation.get("experiment_id"))
    if not experiment or experiment["report"] != report:
        raise MemoryError("配对标签报告必须对应内部完整实验，不能从外部伪造或修改 summary。")
    identity = {key: experiment[key] for key in ("memory_id", "revision", "record", "evaluated_cases", "summary", "blockers")}
    if automation.get("record_sha256") != store._hash(identity):
        raise MemoryError("内部实验报告绑定哈希不符。")
    candidate = store._candidate(connection, experiment["memory_id"], experiment["revision"])
    if payload != candidate["payload"]:
        raise MemoryError("内部实验候选来源不一致。")
    if report["status"] == "passed" and _candidate_review_status(connection, experiment["memory_id"], experiment["revision"], experiment["record"]["policy_sha256"])["blocking"]:
        raise MemoryError("该实验之后出现待处理经验冲突或拒绝决策，请处理后重新实验。")
    _validate_sources(connection, candidate, experiment["record"].get("development_source_ids", []))
    for item in experiment["record"]["cases"]:
        _check_case(_case(connection, item["case_id"]))
    store._check_artifacts(report)


def main(argv=None):
    parser = argparse.ArgumentParser(description="可信标签案例、局部待办与经验冲突检查")
    parser.add_argument("--root", "--project-root", dest="root", type=Path, default=Path(__file__).resolve().parents[4])
    sub = parser.add_subparsers(dest="action", required=True)
    case = sub.add_parser("import-case")
    case.add_argument("--input", type=Path, required=True)
    case.add_argument("--labels", type=Path, required=True)
    case.add_argument("--family", required=True)
    case.add_argument("--split", choices=("development", "regression", "holdout"), default="development")
    sub.add_parser("cases")
    sub.add_parser("scan-conflicts")
    reviews = sub.add_parser("reviews")
    reviews.add_argument("--state", default="open")
    args = parser.parse_args(argv)
    try:
        if args.action == "import-case":
            result = import_case(args.root, args.input, args.labels, args.family, args.split)
        elif args.action == "cases":
            result = list_cases(args.root)
        elif args.action == "reviews":
            result = list_reviews(args.root, args.state)
        else:
            result = scan_conflicts(args.root, force=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (MemoryError, ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
