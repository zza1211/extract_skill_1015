"""合成数据验证自动候选、人工确认和成功经验发布；无外部模型。"""
from __future__ import annotations

import copy
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import document_io
import memory_learning as learning
import memory_store as store


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="term-learning-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.env = patch.dict(os.environ, {"TERM_EXTRACT_MEMORY_DIR": ""})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.input = self.root / "input.json"
        self.write(self.input, [{"text": "<C9> Core Switch 原文秘密不复制🌐"}, {"text": "<C2> 采购数量2台"}])
        prepared = document_io.prepare(self.input, self.root / "task")
        self.index_path = Path(prepared["index_path"])
        self.index = store._read_json(self.index_path)
        self.result = [{"id": 0, "条款标题": "Core Switch", "条款分类": "normal", "相关条款": [],
                        "产品分类": "盒式交换机", "定位标识": ["<C9>", "<C2>"], "产品数量": "2台"}]
        self.evidence = {"clauses": [{"id": 0, "quantity_source_tags": ["<C2>"], "related_source_tags": [], "notes": "数量来自清单"}], "unresolved": []}
        self.result_path, self.evidence_path, self.run_path = [self.root / name for name in ("result.json", "evidence.json", "run.json")]
        self.write(self.result_path, self.result)
        self.write(self.evidence_path, self.evidence)
        self.run = {"status": "submitted", "production_submittable": True, "source_sha256": self.index["source_sha256"],
                    "coverage": {"complete": True, "index_sha256": store._file_hash(self.index_path)},
                    "result_sha256": store._file_hash(self.result_path), "evidence_sha256": store._file_hash(self.evidence_path)}
        self.write(self.run_path, self.run)

    def write(self, path, value):
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def register(self):
        return learning.register_run(self.root, self.index_path, self.result_path, self.evidence_path, self.run_path)

    def reflection(self):
        return {"insights": [{"title": "清单数量核验🌐", "issue_type": "quantity", "triggers": ["采购数量", "switch"],
                              "applies_when": "正文与清单能定位到同一独立采购对象时", "rule": "核对清单采购量及单位，保留原文证据。",
                              "exclusions": "同名对象归属不同采购项时不能关联。", "observation": "数量位于后续清单。",
                              "evidence_tags": ["<C2>"]}], "no_experience_reason": ""}

    def candidate(self, confirmed=False):
        run = self.register()
        event = learning.record_reflection(self.root, run["run_id"], self.reflection())
        if confirmed:
            learning.confirm_run(self.root, run["run_id"], "合成人工确认者", "family-A", "已逐项核对成功结果")
        return run, event["candidates"][0]

    def report(self, candidate, mode="success", **updates):
        payload = store.load_candidate(self.root, candidate["memory_id"], candidate["revision"])["payload"]
        sources = learning.get_sources(self.root, payload)
        summary = {"fix_improvements": 0, "success_cases": 1, "regressions": 0,
                   "independent_regression_cases": 1, "counterexample_cases": 1, "unfixed_target_cases": 0}
        summary.update(updates)
        return {"candidate": {key: candidate[key] for key in ("memory_id", "revision", "content_sha256")},
                "base_release_sha256": store.release_digest(self.root), "status": "passed", "evaluation_mode": mode,
                "summary": summary, "source_confirmations": [{"source_id": item["source_id"], "confirmation_sha256": item["confirmation_sha256"]} for item in sources],
                "artifacts": [artifact for source in sources for artifact in source["artifacts"]]}

    def publish(self, candidate, report):
        evaluation = store.register_evaluation(self.root, candidate["memory_id"], candidate["revision"], report)
        return store.publish(self.root, candidate["memory_id"], candidate["revision"], evaluation["evaluation_id"], "合成人工审阅者", "测试成功经验门槛")

    def test_empty_and_legacy_read_only_queue_do_not_migrate_or_initialize(self):
        self.assertEqual(learning.list_learning(self.root), {"runs": [], "feedback": []})
        directory = store.resolve_store_dir(self.root)
        self.assertFalse(directory.exists())
        directory.mkdir()
        with closing(sqlite3.connect(directory / "memory.sqlite3")) as connection:
            connection.execute("CREATE TABLE old_version (id TEXT)")
        self.assertEqual(learning.list_learning(self.root), {"runs": [], "feedback": []})
        with self.assertRaisesRegex(store.MemoryError, "不存在"):
            learning.get_run(self.root, "run_missing")

    def test_register_idempotent_and_snapshots_survive_overwritten_final(self):
        first = self.register()
        self.assertEqual(first["status"], "pending_reflection")
        self.run["learning"] = {"run_id": first["run_id"]}
        self.write(self.run_path, self.run)
        self.assertEqual(first["run_id"], self.register()["run_id"])
        self.assertNotIn("result_json", first)
        self.assertEqual(store._read_json(Path(first["result_path"])), self.result)
        self.result[0]["产品数量"] = "1台"
        self.write(self.result_path, self.result)
        self.run["result_sha256"] = store._file_hash(self.result_path)
        self.write(self.run_path, self.run)
        second = self.register()
        self.assertNotEqual(first["run_id"], second["run_id"])
        self.assertEqual(store._read_json(Path(first["result_path"]))[0]["产品数量"], "2台")
        self.assertNotIn("原文秘密", json.dumps(learning.get_run(self.root, first["run_id"]), ensure_ascii=False))

    def test_generation_identity_changes_with_model_but_not_learning_summary(self):
        self.run["model_id"] = "model-A"
        self.write(self.run_path, self.run)
        first = self.register()
        self.run["learning"] = {"run_id": first["run_id"], "status": "pending"}
        self.write(self.run_path, self.run)
        self.assertEqual(first["run_id"], self.register()["run_id"])
        self.run["model_id"] = "model-B"
        self.write(self.run_path, self.run)
        self.assertNotEqual(first["run_id"], self.register()["run_id"])

    def test_reflection_candidate_only_and_exact_retry_not_duplicate(self):
        run = self.register()
        event = learning.record_reflection(self.root, run["run_id"], self.reflection())
        self.assertEqual(event["status"], "unverified_candidates")
        self.assertEqual(learning.record_reflection(self.root, run["run_id"], self.reflection()), event)
        candidate = store.load_candidate(self.root, event["candidates"][0]["memory_id"], 1)
        self.assertEqual(candidate["payload"]["source_ids"], [run["run_id"]])
        self.assertEqual(candidate["payload"]["correction_ids"], [])
        source = learning.show_source(self.root, run["run_id"])
        self.assertEqual(source["reflection_event"]["reflection"]["insights"][0]["evidence_tags"], ["<C2>"])
        self.assertEqual(source["reflection_sha256"], store._hash(source["reflection_event"]))
        self.assertEqual(store.create_snapshot(self.root, self.index_path)["memory_count"], 0)
        self.assertEqual(store.recall(self.root, self.index_path, "采购数量")["memories"], [])
        changed = self.reflection()
        changed["insights"][0]["observation"] = "新的观察"
        with self.assertRaisesRegex(store.MemoryError, "已有不同复盘"):
            learning.record_reflection(self.root, run["run_id"], changed)
        self.assertEqual(len(store.list_memories(self.root)["memories"]), 1)

    def test_reflection_validation_rejects_unknown_tag_and_requires_no_experience_reason(self):
        bad = self.reflection()
        bad["insights"][0]["evidence_tags"] = ["<C99>"]
        for item in (bad, {"insights": [], "no_experience_reason": " "}, {**self.reflection(), "verified": True}):
            with self.subTest(item=item), self.assertRaises(store.MemoryError):
                learning.validate_reflection(item, self.index)
        run = self.register()
        event = learning.record_reflection(self.root, run["run_id"], {"insights": [], "no_experience_reason": "未发现可迁移的新原则。"})
        self.assertEqual(event["status"], "no_experience")
        self.assertEqual(event["candidates"], [])

    def test_unconfirmed_source_cannot_evaluate_or_publish(self):
        run, candidate = self.candidate()
        with self.assertRaisesRegex(store.MemoryError, "未经人工确认"):
            learning.get_sources(self.root, store.load_candidate(self.root, candidate["memory_id"], 1)["payload"])
        report = {"candidate": {key: candidate[key] for key in ("memory_id", "revision", "content_sha256")},
                  "base_release_sha256": store.release_digest(self.root), "status": "passed", "evaluation_mode": "success",
                  "summary": {"fix_improvements": 0, "success_cases": 1, "regressions": 0, "independent_regression_cases": 1, "counterexample_cases": 1, "unfixed_target_cases": 0}}
        with self.assertRaisesRegex(store.MemoryError, "未经人工确认"):
            store.register_evaluation(self.root, candidate["memory_id"], 1, report)
        self.assertFalse(learning.show_source(self.root, run["run_id"])["confirmed"])

    def test_success_publishes_without_fake_fix_and_freezes_old_snapshot(self):
        run, candidate = self.candidate(confirmed=True)
        frozen = store.create_snapshot(self.root, self.index_path)
        self.assertEqual(frozen["memory_count"], 0)
        self.assertEqual(self.publish(candidate, self.report(candidate))["state"], "published")
        self.assertEqual(store.create_snapshot(self.root, self.index_path)["memory_count"], 0)
        path = self.root / "next-index.json"
        path.write_bytes(self.index_path.read_bytes())
        self.assertEqual(store.create_snapshot(self.root, path)["memory_count"], 1)
        self.assertEqual(len(store.recall(self.root, path, "采购数量")["memories"]), 1)

    def test_success_gate_rejects_missing_success_or_regression_and_confirmation_binding(self):
        run, candidate = self.candidate(confirmed=True)
        for changes in ({"success_cases": 0}, {"regressions": 1}, {"counterexample_cases": 0}, {"independent_regression_cases": 0}, {"unfixed_target_cases": 1}):
            with self.subTest(changes=changes), self.assertRaises(store.MemoryError):
                self.publish(candidate, self.report(candidate, **changes))
        report = self.report(candidate)
        report["source_confirmations"][0]["confirmation_sha256"] = "f" * 64
        with self.assertRaisesRegex(store.MemoryError, "确认记录"):
            store.register_evaluation(self.root, candidate["memory_id"], 1, report)
        with self.assertRaisesRegex(store.MemoryError, "模式"):
            self.publish(candidate, self.report(candidate, mode="correction", fix_improvements=1))

    def test_feedback_records_verbatim_and_requires_distinct_corrected_result_to_confirm(self):
        run = self.register()
        text = "  数量应为3台🌐\n"
        feedback = learning.record_feedback(self.root, run["run_id"], text)
        self.assertEqual(learning.show_source(self.root, feedback["feedback_id"])["feedback_text"], text)
        with self.assertRaisesRegex(store.MemoryError, "正确结果"):
            learning.confirm_feedback(self.root, feedback["feedback_id"], "人工", "family-A", "仅原话不够")
        corrected = copy.deepcopy(self.result)
        corrected[0]["产品数量"] = "3台"
        feedback = learning.record_feedback(self.root, run["run_id"], text, corrected, self.reflection())
        same = learning.record_feedback(self.root, run["run_id"], text, corrected, self.reflection())
        self.assertEqual(feedback, same)
        learning.confirm_feedback(self.root, feedback["feedback_id"], "人工", "family-A", "已核对更正数量")
        source = learning.show_source(self.root, feedback["feedback_id"])
        self.assertTrue(source["confirmed"])
        self.assertEqual(source["kind"], "correction")
        self.assertEqual(store._read_json(Path(source["before_path"]))[0]["产品数量"], "2台")
        self.assertEqual(store._read_json(Path(source["after_path"]))[0]["产品数量"], "3台")
        candidate = feedback["candidates"][0]
        with self.assertRaisesRegex(store.MemoryError, "模式"):
            self.publish(candidate, self.report(candidate))
        with self.assertRaisesRegex(store.MemoryError, "门槛"):
            self.publish(candidate, self.report(candidate, mode="correction"))
        self.assertEqual(self.publish(candidate, self.report(candidate, mode="correction", fix_improvements=1))["state"], "published")

    def test_drift_demo_and_bad_source_tags_rejected(self):
        run, candidate = self.candidate(confirmed=True)
        report = self.report(candidate)
        evaluation = store.register_evaluation(self.root, candidate["memory_id"], 1, report)
        self.input.write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(store.MemoryError, "源标书"):
            store.publish(self.root, candidate["memory_id"], 1, evaluation["evaluation_id"], "人工", "不能沿用漂移文件")
        with self.assertRaises(store.MemoryError):
            learning.record_reflection(self.root, run["run_id"], self.reflection())

    def test_confirmed_correction_invalidates_old_success_evaluation(self):
        run, candidate = self.candidate(confirmed=True)
        evaluation = store.register_evaluation(self.root, candidate["memory_id"], 1, self.report(candidate))
        self.run["model_id"] = "different-model-same-output"
        self.write(self.run_path, self.run)
        duplicate = self.register()
        learning.confirm_run(self.root, duplicate["run_id"], "人工", "family-A", "另一模型的相同结果")
        after = copy.deepcopy(self.result)
        after[0]["产品数量"] = "3台"
        feedback = learning.record_feedback(self.root, run["run_id"], "原成功结果其实数量有误", after)
        learning.confirm_feedback(self.root, feedback["feedback_id"], "人工", "family-A", "已核实原数量错误")
        with self.assertRaisesRegex(store.MemoryError, "成功来源失效"):
            store.publish(self.root, candidate["memory_id"], 1, evaluation["evaluation_id"], "人工", "旧报告不能沿用")
        with self.assertRaisesRegex(store.MemoryError, "已有人工确认纠错"):
            learning.confirm_run(self.root, run["run_id"], "人工", "family-A", "不能恢复原错误结果")
        self.assertTrue(learning.show_source(self.root, duplicate["run_id"])["invalidated"])

    def test_demo_registration_and_source_snapshot_tamper_are_rejected(self):
        demo_input = self.root / "demo.json"
        self.write(demo_input, [{"text": "untagged"}])
        prepared = document_io.prepare(demo_input, self.root / "demo-task", demo_tags=True)
        with self.assertRaisesRegex(store.MemoryError, "demo"):
            learning.register_run(self.root, Path(prepared["index_path"]), self.result_path, self.evidence_path, self.run_path)
        run = self.register()
        Path(run["result_path"]).write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(store.MemoryError, "工件"):
            learning.confirm_run(self.root, run["run_id"], "人工", "family-A", "不能确认坏快照")

    def test_cli_errors_and_queue_limits_are_machine_json(self):
        base = [sys.executable, "-B", "-X", "utf8", str(Path(learning.__file__)), "--project-root", str(self.root)]
        output = subprocess.run(base + ["list", "--limit", "51"], capture_output=True, encoding="utf-8")
        self.assertEqual(output.returncode, 2)
        self.assertFalse(json.loads(output.stdout)["ok"])
        run = self.register()
        shown = subprocess.run(base + ["show-run", "--run-id", run["run_id"]], capture_output=True, encoding="utf-8")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertNotIn("result_json", json.loads(shown.stdout))

    def cli(self, *args):
        base = [sys.executable, "-B", "-X", "utf8", str(Path(learning.__file__)), "--project-root", str(self.root)]
        completed = subprocess.run([*base, *args], capture_output=True, encoding="utf-8")
        self.assertTrue(completed.stdout, completed.stderr)
        return completed.returncode, json.loads(completed.stdout)

    def test_cli_reflection_file_and_legacy_json_are_mutually_exclusive(self):
        run = self.register()
        file = self.root / "复盘 request.json"
        reflection = self.reflection()
        reflection["insights"][0]["observation"] = '核对"采购量"与路径\\字段🌐'
        self.write(file, reflection)
        code, event = self.cli("reflect", "--run-id", run["run_id"], "--reflection-file", str(file))
        self.assertEqual(code, 0, event)
        self.assertEqual(event["reflection"], reflection)
        code, retry = self.cli("reflect", "--run-id", run["run_id"], "--reflection-json", json.dumps(reflection, ensure_ascii=False))
        self.assertEqual(code, 0, retry)
        self.assertEqual(retry["candidates"], event["candidates"])
        for extra in ([], ["--reflection-file", str(file), "--reflection-json", "{}"]):
            code, error = self.cli("reflect", "--run-id", run["run_id"], *extra)
            self.assertEqual(code, 2)
            self.assertFalse(error["ok"])

    def test_cli_feedback_file_transports_large_quotes_without_windows_argv_limit(self):
        run = self.register()
        reflection = self.reflection()
        insight = reflection["insights"][0]
        insight["rule"] = '"' * 1200
        insight["observation"] = '"' * 950
        reflection["insights"] = [{**copy.deepcopy(insight), "title": f"经验{i}🌐"} for i in range(3)]
        text = '"' * 7990 + "\n🌐 "
        legacy_argv = ["python", "memory_learning.py", "feedback", "--text", text,
                       "--reflection-json", json.dumps(reflection, ensure_ascii=False)]
        self.assertGreater(len(subprocess.list2cmdline(legacy_argv)), 32767)
        request = {"feedback_text": text, "after_result": self.result, "reflection": reflection}
        file = self.root / "large 引号 feedback.json"
        self.write(file, request)
        code, response = self.cli("feedback", "--run-id", run["run_id"], "--request-file", str(file))
        self.assertEqual(code, 0, response)
        self.assertEqual(len(response["candidates"]), 3)
        source = learning.show_source(self.root, response["source_id"])
        self.assertEqual(source["feedback_text"], text)
        self.assertEqual(source["reflection_event"]["reflection"], reflection)
        code, repeated = self.cli("feedback", "--run-id", run["run_id"], "--request-file", str(file))
        self.assertEqual(code, 0, repeated)
        self.assertEqual(repeated, response)

    def test_cli_feedback_file_strict_types_mixing_and_legacy_short_payload(self):
        run = self.register()
        file = self.root / "feedback.json"
        request = {"feedback_text": '请核对"采购数量"🌐', "after_result": None,
                   "reflection": {"insights": [], "no_experience_reason": "只记录反馈待人工补充。"}}
        self.write(file, request)
        code, reply = self.cli("feedback", "--run-id", run["run_id"], "--request-file", str(file))
        self.assertEqual(code, 0, reply)
        self.assertEqual(reply["candidates"], [])
        for flag, value in (("--text", ""), ("--after-result", str(self.result_path)),
                            ("--after-result-json", "[]"), ("--reflection-json", "{}")):
            code, error = self.cli("feedback", "--run-id", run["run_id"], "--request-file", str(file), flag, value)
            self.assertEqual(code, 2, error)
            self.assertIn("混用", error["error"])
        invalid_requests = [{**request, "extra": True}, {key: value for key, value in request.items() if key != "reflection"},
                            {**request, "feedback_text": True}, {**request, "after_result": {}}, {**request, "reflection": None}]
        for invalid in invalid_requests:
            self.write(file, invalid)
            code, error = self.cli("feedback", "--run-id", run["run_id"], "--request-file", str(file))
            self.assertEqual(code, 2, error)
            self.assertFalse(error["ok"])
        code, error = self.cli("feedback", "--run-id", run["run_id"])
        self.assertEqual(code, 2)
        self.assertIn("--text", error["error"])
        code, legacy = self.cli("feedback", "--run-id", run["run_id"], "--text", '短反馈"兼容"🌐')
        self.assertEqual(code, 0, legacy)


if __name__ == "__main__":
    unittest.main()
