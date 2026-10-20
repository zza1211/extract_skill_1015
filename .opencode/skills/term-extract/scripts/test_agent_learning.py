"""自主学习原子协议的合成测试，不调用模型，不使用实际标书或经验库。"""
from __future__ import annotations

import copy
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import agent_learning as agent
import agent_audit as audit
import commit_result
import document_io
import evolution_store as evolution
import memory_learning as learning
import memory_store as store


class AgentLearningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agent-learning-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"TERM_EXTRACT_MEMORY_DIR": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.context = {"session_id": "synthetic_session_a", "message_id": "synthetic_message_1", "agent": "term-extractor"}
        policy = self.root / ".opencode/skills/term-extract/references/business-rules.md"
        policy.parent.mkdir(parents=True)
        policy.write_text("只抽设备。机柜、布线与纯品牌准入不抽。", encoding="utf-8")
        self.input = self.write("input.json", [{"text": "<C1> Core Switch GE端口"}, {"text": "<C2> 采购数量2台"}, {"text": "<C3> 交货说明"}])
        prepared = document_io.prepare(self.input, self.root / "prepared")
        self.index_path = Path(prepared["index_path"])
        self.index = store._read_json(self.index_path)
        store.create_snapshot(self.root, self.index_path)
        self.labels = [{"id": 0, "条款标题": "Core Switch", "条款分类": "normal", "相关条款": [],
                        "产品分类": "盒式交换机", "定位标识": ["<C1>", "<C2>"], "产品数量": "2台"}]
        self.labels_path = self.write("labels_DO_NOT_LEAK.json", self.labels)

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def begin(self, **changes):
        return agent.begin_study(self.root, **({"input_path": self.input, "labels_path": self.labels_path, "family_id": "family-A",
            "index_path": self.index_path, "execution_context": self.context} | changes))

    def commit(self, result=None, context=None, receipt_context=None, expected_code=0):
        result = result if result is not None else self.labels
        context = context if context is not None else self.context
        receipts = {"index_sha256": store._file_hash(self.index_path), "coverage_kind": "full_original",
                    "parts": [{"unit_id": unit["unit_id"], "part_start": 0, "part_end": unit["source_length"],
                               "total_length": unit["source_length"]} for unit in self.index["units"]]}
        if receipt_context is not False:
            receipts["execution_context"] = receipt_context if receipt_context is not None else context
        receipt_path = self.index_path.with_name("read_receipts.jsonl")
        receipt_path.write_text(json.dumps(receipts) + "\n", encoding="utf-8")
        result_path = self.write("draft/result.json", result)
        evidence_path = self.write("draft/evidence.json", {"clauses": [{"id": 0, "quantity_source_tags": ["<C2>"], "related_source_tags": [], "notes": "合成协议测试数量来源"}], "unresolved": []})
        if context and context.get("agent") == "term-extractor":
            # 本类测试标签隔离与学习协议；以合成空发现报告准备新的提交前置工件。
            # 不改变待测预测，也不把核验意见当作标签正确性依据。
            self.close_synthetic_audit(context, result_path, evidence_path)
        argv = ["commit_result.py", "--project-root", str(self.root), "--index", str(self.index_path), "--result", str(result_path),
                "--evidence", str(evidence_path), "--receipts", str(receipt_path), "--out-dir", str(self.root / "final"), "--model-id", "synthetic-protocol-fixture"]
        if context is not False:
            argv += ["--execution-context-json", json.dumps(context)]
        output = io.StringIO()
        with patch("sys.argv", argv), redirect_stdout(output):
            code = commit_result.main()
        self.assertEqual(code, expected_code, output.getvalue())
        response = json.loads(output.getvalue())
        if expected_code:
            return response
        return response["learning"]["run_id"]

    def close_synthetic_audit(self, context, result_path, evidence_path):
        def call(action, caller, **body):
            return audit.dispatch(self.root, {"action": action, "execution_context": caller, **body})
        prepared = call("prepare", context, index_path=str(self.index_path), result_path=str(result_path), evidence_path=str(evidence_path))
        audit_id = prepared["audit_id"]
        child = {"session_id": "synthetic_auditor_" + audit_id, "message_id": "synthetic_audit_message", "agent": "term-auditor"}
        call("open", child, audit_id=audit_id)
        cursor = "0"
        while True:
            page = call("read", child, audit_id=audit_id, cursor=cursor)["model_view"]
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
        # 固定合成映射只用于学习协议运输，不模拟独立设备识别或召回效果。
        clauses = [item for item in store._read_json(result_path) if item["条款分类"] != "general"]
        saved = call("inventory", child, audit_id=audit_id, objects=[
            {"title": item["条款标题"], "object_kind": "equipment", "evidence_tags": list(dict.fromkeys(item["定位标识"]))}
            for item in clauses])
        cursor = "0"
        while True:
            page = call("draft", child, audit_id=audit_id, cursor=cursor)
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
        coverage = [{"object_id": oid, "status": "covered", "clause_ids": [item["id"]], "finding_ids": [],
                     "reason": "合成夹具固定映射，不是模型质量证据。"} for oid, item in zip(saved["object_ids"], clauses)]
        call("finish", child, audit_id=audit_id, findings=[], coverage=coverage, summary="合成学习协议 fixture，不模拟模型语义准确率。")
        call("inspect", context, audit_id=audit_id)
        call("resolve", context, audit_id=audit_id, result_path=str(result_path), evidence_path=str(evidence_path), decisions=[])

    def compared(self, result=None):
        study = self.begin()
        run_id = self.commit(result)
        return study, agent.compare_study(self.root, study["study_id"], run_id, self.context)

    def insight(self):
        return {"insights": [{"title": "采购量核对", "issue_type": "quantity", "triggers": ["数量", "switch"],
            "applies_when": "正文与清单定位到同一设备", "rule": "保留采购数量及原文证据，不能把端口量当成采购量。",
            "exclusions": "不同对象不能合并。", "observation": "本例数量明确位于清单段落。", "evidence_tags": ["<C2>"]}], "no_experience_reason": ""}

    def test_empty_read_does_not_create_database(self):
        self.assertEqual(agent.list_studies(self.root), {"items": [], "total": 0})
        self.assertEqual(agent.list_cases(self.root)["items"], [])
        self.assertEqual(agent.list_reviews(self.root, self.context)["items"], [])
        self.assertFalse(store.resolve_store_dir(self.root).exists())

    def test_begin_keeps_labels_private_and_is_idempotent(self):
        first, second = self.begin(), self.begin()
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["study_id"], second["study_id"])
        encoded = json.dumps({"begin": first, "list": agent.list_studies(self.root), "status": agent.get_study(self.root, first["study_id"]), "cases": agent.list_cases(self.root)}, ensure_ascii=False)
        self.assertNotIn("labels_DO_NOT_LEAK", encoded)
        self.assertNotIn("labels_archive_path", encoded)
        self.assertNotIn("采购数量2台", encoded)
        self.assertNotIn("labels_page", encoded)
        self.assertEqual(first["state"], "awaiting_blind_extraction")
        self.assertEqual(evolution.list_cases(self.root)["items"][0]["trust"], "user_provided_label")

    def test_begin_requires_real_context_and_matching_prepared_input(self):
        for bad in (None, {}, self.context | {"agent": "term-trial"}, self.context | {"session_id": " "}):
            with self.subTest(context=bad), self.assertRaises(store.MemoryError):
                self.begin(execution_context=bad)
        other = self.write("other.json", [{"text": "<C1> unrelated"}])
        with self.assertRaisesRegex(store.MemoryError, "索引不匹配"):
            self.begin(input_path=other)

    def test_labels_cannot_be_read_or_reflected_before_freeze(self):
        study = self.begin()
        with self.assertRaisesRegex(store.MemoryError, "尚未冻结"):
            agent.get_comparison(self.root, study["study_id"], self.context)
        with self.assertRaisesRegex(store.MemoryError, "尚未冻结"):
            agent.reflect_study(self.root, study["study_id"], self.insight(), self.context)
        with self.assertRaisesRegex(store.MemoryError, "尚未冻结"):
            agent.judge_study(self.root, study["study_id"], [], self.context)

    def test_compare_accepts_actual_committed_run_and_freezes_once(self):
        study, comparison = self.compared()
        self.assertEqual(comparison["labels_page"], self.labels)
        self.assertEqual(comparison["comparison"]["status"], "passed")
        self.assertTrue(comparison["prediction_frozen"])
        again = agent.compare_study(self.root, study["study_id"], comparison["run_id"], self.context)
        self.assertEqual(again, comparison)
        changed = copy.deepcopy(self.labels)
        changed[0]["产品数量"] = "3台"
        other = self.commit(changed)
        with self.assertRaisesRegex(store.MemoryError, "禁止看过标签后替换"):
            agent.compare_study(self.root, study["study_id"], other, self.context)

    def test_other_session_or_trial_agent_cannot_unseal_labels(self):
        study = self.begin()
        run_id = self.commit()
        for context in (self.context | {"session_id": "other"}, self.context | {"agent": "term-trial"}):
            with self.subTest(context=context), self.assertRaises(store.MemoryError):
                agent.compare_study(self.root, study["study_id"], run_id, context)
        agent.compare_study(self.root, study["study_id"], run_id, self.context)
        with self.assertRaises(store.MemoryError):
            agent.get_comparison(self.root, study["study_id"], self.context | {"session_id": "other"})

    def test_run_and_receipts_must_have_same_main_agent_session(self):
        study = self.begin()
        for kwargs in ({"context": self.context | {"session_id": "other"}}, {"receipt_context": self.context | {"session_id": "other"}},
                       {"receipt_context": self.context | {"agent": "term-trial"}}, {"receipt_context": False}, {"context": False, "receipt_context": self.context}):
            with self.subTest(kwargs=kwargs):
                if "receipt_context" in kwargs and kwargs.get("context") is not False:
                    rejected = self.commit(**kwargs, expected_code=2)
                    self.assertFalse(rejected["committed"])
                    self.assertFalse(rejected["coverage"]["complete"])
                    continue
                run_id = self.commit(**kwargs)
                with self.assertRaises(store.MemoryError):
                    agent.compare_study(self.root, study["study_id"], run_id, self.context)

    def test_historical_prediction_before_study_is_rejected(self):
        run_id = self.commit()
        study = self.begin()
        with self.assertRaisesRegex(store.MemoryError, "创建前的历史预测"):
            agent.compare_study(self.root, study["study_id"], run_id, self.context)

    def test_source_and_policy_drift_prevent_label_access(self):
        study = self.begin()
        run_id = self.commit()
        policy = self.root / ".opencode/skills/term-extract/references/business-rules.md"
        policy.write_text("变更后的业务规则", encoding="utf-8")
        with self.assertRaisesRegex(store.MemoryError, "业务口径已变化"):
            agent.compare_study(self.root, study["study_id"], run_id, self.context)

    def test_new_document_requires_new_session_after_labels_are_revealed(self):
        study, _ = self.compared()
        self.assertTrue(self.begin()["replayed"])
        another_input = self.write("another.json", [{"text": "<C1> Second Switch"}])
        another_index = Path(document_io.prepare(another_input, self.root / "second")["index_path"])
        store.create_snapshot(self.root, another_index)
        labels = self.write("another-labels.json", [{**self.labels[0], "定位标识": ["<C1>", "<C1>"], "产品数量": "未提及"}])
        with self.assertRaisesRegex(store.MemoryError, "新建 OpenCode 会话"):
            self.begin(input_path=another_input, labels_path=labels, index_path=another_index, family_id="family-B")
        accepted = self.begin(input_path=another_input, labels_path=labels, index_path=another_index, family_id="family-B", execution_context=self.context | {"session_id": "session-B"})
        self.assertNotEqual(study["study_id"], accepted["study_id"])

    def test_prequeued_second_study_cannot_unseal_after_first_labels(self):
        first = self.begin()
        another_input = self.write("prequeued-b.json", [{"text": "<C1> Second Switch"}, {"text": "<C2> 采购数量2台"}])
        another_labels = self.write("prequeued-b-labels.json", self.labels)
        another_index = Path(document_io.prepare(another_input, self.root / "prequeued-b")["index_path"])
        store.create_snapshot(self.root, another_index)
        second = self.begin(input_path=another_input, labels_path=another_labels, index_path=another_index, family_id="family-B")
        first_run = self.commit()
        agent.compare_study(self.root, first["study_id"], first_run, self.context)
        self.input, self.index_path, self.index, self.labels_path = another_input, another_index, store._read_json(another_index), another_labels
        second_run = self.commit()
        with self.assertRaisesRegex(store.MemoryError, "新建 OpenCode 会话"):
            agent.compare_study(self.root, second["study_id"], second_run, self.context)
        self.assertEqual(agent.get_study(self.root, second["study_id"])["state"], "awaiting_blind_extraction")

    def test_experiment_label_disclosure_blocks_new_and_prequeued_blind_studies(self):
        first = self.begin()
        disclosure = agent.record_label_disclosure(self.root, self.context, source_type="experiment", source_key="synthetic_experiment")
        self.assertFalse(disclosure["replayed"])
        self.assertTrue(agent.record_label_disclosure(self.root, self.context, source_type="experiment", source_key="synthetic_experiment")["replayed"])
        run = self.commit()
        with self.assertRaisesRegex(store.MemoryError, "验证案例的标签"):
            agent.compare_study(self.root, first["study_id"], run, self.context)
        with self.assertRaisesRegex(store.MemoryError, "验证案例的标签"):
            self.begin()
        agent.assert_blind_context(self.root, self.context | {"session_id": "fresh-session"})

    def test_frozen_study_can_continue_after_experiment_label_disclosure(self):
        study, comparison = self.compared()
        agent.record_label_disclosure(self.root, self.context, source_type="experiment", source_key="synthetic_experiment")
        repeated = agent.compare_study(self.root, study["study_id"], comparison["run_id"], self.context)
        self.assertEqual(repeated["comparison"]["status"], "passed")
        self.assertEqual(agent.reflect_study(self.root, study["study_id"], self.insight(), self.context)["state"], "reflected")

    def test_judgments_cannot_override_hard_quantity_error(self):
        changed = copy.deepcopy(self.labels)
        changed[0]["产品数量"] = "3台"
        study, comparison = self.compared(changed)
        error = comparison["comparison"]["differences"][0]
        self.assertEqual(error["severity"], "error")
        with self.assertRaisesRegex(ValueError, "不能覆盖硬错误"):
            agent.judge_study(self.root, study["study_id"], [{"difference_id": error["difference_id"], "decision": "harmless", "reason": "不能接受的覆盖"}], self.context)

    def test_uncertain_difference_creates_shared_review_and_human_answer_unblocks(self):
        changed = copy.deepcopy(self.labels)
        changed[0]["定位标识"] = ["<C1>", "<C3>"]
        study, comparison = self.compared(changed)
        difference = comparison["comparison"]["differences"][0]
        judgment = [{"difference_id": difference["difference_id"], "decision": "uncertain", "reason": "需要确认交货说明是否影响选型。"}]
        judged = agent.judge_study(self.root, study["study_id"], judgment, self.context)
        self.assertEqual(judged["state"], "needs_review")
        self.assertTrue(agent.judge_study(self.root, study["study_id"], judgment, self.context)["replayed"])
        with self.assertRaisesRegex(store.MemoryError, "仍有未确定"):
            agent.reflect_study(self.root, study["study_id"], self.insight(), self.context)
        review_id = judged["review_ids"][0]
        agent.reply_review(self.root, review_id, "harmless", "交货说明不影响选型，可以保留。", self.context)
        resolved = evolution.get_review(self.root, review_id)
        self.assertEqual(resolved["resolution"]["channel"], "conversation")
        self.assertIn(self.context["message_id"], resolved["resolution"]["source_ref"])
        current = agent.get_study(self.root, study["study_id"])
        self.assertEqual(current["state"], "compared")
        self.assertEqual(current["comparison_summary"]["status"], "passed")
        self.assertEqual(agent.reflect_study(self.root, study["study_id"], self.insight(), self.context)["state"], "reflected")

    def test_reflection_creates_case_sourced_candidates_without_publication(self):
        study, comparison = self.compared()
        first = agent.reflect_study(self.root, study["study_id"], self.insight(), self.context)
        second = agent.reflect_study(self.root, study["study_id"], self.insight(), self.context)
        self.assertTrue(second["replayed"])
        self.assertEqual(first["candidates"], second["candidates"])
        candidate = first["candidates"][0]
        payload = store.load_candidate(self.root, candidate["memory_id"], candidate["revision"])["payload"]
        self.assertEqual(payload["source_ids"], [study["case_id"]])
        self.assertEqual(store.create_snapshot(self.root, self.index_path)["memory_count"], 0)
        with store._db(self.root) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM learning_confirmations").fetchone()[0], 0)
        changed = self.insight()
        changed["insights"][0]["rule"] = "不同规则"
        with self.assertRaisesRegex(store.MemoryError, "已有不同复盘"):
            agent.reflect_study(self.root, study["study_id"], changed, self.context)

    def test_no_experience_reason_is_recorded_without_forcing_candidate(self):
        study, _ = self.compared()
        record = agent.reflect_study(self.root, study["study_id"], {"insights": [], "no_experience_reason": "本次没有新增可迁移经验。"}, self.context)
        self.assertEqual(record["candidates"], [])
        self.assertEqual(record["state"], "reflected")

    def test_case_preview_is_input_only_and_holdout_is_rejected(self):
        study = self.begin()
        case = agent.case_preview(self.root, study["case_id"], self.context, q="数量")
        encoded = json.dumps(case, ensure_ascii=False)
        self.assertIn("采购数量2台", encoded)
        self.assertNotIn("labels_", encoded)
        self.assertNotIn("产品数量", encoded)
        self.assertEqual(agent.list_cases(self.root, q="family-A")["total"], 1)
        holdout_input = self.write("holdout.json", [{"text": "<C1> Holdout Switch"}])
        holdout_labels = self.write("holdout-labels.json", [{**self.labels[0], "定位标识": ["<C1>", "<C1>"], "产品数量": "未提及"}])
        holdout = evolution.import_case(self.root, holdout_input, holdout_labels, "holdout-family", "holdout")
        with self.assertRaisesRegex(store.MemoryError, "holdout"):
            agent.case_preview(self.root, holdout["case_id"], self.context)

    def test_import_case_keeps_labels_private_and_requires_main_context(self):
        imported = agent.import_case(self.root, input_path=self.input, labels_path=self.labels_path, family_id="family-A",
                                     split="regression", execution_context=self.context)
        self.assertEqual(imported["split"], "regression")
        self.assertNotIn("labels", json.dumps(imported, ensure_ascii=False))
        with self.assertRaises(store.MemoryError):
            agent.import_case(self.root, input_path=self.input, labels_path=self.labels_path, family_id="family-A", split="regression",
                              execution_context=self.context | {"agent": "term-trial"})
        page = agent.list_cases(self.root, limit=1)
        self.assertEqual(page["total"], 1)
        self.assertIsNone(page["next_cursor"])

    def test_comparison_view_has_bounded_pages_and_validates_cursors(self):
        study, result = self.compared()
        self.assertLessEqual(len(store._canonical(result)), 65536)
        for options in ({"label_cursor": -1}, {"difference_cursor": 1000}, {"max_bytes": 4096}):
            with self.subTest(options=options), self.assertRaises(store.MemoryError):
                agent.get_comparison(self.root, study["study_id"], self.context, **options)

    def test_run_observation_cannot_use_development_family_as_independent_regression(self):
        import agent_trials
        study = self.begin()
        run_id = self.commit()
        insight = self.insight()["insights"][0]
        payload = {key: value for key, value in insight.items() if key not in {"observation", "evidence_tags"}}
        payload.update(source_ids=[run_id], correction_ids=[])
        candidate = evolution.propose_candidate(self.root, payload)
        regression_input = self.write("sister-regression.json", [{"text": "<C1> Sister Switch"}, {"text": "<C2> 采购数量2台"}])
        regression_labels = self.write("sister-regression-labels.json", self.labels)
        regression = evolution.import_case(self.root, regression_input, regression_labels, "family-A", "regression")
        counter_input = self.write("independent-counter.json", [{"text": "<C1> Other Switch"}, {"text": "<C2> 采购数量2台"}])
        counter_labels = self.write("independent-counter-labels.json", self.labels)
        counter = evolution.import_case(self.root, counter_input, counter_labels, "family-C", "regression")
        roles = [{"case_id": regression["case_id"], "role": "regression"},
                 {"case_id": study["case_id"], "role": "development"},
                 {"case_id": counter["case_id"], "role": "counterexample", "scope_note": "端口数不能作为设备采购量。"}]
        # 把 regression 放在 development 之前也必须拒绝；不能只靠单遍已见 family。
        with self.assertRaisesRegex(store.MemoryError, "独立原文和 family"):
            agent_trials.prepare_experiment(self.root, candidate["memory_id"], candidate["revision"], roles, self.context)
        record = {"schema_version": 1, "base_release_sha256": store.release_digest(self.root),
                  "policy_sha256": evolution.policy_digest(self.root), "generation": {"model_id": "synthetic", "variant": "", "runner_version": "synthetic"},
                  "development_source_ids": [run_id], "cases": roles, "artifacts": []}
        with self.assertRaisesRegex(store.MemoryError, "family 独立"):
            evolution.register_experiment(self.root, candidate["memory_id"], candidate["revision"], record)

    def make_review(self, suffix="1", large=False):
        return evolution.create_review(self.root, kind="semantic_difference", title="SECRET_CORRECT_99", question="标签的正确答案为99台？",
            context={"label_quantity": "SECRET_CORRECT_99", "source": "x" * 40000 if large else "局部原文"},
            choices=["harmless", "error", "uncertain"], subject={"case_id": "case_" + suffix, "difference_id": "diff_" + suffix, "business_fact": "SECRET_CORRECT_99"})

    def test_review_listing_defaults_to_answer_free_directory(self):
        self.make_review()
        listing = agent.list_reviews(self.root, self.context)
        text = json.dumps(listing, ensure_ascii=False)
        self.assertNotIn("SECRET_CORRECT", text)
        self.assertNotIn("question", text)
        self.assertNotIn("title", text)
        self.assertNotIn("business_fact", text)
        self.assertEqual(listing["items"][0]["subject"], {"case_id": "case_1", "difference_id": "diff_1"})
        agent.assert_blind_context(self.root, self.context)

    def test_explicit_review_context_and_conversation_reply_disclose_answers(self):
        study = self.begin()
        review = self.make_review()
        listing = agent.list_reviews(self.root, self.context, include_context=True)
        self.assertIn("SECRET_CORRECT_99", json.dumps(listing, ensure_ascii=False))
        with self.assertRaisesRegex(store.MemoryError, "验证案例的标签"):
            agent.assert_blind_context(self.root, self.context)
        run_id = self.commit()
        with self.assertRaisesRegex(store.MemoryError, "新建 OpenCode 会话"):
            agent.compare_study(self.root, study["study_id"], run_id, self.context)
        fresh = self.context | {"session_id": "reply-session"}
        replied = agent.reply_review(self.root, review["review_id"], "error", "这个局部的正确答案是99台。", fresh)
        self.assertTrue(replied["replied"])
        self.assertNotIn("SECRET_CORRECT", json.dumps(replied, ensure_ascii=False))
        self.assertNotIn("question", replied)
        with self.assertRaises(store.MemoryError):
            agent.assert_blind_context(self.root, fresh)

    def test_review_context_pagination_and_oversize_are_explicit(self):
        self.make_review("a")
        self.make_review("b", large=True)
        listing = agent.list_reviews(self.root, self.context, limit=1, include_context=True)
        self.assertEqual(listing["total"], 2)
        self.assertEqual(listing["next_cursor"], 1)
        self.assertTrue(listing["items"][0]["context_omitted"])
        self.assertIn("网页", listing["items"][0]["notice"])
        self.assertLessEqual(len(store._canonical(listing)), 32768)
        remaining = agent.list_reviews(self.root, self.context, cursor=1, limit=1)
        self.assertIsNone(remaining["next_cursor"])

    def test_study_and_events_are_append_only(self):
        study, _ = self.compared()
        with agent._db(self.root, write=True) as connection:
            for table in agent.TABLES:
                with self.subTest(table=table), self.assertRaisesRegex(sqlite3.IntegrityError, "immutable record"):
                    connection.execute(f"DELETE FROM {table}")

    def test_cli_uses_request_file_and_never_invokes_runner(self):
        request = self.write("request.json", {"input_path": str(self.input), "labels_path": str(self.labels_path), "family_id": "family-A", "index_path": str(self.index_path), "execution_context": self.context})
        output = io.StringIO()
        with redirect_stdout(output):
            code = agent.main(["--project-root", str(self.root), "begin", "--request-file", str(request)])
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())["state"], "awaiting_blind_extraction")
        text = Path(agent.__file__).read_text(encoding="utf-8")
        self.assertNotIn("learning_runner", text)
        self.assertNotIn("subprocess", text)


if __name__ == "__main__":
    unittest.main()
