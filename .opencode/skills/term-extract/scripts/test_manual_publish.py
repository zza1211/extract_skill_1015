"""人工规则审核独立于整篇标签确认；所有来源与数据库均为临时合成数据。"""
from __future__ import annotations

import copy
import json
import sqlite3
import subprocess
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import document_io
import evolution_store as evolution
import memory_learning as learning
import memory_store as store
import test_memory_learning as learning_fixture


class ManualPublishTests(unittest.TestCase):
    setUp = learning_fixture.LearningTests.setUp
    write = learning_fixture.LearningTests.write
    register = learning_fixture.LearningTests.register
    reflection = learning_fixture.LearningTests.reflection
    candidate = learning_fixture.LearningTests.candidate
    report = learning_fixture.LearningTests.report

    def approve(self, candidate, note="人工核对了适用与排除条件，仅批准此规则"):
        return store.manual_publish(self.root, candidate["memory_id"], candidate["revision"], "测试人工审核者", note,
                                    expected_content_sha256=candidate["content_sha256"])

    def status(self, candidate):
        with store._db(self.root) as connection:
            return store.manual_publish_status(connection, self.root, candidate["memory_id"], candidate["revision"])

    def counts(self):
        with store._db(self.root) as connection:
            return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("manual_approvals", "evaluations", "learning_confirmations", "corrections")}

    def test_unconfirmed_unassessed_rule_publishes_without_forged_labels_or_evaluation(self):
        run, candidate = self.candidate()
        before = self.counts()
        status = self.status(candidate)
        self.assertTrue(status["can_publish"])
        self.assertIn("SOURCE_NOT_CONFIRMED", {item["code"] for item in status["warnings"]})
        released = self.approve(candidate)
        self.assertEqual(self.counts(), {**before, "manual_approvals": 1})
        self.assertFalse(learning.show_source(self.root, run["run_id"])["confirmed"])
        publication = released["publication"]
        self.assertEqual(publication["basis"], "human_review")
        self.assertEqual(publication["evaluation_status"], "not_evaluated")
        self.assertIsNone(publication["evaluation_id"])
        self.assertTrue(publication["current_active"])
        self.assertEqual(store.load_candidate(self.root, candidate["memory_id"], 1)["publication"], publication)
        self.assertEqual(store.list_memories(self.root)["memories"][0]["active_publication"], publication)
        with store._db(self.root) as connection:
            active = connection.execute("SELECT * FROM active").fetchone()
            self.assertIsNone(active["evaluation_id"])
            self.assertEqual(active["approval_id"], released["approval_id"])
            record = store._approval(connection, released["approval_id"])
            self.assertEqual(record["scope"], "rule_only")
        with self.assertRaisesRegex(store.MemoryError, "immutable"):
            with store._db(self.root, write=True) as connection:
                connection.execute("UPDATE manual_approvals SET record_json='{}'")

    def test_manual_publication_is_recalled_only_by_new_task_snapshots(self):
        _, candidate = self.candidate()
        old = store.create_snapshot(self.root, self.index_path)
        released = self.approve(candidate)
        self.assertNotEqual(released["release_sha256"], old["release_sha256"])
        self.assertEqual(store.create_snapshot(self.root, self.index_path), old)
        self.assertEqual(store.recall(self.root, self.index_path, "采购数量")["memories"], [])
        prepared = document_io.prepare(self.input, self.root / "next-task")
        next_index = Path(prepared["index_path"])
        store.create_snapshot(self.root, next_index)
        self.assertEqual(store.recall(self.root, next_index, "采购数量")["memories"][0]["memory_id"], candidate["memory_id"])
        store.withdraw(self.root, candidate["memory_id"], "审核者", "撤回验证", expected_revision=1)
        self.assertEqual(len(store.recall(self.root, next_index, "采购数量")["memories"]), 1)
        later = document_io.prepare(self.input, self.root / "later-task")
        store.create_snapshot(self.root, Path(later["index_path"]))
        self.assertEqual(store.recall(self.root, Path(later["index_path"]), "采购数量")["memories"], [])

    def test_feedback_without_complete_corrected_labels_can_be_rule_source(self):
        run = self.register()
        feedback = learning.record_feedback(self.root, run["run_id"], "规则有效，当前整篇结果仍有其他遗漏", reflection=self.reflection())
        candidate = feedback["candidates"][0]
        self.assertIn("FEEDBACK_WITHOUT_LABELS", {item["code"] for item in self.status(candidate)["warnings"]})
        self.approve(candidate)
        self.assertEqual(self.counts()["learning_confirmations"], 0)
        self.assertIsNone(learning.show_source(self.root, feedback["feedback_id"])["after_path"])

    def test_prior_confirmed_document_correction_is_warning_not_rule_rejection(self):
        run, candidate = self.candidate()
        corrected = copy.deepcopy(self.result)
        corrected[0]["产品数量"] = "3台"
        feedback = learning.record_feedback(self.root, run["run_id"], "设备数量应为3台，其余数量取清单原则仍成立", corrected)
        learning.confirm_feedback(self.root, feedback["feedback_id"], "合成人工", "family-A", "核对修正")
        self.assertTrue(learning.show_source(self.root, run["run_id"])["invalidated"])
        self.assertIn("SOURCE_HAS_CORRECTION", {item["code"] for item in self.status(candidate)["warnings"]})
        before = self.counts()
        self.approve(candidate)
        self.assertEqual(self.counts(), {**before, "manual_approvals": 1})

    def test_missing_or_changed_sources_remain_hard_gate(self):
        _, candidate = self.candidate()
        self.input.write_text("[]", encoding="utf-8")
        status = self.status(candidate)
        self.assertFalse(status["can_publish"])
        self.assertIn("INVALID_SOURCE", {item["code"] for item in status["blockers"]})
        with self.assertRaises(store.MemoryError):
            self.approve(candidate)
        self.assertEqual(self.counts()["manual_approvals"], 0)

    def test_source_less_rules_are_blocked(self):
        payload = {key: value for key, value in self.reflection()["insights"][0].items() if key not in {"observation", "evidence_tags"}}
        candidate = store.propose(self.root, {**payload, "correction_ids": []})
        self.assertIn("MISSING_SOURCE", {item["code"] for item in self.status(candidate)["blockers"]})
        with self.assertRaisesRegex(store.MemoryError, "真实来源"):
            self.approve(candidate)

    def test_hash_and_latest_revision_prevent_stale_page_publication(self):
        _, first = self.candidate()
        with self.assertRaisesRegex(store.MemoryError, "哈希"):
            store.manual_publish(self.root, first["memory_id"], 1, "人工", "审核", "f" * 64)
        payload = store.load_candidate(self.root, first["memory_id"], 1)["payload"]
        second = store.propose(self.root, {**payload, "rule": payload["rule"] + "保留单位。"}, first["memory_id"])
        self.assertIn("STALE_REVISION", {item["code"] for item in self.status(first)["blockers"]})
        with self.assertRaisesRegex(store.MemoryError, "更新修订"):
            self.approve(first)
        self.approve(second)

    def test_concurrent_duplicate_manual_approval_has_one_winner(self):
        _, candidate = self.candidate()
        def attempt():
            try:
                return self.approve(candidate)["state"]
            except store.MemoryError:
                return "blocked"
        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(lambda _: attempt(), range(2)))
        self.assertEqual(sorted(outcomes), ["blocked", "published"])
        self.assertEqual(self.counts()["manual_approvals"], 1)

    def test_conflict_rejected_and_merged_states_remain_blocking(self):
        for state in ("rejected", "merged", "needs_review"):
            with self.subTest(state=state):
                run = self.register()
                payload = {key: value for key, value in self.reflection()["insights"][0].items() if key not in {"observation", "evidence_tags"}}
                candidate = store.propose(self.root, {**payload, "correction_ids": [], "source_ids": [run["run_id"]]})
                target = store.propose(self.root, {**payload, "correction_ids": [], "source_ids": [run["run_id"]]}) if state == "merged" else None
                evolution.record_decision(self.root, candidate["memory_id"], 1, state, "测试不可直接发布", target_memory_id=target["memory_id"] if target else None)
                self.assertFalse(self.status(candidate)["can_publish"])
                with self.assertRaises(store.MemoryError):
                    self.approve(candidate)

    def test_open_experience_conflict_must_be_resolved_before_manual_publication(self):
        _, candidate = self.candidate()
        evolution.create_review(self.root, kind="experience_conflict", title="规则范围有冲突", question="是否仍然适用？",
                                context={}, choices=["仍适用", "不采纳"],
                                subject={"memory_id": candidate["memory_id"], "revision": 1,
                                         "policy_sha256": evolution.policy_digest(self.root)})
        self.assertFalse(self.status(candidate)["can_publish"])
        with self.assertRaisesRegex(store.MemoryError, "冲突"):
            self.approve(candidate)

    def test_trusted_development_case_can_supply_source_without_legacy_confirmation(self):
        labels = self.root / "labels.json"
        self.write(labels, self.result)
        case = evolution.import_case(self.root, self.input, labels, "family-trusted")
        payload = {key: value for key, value in self.reflection()["insights"][0].items() if key not in {"observation", "evidence_tags"}}
        candidate = store.propose(self.root, {**payload, "correction_ids": [], "source_ids": [case["case_id"]]})
        self.assertTrue(self.status(candidate)["can_publish"])
        self.approve(candidate)
        self.assertEqual(self.counts()["learning_confirmations"], 0)
        labels.write_text("[]", encoding="utf-8")
        store.withdraw(self.root, candidate["memory_id"], "人工", "重新检查")
        with self.assertRaisesRegex(store.MemoryError, "来源"):
            self.approve(candidate)

    def test_existing_failed_assessment_is_not_relabelled_as_passed(self):
        _, candidate = self.candidate(confirmed=True)
        report = self.report(candidate, regressions=1)
        report["status"] = "failed"
        evaluated = store.register_evaluation(self.root, candidate["memory_id"], 1, report)
        with self.assertRaisesRegex(store.MemoryError, "门槛"):
            store.publish(self.root, candidate["memory_id"], 1, evaluated["evaluation_id"], "人工", "评测发布仍受门槛约束")
        self.assertEqual(self.approve(candidate)["publication"]["evaluation_status"], "not_adopted")
        with store._db(self.root) as connection:
            self.assertEqual(store._evaluation(connection, evaluated["evaluation_id"])["report"]["status"], "failed")
            self.assertIsNone(connection.execute("SELECT 1 FROM used_evaluations").fetchone())

    def test_withdraw_reapproval_creates_new_immutable_receipt(self):
        _, candidate = self.candidate()
        first = self.approve(candidate)
        store.withdraw(self.root, candidate["memory_id"], "人工", "暂时撤回")
        self.assertFalse(store.load_candidate(self.root, candidate["memory_id"], 1)["publication"]["current_active"])
        second = self.approve(candidate)
        self.assertNotEqual(first["approval_id"], second["approval_id"])
        self.assertEqual(self.counts()["manual_approvals"], 2)

    def test_legacy_active_schema_migration_preserves_old_audit_and_evaluation(self):
        _, candidate = self.candidate(confirmed=True)
        evaluation = store.register_evaluation(self.root, candidate["memory_id"], 1, self.report(candidate))
        released = store.publish(self.root, candidate["memory_id"], 1, evaluation["evaluation_id"], "原审核者", "原评测发布")
        file = store.resolve_store_dir(self.root) / "memory.sqlite3"
        connection = sqlite3.connect(file)
        try:
            original_audit = connection.execute("SELECT * FROM audit").fetchall()
            original_eval = connection.execute("SELECT * FROM evaluations").fetchall()
            connection.execute("ALTER TABLE active RENAME TO active_new")
            connection.execute("CREATE TABLE active(memory_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,evaluation_id TEXT NOT NULL)")
            connection.execute("INSERT INTO active SELECT memory_id,revision,evaluation_id FROM active_new")
            connection.execute("DROP TABLE active_new")
            connection.commit()
        finally:
            connection.close()
        # 旧库读取无需迁移；首次写入只迁移活动指针，不篡改旧记录。
        self.assertEqual(store.list_memories(self.root)["memories"][0]["active_publication"]["evaluation_id"], evaluation["evaluation_id"])
        with store._db(self.root, write=True) as connection:
            self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM audit")], original_audit)
            self.assertEqual([tuple(row) for row in connection.execute("SELECT * FROM evaluations")], original_eval)
            active = connection.execute("SELECT * FROM active").fetchone()
            self.assertIsNone(active["approval_id"])
            self.assertEqual(active["evaluation_id"], evaluation["evaluation_id"])
        self.assertEqual(store.release_digest(self.root), released["release_sha256"])
        store.withdraw(self.root, candidate["memory_id"], "人工", "迁移后撤回")
        self.approve(candidate)

    def test_cli_manual_approval_requires_explicit_hash_and_retains_old_publish_contract(self):
        _, candidate = self.candidate()
        command = [sys.executable, "-B", str(Path(store.__file__)), "--project-root", str(self.root), "publish-reviewed",
                   "--memory-id", candidate["memory_id"], "--revision", "1", "--reviewer", "人工", "--note", "只批准规则"]
        refused = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(refused.returncode, 2)
        approved = subprocess.run(command + ["--content-sha256", candidate["content_sha256"]], capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(approved.returncode, 0, approved.stdout + approved.stderr)
        self.assertEqual(json.loads(approved.stdout)["publication"]["basis"], "human_review")


if __name__ == "__main__":
    unittest.main()
