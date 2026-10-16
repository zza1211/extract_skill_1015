"""合成离线示例真实接入现有评测器；不调用模型或写入项目实际库。"""
from __future__ import annotations

import copy
from contextlib import closing
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import memory_evaluate
import memory_store


REPO = Path(__file__).resolve().parents[4]
SPEC = importlib.util.spec_from_file_location("term_evaluation_example", REPO / "examples/evaluation/prepare_demo.py")
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)


class EvaluationExampleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="term-evaluation-fixture-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.environment = patch.dict(os.environ, {"TERM_EXTRACT_MEMORY_DIR": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def _counts(self, root):
        with closing(sqlite3.connect(root / ".term-memory/memory.sqlite3")) as connection:
            return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("revisions", "evaluations", "active")}

    def test_prepares_only_then_real_evaluator_passes_without_publish(self):
        info = demo.prepare_demo(self.base / "隔离演示")
        root = Path(info["demo_root"])
        self.assertEqual(self._counts(root), {"revisions": 1, "evaluations": 0, "active": 0})
        self.assertTrue(Path(info["manifest_path"]).is_absolute())
        self.assertTrue((root / "web/memory/index.html").is_file())
        self.assertEqual((root / "examples/evaluation/manifest.example.json").read_bytes(),
                         (demo.PACKAGE / "manifest.example.json").read_bytes())
        self.assertIn("--project-root", info["evaluation_command"])
        self.assertIn("8766", info["ui_command"])
        manifest = json.loads(Path(info["manifest_path"]).read_text(encoding="utf-8"))
        for case in manifest["cases"]:
            for field in ("index_path", "baseline_result_path", "candidate_result_path"):
                self.assertFalse(Path(case[field]).is_absolute())
                self.assertTrue((root / case[field]).is_file())
            folder = Path(case["index_path"]).parts[0]
            self.assertTrue((root / folder / "labels.json").is_file())
            self.assertEqual(case["assertions"], json.loads((root / folder / "annotations.json").read_text(encoding="utf-8")))
        with demo.isolated_memory(root):
            report = memory_evaluate.evaluate_manifest(root, info["memory_id"], info["revision"], Path(info["manifest_path"]))
            correction = memory_store.get_correction(root, info["correction_id"])
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["summary"]["fix_improvements"], 1)
        self.assertEqual(report["summary"]["regressions"], 0)
        self.assertEqual(report["summary"]["counterexample_cases"], 1)
        self.assertEqual(correction["family_id"], "synthetic-school-a")
        self.assertEqual(Path(correction["input_path"]), root / "development/input.json")
        self.assertEqual(self._counts(root), {"revisions": 1, "evaluations": 1, "active": 0})

    def test_ignores_external_store_and_restores_environment(self):
        actual = self.base / "external-real-store"
        actual.mkdir()
        marker = actual / "do-not-touch.txt"
        marker.write_text("生产环境变量指向这里", encoding="utf-8")
        with patch.dict(os.environ, {"TERM_EXTRACT_MEMORY_DIR": str(actual)}):
            info = demo.prepare_demo(self.base / "demo")
            self.assertEqual(os.environ["TERM_EXTRACT_MEMORY_DIR"], str(actual))
        self.assertTrue(info["environment_override_ignored"])
        self.assertEqual([p.name for p in actual.iterdir()], [marker.name])
        self.assertEqual(marker.read_text(encoding="utf-8"), "生产环境变量指向这里")
        self.assertEqual(Path(info["memory_dir"]), Path(info["demo_root"]) / ".term-memory")

    def test_rejects_repo_real_store_ancestors_and_nonempty_output(self):
        nonempty = self.base / "existing"
        nonempty.mkdir()
        (nonempty / "keep.txt").write_text("keep", encoding="utf-8")
        for path in (REPO, REPO / "runs/demo", REPO / ".term-memory/demo", REPO.parent, nonempty):
            with self.subTest(path=path), self.assertRaises(ValueError):
                demo.prepare_demo(path)
        actual = self.base / "external-store"
        with patch.dict(os.environ, {"TERM_EXTRACT_MEMORY_DIR": str(actual)}):
            for path in (actual, actual / "child", actual.parent):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    demo.prepare_demo(path)
        self.assertEqual((nonempty / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_evaluator_enforces_family_binding_and_key_parameter_assertions(self):
        info = demo.prepare_demo(self.base / "demo")
        root = Path(info["demo_root"])
        path = Path(info["manifest_path"])
        original = json.loads(path.read_text(encoding="utf-8"))
        modified = copy.deepcopy(original)
        modified["cases"][0]["family_id"] = "invented-unbound-family"
        path.write_text(json.dumps(modified, ensure_ascii=False), encoding="utf-8")
        with demo.isolated_memory(root), self.assertRaisesRegex(memory_evaluate.EvaluationError, "未被此候选"):
            memory_evaluate.evaluate_manifest(root, info["memory_id"], info["revision"], path)
        path.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
        # 模拟候选仍漏关键参数，必须真实失败，而不是固定返回通过。
        candidate_path = root / "development/candidate.json"
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        candidate[0]["定位标识"][1] = "<C5>"
        candidate_path.write_text(json.dumps(candidate, ensure_ascii=False), encoding="utf-8")
        with demo.isolated_memory(root):
            report = memory_evaluate.evaluate_manifest(root, info["memory_id"], info["revision"], path)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["summary"]["unfixed_target_cases"], 1)
        self.assertEqual(self._counts(root)["active"], 0)

    @unittest.skipUnless(os.name == "nt" and shutil.which("powershell"), "PowerShell 命令恢复校验仅适用于Windows")
    def test_printed_powershell_command_restores_missing_and_existing_environment(self):
        command = demo._command(self.base / "demo-memory", [sys.executable, "-c", "print('demo-child-ok')"])
        for previous in (None, str(self.base / "original-memory")):
            environment = os.environ.copy()
            environment.pop("TERM_EXTRACT_MEMORY_DIR", None)
            if previous is not None:
                environment["TERM_EXTRACT_MEMORY_DIR"] = previous
            script = (command + "\n" +
                      "[pscustomobject]@{ present=(Test-Path Env:TERM_EXTRACT_MEMORY_DIR); "
                      "value=[Environment]::GetEnvironmentVariable('TERM_EXTRACT_MEMORY_DIR','Process') } | ConvertTo-Json -Compress")
            response = subprocess.run(["powershell", "-NoProfile", "-Command", script], env=environment,
                                      capture_output=True, text=True, encoding="utf-8", timeout=20, check=True)
            lines = response.stdout.strip().splitlines()
            self.assertIn("demo-child-ok", lines)
            restored = json.loads(lines[-1])
            self.assertEqual(restored["present"], previous is not None)
            self.assertEqual(restored["value"], previous)


if __name__ == "__main__":
    unittest.main()
