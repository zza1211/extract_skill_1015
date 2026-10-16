"""作业隔离、可信标签顺序、预算/续跑和真实事件来源绑定。全部写入临时目录。"""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import evolution_store as evolution
import learning_runner as runner


def clause(end="<C2>", quantity="2台"):
    return {"id": 0, "条款标题": "Core Switch", "条款分类": "normal", "相关条款": [],
            "产品分类": "盒式交换机", "定位标识": ["<C1>", end], "产品数量": quantity}


def output(result=None, insights=None):
    result = [clause()] if result is None else result
    return {"result": result, "evidence": {"clauses": [{"id": item["id"], "quantity_source_tags": ["<C2>"],
            "related_source_tags": [], "notes": "数量来自明确的本设备采购清单。"} for item in result], "unresolved": []},
            "reflection": {"insights": insights or [], "no_experience_reason": "本次没有新的可迁移经验。"}}


class FakeExecutor:
    kind = "fake"

    def __init__(self, responses=None):
        self.requests = []
        self.responses = responses or {}

    def execute(self, request):
        self.requests.append(copy.deepcopy(request))
        response = self.responses.get(request["phase"], output())
        if callable(response):
            response = response(request)
        if isinstance(response, Exception):
            raise response
        text = response if isinstance(response, str) else json.dumps(response, ensure_ascii=False)
        return {"events": json.dumps({"type": "text", "sessionID": "ses_fake_" + str(len(self.requests)), "part": {"text": text}}, ensure_ascii=False) + "\n", "kind": "fake"}


class LearningRunnerTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = mock.patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        os.environ.pop("TERM_EXTRACT_MEMORY_DIR", None)
        self.source = self.root / "source.json"
        self.source.write_text(json.dumps([{"text": "<C1>Core Switch, 24 GE ports"},
                                          {"text": "<C2>Core Switch quantity 2台"},
                                          {"text": "<C3>Service training"}], ensure_ascii=False), encoding="utf-8")
        self.labels = self.root / "secret-correct-labels.json"
        self.labels.write_text(json.dumps([clause()], ensure_ascii=False), encoding="utf-8")
        reference = self.root / ".opencode/skills/term-extract/references"
        reference.mkdir(parents=True)
        (reference / "business-rules.md").write_text("只抽设备；机柜布线排除；纯品牌准入不单独抽general。", encoding="utf-8")
        (reference / "output-format.md").write_text("使用七字段输出。", encoding="utf-8")

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def create(self, mode="extract", **kwargs):
        return runner.create_job(self.root, mode=mode, input_path=self.source,
                                 **({"labels_path": self.labels} if mode == "learn_labels" else {}), **kwargs)

    def test_empty_reads_do_not_create_store(self):
        self.assertEqual(runner.list_jobs(self.root)["total"], 0)
        self.assertFalse(runner.get_settings(self.root)["configured"])
        self.assertFalse((self.root / ".term-memory").exists())

    def test_stopped_process_is_read_as_resumable_without_writing_history(self):
        job = self.create()
        runner._event(self.root, job["job_id"], "state", state="running")
        result = runner.get_job(self.root, job["job_id"])
        self.assertEqual(result["state"], "interrupted")
        self.assertIn("继续", result["next_action"])

    def test_resolved_conflict_uses_current_state_instead_of_frozen_scan(self):
        insight = {"title": "共同品牌和技术条件的区分", "issue_type": "boundary", "triggers": ["共同品牌"],
            "applies_when": "共同品牌要求同时包含无线并发性能选型条件时。",
            "rule": "把共同品牌及技术参数抽为 general，并保留其技术参数约束对象。",
            "exclusions": "只有品牌准入或认证，没有选型技术内容时不适用。",
            "observation": "合成样例用于验证冲突处理后的任务恢复。", "evidence_tags": ["<C1>"]}
        job = self.create("learn_labels")
        executor = FakeExecutor({"reflection": {"reflection": {"insights": [insight], "no_experience_reason": ""}}})
        first = runner.run_job(self.root, job["job_id"], executor)
        self.assertEqual(first["state"], "needs_review", first.get("error"))
        reviews = evolution.list_reviews(self.root)["items"]
        self.assertTrue(reviews)
        for review in reviews:
            evolution.resolve_review(self.root, review["review_id"], "保留，条件不同", actor="合成维护者")
        second = runner.run_job(self.root, job["job_id"], executor)
        self.assertEqual(second["state"], "needs_examples", second.get("error"))
        self.assertEqual(sum(r["phase"] == "production" for r in executor.requests), 1)

    def test_settings_validate_and_append(self):
        with self.assertRaises(ValueError):
            runner.save_settings(self.root, {"api_key": "forbidden"})
        with self.assertRaises(ValueError):
            runner.save_settings(self.root, {"max_calls": True})
        with self.assertRaises(ValueError):
            runner.save_settings(self.root, {"model_id": "flash"})
        saved = runner.save_settings(self.root, {"model_id": "deepseek/deepseek-flash", "max_calls": 3})
        self.assertEqual(saved["settings"]["max_calls"], 3)

    def test_create_real_index_and_frozen_snapshot(self):
        job = self.create()
        self.assertEqual(job["state"], "queued")
        self.assertEqual(runner.store._file_hash(Path(job["index_path"])), job["index_sha256"])
        self.assertEqual(job["memory_snapshot"]["memory_count"], 0)
        self.assertEqual(runner.list_jobs(self.root)["total"], 1)
        with self.assertRaises(ValueError):
            runner.create_job(self.root, mode="extract", input_path=self.source, labels_path=self.labels)

    def test_unconfigured_real_job_preserved(self):
        job = self.create()
        result = runner.run_job(self.root, job["job_id"])
        self.assertEqual(result["state"], "needs_configuration")
        self.assertEqual(result["usage"]["calls"], 0)

    def test_pure_extract_archives_actual_full_prompt_and_usage_unknown(self):
        job, executor = self.create(), FakeExecutor()
        result = runner.run_job(self.root, job["job_id"], executor)
        self.assertEqual(result["state"], "completed", result.get("error"))
        self.assertEqual(result["result"], [clause()])
        self.assertEqual(result["usage"]["calls"], 1)
        self.assertIsNone(result["usage"]["cost"])
        self.assertIsNone(result["usage"]["input_tokens"])
        prompt = executor.requests[0]["payload"]
        self.assertNotIn("labels", prompt)
        self.assertEqual(len(prompt["source_document"]), 3)
        self.assertEqual(result["prediction"]["provenance"]["kind"], "fake")
        archived = runner.store._read_json(self.root / ".term-memory/.workflow/jobs" / job["job_id"] / "run.json")
        self.assertEqual(archived["coverage"]["delivery_kind"], "full_prompt")
        again = runner.run_job(self.root, job["job_id"], executor)
        self.assertEqual(again["usage"]["calls"], 1)

    def test_trusted_label_blind_extraction_before_comparison(self):
        job, executor = self.create("learn_labels", auto_evolve=False), FakeExecutor()
        result = runner.run_job(self.root, job["job_id"], executor)
        self.assertEqual(result["state"], "completed", result.get("error"))
        self.assertEqual(result["comparison"]["status"], "passed")
        blind = executor.requests[0]["prompt"]
        self.assertNotIn("secret-correct-labels", blind)
        self.assertNotIn('"labels":', blind)
        self.assertEqual(len(executor.requests), 1)
        self.assertEqual(evolution.get_case(self.root, job["case_id"])["trust"], "user_provided_label")

    def test_hard_error_reflection_without_label_leak_then_needs_examples(self):
        insight = {"title": "采购量应和对象绑定", "issue_type": "quantity", "triggers": ["数量清单"],
                   "applies_when": "同名设备规格和采购量位于不同段落", "rule": "核对采购对象后读取数量清单，不把端口数当采购量。",
                   "exclusions": "不同规格不得直接相加。", "observation": "预测1台与可信标签2台存在明确差异。", "evidence_tags": ["<C2>"]}
        executor = FakeExecutor({"production": output([clause(quantity="1台")]),
                                 "reflection": {"reflection": {"insights": [insight], "no_experience_reason": ""}}})
        job = self.create("learn_labels")
        result = runner.run_job(self.root, job["job_id"], executor)
        self.assertEqual(result["state"], "needs_examples", result.get("error"))
        self.assertEqual(len(result["candidates"]), 1)
        self.assertNotIn("labels", executor.requests[0]["payload"])
        self.assertIn("labels", executor.requests[1]["payload"])
        self.assertEqual(runner.store.list_memories(self.root)["memories"][0]["active_revision"], None)

    def test_local_review_resume_keeps_blind_prediction(self):
        executor = FakeExecutor({"production": output([clause(end="<C3>")]),
                                 "reflection": {"reflection": {"insights": [], "no_experience_reason": "仅为无害边界，无新经验。"}}})
        job = self.create("learn_labels")
        first = runner.run_job(self.root, job["job_id"], executor)
        self.assertEqual(first["state"], "needs_review", first.get("error"))
        self.assertEqual(len(executor.requests), 2)  # 一次盲抽、一次局部judge失败后才人工。
        review = evolution.get_review(self.root, first["review_ids"][0])
        self.assertEqual(review["subject"]["prediction_sha256"], first["comparison"]["prediction_sha256"])
        evolution.resolve_review(self.root, review["review_id"], "harmless", "附带服务文字不影响本设备选型。")
        final = runner.run_job(self.root, job["job_id"], executor)
        self.assertEqual(final["state"], "completed", final.get("error"))
        self.assertEqual(final["prediction"]["result_sha256"], first["prediction"]["result_sha256"])
        self.assertEqual([request["phase"] for request in executor.requests], ["production", "judge", "reflection"])

    def test_local_judge_harmless_avoids_human_review(self):
        def judge(request):
            return {"judgments": [{"difference_id": item["difference_id"], "decision": "harmless", "reason": "增加的服务说明不含其他设备参数。"}
                                  for item in request["payload"]["differences"]]}
        executor = FakeExecutor({"production": output([clause(end="<C3>")]), "judge": judge})
        job = self.create("learn_labels", auto_evolve=False)
        final = runner.run_job(self.root, job["job_id"], executor)
        self.assertEqual(final["state"], "completed", final.get("error"))
        self.assertEqual(final["comparison"]["status"], "passed")
        self.assertEqual(final["review_ids"], [])
        self.assertEqual(final["comparison_judge"]["provenance"]["phase"], "judge")

    def test_budget_is_cumulative_and_resume_reuses_prediction(self):
        runner.save_settings(self.root, {"max_calls": 1})
        executor = FakeExecutor({"reflection": {"reflection": {"insights": [], "no_experience_reason": "无新经验。"}}})
        job = self.create("learn_labels")
        first = runner.run_job(self.root, job["job_id"], executor)
        self.assertEqual(first["state"], "budget_exhausted")
        self.assertEqual(first["usage"]["calls"], 1)
        runner.save_settings(self.root, {"max_calls": 3})
        final = runner.run_job(self.root, job["job_id"], executor)
        self.assertEqual(final["state"], "completed", final.get("error"))
        self.assertEqual(len(executor.requests), 2)

    def test_changed_input_or_policy_fails_before_model(self):
        job, executor = self.create(), FakeExecutor()
        self.source.write_text("[]", encoding="utf-8")
        final = runner.run_job(self.root, job["job_id"], executor)
        self.assertEqual(final["state"], "failed")
        self.assertEqual(len(executor.requests), 0)

    def test_output_repair_is_at_most_one_extra_call(self):
        job = self.create()
        executor = FakeExecutor({"production": "invalid json"})
        result = runner.run_job(self.root, job["job_id"], executor)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(len(executor.requests), 2)

    def test_claim_prevents_concurrent_same_job(self):
        job = self.create()
        with runner._claim(self.root, job["job_id"]):
            with self.assertRaises(ValueError):
                runner.run_job(self.root, job["job_id"], FakeExecutor())

    def test_start_subprocess_without_shell_or_interactive_window(self):
        job = self.create()
        with mock.patch.object(runner.subprocess, "Popen") as popen:
            popen.return_value.pid = 12345
            started = runner.start_job(self.root, job["job_id"])
        self.assertTrue(started["started"])
        args, kwargs = popen.call_args
        self.assertIsInstance(args[0], list)
        self.assertNotIn("shell", kwargs)
        self.assertIn("run", args[0])

    def test_event_parser_uses_actual_text_and_token_events(self):
        events = [
            {"type": "text", "sessionID": "ses_1", "part": {"text": "```json\n" + json.dumps(output()) + "\n```"}},
            {"type": "step_finish", "sessionID": "ses_1", "part": {"tokens": {"input": 10, "output": 4}, "cost": 0.02}}]
        parsed = runner.extract_event_output("\n".join(map(json.dumps, events)))
        self.assertEqual(parsed["session_id"], "ses_1")
        self.assertEqual(parsed["usage"]["input_tokens"], 10)
        with self.assertRaises(ValueError):
            runner.extract_event_output(json.dumps(events[0]) + "\n" + json.dumps({**events[0], "sessionID": "ses_2"}))

    def test_wrapper_executable_resolved_without_shell(self):
        wrapper = self.root / "opencode.ps1"
        wrapper.write_text("irrelevant wrapper", encoding="utf-8")
        self.assertIsNone(runner._exe(str(wrapper)))
        binary = self.root / "node_modules/opencode-ai/bin/opencode.exe"
        binary.parent.mkdir(parents=True)
        binary.write_bytes(b"placeholder")
        self.assertEqual(runner._exe(str(wrapper)), str(binary.resolve()))

    def test_labeled_experiment_seals_events_and_auto_promotes_synthetic_fixture(self):
        """纯合成事件夹具检验封存协议，不代表执行了真实模型或取得业务准确率。"""
        runner.save_settings(self.root, {"max_calls": 30})
        for number, name in ((2, "Router"), (3, "Access Point")):
            source = self.root / (name + ".json")
            source.write_text(json.dumps([{"text": "<C1>" + name}, {"text": "<C2>quantity 2台"}]), encoding="utf-8")
            labels = self.root / (name + "-labels.json")
            labels.write_text(json.dumps([{**clause(), "条款标题": name}], ensure_ascii=False), encoding="utf-8")
            evolution.import_case(self.root, source, labels, "family_" + str(number), "regression")
        insight = {"title": "根据采购对象核对数量", "issue_type": "quantity", "triggers": ["Core Switch", "quantity"],
                   "applies_when": "设备采购数量位于对应清单", "rule": "依据对应采购对象的清单数量核对，不使用端口个数。",
                   "exclusions": "配件数量不等于主设备数量。", "observation": "预测1台与清单2台不符。", "evidence_tags": ["<C2>"]}
        boundary_insight = {**insight, "title": "服务段落与选型参数核对", "issue_type": "boundary",
                            "rule": "完成设备范围后核对相邻段落是否含关键参数；无关培训服务可以不圈入。",
                            "exclusions": "相邻其他设备参数不能合并。"}
        executor = FakeExecutor()
        executor.responses = {
            "production": output([clause(quantity="1台")]),
            "reflection": {"reflection": {"insights": [insight, boundary_insight], "no_experience_reason": ""}},
            "selection": lambda request: {"regression_case_id": request["payload"]["cases"][0]["case_id"],
                                           "counterexample_case_id": request["payload"]["cases"][1]["case_id"],
                                           "development_applicable": True, "regression_applicable": True,
                                           "scope_note": "独立设备直接给出主设备数量，不应把端口数或配件数量当采购量。"}}
        def prediction(request):
            text = request["payload"]["source_document"][0]["text"]
            name = "Core Switch" if "Core Switch" in text else text.replace("<C1>", "")
            quantity = "1台" if request["phase"] == "baseline" and name == "Core Switch" and not request["payload"]["memories"] else "2台"
            return output([{**clause(quantity=quantity), "条款标题": name}])
        executor.responses.update(baseline=prediction, candidate=prediction)
        execute = executor.execute
        def recorded_fixture(request):
            returned = execute(request)
            return {**returned, "kind": "opencode"}  # 协议测试专用合成 OpenCode 事件。
        executor.execute = recorded_fixture
        job = self.create("learn_labels", family_id="family_1")
        final = runner.run_job(self.root, job["job_id"], executor)
        self.assertEqual(final["state"], "completed", final.get("error"))
        self.assertEqual(final["experiments"][0]["status"], "passed")
        self.assertEqual(final["experiments"][0]["summary"]["fix_improvements"], 1)
        self.assertEqual(final["experiments"][1]["status"], "passed")
        self.assertNotEqual(final["experiments"][0]["base_release_sha256"], final["experiments"][1]["base_release_sha256"])
        self.assertEqual(final["memory_snapshot"]["release_sha256"], final["experiments"][0]["base_release_sha256"])
        memories = runner.store.list_memories(self.root)["memories"]
        self.assertEqual(len(memories), 2)
        self.assertTrue(all(memory["active_revision"] == 1 for memory in memories))

    def test_fake_experiment_never_auto_publishes(self):
        # 单个fake生产结果可用于本地流程测试，fake事件不能通过晋级来源核验。
        job = self.create("learn_labels")
        self.assertEqual(job["state"], "queued")
        artifact = {"provenance": {"kind": "fake"}}
        with self.assertRaises(ValueError):
            evolution._check_provenance(artifact, phase="baseline", candidate={}, generation={}, base_release="")

    def test_pure_observation_validated_against_history_with_local_judge(self):
        """合成隔离事件验证流程：原无标签运行无需伪造人工确认。"""
        evolution.import_case(self.root, self.source, self.labels, "family_core", "development")
        for number, name in ((2, "Router"), (3, "Access Point")):
            source = self.root / (name + ".json")
            source.write_text(json.dumps([{"text": "<C1>" + name}, {"text": "<C2>quantity 2台"}]), encoding="utf-8")
            labels = self.root / (name + "-labels.json")
            labels.write_text(json.dumps([{**clause(), "条款标题": name}], ensure_ascii=False), encoding="utf-8")
            evolution.import_case(self.root, source, labels, "family_" + str(number), "regression")
        insight = {"title": "Core Switch 清单数量核验", "issue_type": "quantity", "triggers": ["Core Switch", "quantity"],
                   "applies_when": "Core Switch 规格和清单数量分段描述", "rule": "核对Core Switch采购对象再读取quantity清单；相邻培训说明可保留无害冗余。",
                   "exclusions": "不同对象的数量不直接合并。", "observation": "完成数量核验并检查了相邻服务说明。", "evidence_tags": ["<C2>"]}
        def prediction(request):
            text = request["payload"]["source_document"][0]["text"]
            name = "Core Switch" if "Core Switch" in text else text.replace("<C1>", "")
            return output([{**clause(end="<C3>" if name == "Core Switch" else "<C2>"), "条款标题": name}])
        executor = FakeExecutor({
            "production": output(insights=[insight]), "baseline": prediction, "candidate": prediction,
            "selection": lambda request: {"regression_case_id": request["payload"]["cases"][0]["case_id"],
                                           "counterexample_case_id": request["payload"]["cases"][1]["case_id"],
                                           "development_applicable": True, "regression_applicable": True,
                                           "scope_note": "其他设备独立数量要求，不能与Core Switch数量交叉合并。"},
            "judge": lambda request: {"judgments": [{"difference_id": item["difference_id"], "decision": "harmless", "reason": "额外范围仅为培训说明，不包含其他设备规格。"}
                                                     for item in request["payload"]["differences"]]}})
        execute = executor.execute
        executor.execute = lambda request: {**execute(request), "kind": "opencode"}
        job = self.create("extract")
        final = runner.run_job(self.root, job["job_id"], executor)
        self.assertEqual(final["state"], "completed", final.get("error"))
        self.assertEqual(final["experiments"][0]["status"], "passed")
        self.assertEqual(final["review_ids"], [])
        self.assertEqual(final["comparison"] if "comparison" in final else None, None)
        self.assertNotIn("labels", executor.requests[0]["payload"])
        self.assertNotIn("labels", next(request for request in executor.requests if request["phase"] == "baseline")["payload"])
        self.assertEqual(evolution.list_reviews(self.root)["total"], 0)
        with runner.store._db(self.root) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM learning_confirmations").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
