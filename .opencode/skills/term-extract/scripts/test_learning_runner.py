"""旧任务只读、旧报告事件兼容，以及退休入口不再启动模型。"""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import memory_store as store
import learning_runner as runner


class LegacyHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = mock.patch.dict(os.environ, {"TERM_EXTRACT_MEMORY_DIR":str(self.root / "memory")})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def seed(self, bad_hash=False):
        records = {
            "workflow_jobs": {"job_id":"job_old","input_path":"private.json","created_at":"2026-10-16T00:00:00Z"},
            "workflow_job_events": {"job_id":"job_old","kind":"state","state":"completed","created_at":"2026-10-16T00:00:01Z"},
        }
        with store._db(self.root,write=True) as c:
            for table,record in records.items():
                c.execute(f"CREATE TABLE {table}(record_id TEXT PRIMARY KEY,record_json TEXT,record_sha256 TEXT,created_at TEXT)")
                c.execute(f"INSERT INTO {table} VALUES(?,?,?,?)",("old",store._canonical(record).decode(), "bad" if bad_hash else store._hash(record),record["created_at"]))

    def test_empty_history_read_does_not_create_database(self):
        self.assertEqual(runner.list_jobs(self.root),{"items":[],"total":0})
        self.assertFalse((self.root/"memory").exists())

    def test_previous_job_is_visible_but_cannot_resume(self):
        self.seed()
        item=runner.get_job(self.root,"job_old")
        self.assertTrue(item["execution_disabled"])
        self.assertTrue(item["legacy"])
        self.assertEqual(item["state"],"completed")
        self.assertIn("OpenCode",item["next_action"])
        self.assertEqual(runner.list_jobs(self.root)["total"],1)

    def test_tampered_history_is_rejected(self):
        self.seed(bad_hash=True)
        with self.assertRaisesRegex(ValueError,"哈希"):
            runner.list_jobs(self.root)

    def test_all_retired_execution_methods_fail_without_model_or_store(self):
        with mock.patch("subprocess.Popen") as popen:
            for action in (runner.create_job,runner.run_job,runner.start_job,runner.save_settings,runner.list_models):
                with self.assertRaisesRegex(ValueError,"已停用"):
                    action(self.root,"ignored")
            popen.assert_not_called()
        self.assertFalse((self.root/"memory").exists())

    def test_cli_execution_is_explicitly_retired(self):
        with mock.patch("builtins.print") as printed:
            self.assertEqual(runner.main(["--project-root",str(self.root),"run","--job-id","job_old"]),2)
        self.assertIn("已停用",printed.call_args.args[0])

    def test_old_event_reports_still_parse(self):
        events=[
            {"type":"text","sessionID":"ses_old","part":{"text":'{"result":[]}'}},
            {"type":"step_finish","sessionID":"ses_old","part":{"tokens":{"input":12,"output":3},"cost":0.2}},
        ]
        result=runner.extract_event_output("\n".join(json.dumps(x) for x in events))
        self.assertEqual(result["output"],{"result":[]})
        self.assertEqual(result["session_id"],"ses_old")
        self.assertEqual(result["usage"]["input_tokens"],12)

    def test_mixed_sessions_or_non_object_results_remain_invalid(self):
        with self.assertRaises(ValueError):
            runner.extract_event_output(json.dumps({"type":"text","sessionID":"a","part":{"text":"{}"}})+"\n"+json.dumps({"type":"text","sessionID":"b","part":{"text":"{}"}}))
        with self.assertRaises(ValueError):
            runner.parse_model_output("[]")


if __name__=="__main__":
    unittest.main()
