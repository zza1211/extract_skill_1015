"""验证读完整性，尤其分片末尾不能冒充完整单元。"""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import commit_result
import document_io
import memory_store
from commit_result import coverage_report


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.index = self.directory / "index.json"
        self.index.write_text(json.dumps({"units": [{"unit_id": "u0", "tag": "<C9>", "source_length": 10, "source_text": "0123456789"}]}), encoding="utf-8")
        self.hash = hashlib.sha256(self.index.read_bytes()).hexdigest()
        self.receipts = self.directory / "read_receipts.jsonl"

    def record(self, spans, fingerprint=None):
        receipt = {"index_sha256": fingerprint or self.hash, "coverage_kind": "full_original", "parts": [{"unit_id": "u0", "part_start": start, "part_end": end, "total_length": 10} for start, end in spans]}
        self.receipts.write_text(json.dumps(receipt) + "\n", encoding="utf-8")

    def test_last_part_is_not_complete(self):
        self.record([(5, 10)])
        self.assertFalse(coverage_report(self.index, self.receipts)["complete"])

    def test_adjacent_parts_complete_original(self):
        self.record([(0, 4), (4, 10)])
        self.assertTrue(coverage_report(self.index, self.receipts)["complete"])

    def test_interior_gap_blocks_commit(self):
        self.record([(0, 4), (5, 10)])
        self.assertFalse(coverage_report(self.index, self.receipts)["complete"])

    def test_receipts_from_other_index_are_rejected(self):
        self.record([(0, 10)], "old-index")
        self.assertFalse(coverage_report(self.index, self.receipts)["complete"])


class CommitLearningTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="term-commit-learning-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"TERM_EXTRACT_MEMORY_DIR": str(self.root / "private-memory")})
        environment.start()
        self.addCleanup(environment.stop)
        self.input = self.root / "source.json"
        self.input.write_text('[{"text":"<C1>Access Point<C2>Technical requirement; procurement quantity 2."}]', encoding="utf-8")
        prepared = document_io.prepare(self.input, self.root / "prepared")
        self.index_path = Path(prepared["index_path"])
        self.index = json.loads(self.index_path.read_text(encoding="utf-8"))
        memory_store.create_snapshot(self.root, self.index_path)
        self.result = self.root / "result.json"
        self.evidence = self.root / "evidence.json"
        self.result.write_text(json.dumps([{"id": 0, "条款标题": "Access Point", "条款分类": "normal",
            "相关条款": [], "产品分类": "WLAN AP", "定位标识": ["<C1>", "<C2>"], "产品数量": "2"}], ensure_ascii=False), encoding="utf-8")
        self.evidence.write_text(json.dumps({"clauses": [{"id": 0, "quantity_source_tags": ["<C2>"],
            "related_source_tags": [], "notes": "Synthetic protocol fixture."}], "unresolved": []}), encoding="utf-8")
        receipts = {"index_sha256": hashlib.sha256(self.index_path.read_bytes()).hexdigest(),
            "coverage_kind": "full_original", "parts": [{"unit_id": unit["unit_id"], "part_start": 0,
                "part_end": unit["source_length"], "total_length": unit["source_length"]} for unit in self.index["units"]]}
        self.receipts = self.root / "read_receipts.jsonl"
        self.receipts.write_text(json.dumps(receipts) + "\n", encoding="utf-8")
        self.output = self.root / "final"

    def invoke(self, reflection=None):
        argv = ["commit_result.py", "--project-root", str(self.root), "--index", str(self.index_path),
            "--result", str(self.result), "--evidence", str(self.evidence), "--receipts", str(self.receipts),
            "--out-dir", str(self.output), "--model-id", "synthetic-commit-test"]
        if reflection is not None:
            argv.extend(["--reflection-json", json.dumps(reflection)])
        captured = io.StringIO()
        with patch("sys.argv", argv), redirect_stdout(captured):
            code = commit_result.main()
        return code, json.loads(captured.getvalue())

    def test_legacy_cli_registers_pending_reflection_without_fabricating_experience(self):
        code, response = self.invoke()
        self.assertEqual(code, 0)
        self.assertTrue(response["committed"])
        self.assertEqual(response["learning"]["status"], "pending_reflection")
        self.assertEqual(memory_store.list_memories(self.root)["memories"], [])

    def test_learning_storage_failure_preserves_committed_result_and_reports_retry(self):
        with patch.object(commit_result, "register_run", side_effect=ValueError("synthetic storage failure")):
            code, response = self.invoke({"insights": [], "no_experience_reason": "No new reusable observation."})
        self.assertEqual(code, 0)
        self.assertTrue(response["committed"])
        self.assertEqual(response["learning"]["status"], "learning_failed")
        self.assertNotIn("notice", response["learning"])
        self.assertIn("synthetic storage failure", response["learning"]["error"])
        self.assertEqual(json.loads((self.output / "result.json").read_text(encoding="utf-8")),
                         json.loads(self.result.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
