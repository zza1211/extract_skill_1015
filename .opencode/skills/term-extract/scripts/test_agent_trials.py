"""临时合成协议测试；真实会话导出由测试替身提供，不宣称模型质量。"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_trials as trials
import document_io
import evolution_store as evo
import memory_store as store
from commit_result import coverage_report


class AgentTrialTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="agent-trials-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"TERM_EXTRACT_MEMORY_DIR": ""})
        environment.start()
        self.addCleanup(environment.stop)
        reference = self.root / ".opencode/skills/term-extract/references/business-rules.md"
        reference.parent.mkdir(parents=True)
        reference.write_text("仅抽目标设备，数量基于原文。", encoding="utf-8")
        self.controller = {"session_id": "ses_parent", "message_id": "msg_prepare", "agent": "term-extractor"}
        self.cases = [self.case("dev"), self.case("reg", "regression"), self.case("counter", "regression")]
        payload = {"title": "数量以设备为单位", "issue_type": "quantity", "triggers": ["设备数量"], "applies_when": "明确采购设备", "rule": "区分设备采购数量与接口数量。", "exclusions": "不按端口数推断设备数量。", "correction_ids": [], "source_ids": [self.cases[0]["case_id"]]}
        self.candidate = evo.propose_candidate(self.root, payload)
        self.roles = [{"case_id": case["case_id"], "role": role, "scope_note": "端口数不能视作设备数量。"} for case, role in zip(self.cases, ("development", "regression", "counterexample"))]
        self.plan = trials.prepare_experiment(self.root, self.candidate["memory_id"], 1, self.roles, self.controller)
        self.exports = {}

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def case(self, name, split="development"):
        source = self.write(name + ".json", [{"text": f"<C1>{name} Switch 2台，GE端口"}, {"text": "<C2>投标文件应整齐排版。"}])
        labels = self.write(name + "-gold.json", [{"id": 0, "条款标题": "Switch", "条款分类": "normal", "相关条款": [], "产品分类": "盒式交换机", "定位标识": ["<C1>", "<C1>"], "产品数量": "2台"}])
        return evo.import_case(self.root, source, labels, name, split)

    def context(self, trial, message="msg_commit"):
        return {"session_id": "ses_" + trial["trial_id"], "message_id": message, "agent": "term-trial"}

    def freeze(self, trial, *, wrong_quantity=False, extra_boundary=False):
        context = self.context(trial)
        ticket = trials.get_trial(self.root, trial["trial_id"], context, claim=True)
        trials.delivery(self.root, trial["trial_id"], context)
        index_path = Path(ticket["index_path"])
        index = store._read_json(index_path)
        read = document_io.read_document(index_path)
        receipt = {"index_sha256": store._file_hash(index_path), "coverage_kind": "full_original", "parts": read["parts"], "execution_context": {**context, "message_id": "msg_read"}}
        receipts = index_path.with_name("read_receipts.jsonl")
        receipts.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        case = evo.get_case(self.root, trial["case_id"], True)
        result = copy.deepcopy(case["labels_json"])
        if wrong_quantity:
            result[0]["产品数量"] = "1台"
        if extra_boundary:
            result[0]["定位标识"][1] = "<C2>"
        directory = index_path.parent / "final"
        result_path = self.write(str(directory / "result.json"), result)
        evidence_path = self.write(str(directory / "evidence.json"), {"clauses": [], "unresolved": []})
        run = {"status": "submitted", "production_submittable": True, "trial_only": True, "learning": {"status": "trial_skipped"}, "coverage": coverage_report(index_path, receipts), "execution_context": context, "result_sha256": store._file_hash(result_path), "evidence_sha256": store._file_hash(evidence_path), "source_sha256": index["source_sha256"]}
        run_path = self.write(str(directory / "run.json"), run)
        committed = {"committed": True, "result_path": str(result_path), "evidence_path": str(evidence_path), "run_path": str(run_path)}
        recorded = trials.record_trial(self.root, trial["trial_id"], committed, context)
        model = {"role": "assistant", "agent": "term-trial", "providerID": "synthetic", "modelID": "fixture", "variant": "", "sessionID": context["session_id"]}
        self.exports[context["session_id"]] = {"info": {"id": context["session_id"], "parentID": self.controller["session_id"]}, "messages": [
            {"info": {"role": "user", "id": "msg_user", "sessionID": context["session_id"]}, "parts": [{"type": "text", "text": "trial_id=" + trial["trial_id"]}]},
            {"info": {**model, "id": "msg_open"}, "parts": [{"type": "tool", "tool": "term_trial_open", "state": {"status": "completed", "input": {"trial_id": trial["trial_id"]}, "output": json.dumps(ticket)}}]},
            {"info": {**model, "id": "msg_read"}, "parts": [{"type": "tool", "tool": "term_trial_read", "state": {"status": "completed", "input": {"trial_id": trial["trial_id"]}, "output": json.dumps(read)}}]},
            {"info": {**model, "id": context["message_id"]}, "parts": [{"type": "tool", "tool": "term_trial_commit", "state": {"status": "completed", "input": {"trial_id": trial["trial_id"]}, "output": json.dumps({**committed, **recorded})}}]},
        ]}
        return ticket, committed, context

    def finish_all(self):
        for trial in self.plan["trials"]:
            self.freeze(trial, wrong_quantity=trial["phase"] == "baseline" and trial["case_id"] == self.cases[0]["case_id"])

    def exported(self, root, session_id):
        return copy.deepcopy(self.exports[session_id])

    def test_ticket_is_opaque_and_session_bound(self):
        trial = self.plan["trials"][0]
        context = self.context(trial)
        with self.assertRaisesRegex(store.MemoryError, "先领取"):
            trials.get_trial(self.root, trial["trial_id"], context)
        ticket = trials.get_trial(self.root, trial["trial_id"], context, True)
        self.assertNotIn("case_id", ticket)
        self.assertNotIn("labels", json.dumps(ticket))
        with self.assertRaisesRegex(store.MemoryError, "一张试验票"):
            trials.get_trial(self.root, self.plan["trials"][1]["trial_id"], context, True)
        with self.assertRaisesRegex(store.MemoryError, "其他子会话"):
            trials.get_trial(self.root, trial["trial_id"], {**context, "session_id": "ses_other"}, True)

    def test_parent_cannot_claim_or_leak_labels_before_all_freeze(self):
        with self.assertRaisesRegex(store.MemoryError, "term-trial"):
            trials.get_trial(self.root, self.plan["trials"][0]["trial_id"], self.controller, True)
        with self.assertRaisesRegex(store.MemoryError, "全部|所有"):
            trials.inspect_experiment(self.root, self.plan["experiment_id"], self.controller)
        with self.assertRaisesRegex(store.MemoryError, "主会话"):
            trials.inspect_experiment(self.root, self.plan["experiment_id"], {**self.controller, "session_id": "ses_other"})

    def test_prepare_rejects_holdout_and_non_independent_cases(self):
        holdout = self.case("held", "holdout")
        bad = [*self.roles[:2], {"case_id": holdout["case_id"], "role": "counterexample", "scope_note": "边界"}]
        with self.assertRaisesRegex(store.MemoryError, "保留集"):
            trials.prepare_experiment(self.root, self.candidate["memory_id"], 1, bad, self.controller)
        with self.assertRaisesRegex(store.MemoryError, "重复"):
            trials.prepare_experiment(self.root, self.candidate["memory_id"], 1, [self.roles[0], self.roles[1], {**self.roles[1], "role": "counterexample"}], self.controller)

    def test_record_requires_trial_only_and_true_read_session(self):
        trial = self.plan["trials"][0]
        ticket, committed, context = self.freeze(trial)
        path = Path(committed["run_path"])
        run = store._read_json(path)
        run["trial_only"] = False
        self.write(str(path), run)
        with self.assertRaisesRegex(store.MemoryError, "trial_only"):
            trials.record_trial(self.root, trial["trial_id"], committed, context)

    def test_finalize_recomputes_and_registers_from_verified_tool_calls(self):
        self.finish_all()
        with patch.object(trials, "export_session", side_effect=self.exported):
            evaluation = trials.finalize(self.root, self.plan["experiment_id"], self.controller)
        self.assertEqual(evaluation["status"], "passed")
        self.assertEqual(evaluation["summary"]["fix_improvements"], 1)
        self.assertEqual(evaluation["summary"]["independent_regression_cases"], 1)
        self.assertEqual(evaluation["summary"]["counterexample_cases"], 1)
        with store._db(self.root) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM learning_runs").fetchone()[0], 0)
        promoted = evo.auto_promote(self.root, evaluation["evaluation_id"])
        self.assertEqual(promoted["state"], "published")

    def test_unverified_export_never_qualifies(self):
        self.finish_all()
        with patch.object(trials, "export_session", side_effect=store.MemoryError("未核验模型")):
            with self.assertRaisesRegex(store.MemoryError, "未核验"):
                trials.finalize(self.root, self.plan["experiment_id"], self.controller)
        with evo._db(self.root) as connection:
            self.assertEqual(len(evo._rows(connection, "evoz_experiments")), 0)

    def test_answer_in_task_prompt_or_other_tool_blocks_qualification(self):
        self.finish_all()
        first = next(iter(self.exports.values()))
        first["messages"][0]["parts"][0]["text"] += "正确数量是2台"
        with patch.object(trials, "export_session", side_effect=self.exported):
            with self.assertRaisesRegex(store.MemoryError, "夹带"):
                trials.finalize(self.root, self.plan["experiment_id"], self.controller)
        first["messages"][0]["parts"][0]["text"] = "trial_id=" + self.plan["trials"][0]["trial_id"]
        first["messages"][1]["parts"][0]["tool"] = "read"
        with patch.object(trials, "export_session", side_effect=self.exported):
            with self.assertRaisesRegex(store.MemoryError, "范围外"):
                trials.finalize(self.root, self.plan["experiment_id"], self.controller)

    def test_model_change_and_pending_commit_block_qualification(self):
        self.finish_all()
        first = next(iter(self.exports.values()))
        first["messages"][-1]["parts"][0]["state"]["status"] = "running"
        with patch.object(trials, "export_session", side_effect=self.exported):
            with self.assertRaisesRegex(store.MemoryError, "已完成"):
                trials.finalize(self.root, self.plan["experiment_id"], self.controller)
        first["messages"][-1]["parts"][0]["state"]["status"] = "completed"
        first["messages"][-1]["info"]["modelID"] = "different"
        with patch.object(trials, "export_session", side_effect=self.exported):
            with self.assertRaisesRegex(store.MemoryError, "单一"):
                trials.finalize(self.root, self.plan["experiment_id"], self.controller)

    def test_inspect_is_bounded_and_ignores_titles_classifications(self):
        self.finish_all()
        report = trials.inspect_experiment(self.root, self.plan["experiment_id"], self.controller)
        self.assertLessEqual(len(store._canonical(report)), 32768)
        self.assertNotIn("objects", report["comparison"])
        self.assertEqual(len(report["trials"]), 6)
        self.assertIsNotNone(report["next_trial_id"])

    def test_policy_and_frozen_file_changes_fail(self):
        self.finish_all()
        path = self.root / ".opencode/skills/term-extract/references/business-rules.md"
        path.write_text("新口径", encoding="utf-8")
        with self.assertRaisesRegex(store.MemoryError, "口径"):
            trials.inspect_experiment(self.root, self.plan["experiment_id"], self.controller)

    def test_local_agent_judgment_requires_completed_parent_transcript(self):
        chosen = self.plan["trials"][1]
        for trial in self.plan["trials"]:
            self.freeze(trial, extra_boundary=trial == chosen)
        inspected = trials.inspect_experiment(self.root, self.plan["experiment_id"], self.controller, chosen["trial_id"])
        self.assertEqual(inspected["comparison"]["status"], "needs_review")
        difference = next(d for d in inspected["comparison"]["differences"] if d["severity"] == "review")
        judgments = [{"trial_id": chosen["trial_id"], "judgments": [{"difference_id": difference["difference_id"], "decision": "harmless", "reason": "多出的排版要求没有设备选型参数。"}]}]
        context = {**self.controller, "message_id": "msg_judge"}
        saved = trials.judge_experiment(self.root, self.plan["experiment_id"], judgments, context)
        self.exports[context["session_id"]] = {"info": {"id": context["session_id"]}, "messages": [{"info": {"id": context["message_id"], "role": "assistant", "agent": "term-extractor", "sessionID": context["session_id"], "providerID": "synthetic", "modelID": "fixture", "variant": ""}, "parts": [{"type": "tool", "tool": "term_learning_trial_judge", "state": {"status": "running", "input": {"experiment_id": self.plan["experiment_id"], "judgments_json": json.dumps(judgments)}, "output": json.dumps(saved)}}]}]}
        with patch.object(trials, "export_session", side_effect=self.exported):
            with self.assertRaisesRegex(store.MemoryError, "已完成"):
                trials.finalize(self.root, self.plan["experiment_id"], self.controller)
        self.exports[context["session_id"]]["messages"][0]["parts"][0]["state"]["status"] = "completed"
        with patch.object(trials, "export_session", side_effect=self.exported):
            evaluated = trials.finalize(self.root, self.plan["experiment_id"], self.controller)
        self.assertEqual(evaluated["status"], "passed")
        published = trials.publish_experiment(self.root, self.plan["experiment_id"], self.controller)
        self.assertEqual(published["state"], "published")

    def test_uncertain_local_judgment_creates_exception_without_human_confirmation(self):
        chosen = self.plan["trials"][1]
        for trial in self.plan["trials"]:
            self.freeze(trial, extra_boundary=trial == chosen)
        inspected = trials.inspect_experiment(self.root, self.plan["experiment_id"], self.controller, chosen["trial_id"])
        difference = next(d for d in inspected["comparison"]["differences"] if d["severity"] == "review")
        judged = trials.judge_experiment(self.root, self.plan["experiment_id"], [{"trial_id": chosen["trial_id"], "judgments": [{"difference_id": difference["difference_id"], "decision": "uncertain", "reason": "需要确认该描述是否影响选型。"}]}], self.controller)
        self.assertEqual(len(judged["review_ids"]), 1)
        review = evo.get_review(self.root, judged["review_ids"][0])
        self.assertEqual(review["scope"], "case")
        with store._db(self.root) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM learning_confirmations").fetchone()[0], 0)

    def test_publish_retry_returns_original_audit_without_new_validation(self):
        self.finish_all()
        with patch.object(trials, "export_session", side_effect=self.exported):
            trials.finalize(self.root, self.plan["experiment_id"], self.controller)
        first = trials.publish_experiment(self.root, self.plan["experiment_id"], self.controller)
        self.assertFalse(first["replayed"])
        with store._db(self.root) as connection:
            before = connection.execute("SELECT COUNT(*) FROM audit").fetchone()[0]
        with patch.object(trials, "export_session", side_effect=AssertionError("重复发布不应重新导出会话或重做试验")):
            repeated = trials.publish_experiment(self.root, self.plan["experiment_id"], self.controller)
        self.assertTrue(repeated["replayed"])
        self.assertTrue(repeated["historical_receipt"])
        self.assertFalse(repeated["revalidated"])
        self.assertIn("历史回执", repeated["notice"])
        for key in ("memory_id", "revision", "event_id", "evaluation_id", "report_sha256", "release_sha256"):
            self.assertEqual(first[key], repeated[key])
        with store._db(self.root) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM audit").fetchone()[0], before)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM used_evaluations").fetchone()[0], 1)

    def test_historical_publish_retry_still_requires_owner_and_cannot_undo_withdrawal(self):
        self.finish_all()
        with patch.object(trials, "export_session", side_effect=self.exported):
            trials.finalize(self.root, self.plan["experiment_id"], self.controller)
        first = trials.publish_experiment(self.root, self.plan["experiment_id"], self.controller)
        with self.assertRaisesRegex(store.MemoryError, "主会话"):
            trials.publish_experiment(self.root, self.plan["experiment_id"], {**self.controller, "session_id": "other-session"})
        store.withdraw(self.root, first["memory_id"], "synthetic-reviewer", "合成测试撤回，不代表真实人工判断。")
        with self.assertRaisesRegex(store.MemoryError, "撤回或被其他发布替换"):
            trials.publish_experiment(self.root, self.plan["experiment_id"], self.controller)
        with store._db(self.root) as connection:
            self.assertIsNone(connection.execute("SELECT 1 FROM active WHERE memory_id=?", (first["memory_id"],)).fetchone())

    def test_publish_retry_is_explicit_history_when_policy_later_changes(self):
        self.finish_all()
        with patch.object(trials, "export_session", side_effect=self.exported):
            trials.finalize(self.root, self.plan["experiment_id"], self.controller)
        first = trials.publish_experiment(self.root, self.plan["experiment_id"], self.controller)
        policy = self.root / ".opencode/skills/term-extract/references/business-rules.md"
        policy.write_text("后续业务口径变更，重试只展示既有历史。", encoding="utf-8")
        repeated = trials.publish_experiment(self.root, self.plan["experiment_id"], self.controller)
        self.assertEqual(repeated["event_id"], first["event_id"])
        self.assertFalse(repeated["revalidated"])
        self.assertTrue(repeated["historical_receipt"])


if __name__ == "__main__":
    unittest.main()
