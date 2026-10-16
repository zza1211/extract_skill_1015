"""网页确认到候选的真实本地链路测试；合成输入，不调用模型。"""
from __future__ import annotations

import copy
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import patch

import agent_learning
import agent_review as agent
import document_review as reviews
import evolution_store as evolution
import memory_learning as learning
import memory_store as store
import test_agent_learning as fixtures


class AgentDocumentReviewTests(unittest.TestCase):
    def setUp(self):
        # 复用已有“合成输入→读取回执→正式提交”夹具；断言仍针对真实注册、案例和候选存储。
        self.fixture = fixtures.AgentLearningTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.context = self.fixture.root, self.fixture.context
        wrong = copy.deepcopy(self.fixture.labels)
        wrong[0]["产品数量"] = "24台"
        self.run_id = self.fixture.commit(wrong)
        reviews.ensure_run(self.root, self.run_id)
        self.detail = reviews.get_review(self.root, self.run_id)
        self.doc_id = self.detail["document_id"]

    def action(self, endpoint, **changes):
        current = reviews.get_review(self.root, self.run_id)
        self.detail = reviews.handle_post(self.root, [self.doc_id, endpoint],
            {"run_id": self.run_id, "expected_revision": current["revision"], **changes})
        return self.detail

    def verify(self, corrected=True):
        if corrected:
            self.action("edit", action="update", clause_key=self.detail["clauses"][0]["clause_key"], patch={"产品数量": "2台"})
        self.action("verify", acknowledged=True, reviewer="合成校验人", family_id="family-A", note="已核对整篇")
        return self.detail["revision"]

    def read(self, revision, **changes):
        return agent.read_review(self.root, self.run_id, revision, self.context, **changes)

    def reflect(self, revision, reflection=None, context=None):
        return agent.reflect_review(self.root, self.run_id, revision,
            self.fixture.insight() if reflection is None else reflection, self.context if context is None else context)

    def test_pending_metadata_never_reveals_labels_or_user_operations(self):
        self.assertEqual(agent.pending_reviews(self.root, self.context)["reviews"], [])
        revision = self.verify()
        response = agent.pending_reviews(self.root, self.context)
        self.assertEqual(response["reviews"][0]["revision"], revision)
        text = json.dumps(response, ensure_ascii=False)
        for forbidden in ("24台", "2台", "before_result", "labels_path", "已核对整篇", "patch"):
            self.assertNotIn(forbidden, text)
        with agent_learning._db(self.root) as connection:
            self.assertEqual(agent_learning._records(connection, "agent_label_disclosures"), [])

    def test_read_requires_human_verified_current_revision_and_main_context(self):
        with self.assertRaises(store.MemoryError):
            self.read(self.detail["revision"])
        revision = self.verify()
        for bad in (None, {}, self.context | {"agent": "term-trial"}):
            with self.subTest(context=bad), self.assertRaises(store.MemoryError):
                agent.read_review(self.root, self.run_id, revision, bad)
        with self.assertRaises(store.MemoryError):
            self.read(revision - 1)
        response = self.read(revision)
        self.assertEqual([item["kind"] for item in response["items"][:2]], ["before", "after"])
        self.assertEqual(response["items"][0]["clause"]["产品数量"], "24台")
        self.assertEqual(response["items"][1]["clause"]["产品数量"], "2台")
        with self.assertRaisesRegex(store.MemoryError, "已查看"):
            agent_learning.assert_blind_context(self.root, self.context, source_type="study", source_key="another")

    def test_reflect_creates_trusted_snapshot_case_candidate_and_keeps_original(self):
        revision = self.verify()
        original_path = Path(self.detail["original_result_path"])
        original_hash = store._file_hash(original_path)
        self.read(revision)
        result = self.reflect(revision)
        self.assertEqual(result["status"], "learned")
        self.assertEqual(len(result["candidates"]), 1)
        case = evolution.get_case(self.root, result["case_id"], include_labels=True)
        self.assertEqual(case["trust"], "user_provided_label")
        self.assertEqual(case["split"], "development")
        self.assertEqual(case["labels_json"], self.fixture.labels)
        self.assertNotEqual(case["labels_path"], self.detail["current_result_path"])
        candidate = result["candidates"][0]
        saved = store.load_candidate(self.root, candidate["memory_id"], candidate["revision"])
        self.assertEqual(saved["payload"]["source_ids"], [case["case_id"]])
        self.assertEqual(saved["payload"]["correction_ids"], [])
        self.assertNotEqual(saved["state"], "published")
        self.assertEqual(store._file_hash(original_path), original_hash)
        self.assertEqual(store._read_json(original_path)[0]["产品数量"], "24台")
        self.assertEqual(agent.pending_reviews(self.root, self.context)["reviews"], [])
        self.assertEqual(reviews.get_review(self.root, self.run_id)["learning_status"], "learned")
        # 后续当前文件编辑不会改写可信案例引用的不可变标签。
        self.action("edit", action="update", clause_key=self.detail["clauses"][0]["clause_key"], patch={"产品数量": "3台"})
        self.assertEqual(evolution.get_case(self.root, case["case_id"], include_labels=True)["labels_json"], self.fixture.labels)

    def test_read_required_in_same_session_before_reflection(self):
        revision = self.verify()
        with self.assertRaisesRegex(store.MemoryError, "完整读取"):
            self.reflect(revision)
        self.read(revision)
        with self.assertRaisesRegex(store.MemoryError, "完整读取"):
            self.reflect(revision, context=self.context | {"session_id": "other"})

    def test_repeated_learning_is_idempotent_across_messages_and_sessions(self):
        revision = self.verify()
        self.read(revision)
        first = self.reflect(revision)
        again = self.reflect(revision, context=self.context | {"message_id": "new_message"})
        self.assertTrue(again["replayed"])
        self.assertEqual(first["candidates"], again["candidates"])
        other = self.context | {"session_id": "another_session", "message_id": "another_message"}
        agent.read_review(self.root, self.run_id, revision, other)
        third = self.reflect(revision, context=other)
        self.assertTrue(third["replayed"])
        self.assertEqual(first["candidates"], third["candidates"])
        changed = self.fixture.insight()
        changed["insights"][0]["observation"] = "另一份结论"
        with self.assertRaisesRegex(store.MemoryError, "不同复盘"):
            self.reflect(revision, changed)

    def test_no_new_insight_records_reason_instead_of_fabricating_candidates(self):
        revision = self.verify(corrected=False)
        self.read(revision)
        result = self.reflect(revision, {"insights": [], "no_experience_reason": "合成测试仅确认结果，无新增可迁移规律。"})
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["status"], "learned")
        self.assertIn("无新增", result["no_experience_reason"])

    def test_later_edit_and_external_change_invalidate_learning(self):
        revision = self.verify()
        self.read(revision)
        self.action("edit", action="update", clause_key=self.detail["clauses"][0]["clause_key"], patch={"产品数量": "4台"})
        with self.assertRaises(store.MemoryError):
            self.reflect(revision)
        new_revision = self.verify()
        with self.assertRaisesRegex(store.MemoryError, "完整读取"):
            self.reflect(new_revision)
        self.read(new_revision)
        path = Path(self.detail["current_result_path"])
        path.write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(store.MemoryError, "变化"):
            self.reflect(new_revision)

    def test_discard_is_not_negative_training_and_removes_pending(self):
        revision = self.verify()
        self.read(revision)
        self.action("discard", note="本篇不再使用，不代表抽取错误")
        self.assertEqual(agent.pending_reviews(self.root, self.context)["reviews"], [])
        with self.assertRaises(store.MemoryError):
            self.reflect(revision)
        self.assertEqual(evolution.list_cases(self.root)["items"], [])

    def test_source_and_label_snapshot_drift_reject_read(self):
        revision = self.verify()
        verified = reviews.verified_for_learning(self.root, self.run_id, revision)
        Path(verified["labels_path"]).write_text("[]", encoding="utf-8")
        with self.assertRaises(store.MemoryError):
            self.read(revision)

    def test_invalid_evidence_tag_cannot_create_candidate(self):
        revision = self.verify()
        self.read(revision)
        reflection = self.fixture.insight()
        reflection["insights"][0]["evidence_tags"] = ["<C999>"]
        with self.assertRaisesRegex(store.MemoryError, "本次原文"):
            self.reflect(revision, reflection)
        self.assertEqual(evolution.list_cases(self.root)["items"], [])

    def test_paginated_read_requires_all_parts_and_respects_byte_budget(self):
        for i in range(18):
            self.action("edit", action="update", clause_key=self.detail["clauses"][0]["clause_key"],
                patch={"条款标题": "合成修改记录" + str(i) + "A" * 100})
        revision = self.verify()
        page = self.read(revision, max_bytes=4096)
        self.assertLessEqual(len(store._canonical(page)), 4096)
        self.assertIsNotNone(page["next_cursor"])
        with self.assertRaisesRegex(store.MemoryError, "完整读取"):
            self.reflect(revision)
        seen = len(page["items"])
        while page["next_cursor"] is not None:
            page = self.read(revision, cursor=page["next_cursor"], max_bytes=4096)
            seen += len(page["items"])
            self.assertLessEqual(len(store._canonical(page)), 4096)
        self.assertEqual(seen, page["total"])
        result = self.reflect(revision, {"insights": [], "no_experience_reason": "重复改名不构成新经验。"})
        self.assertEqual(result["status"], "learned")

    def test_cli_reports_invalid_context_without_reading_labels(self):
        request = self.fixture.write("request.json", {"execution_context": {}})
        output = io.StringIO()
        with redirect_stdout(output):
            code = agent.main(["--project-root", str(self.root), "pending", "--request-file", str(request)])
        self.assertEqual(code, 2)
        self.assertIn("error", json.loads(output.getvalue()))

    def test_two_sessions_with_different_insights_only_create_one_learning_result(self):
        revision = self.verify()
        second = self.context | {"session_id": "concurrent_session", "message_id": "concurrent_message"}
        self.read(revision)
        agent.read_review(self.root, self.run_id, revision, second)
        other_reflection = self.fixture.insight()
        other_reflection["insights"][0].update(title="另一条并发规则", rule="另一条适用条件不同的合成规则。")
        barrier = threading.Barrier(2)
        real_propose = evolution.propose_candidate

        def delayed_propose(*args, **kwargs):
            time.sleep(0.1)  # 扩大“候选创建→记录学习”窗口，检验整段操作受到串行保护。
            return real_propose(*args, **kwargs)

        def work(context, reflection):
            barrier.wait(timeout=5)
            return self.reflect(revision, reflection, context)

        with patch.object(evolution, "propose_candidate", side_effect=delayed_propose), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(work, self.context, self.fixture.insight())
            second_call = pool.submit(work, second, other_reflection)
            results = [first.result(timeout=10), second_call.result(timeout=10)]
        self.assertEqual(sorted(result["replayed"] for result in results), [False, True])
        self.assertEqual(results[0]["candidates"], results[1]["candidates"])
        with store._db(self.root) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(DISTINCT memory_id) FROM revisions").fetchone()[0], 1)

    def test_reflection_lock_serializes_an_actual_second_process(self):
        revision = self.verify()
        self.read(revision)
        request = self.fixture.write("concurrent-request.json", {"run_id": self.run_id, "revision": revision,
            "reflection": self.fixture.insight(), "execution_context": self.context})
        command = [sys.executable, "-X", "utf8", str(Path(agent.__file__)), "--project-root", str(self.root),
                   "reflect", "--request-file", str(request)]
        with agent._reflection_lock(self.root, self.run_id, revision):
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            self.addCleanup(lambda: process.kill() if process.poll() is None else None)
            time.sleep(0.35)
            self.assertIsNone(process.poll(), "另一个进程必须等到当前修订锁释放后才能开始写候选")
            self.assertEqual(evolution.list_cases(self.root)["items"], [])
        stdout, stderr = process.communicate(timeout=15)
        self.assertEqual(process.returncode, 0, stderr + stdout)
        self.assertEqual(json.loads(stdout)["status"], "learned")


if __name__ == "__main__":
    unittest.main()
