"""纯合成数据的经验库协议测试，不调用模型或冒充真实评测收益。"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import memory_store as store


class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="term-memory-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"TERM_EXTRACT_MEMORY_DIR": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.input = self.root / "input.json"
        self.before = self.root / "before.json"
        self.after = self.root / "after.json"
        self.input.write_text('[{"text":"<C1> CONFIDENTIAL_RAW 采购清单🌐"}]', encoding="utf-8")
        self.before.write_text('[{"产品数量":"1台"}]', encoding="utf-8")
        self.after.write_text('[{"产品数量":"2台"}]', encoding="utf-8")

    def correction(self):
        return store.import_correction(self.root, self.input, self.before, self.after,
                                       "人工确认数量来自采购清单", "测试审阅人", "synthetic-family-a")

    def payload(self, correction=None, **updates):
        correction = correction or self.correction()
        result = {"title": "采购数量来源🌐", "issue_type": "quantity", "triggers": ["采购清单", "switch"],
                  "applies_when": "正文缺采购量且清单能明确关联该采购项时",
                  "rule": "保留清单数量及单位，不把端口数当作采购数量。🌐",
                  "exclusions": "同名但属于不同采购项，或清单只列系统组件时不适用。",
                  "correction_ids": [correction["correction_id"]]}
        result.update(updates)
        return result

    def index(self, name="run"):
        directory = self.root / name
        directory.mkdir()
        path = directory / "index.json"
        path.write_text('{"schema_version":2,"units":[]}', encoding="utf-8")
        return path

    def report(self, candidate, **summary_updates):
        summary = {"fix_improvements": 1, "regressions": 0, "independent_regression_cases": 1,
                   "counterexample_cases": 1, "unfixed_target_cases": 0}
        summary.update(summary_updates)
        return {"candidate": {key: candidate[key] for key in ("memory_id", "revision", "content_sha256")},
                "base_release_sha256": store.release_digest(self.root), "status": "passed", "summary": summary,
                "artifacts": []}

    def publish(self, candidate, report=None):
        evaluation = store.register_evaluation(self.root, candidate["memory_id"], candidate["revision"], report or self.report(candidate))
        released = store.publish(self.root, candidate["memory_id"], candidate["revision"], evaluation["evaluation_id"], "人工审阅人", "合成协议测试发布")
        return released, evaluation

    def test_empty_reads_and_snapshot_do_not_initialize_database(self):
        digest = store.release_digest(self.root)
        self.assertEqual(digest, hashlib.sha256(b"[]").hexdigest())
        self.assertEqual(store.list_memories(self.root)["memories"], [])
        index = self.index()
        snapshot = store.create_snapshot(self.root, index)
        self.assertEqual(snapshot["memory_count"], 0)
        self.assertFalse(store.resolve_store_dir(self.root).exists())
        result = store.recall(self.root, index, "采购清单")
        self.assertEqual(result["memories"], [])
        self.assertEqual(store.run_memory_record(index)["delivered"], [])
        self.assertFalse(store.resolve_store_dir(self.root).exists())

    def test_store_can_be_external_without_implicit_directory_creation(self):
        external = self.root / "外部经验库"
        with patch.dict(os.environ, {"TERM_EXTRACT_MEMORY_DIR": str(external)}):
            self.assertEqual(store.resolve_store_dir(self.root), external)
            store.release_digest(self.root)
            self.assertFalse(external.exists())
            self.correction()
            self.assertTrue((external / "memory.sqlite3").is_file())

    def test_correction_records_hashes_and_keeps_input_only_as_external_reference(self):
        imported = self.correction()
        self.assertNotIn("before_json", imported)
        record = store.get_correction(self.root, imported["correction_id"])
        self.assertEqual(record["source_sha256"], hashlib.sha256(self.input.read_bytes()).hexdigest())
        self.assertEqual(record["input_path"], str(self.input.resolve()))
        self.assertEqual(record["after_json"], [{"产品数量": "2台"}])
        self.assertNotIn("CONFIDENTIAL_RAW", json.dumps(record, ensure_ascii=False))
        with self.assertRaises(store.MemoryError):
            store.import_correction(self.root, self.input, self.before, self.after, "理由", True, "family")

    def test_invalid_before_can_be_preserved_as_path_and_hash_only(self):
        self.before.write_text("not JSON: 故障输出", encoding="utf-8")
        item = self.correction()
        self.assertIsNone(store.get_correction(self.root, item["correction_id"])["before_json"])

    def test_payload_rejects_empty_conditions_missing_corrections_and_unknown_fields(self):
        payload = self.payload()
        for updates in ({"applies_when": " "}, {"exclusions": ""}, {"triggers": []},
                        {"correction_ids": ["corr_missing"]}, {"extra": "no"}):
            with self.subTest(updates=updates), self.assertRaises(store.MemoryError):
                store.propose(self.root, {**payload, **updates})
        self.assertEqual(store.list_memories(self.root)["memories"], [])

    def test_payload_budget_is_small_enough_to_deliver_a_whole_published_rule(self):
        payload = self.payload(rule="A" * 2400, applies_when="B" * 900, exclusions="C" * 900)
        with self.assertRaises(store.MemoryError):
            store.propose(self.root, payload)
        payload["rule"] = "A" * 1400
        candidate = store.propose(self.root, payload)
        self.publish(candidate)
        index = self.index()
        store.create_snapshot(self.root, index)
        response = store.recall(self.root, index, "采购清单")
        self.assertEqual(response["memories"][0]["payload"], payload)
        self.assertLessEqual(len(store._canonical(response)), 6144)

    def test_candidate_never_recalled_and_publish_affects_only_new_snapshots(self):
        candidate = store.propose(self.root, self.payload())
        old_index = self.index("before-publish")
        old_snapshot = store.create_snapshot(self.root, old_index)
        self.assertEqual(store.recall(self.root, old_index, "采购清单")["memories"], [])
        self.publish(candidate)
        self.assertEqual(store.create_snapshot(self.root, old_index)["snapshot_sha256"], old_snapshot["snapshot_sha256"])
        self.assertEqual(store.recall(self.root, old_index, "采购清单")["memories"], [])
        new_index = self.index("after-publish")
        store.create_snapshot(self.root, new_index)
        response = store.recall(self.root, new_index, "采购清单🌐")
        self.assertEqual(response["memories"][0]["memory_id"], candidate["memory_id"])
        text = json.dumps(response, ensure_ascii=False)
        self.assertIn("🌐", text)
        self.assertNotIn("CONFIDENTIAL_RAW", text)
        self.assertNotIn("after_json", text)
        record = store.run_memory_record(new_index)
        self.assertEqual(record["delivered"][0]["content_sha256"], candidate["content_sha256"])
        self.assertNotIn("applied", record)

    def test_new_revision_is_immutable_and_does_not_replace_active_revision(self):
        payload = self.payload()
        first = store.propose(self.root, payload)
        self.publish(first)
        release = store.release_digest(self.root)
        second = store.propose(self.root, {**payload, "rule": "新候选规则，仍需评测。"}, first["memory_id"])
        self.assertEqual(second["revision"], 2)
        self.assertEqual(store.release_digest(self.root), release)
        self.assertEqual(store.load_candidate(self.root, first["memory_id"], 1)["state"], "published")
        self.assertEqual(store.load_candidate(self.root, first["memory_id"], 2)["state"], "candidate")
        self.publish(second)
        self.assertEqual(store.load_candidate(self.root, first["memory_id"], 1)["state"], "superseded")
        self.assertEqual(store.load_candidate(self.root, first["memory_id"], 1)["payload"], payload)
        with closing(sqlite3.connect(store.resolve_store_dir(self.root) / "memory.sqlite3")) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE revisions SET payload_json='{}'")

    def test_registration_rejects_wrong_hash_and_bool_summary(self):
        candidate = store.propose(self.root, self.payload())
        report = self.report(candidate)
        report["candidate"]["content_sha256"] = "bad"
        with self.assertRaises(store.MemoryError):
            store.register_evaluation(self.root, candidate["memory_id"], 1, report)
        with self.assertRaises(store.MemoryError):
            store.register_evaluation(self.root, candidate["memory_id"], 1, self.report(candidate, regressions=False))

    def test_each_publication_gate_is_enforced(self):
        candidate = store.propose(self.root, self.payload())
        for summary in ({"fix_improvements": 0}, {"regressions": 1}, {"independent_regression_cases": 0},
                        {"counterexample_cases": 0}, {"unfixed_target_cases": 1}):
            with self.subTest(summary=summary):
                registered = store.register_evaluation(self.root, candidate["memory_id"], 1, self.report(candidate, **summary))
                with self.assertRaises(store.MemoryError):
                    store.publish(self.root, candidate["memory_id"], 1, registered["evaluation_id"], "审阅人", "不能发布")
        report = self.report(candidate)
        report["status"] = "failed"
        registered = store.register_evaluation(self.root, candidate["memory_id"], 1, report)
        with self.assertRaises(store.MemoryError):
            store.publish(self.root, candidate["memory_id"], 1, registered["evaluation_id"], "审阅人", "不能发布")

    def test_release_change_invalidates_old_report_and_external_report_is_not_accepted(self):
        payload = self.payload()
        first = store.propose(self.root, payload)
        second = store.propose(self.root, {**payload, "title": "另一采购数量经验"})
        registered = store.register_evaluation(self.root, second["memory_id"], 1, self.report(second))
        self.publish(first)
        with self.assertRaises(store.MemoryError):
            store.publish(self.root, second["memory_id"], 1, registered["evaluation_id"], "审阅人", "旧基线报告")
        with self.assertRaises(store.MemoryError):
            store.publish(self.root, second["memory_id"], 1, str(self.root / "forged-report.json"), "审阅人", "外部报告")

    def test_correction_and_report_artifact_drift_block_publication(self):
        candidate = store.propose(self.root, self.payload())
        registered = store.register_evaluation(self.root, candidate["memory_id"], 1, self.report(candidate))
        old = self.after.read_bytes()
        self.after.write_text("[]", encoding="utf-8")
        with self.assertRaises(store.MemoryError):
            store.publish(self.root, candidate["memory_id"], 1, registered["evaluation_id"], "审阅人", "不能发布漂移输入")
        self.after.write_bytes(old)
        artifact = self.root / "manifest.json"
        artifact.write_text("{}", encoding="utf-8")
        report = self.report(candidate)
        report["artifacts"] = [{"path": str(artifact), "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest()}]
        registered = store.register_evaluation(self.root, candidate["memory_id"], 1, report)
        artifact.write_text('{"changed":true}', encoding="utf-8")
        with self.assertRaises(store.MemoryError):
            store.publish(self.root, candidate["memory_id"], 1, registered["evaluation_id"], "审阅人", "不能发布漂移评测")

    def test_withdraw_keeps_old_snapshot_and_blocks_reuse_of_consumed_evaluation(self):
        candidate = store.propose(self.root, self.payload())
        _, evaluation = self.publish(candidate)
        old = self.index("old")
        frozen = store.create_snapshot(self.root, old)
        store.withdraw(self.root, candidate["memory_id"], "人工审阅人", "发现规则范围问题")
        self.assertEqual(store.load_candidate(self.root, candidate["memory_id"], 1)["state"], "withdrawn")
        self.assertEqual(store.recall(self.root, old, "采购清单")["memories"][0]["memory_id"], candidate["memory_id"])
        self.assertEqual(store.create_snapshot(self.root, old)["snapshot_sha256"], frozen["snapshot_sha256"])
        new = self.index("new")
        store.create_snapshot(self.root, new)
        self.assertEqual(store.recall(self.root, new, "采购清单")["memories"], [])
        with self.assertRaises(store.MemoryError):
            store.publish(self.root, candidate["memory_id"], 1, evaluation["evaluation_id"], "审阅人", "重用已消费评测")

    def test_empty_query_no_hit_issue_filter_and_budget_preserve_whole_rule(self):
        candidate = store.propose(self.root, self.payload(rule="不能删掉反例或适用条件。" + "A" * 1800))
        self.publish(candidate)
        index = self.index()
        store.create_snapshot(self.root, index)
        with self.assertRaises(store.MemoryError):
            store.recall(self.root, index, " ")
        self.assertEqual(store.recall(self.root, index, "zzunrelatedword")["memories"], [])
        self.assertEqual(store.recall(self.root, index, "采购清单", issue_type="boundary")["memories"], [])
        small = store.recall(self.root, index, "采购清单", max_bytes=1024)
        self.assertEqual(small["memories"], [])
        self.assertEqual(small["budget_omitted"], 1)
        self.assertLessEqual(len(store._canonical(small)), 1024)
        full = store.recall(self.root, index, "采购清单", max_bytes=8192)
        self.assertEqual(full["memories"][0]["payload"]["rule"], store.load_candidate(self.root, candidate["memory_id"], 1)["payload"]["rule"])
        with self.assertRaises(store.MemoryError):
            store.recall(self.root, index, "采购清单", limit=6)

    def test_deferred_receipt_and_forged_response_rejected(self):
        candidate = store.propose(self.root, self.payload())
        self.publish(candidate)
        index = self.index()
        store.create_snapshot(self.root, index)
        response = store.recall(self.root, index, "采购清单", record_receipt=False)
        self.assertFalse((index.parent / store.RECEIPTS_NAME).exists())
        altered = copy.deepcopy(response)
        altered["memories"][0]["payload"]["rule"] = "伪造规则"
        with self.assertRaises(store.MemoryError):
            store.record_delivery(index, altered)
        store.record_delivery(index, response)
        record = store.run_memory_record(index)
        self.assertEqual(record["delivered"][0]["memory_id"], candidate["memory_id"])
        receipt = json.loads((index.parent / store.RECEIPTS_NAME).read_text(encoding="utf-8"))
        receipt["delivered"][0]["content_sha256"] = "forged"
        (index.parent / store.RECEIPTS_NAME).write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaises(store.MemoryError):
            store.run_memory_record(index)

    def test_bool_schema_and_revision_cannot_impersonate_integer_one(self):
        candidate = store.propose(self.root, self.payload())
        self.publish(candidate)
        index = self.index()
        store.create_snapshot(self.root, index)
        response = store.recall(self.root, index, "采购清单", record_receipt=False)
        for field in ("schema_version", "revision"):
            fake = copy.deepcopy(response)
            if field == "schema_version":
                fake[field] = True
            else:
                fake["memories"][0][field] = True
            with self.subTest(field=field), self.assertRaises(store.MemoryError):
                store.record_delivery(index, fake)
        store.record_delivery(index, response)
        path = index.parent / store.RECEIPTS_NAME
        original = json.loads(path.read_text(encoding="utf-8"))
        for field in ("schema_version", "revision"):
            fake = copy.deepcopy(original)
            if field == "schema_version":
                fake[field] = True
            else:
                fake["delivered"][0][field] = True
            path.write_text(json.dumps(fake), encoding="utf-8")
            with self.subTest(field=field), self.assertRaises(store.MemoryError):
                store.run_memory_record(index)

    def test_snapshot_content_and_index_hash_rechecked(self):
        candidate = store.propose(self.root, self.payload())
        self.publish(candidate)
        index = self.index()
        store.create_snapshot(self.root, index)
        path = index.parent / store.SNAPSHOT_NAME
        original = path.read_bytes()
        snapshot = json.loads(original)
        snapshot["memories"][0]["payload"]["rule"] = "改写经验"
        path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(store.MemoryError):
            store.run_memory_record(index)
        path.write_bytes(original)
        index.write_text("{}", encoding="utf-8")
        with self.assertRaises(store.MemoryError):
            store.recall(self.root, index, "采购清单")

    def test_legacy_is_explicit_but_orphan_receipts_are_invalid(self):
        index = self.index()
        self.assertEqual(store.run_memory_record(index)["status"], "legacy_no_snapshot")
        (index.parent / store.RECEIPTS_NAME).write_text("{}\n", encoding="utf-8")
        with self.assertRaises(store.MemoryError):
            store.run_memory_record(index)

    def test_concurrent_revisions_keep_distinct_sequence(self):
        payload = self.payload()
        first = store.propose(self.root, payload)
        with ThreadPoolExecutor(max_workers=3) as pool:
            revisions = list(pool.map(lambda _: store.propose(self.root, payload, first["memory_id"])["revision"], range(4)))
        self.assertEqual(sorted(revisions), [2, 3, 4, 5])

    def test_cli_inspection_and_errors_are_json_without_private_gold(self):
        correction = self.correction()
        script = Path(store.__file__)
        base = [sys.executable, str(script), "--project-root", str(self.root)]
        shown = subprocess.run(base + ["show-correction", "--correction-id", correction["correction_id"]], capture_output=True, encoding="utf-8")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        self.assertNotIn("after_json", json.loads(shown.stdout))
        bad = subprocess.run(base + ["register-evaluation", "--report-json", "{}"], capture_output=True, encoding="utf-8")
        self.assertEqual(bad.returncode, 2)
        self.assertFalse(json.loads(bad.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
