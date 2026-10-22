"""可视化服务的合成协议测试；不访问真实经验库，不调用模型。"""
from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import document_io
import memory_dashboard as dashboard
import memory_learning as learning
import memory_store as store


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="term-dashboard-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        environment = patch.dict(os.environ, {"TERM_EXTRACT_MEMORY_DIR": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.service = dashboard.DashboardService(self.root)
        self.input = self.root / "input.json"
        self.write(self.input, [{"text": "<C9> 交换机原文"}, {"text": "<C2> 采购数量2台。" + "参数🌐" * 500}])
        self.result = [{"id": 0, "条款标题": "Core Switch", "条款分类": "normal", "相关条款": [],
                        "产品分类": "盒式交换机", "定位标识": ["<C9>", "<C2>"], "产品数量": "2台"}]
        self.before, self.after = self.root / "bad.json", self.root / "gold.json"
        bad = copy.deepcopy(self.result)
        bad[0]["产品数量"] = "1台"
        self.write(self.before, bad)
        self.write(self.after, self.result)

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def correction(self):
        return store.import_correction(self.root, self.input, self.before, self.after, "合成数量核验", "合成确认角色", "family-a")

    def payload(self, **updates):
        correction = self.correction()
        result = {"title": "采购数量核验🌐", "issue_type": "quantity", "triggers": ["采购数量", "switch"],
                  "applies_when": "清单与技术段明确属于同一采购项时", "rule": "保留采购数量及单位。", "exclusions": "不得合并不同采购项。",
                  "correction_ids": [correction["correction_id"]]}
        result.update(updates)
        return result

    def report(self, candidate, artifacts=None):
        return {"candidate": {key: candidate[key] for key in ("memory_id", "revision", "content_sha256")},
                "base_release_sha256": store.release_digest(self.root), "status": "passed",
                "summary": {"fix_improvements": 1, "regressions": 0, "independent_regression_cases": 1,
                            "counterexample_cases": 1, "unfixed_target_cases": 0}, "artifacts": artifacts or []}

    def evaluate_fixture(self, candidate, **kwargs):
        return store.register_evaluation(self.root, candidate["memory_id"], candidate["revision"], self.report(candidate, **kwargs))

    def publish(self, candidate, evaluation):
        return self.service.post("/api/publish", {"memory_id": candidate["memory_id"], "revision": candidate["revision"],
                    "evaluation_id": evaluation["evaluation_id"], "reviewer": "合成审阅角色", "note": "仅合成协议测试", "acknowledged": True})

    def run_fixture(self):
        prepared = document_io.prepare(self.input, self.root / "task")
        index = Path(prepared["index_path"])
        evidence = {"clauses": [{"id": 0, "quantity_source_tags": ["<C2>"], "related_source_tags": [], "notes": "数量来源"}], "unresolved": []}
        evidence_path = self.root / "evidence.json"
        self.write(evidence_path, evidence)
        run = {"status": "submitted", "production_submittable": True, "source_sha256": store._file_hash(self.input),
               "coverage": {"complete": True, "index_sha256": store._file_hash(index)},
               "generation": {}, "reference_sha256": {"business-rules.md": "a" * 64}}
        run_path = self.root / "run.json"
        self.write(run_path, run)
        return learning.register_run(self.root, index, self.after, evidence_path, run_path)

    def reflection(self):
        return {"insights": [{"title": "核验采购清单", "issue_type": "quantity", "triggers": ["采购数量"], "applies_when": "能够确认同一采购对象",
                 "rule": "保留原文单位。", "exclusions": "不同采购对象不合并。", "observation": "本篇数量位于另一段。", "evidence_tags": ["<C2>"]}], "no_experience_reason": ""}

    def test_manual_publish_needs_no_evaluation_or_whole_run_confirmation(self):
        run = self.run_fixture()
        candidate = learning.record_reflection(self.root, run["run_id"], self.reflection())["candidates"][0]
        path = "/api/memories/" + candidate["memory_id"]
        detail = self.service.get(path, {})
        self.assertFalse(detail["can_publish"])
        self.assertTrue(detail["can_manual_publish"], detail)
        self.assertFalse(detail["sources"][0]["confirmed"])
        body = {"memory_id": candidate["memory_id"], "revision": candidate["revision"],
                "content_sha256": candidate["content_sha256"], "reviewer": "合成人工审核人",
                "note": "仅核对这条规则及适用条件，不认定整篇结果正确", "acknowledged": True}
        for value in (False, 1, "true", None):
            with self.assertRaises(store.MemoryError):
                self.service.post("/api/publish-reviewed", {**body, "acknowledged": value})
        published = self.service.post("/api/publish-reviewed", body)
        self.assertEqual(published["state"], "published")
        detail = self.service.get(path, {})
        self.assertEqual(detail["publication"]["basis"], "human_review")
        self.assertEqual(detail["publication"]["reviewer"], body["reviewer"])
        self.assertEqual(detail["publication"]["evaluation_status"], "not_evaluated")
        self.assertIsNone(detail["publication"]["evaluation_id"])
        self.assertTrue(detail["publication"]["approval_id"])
        self.assertFalse(detail["sources"][0]["confirmed"])
        self.assertFalse(detail["can_manual_publish"])
        listing = self.service.get("/api/memories", {})["items"][0]
        self.assertEqual(listing["active_publication"]["basis"], "human_review")
        history = self.service.get("/api/history", {})["items"]
        self.assertTrue(any(item.get("publication_basis") == "human_review" and item["title"] == "人工审核发布" for item in history))
        with store._db(self.root) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM evaluations").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM used_evaluations").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM learning_confirmations").fetchone()[0], 0)
        self.service.post("/api/withdraw", {"memory_id": candidate["memory_id"], "expected_revision": 1,
                          "reviewer": body["reviewer"], "note": "合成撤回", "acknowledged": True})
        self.assertEqual(self.service.get(path, {})["memory"]["state"], "withdrawn")
        self.assertEqual(store.list_memories(self.root)["memories"][0]["active_revision"], None)

    def test_candidate_can_be_discarded_without_physical_delete(self):
        candidate = self.service.post("/api/memories", {"payload": self.payload()})
        path = "/api/memories/" + candidate["memory_id"]
        detail = self.service.get(path, {})
        self.assertTrue(detail["can_discard"])
        body = {"memory_id": candidate["memory_id"], "revision": 1,
                "actor": "合成操作人", "reason": "误把非设备服务总结为设备规则。", "acknowledged": True}
        for value in (False, 1, "true", None):
            with self.assertRaises(store.MemoryError):
                self.service.post("/api/experiences/discard", {**body, "acknowledged": value})
        discarded = self.service.post("/api/experiences/discard", body)
        self.assertTrue(discarded["discarded"])
        self.assertFalse(discarded["replayed"])
        self.assertEqual(store.load_candidate(self.root, candidate["memory_id"], 1)["state"], "rejected")
        self.assertFalse(self.service.get(path, {})["can_discard"])
        self.assertEqual(self.service.get("/api/memories", {"state": "candidate"})["total"], 0)
        with store._db(self.root) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM revisions WHERE memory_id=?", (candidate["memory_id"],)).fetchone()[0], 1)
            decision = connection.execute("SELECT record_json FROM evoz_decisions").fetchone()[0]
        self.assertEqual(json.loads(decision)["reason"], body["reason"])
        replayed = self.service.post("/api/experiences/discard", body)
        self.assertTrue(replayed["replayed"])

    def test_published_candidate_cannot_be_discarded(self):
        candidate = self.service.post("/api/memories", {"payload": self.payload()})
        self.publish(candidate, self.evaluate_fixture(candidate))
        with self.assertRaisesRegex(store.MemoryError, "已发布经验"):
            self.service.post("/api/experiences/discard", {"memory_id": candidate["memory_id"], "revision": 1,
                              "actor": "合成操作人", "reason": "错误", "acknowledged": True})

    def test_manual_publish_rechecks_reviewed_content_and_source_integrity(self):
        candidate = self.service.post("/api/memories", {"payload": self.payload()})
        body = {"memory_id": candidate["memory_id"], "revision": 1, "reviewer": "合成审核人",
                "note": "核对规则", "acknowledged": True}
        for value in (None, "wrong", "a" * 64):
            with self.assertRaises(store.MemoryError):
                self.service.post("/api/publish-reviewed", {**body, "content_sha256": value})
        self.input.write_text("已改变的原始证据", encoding="utf-8")
        detail = self.service.get("/api/memories/" + candidate["memory_id"], {})
        self.assertFalse(detail["can_manual_publish"])
        self.assertTrue(detail["manual_publish_blockers"])
        with self.assertRaises(store.MemoryError):
            self.service.post("/api/publish-reviewed", {**body, "content_sha256": candidate["content_sha256"]})

    def test_manual_publication_does_not_hide_failed_evaluation(self):
        candidate = self.service.post("/api/memories", {"payload": self.payload()})
        report = self.report(candidate)
        report["summary"]["regressions"] = 1
        report["status"] = "failed"
        failed = store.register_evaluation(self.root, candidate["memory_id"], 1, report)
        self.service.post("/api/publish-reviewed", {"memory_id": candidate["memory_id"], "revision": 1,
                          "content_sha256": candidate["content_sha256"], "reviewer": "合成审核人",
                          "note": "人工核对适用边界后批准，原评测记录仍保留", "acknowledged": True})
        detail = self.service.get("/api/memories/" + candidate["memory_id"], {})
        self.assertEqual(detail["publication"]["evaluation_status"], "not_adopted")
        self.assertIsNone(detail["publication"]["evaluation_id"])
        self.assertEqual(detail["evaluations"][0]["status"], "failed")
        self.assertEqual(self.service.get("/api/evaluations/" + failed["evaluation_id"], {})["report"], report)

    def test_empty_endpoints_are_read_only_and_policy_does_not_invent_history(self):
        overview = self.service.get("/api/overview", {})
        self.assertEqual(set(overview["counts"].values()), {0})
        for path in ("memories", "runs", "feedback", "history"):
            self.assertEqual(self.service.get("/api/" + path, {})["total"], 0)
        policy = self.service.get("/api/policy", {})
        self.assertFalse(policy["history_available"])
        self.assertIsNone(policy["previous_content"])
        self.assertFalse(store.resolve_store_dir(self.root).exists())

    def test_old_database_is_not_migrated_by_reads(self):
        directory = store.resolve_store_dir(self.root)
        directory.mkdir()
        file = directory / "memory.sqlite3"
        with closing(sqlite3.connect(file)) as connection:
            connection.execute("CREATE TABLE legacy (name TEXT)")
        self.assertEqual(self.service.get("/api/overview", {})["counts"]["candidate"], 0)
        self.assertEqual(self.service.get("/api/runs", {})["items"], [])
        with closing(sqlite3.connect(file)) as connection:
            self.assertEqual(connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [("legacy",)])

    def test_revision_edit_is_atomic_and_preserves_active_version(self):
        payload = self.payload()
        first = self.service.post("/api/memories", {"payload": payload})
        self.publish(first, self.evaluate_fixture(first))
        def edit(label):
            try:
                return self.service.post("/api/memories", {"memory_id": first["memory_id"], "expected_revision": 1,
                                          "payload": {**payload, "rule": "候选修订" + label}})
            except ValueError as exc:
                return str(exc)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(edit, ["甲", "乙"]))
        self.assertEqual(sum(isinstance(item, dict) for item in results), 1, results)
        detail = self.service.get("/api/memories/" + first["memory_id"], {})
        self.assertEqual(detail["active_revision"], 1)
        self.assertEqual(detail["memory"]["revision"], 2)
        self.assertEqual(detail["memory"]["state"], "candidate")
        self.assertEqual(detail["changes"][0]["field"], "rule")
        old = self.service.get("/api/memories/" + first["memory_id"], {"revision": "1"})
        self.assertEqual(old["memory"]["state"], "published")
        self.assertFalse(old["can_publish"])
        self.assertTrue(old["can_withdraw"])
        listed = self.service.get("/api/memories", {"state": "published", "q": "采购"})
        self.assertEqual(listed["total"], 1)
        self.assertEqual(listed["items"][0]["active_revision"], 1)

    def test_publish_preview_rechecks_release_artifacts_and_original_gates(self):
        candidate = self.service.post("/api/memories", {"payload": self.payload()})
        before = self.service.get("/api/memories/" + candidate["memory_id"], {})
        self.assertFalse(before["can_publish"])
        self.assertIn("NO_USABLE_EVALUATION", [item["code"] for item in before["blockers"]])
        artifact = self.root / "evaluation-artifact.json"
        self.write(artifact, {"real": "合成工件"})
        evaluation = self.evaluate_fixture(candidate, artifacts=[{"path": str(artifact), "sha256": store._file_hash(artifact)}])
        ready = self.service.get("/api/memories/" + candidate["memory_id"], {})
        self.assertTrue(ready["can_publish"], ready)
        for acknowledged in (False, 1, "true", None):
            with self.assertRaises(store.MemoryError):
                self.service.post("/api/publish", {"memory_id": candidate["memory_id"], "revision": 1, "evaluation_id": evaluation["evaluation_id"],
                                  "reviewer": "合成审阅角色", "note": "测试", "acknowledged": acknowledged})
        self.write(artifact, {"changed": True})
        blocked = self.service.get("/api/memories/" + candidate["memory_id"], {})
        self.assertFalse(blocked["can_publish"])
        self.assertIn("变化", blocked["evaluations"][0]["blocking_reason"])
        with self.assertRaises(store.MemoryError):
            self.publish(candidate, evaluation)

    def test_stale_release_and_withdraw_revision_are_not_bypassed(self):
        first = self.service.post("/api/memories", {"payload": self.payload()})
        stale = self.evaluate_fixture(first)
        second = self.service.post("/api/memories", {"payload": self.payload(title="另一条规则")})
        self.publish(second, self.evaluate_fixture(second))
        detail = self.service.get("/api/memories/" + first["memory_id"], {})
        self.assertFalse(detail["can_publish"])
        self.assertIn("release", detail["evaluations"][0]["blocking_reason"])
        with self.assertRaises(store.MemoryError):
            self.publish(first, stale)
        body = {"memory_id": second["memory_id"], "expected_revision": 2, "reviewer": "合成审阅角色", "note": "测试撤回", "acknowledged": True}
        with self.assertRaisesRegex(store.MemoryError, "活动修订已变化"):
            self.service.post("/api/withdraw", body)
        body["expected_revision"] = 1
        self.assertEqual(self.service.post("/api/withdraw", body)["state"], "withdrawn")

    def test_failed_and_used_evaluations_are_visible_but_not_publishable(self):
        candidate = self.service.post("/api/memories", {"payload": self.payload()})
        report = self.report(candidate)
        report["summary"]["regressions"] = 1
        report["status"] = "failed"
        failed = store.register_evaluation(self.root, candidate["memory_id"], 1, report)
        detail = self.service.get("/api/memories/" + candidate["memory_id"], {})
        self.assertFalse(detail["can_publish"])
        self.assertFalse(detail["evaluations"][0]["usable"])
        self.assertEqual(self.service.get("/api/evaluations/" + failed["evaluation_id"], {})["report"], report)
        good = self.evaluate_fixture(candidate)
        self.publish(candidate, good)
        self.service.post("/api/withdraw", {"memory_id": candidate["memory_id"], "expected_revision": 1,
                            "reviewer": "合成测试", "note": "合成撤回", "acknowledged": True})
        detail = self.service.get("/api/memories/" + candidate["memory_id"], {})
        self.assertFalse(detail["can_publish"])
        self.assertTrue(all(not item["usable"] for item in detail["evaluations"]))
        self.assertIn("evaluation", [item["kind"] for item in self.service.get("/api/history", {})["items"]])

    def test_confirmed_correction_invalidates_success_source_in_page(self):
        run = self.run_fixture()
        reflected = learning.record_reflection(self.root, run["run_id"], self.reflection())
        learning.confirm_run(self.root, run["run_id"], "合成角色", "family-a", "合成原确认")
        corrected = copy.deepcopy(self.result)
        corrected[0]["产品数量"] = "3台"
        feedback = learning.record_feedback(self.root, run["run_id"], "合成新反馈", corrected)
        learning.confirm_feedback(self.root, feedback["feedback_id"], "合成角色", "family-a", "合成纠错确认")
        detail = self.service.get("/api/memories/" + reflected["candidates"][0]["memory_id"], {})
        self.assertIn("SOURCE_INVALIDATED", [item["code"] for item in detail["blockers"]])
        source = self.service.get("/api/sources/" + run["run_id"], {})["source"]
        self.assertTrue(source["invalidated"])
        self.assertFalse(source["can_confirm"])

    def test_feedback_complete_preserves_old_record_and_confirms_only_with_explicit_ack(self):
        run = self.run_fixture()
        learning.record_reflection(self.root, run["run_id"], self.reflection())
        original = learning.record_feedback(self.root, run["run_id"], '用户原话"数量应为3台"', reflection=self.reflection())
        detail = self.service.get("/api/memories/" + original["candidates"][0]["memory_id"], {})
        self.assertIn("FEEDBACK_INCOMPLETE", [item["code"] for item in detail["blockers"]])
        source = self.service.get("/api/sources/" + original["source_id"], {})
        self.assertFalse(source["source"]["can_confirm"])
        self.assertTrue(source["snippets"][0]["truncated"])
        self.assertLessEqual(len(source["snippets"][0]["text"]), 800)
        corrected = copy.deepcopy(self.result)
        corrected[0]["产品数量"] = "3台"
        completed = self.service.post("/api/feedback/" + original["feedback_id"] + "/complete", {"after_result": corrected})
        self.assertNotEqual(completed["new_feedback_id"], original["feedback_id"])
        confirmed_source = self.service.get("/api/sources/" + completed["source_id"], {})
        self.assertTrue(confirmed_source["source"]["can_confirm"])
        self.assertEqual(confirmed_source["before_result"][0]["产品数量"], "2台")
        self.assertEqual(confirmed_source["after_result"][0]["产品数量"], "3台")
        self.service.post("/api/sources/" + completed["source_id"] + "/confirm",
                          {"reviewer": "合成确认角色", "family_id": "family-a", "note": "核对合成更正", "acknowledged": True})
        feedback = self.service.get("/api/feedback", {})["items"]
        self.assertEqual(len(feedback), 2)
        self.assertFalse(next(item for item in feedback if item["feedback_id"] == original["feedback_id"])["confirmed"])
        self.assertTrue(next(item for item in feedback if item["feedback_id"] == completed["feedback_id"])["confirmed"])
        history = self.service.get("/api/history", {})["items"]
        self.assertIn("confirmation", [item["kind"] for item in history])
        self.assertIn("feedback", [item["kind"] for item in history])
        self.assertEqual(self.service.get("/api/runs/" + run["run_id"], {})["result"], self.result)

    def test_bad_revision_does_not_break_list_and_pagination(self):
        candidate = self.service.post("/api/memories", {"payload": self.payload(correction_ids=[])})
        with store._db(self.root, write=True) as connection:
            connection.execute("INSERT INTO revisions VALUES (?,?,?,?,?)", ("mem_bad", 1, "{}", "invalid", store._now()))
        listing = self.service.get("/api/memories", {"limit": "1", "offset": "0"})
        self.assertEqual(listing["total"], 2)
        self.assertTrue(listing["has_more"])
        items = self.service.get("/api/memories", {})["items"]
        self.assertEqual(next(item for item in items if item["memory_id"] == "mem_bad")["state"], "error")
        detail = self.service.get("/api/memories/" + candidate["memory_id"], {})
        self.assertIn("NO_SOURCES", [item["code"] for item in detail["blockers"]])
        self.assertFalse(detail["can_publish"])

    def test_policy_change_is_a_hash_hint_and_source_drift_has_safe_fallback(self):
        policy = self.root / dashboard.POLICY
        policy.parent.mkdir(parents=True)
        policy.write_text("当前业务口径🌐", encoding="utf-8")
        run = self.run_fixture()
        learning.record_reflection(self.root, run["run_id"], self.reflection())
        overview = self.service.get("/api/overview", {})
        self.assertEqual(overview["policy_changed_runs"][0]["run_id"], run["run_id"])
        self.assertEqual(overview["counts"]["candidate"], 1)
        self.input.write_text("drifted", encoding="utf-8")
        source = self.service.get("/api/sources/" + run["run_id"], {})
        self.assertIn("error", source["source"])
        self.assertEqual(source["snippets"], [])
        self.assertFalse(source["source"]["can_confirm"])
        self.assertEqual(source["after_result"], self.result)

    def test_policy_diff_uses_actual_git_versions(self):
        def git(*args):
            completed = subprocess.run(["git", *args], cwd=self.root, capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
        git("init", "-q")
        policy = self.root / dashboard.POLICY
        policy.parent.mkdir(parents=True)
        policy.write_text("第一条\n保留条\n", encoding="utf-8")
        git("add", str(dashboard.POLICY))
        git("-c", "user.name=Synthetic Test", "-c", "user.email=synthetic@example.invalid", "commit", "-qm", "synthetic policy fixture")
        policy.write_text("第二条\n保留条\n", encoding="utf-8")
        result = self.service.get("/api/policy", {})
        self.assertTrue(result["history_available"])
        self.assertEqual(result["previous_content"], "第一条\n保留条\n")
        self.assertIn({"type": "removed", "text": "第一条"}, result["changes"])
        self.assertIn({"type": "added", "text": "第二条"}, result["changes"])

    def test_crlf_worktree_does_not_hide_actual_previous_policy(self):
        def git(*args):
            completed = subprocess.run(["git", *args], cwd=self.root, capture_output=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
        git("init", "-q")
        git("config", "core.autocrlf", "false")
        policy = self.root / dashboard.POLICY
        policy.parent.mkdir(parents=True)
        for text in ("旧口径\n", "新口径\n"):
            policy.write_bytes(text.encode("utf-8"))
            git("add", str(dashboard.POLICY))
            git("-c", "user.name=Synthetic Test", "-c", "user.email=synthetic@example.invalid", "commit", "-qm", "synthetic fixture")
        policy.write_bytes("新口径\r\n".encode("utf-8"))
        result = self.service.get("/api/policy", {})
        self.assertTrue(result["history_available"])
        self.assertEqual(result["previous_content"], "旧口径\n")
        self.assertIn({"type": "added", "text": "新口径"}, result["changes"])


if __name__ == "__main__":
    unittest.main()
