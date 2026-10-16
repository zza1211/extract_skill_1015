"""高层工作台的真实服务联动；只使用临时可信标签和私有库。"""
import http.client
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import evolution_store as evolution
import agent_learning
import document_io
import memory_store
import memory_ui
from memory_dashboard import DashboardService


class EvolutionDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.private = Path(self.temporary.name) / "memory"
        self.environment = patch.dict(os.environ, {"TERM_EXTRACT_MEMORY_DIR": str(self.private)})
        self.environment.start()
        self.service = DashboardService(Path(__file__).resolve().parents[4])

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_all_new_empty_reads_preserve_absent_store(self):
        for path in ("/api/learning/studies", "/api/learning/jobs", "/api/learning/cases", "/api/reviews"):
            self.assertIsInstance(self.service.get(path, {}), dict)
        self.assertFalse(self.private.exists())

    def test_study_routes_only_return_safe_summaries_without_writing(self):
        source = Path(self.temporary.name) / "study-input.json"
        labels = Path(self.temporary.name) / "hidden-labels.json"
        source.write_text(json.dumps([{"text": "<C1> Switch 2台 GE端口"}]), encoding="utf-8")
        labels.write_text(json.dumps([{"id": 0, "条款标题": "秘密正确答案标题", "条款分类": "normal", "相关条款": [],
                                     "产品分类": "盒式交换机", "定位标识": ["<C1>", "<C1>"], "产品数量": "2台"}]), encoding="utf-8")
        prepared = document_io.prepare(source, Path(self.temporary.name) / "study-run")
        memory_store.create_snapshot(self.service.root, Path(prepared["index_path"]))
        study = agent_learning.begin_study(self.service.root, input_path=source, labels_path=labels, family_id="safe-summary-family",
            index_path=prepared["index_path"], execution_context={"session_id": "synthetic-session", "message_id": "synthetic-message", "agent": "term-extractor"})
        before = {str(p.relative_to(self.private)): memory_store._file_hash(p) for p in self.private.rglob("*") if p.is_file()}
        listed = self.service.get("/api/learning/studies", {})
        detail = self.service.get(f"/api/learning/studies/{study['study_id']}", {})
        self.assertEqual(listed["items"][0]["study_id"], study["study_id"])
        self.assertEqual(detail["state"], "awaiting_blind_extraction")
        raw = json.dumps({"list": listed, "detail": detail}, ensure_ascii=False)
        for secret in ("秘密正确答案标题", str(labels), "labels_path", "labels_archive_path", "labels_json"):
            self.assertNotIn(secret, raw)
        after = {str(p.relative_to(self.private)): memory_store._file_hash(p) for p in self.private.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_case_local_answer_uses_shared_queue_without_global_change(self):
        review = evolution.create_review(self.service.root, kind="comparison", title="局部范围",
            question="新增两句是否影响选型？", context={"source": "合成原文"},
            choices=[{"value": "harmless", "label": "无害差异"}, "uncertain"],
            subject={"case_id": "synthetic_case", "difference_id": "diff_1"})
        listed = self.service.get("/api/reviews", {})
        self.assertEqual(listed["items"][0]["review_id"], review["review_id"])
        result = self.service.post(f"/api/reviews/{review['review_id']}/resolve", {
            "decision": "harmless", "answer": "这两句是页脚，不影响选型。", "actor": "合成维护者", "expected_revision": 1})
        self.assertEqual(result["state"], "resolved")
        self.assertEqual(result["resolution"]["channel"], "dashboard")
        self.assertEqual(result["resolution"]["scope"], "case")
        self.assertEqual(self.service.get("/api/overview", {})["counts"]["published"], 0)
        with self.assertRaises(ValueError):
            self.service.post(f"/api/reviews/{review['review_id']}/resolve", {"decision": "uncertain"})

    def test_model_controls_are_rejected_without_initializing_store(self):
        with patch("learning_runner.create_job") as create, patch("learning_runner.start_job") as start, \
                patch("learning_runner.save_settings") as settings, patch("learning_runner.list_models") as models:
            for path in ("/api/learning/jobs", "/api/learning/jobs/old_job/start", "/api/learning/jobs/old_job/resume",
                         "/api/learning/settings", "/api/learning/models", "/api/learning/studies"):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, "OpenCode"):
                    self.service.post(path, {"mode": "extract", "input_path": "unused", "executor": "fake"})
            for path in ("/api/learning/settings", "/api/learning/models"):
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, "OpenCode"):
                    self.service.get(path, {})
            for operation in (create, start, settings, models):
                operation.assert_not_called()
        self.assertFalse(self.private.exists())

    def test_http_cannot_start_model_even_with_valid_csrf(self):
        server = memory_ui.create_server(self.service.root, port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        authority = f"127.0.0.1:{server.server_address[1]}"
        headers = {"Host": authority, "Origin": "http://" + authority,
                   "X-CSRF-Token": server.csrf_token, "Content-Type": "application/json"}
        try:
            with patch("learning_runner.start_job") as start, patch("learning_runner.create_job") as create:
                for route in ("/api/learning/jobs", "/api/learning/jobs/old_job/start", "/api/learning/settings"):
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=3)
                    try:
                        connection.request("POST", route, body=b"{}", headers=headers)
                        response = connection.getresponse()
                        self.assertEqual(response.status, 400)
                        self.assertIn("OpenCode", json.loads(response.read())["error"])
                    finally:
                        connection.close()
                start.assert_not_called()
                create.assert_not_called()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
        self.assertFalse(self.private.exists())

    def test_trusted_case_import_still_works_without_model_execution(self):
        source = Path(self.temporary.name) / "input.json"
        labels = Path(self.temporary.name) / "labels.json"
        source.write_text(json.dumps([{"text": "<C1> Core Switch 2台 GE端口"}]), encoding="utf-8")
        labels.write_text(json.dumps([{"id": 0, "条款标题": "Core Switch", "条款分类": "normal", "相关条款": [],
                                     "产品分类": "盒式交换机", "定位标识": ["<C1>", "<C1>"], "产品数量": "2台"}]), encoding="utf-8")
        with patch("subprocess.Popen", side_effect=AssertionError("网页不应启动子进程或模型")):
            result = self.service.post("/api/learning/cases", {"input_path": str(source), "labels_path": str(labels),
                                      "family_id": "synthetic-family", "split": "regression"})
            self.assertEqual(result["trust"], "user_provided_label")
            self.assertEqual(self.service.get("/api/learning/cases", {})["items"][0]["case_id"], result["case_id"])


if __name__ == "__main__":
    unittest.main()
