"""高层工作台的真实服务联动；只使用临时可信标签和私有库。"""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import evolution_store as evolution
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
        for path in ("/api/learning/settings", "/api/learning/jobs", "/api/learning/cases", "/api/reviews"):
            self.assertIsInstance(self.service.get(path, {}), dict)
        self.assertFalse(self.private.exists())

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

    def test_job_request_rejects_bypass_fields(self):
        with self.assertRaisesRegex(ValueError, "未知"):
            self.service.post("/api/learning/jobs", {"mode": "extract", "input_path": "unused", "executor": "fake"})
        self.assertFalse(self.private.exists())

    def test_settings_roundtrip_has_no_api_key_surface(self):
        value = self.service.post("/api/learning/settings", {"settings": {"model_id": "provider/test", "max_calls": 8}})
        self.assertEqual(value["settings"]["model_id"], "provider/test")
        self.assertEqual(self.service.get("/api/learning/settings", {})["settings"]["max_calls"], 8)
        with self.assertRaises(ValueError):
            self.service.post("/api/learning/settings", {"settings": {"api_key": "never-store"}})


if __name__ == "__main__":
    unittest.main()
