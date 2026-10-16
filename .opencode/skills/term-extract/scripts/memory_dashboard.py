"""本地记忆页面的数据服务；无模型调用，写操作继续走原有确认和发布门槛。"""
from __future__ import annotations

from contextlib import contextmanager
import difflib
import hashlib
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote

import memory_learning as learning
import memory_store as store

MemoryError = store.MemoryError
POLICY = Path(".opencode/skills/term-extract/references/business-rules.md")
NOTICE = "经验可通过独立评测自动发布，也可由人工审核指定版本后发布。人工批准规则不代表整篇抽取结果成为正确标签。"


def _has(connection, table):
    return bool(connection and connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _rows(connection, table, suffix="", params=()):
    return list(connection.execute(f"SELECT * FROM {table} {suffix}", params)) if _has(connection, table) else []


def _number(value, name, default, maximum, minimum=0):
    if value is None:
        return default
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        value = int(value)
    return store._integer(value, name, minimum, maximum)


def _page(items, query, default=100):
    limit = _number(query.get("limit"), "limit", default, 200, 1)
    offset = _number(query.get("offset"), "offset", 0, 10**7)
    return {"items": items[offset:offset + limit], "total": len(items), "has_more": offset + limit < len(items)}


def _ack(body):
    if body.get("acknowledged") is not True:
        raise MemoryError("请明确勾选人工审核确认；acknowledged 必须为 true。")
    return (store._string(body.get("reviewer"), "reviewer（人工审阅者审计名）", 160),
            store._string(body.get("note"), "审核说明", 4000))


class DashboardService:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()

    @contextmanager
    def _read(self):
        with store._db(self.root) as connection:
            yield connection

    def _release(self, connection):
        if not _has(connection, "active") or not _has(connection, "revisions"):
            return store._release([]), None
        try:
            return store._release(store._published(connection)), None
        except (ValueError, KeyError, TypeError) as exc:
            return None, str(exc)

    def _memory_items(self, connection):
        items = []
        for row in _rows(connection, "revisions", "GROUP BY memory_id HAVING revision=MAX(revision) ORDER BY created_at DESC"):
            mid = row["memory_id"]
            try:
                memory = store._candidate(connection, mid, row["revision"])
                payload = memory["payload"]
                active = connection.execute("SELECT revision FROM active WHERE memory_id=?", (mid,)).fetchone()
                items.append({"memory_id": mid, "title": payload["title"], "issue_type": payload["issue_type"],
                              "state": memory["state"], "latest_revision": memory["revision"],
                              "active_revision": active["revision"] if active else None, "created_at": memory["created_at"],
                              "rule_preview": payload["rule"][:180], "source_count": len(payload["correction_ids"]) + len(payload.get("source_ids", [])),
                              "publication": store.publication_info(connection, mid, memory["revision"]),
                              "active_publication": store.publication_info(connection, mid, active["revision"]) if active else None})
            except (ValueError, KeyError, TypeError) as exc:
                items.append({"memory_id": mid, "title": "记录不可读", "issue_type": "", "state": "error",
                              "latest_revision": row["revision"], "active_revision": None, "created_at": row["created_at"],
                              "rule_preview": "", "source_count": 0, "error": str(exc)})
        return items

    def _runs(self, connection):
        items = []
        for row in _rows(connection, "learning_runs", "ORDER BY rowid DESC"):
            try:
                items.append(learning._run_summary(connection, learning._run(connection, row["run_id"])))
            except (ValueError, KeyError, TypeError) as exc:
                items.append({"run_id": row["run_id"], "status": "error", "error": str(exc), "created_at": ""})
        return items

    def _feedback(self, connection):
        items = []
        for row in _rows(connection, "learning_feedback", "ORDER BY rowid DESC"):
            fid = row["feedback_id"]
            try:
                record = learning._table_record(connection, "learning_feedback", "feedback_id", fid)
                event = learning._table_record(connection, "learning_events", "event_key", "reflection:" + fid)
                items.append({"feedback_id": fid, "source_id": fid, "run_id": record["run_id"],
                              "feedback_text": record["feedback_text"], "has_corrected_result": record["after_json"] is not None,
                              "confirmed": learning._confirmation(connection, fid) is not None,
                              "created_at": record["created_at"], "candidate_count": len(event["candidates"])})
            except (ValueError, KeyError, TypeError) as exc:
                items.append({"feedback_id": fid, "source_id": fid, "run_id": "", "feedback_text": "记录不可读",
                              "has_corrected_result": False, "confirmed": False, "created_at": "", "candidate_count": 0, "error": str(exc)})
        return items

    def _source(self, connection, source_id):
        source_id = store._string(source_id, "source_id", 128)
        if source_id.startswith("case_"):
            import evolution_store
            record = evolution_store.get_case(self.root, source_id)
            return {**record, "source_id": source_id, "kind": "trusted_label", "confirmed": True,
                    "invalidated": False, "can_confirm": False, "has_corrected_result": True,
                    "title": "用户提供的可信标签", "notice": "可信标签声明不等于人工批准经验。"}
        if source_id.startswith("corr_"):
            record = store._correction(connection, source_id)
            source = {key: value for key, value in record.items() if key not in {"before_json", "after_json"}}
            source.update(source_id=source_id, kind="correction", confirmed=True, invalidated=False,
                          has_corrected_result=record.get("after_json") is not None, can_confirm=False,
                          title=record.get("reason", "人工纠错"))
            try:
                store._check_correction_files(record)
            except ValueError as exc:
                source["error"] = str(exc)
            return source
        raw = learning._source(connection, source_id)
        run = learning._run(connection, raw["run_id"])
        confirmation = learning._confirmation(connection, source_id)
        source = {**raw, "confirmed": confirmation is not None, "confirmation": confirmation,
                  "invalidated": False, "has_corrected_result": False, "can_confirm": False,
                  "title": "成功运行" if raw["kind"] == "success" else "用户反馈"}
        different = True
        if raw["kind"] == "correction":
            feedback = learning._table_record(connection, "learning_feedback", "feedback_id", source_id)
            source.update(feedback_text=feedback["feedback_text"], has_corrected_result=feedback["after_json"] is not None)
            different = feedback["after_json"] != run["result_json"]
        try:
            source.update(learning.show_source(self.root, source_id))
            source["can_confirm"] = not source["confirmed"] and not source["invalidated"] and (raw["kind"] == "success" or (source["has_corrected_result"] and different))
        except (ValueError, KeyError, TypeError) as exc:
            source["error"] = str(exc)
        return source

    def _usable_evaluation(self, connection, memory, evaluation_id, release):
        try:
            if memory["state"] == "published":
                raise MemoryError("该修订已发布。")
            if release is None:
                raise MemoryError("已发布集合不可读，不能安全判定评测基线。")
            evaluation = store._evaluation(connection, evaluation_id)
            if connection.execute("SELECT 1 FROM used_evaluations WHERE evaluation_id=?", (evaluation_id,)).fetchone():
                raise MemoryError("该评测已用于发布，重新发布需重新评测。")
            if evaluation["memory_id"] != memory["memory_id"] or evaluation["revision"] != memory["revision"]:
                raise MemoryError("评测不属于此修订。")
            report = store._validate_report(evaluation["report"], memory, release)
            payload, summary = memory["payload"], report["summary"]
            if not payload["correction_ids"] and not payload.get("source_ids"):
                raise MemoryError("缺少人工确认的来源。")
            store._check_learning_sources(connection, payload, report)
            for cid in payload["correction_ids"]:
                store._check_correction_files(store._correction(connection, cid))
            mode = report.get("evaluation_mode")
            target = ((summary.get("success_cases", 0) >= 1 or summary["fix_improvements"] >= 1)
                      if mode == "paired_labeled_v1" else
                      summary.get("success_cases", 0) >= 1 if mode == "success" else summary["fix_improvements"] >= 1)
            if not (report["status"] == "passed" and target and summary["regressions"] == 0
                    and summary["independent_regression_cases"] >= 1 and summary["counterexample_cases"] >= 1
                    and summary["unfixed_target_cases"] == 0):
                raise MemoryError("评测未通过发布门槛：需目标通过、独立回归及反例，且无退化。")
            store._check_artifacts(report)
            return True, ""
        except (ValueError, KeyError, TypeError) as exc:
            return False, str(exc)

    def _memory(self, connection, memory_id, revision=None):
        rows = _rows(connection, "revisions", "WHERE memory_id=? ORDER BY revision DESC", (memory_id,))
        if not rows:
            raise MemoryError("经验不存在。")
        selected = _number(revision, "revision", rows[0]["revision"], 2**31 - 1, 1)
        memory = store._candidate(connection, memory_id, selected)
        active = connection.execute("SELECT revision FROM active WHERE memory_id=?", (memory_id,)).fetchone()
        active_revision = active["revision"] if active else None
        versions = []
        for row in rows:
            try:
                item = store._candidate(connection, memory_id, row["revision"])
                versions.append({key: item[key] for key in ("revision", "state", "content_sha256", "created_at")})
            except ValueError as exc:
                versions.append({"revision": row["revision"], "state": "error", "content_sha256": row["content_sha256"], "created_at": row["created_at"], "error": str(exc)})
        blockers, sources = [], []
        payload = memory["payload"]
        ids = payload["correction_ids"] + payload.get("source_ids", [])
        if not ids:
            blockers.append({"code": "NO_SOURCES", "message": "经验缺少来源，请关联人工确认的运行或纠错。"})
        for sid in ids:
            try:
                source = self._source(connection, sid)
                sources.append({key: source.get(key) for key in ("source_id", "kind", "confirmed", "invalidated", "error", "run_id", "title", "can_confirm", "has_corrected_result")})
                code = ("SOURCE_ERROR" if source.get("error") else "SOURCE_INVALIDATED" if source.get("invalidated")
                        else "FEEDBACK_INCOMPLETE" if source["kind"] == "correction" and not source.get("has_corrected_result")
                        else "SOURCE_UNCONFIRMED" if not source.get("confirmed") else None)
                if code:
                    message = source.get("error") or {"SOURCE_INVALIDATED": "原成功输出已有人工确认纠错，来源失效。",
                              "FEEDBACK_INCOMPLETE": "用户反馈尚无完整修正稿，需补齐后确认。",
                              "SOURCE_UNCONFIRMED": "来源尚未人工确认。"}[code]
                    blockers.append({"code": code, "message": message, "source_id": sid})
            except (ValueError, KeyError, TypeError) as exc:
                sources.append({"source_id": sid, "error": str(exc), "confirmed": False, "invalidated": False, "can_confirm": False})
                blockers.append({"code": "SOURCE_ERROR", "message": str(exc), "source_id": sid})
        release, release_error = self._release(connection)
        if release_error:
            blockers.append({"code": "RELEASE_ERROR", "message": release_error})
        evaluations = []
        for row in _rows(connection, "evaluations", "WHERE memory_id=? AND revision=? ORDER BY created_at DESC", (memory_id, selected)):
            try:
                value = store._evaluation(connection, row["evaluation_id"])
                report = value["report"]
                usable, reason = self._usable_evaluation(connection, memory, row["evaluation_id"], release)
                evaluations.append({"evaluation_id": row["evaluation_id"], "status": report["status"], "summary": report["summary"],
                                    "evaluation_mode": report.get("evaluation_mode", "correction"), "created_at": row["created_at"],
                                    "usable": usable, "blocking_reason": reason})
            except (ValueError, KeyError, TypeError) as exc:
                evaluations.append({"evaluation_id": row["evaluation_id"], "status": "error", "summary": {}, "created_at": row["created_at"],
                                    "evaluation_mode": "", "usable": False, "blocking_reason": str(exc)})
        usable = next((item for item in evaluations if item["usable"]), None)
        if usable and usable.get("evaluation_mode") == "paired_labeled_v1":
            # 候选可以来自未标注的观察；真实可信案例对照通过后无需再确认原观察整篇正确。
            blockers = [item for item in blockers if item["code"] != "SOURCE_UNCONFIRMED"]
        if not usable and memory["state"] != "published":
            blockers.append({"code": "NO_USABLE_EVALUATION", "message": evaluations[0]["blocking_reason"] if evaluations else "尚未通过可信案例验证。请在 OpenCode 中进行标签学习与独立验证；网页可管理可信案例。旧手动离线评测入口仍可使用。"})
        audit = []
        for row in _rows(connection, "audit", "WHERE memory_id=? ORDER BY created_at DESC", (memory_id,)):
            try:
                record = store._parse(row["record_json"])
                audit.append({"action": row["action"], "created_at": row["created_at"], "reviewer": record.get("reviewer", ""), "note": record.get("note", ""),
                              "basis": record.get("publication_basis", record.get("basis", "human_review" if record.get("approval_id") else "evaluated" if row["action"] == "publish" else None)),
                              "approval_id": record.get("approval_id"), "evaluation_id": record.get("evaluation_id")})
            except ValueError as exc:
                audit.append({"action": row["action"], "created_at": row["created_at"], "reviewer": "", "note": "", "error": str(exc)})
        changes = []
        older = next((row for row in rows if row["revision"] < selected), None)
        if older:
            try:
                before = store._candidate(connection, memory_id, older["revision"])["payload"]
                changes = [{"field": key, "before": before.get(key), "after": payload.get(key)} for key in sorted(set(before) | set(payload)) if before.get(key) != payload.get(key)]
            except ValueError:
                changes = [{"field": "旧修订不可读", "before": None, "after": None}]
        manual = store.manual_publish_status(connection, self.root, memory_id, selected)
        return {"memory": memory, "versions": versions, "active_revision": active_revision, "sources": sources,
                "evaluations": evaluations, "audit": audit, "changes": changes, "blockers": blockers,
                "can_publish": usable is not None and not blockers, "publish_evaluation_id": usable["evaluation_id"] if usable else None,
                "can_manual_publish": manual["can_publish"], "manual_publish_blockers": manual["blockers"],
                "manual_publish_warnings": manual.get("warnings", []),
                "publication": store.publication_info(connection, memory_id, selected),
                "active_publication": store.publication_info(connection, memory_id, active_revision) if active_revision else None,
                "can_withdraw": active_revision is not None}

    def _run_detail(self, connection, run_id):
        raw = learning._run(connection, run_id)
        summary = learning._run_summary(connection, raw)
        try:
            learning._check_record_files(raw)
        except ValueError as exc:
            summary["artifact_warning"] = str(exc)
        return {"run": summary, "result": raw["result_json"], "evidence": raw["evidence_json"]}

    def _source_detail(self, connection, source_id):
        if source_id.startswith("case_"):
            import evolution_store
            case = evolution_store.get_case(self.root, source_id, include_labels=True)
            return {"source": self._source(connection, source_id), "run": None, "before_result": None,
                    "after_result": case.get("labels", case.get("labels_json")), "snippets": [],
                    "excerpt_notice": "标签与输入同套定位，案例原件已单独归档。"}
        try:
            source = self._source(connection, source_id)
        except (ValueError, KeyError, TypeError) as exc:
            return {"source": {"source_id": source_id, "error": str(exc)}, "run": None, "before_result": None, "after_result": None,
                    "snippets": [], "excerpt_notice": "来源不可读，未返回原文。"}
        run, before, after = None, None, None
        tags = []
        if source_id.startswith("corr_"):
            raw = store._correction(connection, source_id)
            before, after = raw.get("before_json"), raw.get("after_json")
        else:
            raw_source = learning._source(connection, source_id)
            raw_run = learning._run(connection, raw_source["run_id"])
            run = learning._run_summary(connection, raw_run)
            before = raw_run["result_json"]
            if raw_source["kind"] == "correction":
                after = learning._table_record(connection, "learning_feedback", "feedback_id", source_id)["after_json"]
            else:
                after = raw_run["result_json"]
            event = source.get("reflection_event") or {}
            for insight in event.get("reflection", {}).get("insights", []):
                tags.extend(insight.get("evidence_tags", []))
        if not tags:
            for item in after or before or []:
                if isinstance(item, dict):
                    tags.extend(item.get("定位标识", []))
        tags = list(dict.fromkeys(tag for tag in tags if isinstance(tag, str)))
        snippets, budget = [], 6000
        if source.get("index_path") and not source.get("error"):
            try:
                index = store._read_json(Path(source["index_path"]), 32 * 1024 * 1024)
                units = {unit.get("tag"): unit for unit in index.get("units", []) if isinstance(unit, dict)}
                for tag in tags[:12]:
                    unit = units.get(tag)
                    if unit is None or budget <= 0:
                        continue
                    text = unit.get("source_text", unit.get("text", ""))
                    excerpt = text[:min(800, budget)]
                    snippets.append({"tag": tag, "text": excerpt, "block_index": unit.get("block_index"), "row": unit.get("row"),
                                     "col": unit.get("col"), "truncated": len(excerpt) < len(text)})
                    budget -= len(excerpt)
            except (ValueError, KeyError, TypeError) as exc:
                source["excerpt_error"] = str(exc)
        return {"source": source, "run": run, "before_result": before if isinstance(before, list) else None,
                "after_result": after if isinstance(after, list) else None, "snippets": snippets,
                "excerpt_notice": "仅展示观察标签或结果端点的节选：最多12段，每段800字符，总计6000字符；不是全文覆盖。原文漂移/不可读时不展示节选。"}

    def _policy(self):
        path = self.root / POLICY
        content, sha = "", None
        if path.is_file():
            raw = path.read_bytes()
            if len(raw) > 256 * 1024:
                raise MemoryError("业务口径文件超过256 KiB页面预算。")
            content, sha = raw.decode("utf-8-sig"), hashlib.sha256(raw).hexdigest()
        previous, previous_sha, history = None, None, False
        notice = "仅比较文本与哈希；策略变化不自动判定旧运行或经验错误。"
        try:
            def git(*args):
                completed = subprocess.run(["git", *args], cwd=self.root, capture_output=True, timeout=5)
                if completed.returncode:
                    raise MemoryError("当前目录没有可读取的Git历史。")
                return completed.stdout
            repo = Path(git("rev-parse", "--show-toplevel").decode().strip()).resolve()
            relative = path.resolve().relative_to(repo).as_posix()
            commits = git("log", "-2", "--format=%H", "--", relative).decode().splitlines()
            for commit in commits:
                old = git("show", f"{commit}:{relative}")
                digest = hashlib.sha256(old).hexdigest()
                # Git 保存 LF，Windows 工作区可能为 CRLF；同正文不能当作上一版。
                if old.decode("utf-8-sig").replace("\r\n", "\n") != content.replace("\r\n", "\n"):
                    if len(old) > 256 * 1024:
                        raise MemoryError("上一版策略超过页面预算。")
                    previous, previous_sha, history = old.decode("utf-8-sig"), digest, True
                    break
        except (ValueError, OSError, subprocess.SubprocessError, UnicodeError):
            notice += " 无可读取的上一版正文，未构造历史差异。"
        if not history:
            notice += " 未发现不同的上一版正文。"
        changes = []
        if history:
            for line in difflib.ndiff(previous.splitlines(), content.splitlines()):
                kind = {"+ ": "added", "- ": "removed", "  ": "unchanged"}.get(line[:2])
                if kind:
                    changes.append({"type": kind, "text": line[2:]})
        return {"path": str(path), "content": content, "sha256": sha, "previous_content": previous,
                "previous_sha256": previous_sha, "changes": changes, "history_available": history, "notice": notice}

    def _history(self, connection):
        items = []
        for row in _rows(connection, "learning_runs"):
            try:
                run = learning._run(connection, row["run_id"])
                items.append({"kind": "run", "title": "抽取运行已登记", "summary": "结果和来源已归档；保存运行不等于确认正确或发布经验。",
                              "source_id": row["run_id"], "created_at": run["created_at"]})
            except (ValueError, KeyError, TypeError):
                continue
        for row in _rows(connection, "revisions"):
            try:
                memory = store._candidate(connection, row["memory_id"], row["revision"])
                items.append({"kind": "revision", "title": memory["payload"]["title"], "summary": f"创建不可变修订 v{row['revision']}，不等于发布。",
                              "memory_id": row["memory_id"], "revision": row["revision"], "created_at": row["created_at"]})
            except (ValueError, KeyError, TypeError) as exc:
                items.append({"kind": "error", "title": "修订记录不可读", "summary": str(exc), "memory_id": row["memory_id"], "created_at": row["created_at"]})
        for row in _rows(connection, "learning_feedback"):
            try:
                feedback = learning._table_record(connection, "learning_feedback", "feedback_id", row["feedback_id"])
                items.append({"kind": "feedback", "title": "用户反馈", "summary": feedback["feedback_text"][:240],
                              "source_id": feedback["feedback_id"], "created_at": feedback["created_at"]})
            except (ValueError, KeyError, TypeError):
                continue
        for row in _rows(connection, "learning_confirmations"):
            try:
                confirmation = learning._confirmation(connection, row["source_id"])
                items.append({"kind": "confirmation", "title": "人工确认来源", "summary": confirmation["note"],
                              "source_id": row["source_id"], "created_at": confirmation["confirmed_at"]})
            except (ValueError, KeyError, TypeError):
                continue
        for row in _rows(connection, "audit"):
            try:
                record = store._parse(row["record_json"])
                basis = record.get("publication_basis", record.get("basis", "human_review" if record.get("approval_id") else "evaluated"))
                published = row["action"] in {"publish", "manual_publish"}
                items.append({"kind": row["action"], "title": "人工审核发布" if published and basis == "human_review" else "评测通过后发布" if published else "经验撤回",
                              "summary": record.get("note", ""), "memory_id": row["memory_id"], "revision": row["revision"],
                              "publication_basis": basis if published else None, "approval_id": record.get("approval_id"),
                              "evaluation_id": record.get("evaluation_id"), "created_at": row["created_at"]})
            except (ValueError, KeyError, TypeError):
                continue
        for row in _rows(connection, "evaluations"):
            try:
                evaluation = store._evaluation(connection, row["evaluation_id"])
                items.append({"kind": "evaluation", "title": "离线评测已登记", "summary": "给定工件的断言评测" + ("通过" if evaluation["report"]["status"] == "passed" else "未通过") + "；不等于已发布。",
                              "memory_id": row["memory_id"], "revision": row["revision"], "evaluation_id": row["evaluation_id"], "created_at": row["created_at"]})
            except (ValueError, KeyError, TypeError):
                continue
        return sorted(items, key=lambda item: item.get("created_at", ""), reverse=True)

    def get(self, path: str, query: dict) -> dict:
        query = query or {}
        parts = [unquote(part) for part in path.strip("/").split("/")]
        if parts[:2] in (["api", "learning"], ["api", "reviews"]):
            import evolution_dashboard
            value = evolution_dashboard.get(self.root, parts, query)
            if value is not None:
                return value
        if parts == ["api", "policy"]:
            return self._policy()
        with self._read() as connection:
            if parts == ["api", "overview"]:
                memories, runs, feedback = self._memory_items(connection), self._runs(connection), self._feedback(connection)
                release, error = self._release(connection)
                policy_path = self.root / POLICY
                current = store._file_hash(policy_path) if policy_path.is_file() else None
                changed = [{"run_id": run["run_id"], "recorded_sha256": run.get("generation", {}).get("reference_sha256", {}).get("business-rules.md"), "current_sha256": current}
                           for run in runs if current and run.get("generation", {}).get("reference_sha256", {}).get("business-rules.md") not in (None, current)]
                resolved = {store._parse(row["record_json"])["review_id"] for row in _rows(connection, "evoz_resolutions")}
                pending_reviews = sum(store._parse(row["record_json"])["review_id"] not in resolved for row in _rows(connection, "evoz_reviews"))
                return {"project": {"name": self.root.name, "path": str(self.root), "store_dir": str(store.resolve_store_dir(self.root))},
                        "counts": {"published": len(_rows(connection, "active")), "candidate": sum(item["state"] == "candidate" for item in memories),
                                   "memories": len(memories), "needs_review": sum(item["state"] == "needs_review" for item in memories),
                                   "pending_reviews": pending_reviews, "learning_jobs": len(_rows(connection, "workflow_jobs")),
                                   "trusted_cases": len(_rows(connection, "evoz_cases")),
                                   "withdrawn": sum(item["state"] == "withdrawn" and not item["active_revision"] for item in memories),
                                   "runs": len(runs), "feedback": len(feedback), "pending_feedback": sum(not item["confirmed"] for item in feedback)},
                        "release_sha256": release, "status_message": error or ("尚无记忆记录；抽取后可在此审阅候选。" if not memories and not runs else NOTICE),
                        "policy_changed_runs": changed}
            if parts == ["api", "memories"]:
                state = query.get("state", "all")
                if state not in {"all", "candidate", "published", "withdrawn", "superseded", "error", "rejected", "merged", "needs_review"}:
                    raise MemoryError("state 不合法。")
                raw_query = query.get("q", "")
                if not isinstance(raw_query, str) or len(raw_query) > 500:
                    raise MemoryError("q 必须为最多500字符的字符串。")
                q = raw_query.strip().casefold()
                items = [item for item in self._memory_items(connection) if (state == "all" or item["state"] == state or (state == "published" and item["active_revision"] is not None))
                         and (not q or q in (item["memory_id"] + item["title"] + item["rule_preview"]).casefold())]
                return _page(items, query)
            if len(parts) == 3 and parts[:2] == ["api", "memories"]:
                return self._memory(connection, parts[2], query.get("revision"))
            if len(parts) == 3 and parts[:2] == ["api", "sources"]:
                return self._source_detail(connection, parts[2])
            if len(parts) == 3 and parts[:2] == ["api", "evaluations"]:
                return store._evaluation(connection, parts[2])
            if parts == ["api", "runs"]:
                return _page(self._runs(connection), query, 50)
            if len(parts) == 3 and parts[:2] == ["api", "runs"]:
                return self._run_detail(connection, parts[2])
            if parts == ["api", "feedback"]:
                return _page(self._feedback(connection), query, 50)
            if parts == ["api", "history"]:
                return _page(self._history(connection), query)
        raise MemoryError("未找到该API。")

    def post(self, path: str, body: dict) -> dict:
        if not isinstance(body, dict):
            raise MemoryError("请求体必须为JSON对象。")
        parts = [unquote(part) for part in path.strip("/").split("/")]
        if parts[:2] in (["api", "learning"], ["api", "reviews"], ["api", "experiences"]):
            import evolution_dashboard
            value = evolution_dashboard.post(self.root, parts, body)
            if value is not None:
                return value
        if parts == ["api", "memories"]:
            memory_id = body.get("memory_id")
            with store._db(self.root, write=True) as connection:
                if memory_id is not None:
                    memory_id = store._string(memory_id, "memory_id", 128)
                    expected = store._integer(body.get("expected_revision"), "expected_revision")
                    latest = connection.execute("SELECT MAX(revision) FROM revisions WHERE memory_id=?", (memory_id,)).fetchone()[0]
                    if latest != expected:
                        raise MemoryError("最新修订已变化，请刷新后再保存，不能覆盖并发修改。")
                elif body.get("expected_revision") is not None:
                    raise MemoryError("新经验不接受 expected_revision。")
                return store._insert_candidate(connection, body.get("payload"), memory_id)
        if len(parts) == 4 and parts[:2] == ["api", "sources"] and parts[3] == "confirm":
            reviewer, note = _ack(body)
            with self._read() as connection:
                source = learning._source(connection, parts[2])
            family = store._string(body.get("family_id"), "family_id", 200)
            if source["kind"] == "success":
                return learning.confirm_run(self.root, parts[2], reviewer, family, note)
            return learning.confirm_feedback(self.root, parts[2], reviewer, family, note)
        if len(parts) == 4 and parts[:2] == ["api", "feedback"] and parts[3] == "complete":
            if not isinstance(body.get("after_result"), list):
                raise MemoryError("after_result 必须为完整七字段结果数组。")
            with self._read() as connection:
                prior = learning._table_record(connection, "learning_feedback", "feedback_id", parts[2])
                event = learning._table_record(connection, "learning_events", "event_key", "reflection:" + parts[2])
            reflection = body.get("reflection", event["reflection"])
            result = learning.record_feedback(self.root, prior["run_id"], prior["feedback_text"], body["after_result"], reflection)
            return {**result, "original_feedback_id": parts[2], "new_feedback_id": result["feedback_id"]}
        if parts == ["api", "evaluate"]:
            import memory_evaluate
            manifest = Path(store._string(body.get("manifest_path"), "manifest_path", 4096))
            if not manifest.is_absolute():
                manifest = self.root / manifest
            report = memory_evaluate.evaluate_manifest(self.root, store._string(body.get("memory_id"), "memory_id", 128),
                        store._integer(body.get("revision"), "revision"), manifest.resolve())
            return {key: report[key] for key in ("evaluation_id", "report_sha256", "status", "summary", "evaluation_mode", "notice")}
        if parts == ["api", "publish-reviewed"]:
            reviewer, note = _ack(body)
            content_sha256 = store._string(body.get("content_sha256"), "content_sha256（已审核版本内容摘要）", 64)
            if not re.fullmatch(r"[0-9a-f]{64}", content_sha256):
                raise MemoryError("content_sha256 必须为已审核版本的 64 位内容摘要，请刷新后重新审核。")
            return store.manual_publish(self.root, body.get("memory_id"), body.get("revision"), reviewer, note,
                                        expected_content_sha256=content_sha256)
        if parts == ["api", "publish"]:
            reviewer, note = _ack(body)
            return store.publish(self.root, body.get("memory_id"), body.get("revision"), body.get("evaluation_id"), reviewer, note)
        if parts == ["api", "withdraw"]:
            reviewer, note = _ack(body)
            expected = store._integer(body.get("expected_revision"), "expected_revision")
            return store.withdraw(self.root, body.get("memory_id"), reviewer, note, expected_revision=expected)
        raise MemoryError("未找到该操作API。")
