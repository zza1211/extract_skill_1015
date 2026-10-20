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

    def test_real_session_cannot_borrow_other_session_or_role_coverage(self):
        self.record([(0, 10)])
        receipt = json.loads(self.receipts.read_text())
        parent = {"session_id": "new-parent", "message_id": "new-read", "agent": "term-extractor"}
        for actual in ({"session_id": "old-parent", "agent": "term-extractor"},
                       {"session_id": "new-parent", "agent": "term-auditor"}, {}):
            receipt["execution_context"] = actual
            self.receipts.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
            self.assertFalse(coverage_report(self.index, self.receipts, parent)["complete"])
        receipt["execution_context"] = parent
        self.receipts.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        self.assertTrue(coverage_report(self.index, self.receipts, parent)["complete"])


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

    def invoke(self, reflection=None, context=None):
        argv = ["commit_result.py", "--project-root", str(self.root), "--index", str(self.index_path),
            "--result", str(self.result), "--evidence", str(self.evidence), "--receipts", str(self.receipts),
            "--out-dir", str(self.output), "--model-id", "synthetic-commit-test"]
        if reflection is not None:
            argv.extend(["--reflection-json", json.dumps(reflection)])
        if context is not None:
            argv.extend(["--execution-context-json", json.dumps(context)])
        captured = io.StringIO()
        with patch("sys.argv", argv), redirect_stdout(captured):
            code = commit_result.main()
        return code, json.loads(captured.getvalue())

    def close_audit(self):
        import agent_audit
        parent = {"session_id": "commit-parent", "message_id": "parent-tool", "agent": "term-extractor"}
        child = {"session_id": "commit-child", "message_id": "child-tool", "agent": "term-auditor"}
        def call(action, context, **body):
            return agent_audit.dispatch(self.root, {"action": action, "execution_context": context, **body})
        prepared = call("prepare", parent, index_path=str(self.index_path), result_path=str(self.result), evidence_path=str(self.evidence))
        audit_id = prepared["audit_id"]
        call("open", child, audit_id=audit_id)
        cursor = "0"
        while True:
            page = call("read", child, audit_id=audit_id, cursor=cursor)["model_view"]
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
        call("inventory", child, audit_id=audit_id, objects=[{"title": "Access Point", "evidence_tags": ["<C1>"]}])
        call("draft", child, audit_id=audit_id)
        call("finish", child, audit_id=audit_id, findings=[], summary="提交一致性的合成协议测试，不宣称模型正确。")
        call("inspect", parent, audit_id=audit_id)
        call("resolve", parent, audit_id=audit_id, result_path=str(self.result), evidence_path=str(self.evidence), decisions=[])
        receipt = json.loads(self.receipts.read_text(encoding="utf-8"))
        receipt["execution_context"] = parent
        self.receipts.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        return parent

    def test_real_commit_preserves_exact_audited_bytes_and_hashes(self):
        parent = self.close_audit()
        original = self.result.read_bytes()
        code, response = self.invoke(context=parent)
        self.assertEqual(code, 0, response)
        self.assertEqual(Path(response["result_path"]).read_bytes(), original)
        run = json.loads(Path(response["run_path"]).read_text(encoding="utf-8"))
        self.assertEqual(run["result_sha256"], run["agent_audit"]["final_result_sha256"])
        self.assertEqual(run["evidence_sha256"], run["agent_audit"]["final_evidence_sha256"])

    def test_working_copy_race_cannot_save_a_different_version_than_audited(self):
        parent = self.close_audit()
        audited = self.result.read_bytes()
        changed = json.loads(audited)
        changed[0]["产品数量"] = "9"
        self.result.write_text(json.dumps(changed, ensure_ascii=False), encoding="utf-8")
        original_run = commit_result.subprocess.run
        def concurrent_validate(*args, **kwargs):
            # 提交捕获错版后，另一个 validate 将工作文件恢复到已关闭版。
            self.result.write_bytes(audited)
            return original_run(*args, **kwargs)
        with patch.object(commit_result.subprocess, "run", side_effect=concurrent_validate):
            code, response = self.invoke(context=parent)
        self.assertEqual(code, 2)
        self.assertFalse(response["committed"])
        self.assertIn("版本不一致", " ".join(response["errors"]))
        self.assertFalse((self.output / "result.json").exists())

    def test_real_commit_cannot_borrow_an_earlier_parent_session_receipt(self):
        parent = self.close_audit()
        receipt = json.loads(self.receipts.read_text(encoding="utf-8"))
        receipt["execution_context"]["session_id"] = "old-parent"
        self.receipts.write_text(json.dumps(receipt) + "\n", encoding="utf-8")
        code, response = self.invoke(context=parent)
        self.assertEqual(code, 2)
        self.assertFalse(response["committed"])
        self.assertFalse(response["coverage"]["complete"])

    def test_auditor_cannot_commit_final_result(self):
        code, response = self.invoke(context={"session_id": "child", "message_id": "tool", "agent": "term-auditor"})
        self.assertEqual(code, 2)
        self.assertFalse(response["committed"])

    def test_legacy_cli_registers_pending_reflection_without_fabricating_experience(self):
        code, response = self.invoke()
        self.assertEqual(code, 0)
        self.assertTrue(response["committed"])
        self.assertEqual(response["learning"]["status"], "pending_reflection")
        self.assertEqual(memory_store.list_memories(self.root)["memories"], [])

    def test_commit_initializes_pending_human_review_and_editable_work_file(self):
        code, response = self.invoke({"insights": [], "no_experience_reason": "合成提交不产生新经验。"})
        self.assertEqual(code, 0)
        self.assertTrue(response["committed"])
        review = response["result_review"]
        self.assertEqual(review["run_id"], response["learning"]["run_id"])
        self.assertEqual(review["status"], "pending")
        self.assertEqual(json.loads(Path(review["current_result_path"]).read_text(encoding="utf-8")),
                         json.loads(self.result.read_text(encoding="utf-8")))
        self.assertNotEqual(Path(review["current_result_path"]), Path(response["result_path"]))
        self.assertIn("校验", response["next_action"])

    def test_review_initialization_failure_does_not_lose_committed_result(self):
        import document_review
        with patch.object(document_review, "ensure_run", side_effect=ValueError("synthetic review storage failure")):
            code, response = self.invoke({"insights": [], "no_experience_reason": "合成提交不产生新经验。"})
        self.assertEqual(code, 0)
        self.assertTrue(response["committed"])
        self.assertEqual(response["result_review"]["status"], "initialization_failed")
        self.assertTrue(Path(response["result_path"]).is_file())

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
