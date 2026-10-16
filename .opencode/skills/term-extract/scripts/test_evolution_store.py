"""演进协议的临时合成测试；不调用模型、不修改实际经验库。"""
from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import evolution_store as evolution
import memory_store as store


class EvolutionStoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="evolution-store-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"TERM_EXTRACT_MEMORY_DIR": ""})
        environment.start()
        self.addCleanup(environment.stop)
        policy = self.root / ".opencode/skills/term-extract/references/business-rules.md"
        policy.parent.mkdir(parents=True)
        policy.write_text("只抽目标设备；机柜布线不抽；纯品牌准入不独立作为 general。", encoding="utf-8")

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def case(self, name="a", split="development"):
        source = self.write(name + "-input.json", [{"text": f"<C1> {name} Switch 2台 GE端口"}])
        labels = self.write(name + "-labels.json", [{"id": 0, "条款标题": "Switch", "条款分类": "normal",
                                                   "相关条款": [], "产品分类": "盒式交换机", "定位标识": ["<C1>", "<C1>"], "产品数量": "2台"}])
        return evolution.import_case(self.root, source, labels, "family-" + name, split)

    def payload(self, source):
        return {"title": "数量需核对清单", "issue_type": "quantity", "triggers": ["数量", "清单"],
                "applies_when": "正文无数量且清单明确对应", "rule": "以采购清单的设备数量为准。",
                "exclusions": "不能把端口数量视为设备数量。", "correction_ids": [], "source_ids": [source]}

    def test_empty_read_does_not_initialize(self):
        self.assertEqual(evolution.list_cases(self.root)["items"], [])
        self.assertEqual(evolution.list_reviews(self.root)["items"], [])
        self.assertFalse(store.resolve_store_dir(self.root).exists())

    def test_trusted_import_is_idempotent_without_forged_confirmation(self):
        case = self.case()
        replay = evolution.import_case(self.root, Path(case["input_path"]), Path(case["labels_path"]), case["family_id"])
        self.assertTrue(replay["replayed"])
        self.assertEqual(case["case_id"], replay["case_id"])
        self.assertEqual(case["trust"], "user_provided_label")
        self.assertNotIn("confirmed_by", case)
        self.assertNotIn("labels_json", evolution.get_case(self.root, case["case_id"]))
        self.assertIsInstance(evolution.get_case(self.root, case["case_id"], True)["labels_json"], list)
        with store._db(self.root) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM learning_confirmations").fetchone()[0], 0)

    def test_case_input_family_or_split_cannot_drift(self):
        case = self.case()
        with self.assertRaisesRegex(store.MemoryError, "family/split"):
            evolution.import_case(self.root, Path(case["input_path"]), Path(case["labels_path"]), "other", "regression")
        with self.assertRaisesRegex(store.MemoryError, "family/split"):
            evolution.import_case(self.root, Path(case["input_path"]), Path(case["labels_path"]), case["family_id"], "holdout")

    def test_invalid_labels_not_registered_and_changed_files_fail(self):
        case = self.case()
        Path(case["labels_archive_path"]).write_text("[]", encoding="utf-8")
        with self.assertRaisesRegex(store.MemoryError, "依据已变化"):
            evolution.validate_case(self.root, case["case_id"])
        self.assertFalse(evolution.list_cases(self.root)["items"][0]["valid"])

    def test_policy_reconfirmation_is_a_new_case_version(self):
        case = self.case()
        policy = self.root / ".opencode/skills/term-extract/references/business-rules.md"
        policy.write_text("新的已确认业务口径", encoding="utf-8")
        self.assertFalse(evolution.validate_case(self.root, case["case_id"])["policy_current"])
        newer = evolution.import_case(self.root, Path(case["input_path"]), Path(case["labels_path"]), case["family_id"])
        self.assertNotEqual(newer["case_id"], case["case_id"])

    def test_review_local_resolution_shared_by_channels_and_immutable(self):
        review = evolution.create_review(self.root, kind="quantity", title="是否同一设备？", question="两行是否为同型设备？",
                                         context={"tags": ["<C1>"]}, choices=["合并", "分别"], subject={"case_id": "case_demo"})
        resolved = evolution.resolve_review(self.root, review["review_id"], "合并", "这两行属于同型设备", "测试用户", "conversation", "user-message-1")
        self.assertEqual(resolved["state"], "resolved")
        self.assertEqual(resolved["scope"], "case")
        self.assertEqual(resolved["resolution"]["channel"], "conversation")
        self.assertIn("不确认整篇", resolved["notice"])
        repeated = evolution.resolve_review(self.root, review["review_id"], "合并", "这两行属于同型设备", "测试用户", "conversation", "user-message-1")
        self.assertTrue(repeated["replayed"])
        with self.assertRaisesRegex(store.MemoryError, "不同回复"):
            evolution.resolve_review(self.root, review["review_id"], "分别")
        self.assertEqual(evolution.list_reviews(self.root)["total"], 0)

    def test_review_dedupe_does_not_overwrite_question(self):
        data = dict(kind="boundary", title="边界", question="是否同一对象？", context={}, choices=["是", "否"], subject={}, dedupe_key="test-key")
        first = evolution.create_review(self.root, **data)
        self.assertEqual(evolution.create_review(self.root, **data)["review_id"], first["review_id"])
        with self.assertRaisesRegex(store.MemoryError, "禁止覆盖"):
            evolution.create_review(self.root, **{**data, "question": "另一个问题？"})

    def test_candidate_merges_sources_into_new_immutable_revision(self):
        first, second = self.case("a"), self.case("b")
        one = evolution.propose_candidate(self.root, self.payload(first["case_id"]))
        two = evolution.propose_candidate(self.root, self.payload(second["case_id"]))
        self.assertEqual(one["memory_id"], two["memory_id"])
        self.assertEqual(two["revision"], 2)
        original = store.load_candidate(self.root, one["memory_id"], 1)
        latest = store.load_candidate(self.root, one["memory_id"], 2)
        self.assertEqual(original["payload"]["source_ids"], [first["case_id"]])
        self.assertEqual(latest["payload"]["source_ids"], [first["case_id"], second["case_id"]])
        three = evolution.propose_candidate(self.root, self.payload(second["case_id"]))
        self.assertEqual(three["revision"], 2)

    def test_holdout_and_regression_not_allowed_as_derivation_source(self):
        for name, split in (("r", "regression"), ("h", "holdout")):
            case = self.case(name, split)
            with self.assertRaisesRegex(store.MemoryError, "归纳来源"):
                evolution.propose_candidate(self.root, self.payload(case["case_id"]))

    def test_conflicts_suspected_only_and_do_not_remove_active_or_claim_proof(self):
        source = self.case()
        payload = self.payload(source["case_id"])
        payload.update(title="布线处理", rule="布线交织时应抽取为 separate。")
        candidate = evolution.propose_candidate(self.root, payload)
        scan = candidate["conflict_scan"]
        self.assertEqual(len(scan["created"]), 1)
        self.assertIn("不是矛盾证明", scan["findings"][0]["reason"])
        self.assertEqual(store.load_candidate(self.root, candidate["memory_id"], 1)["state"], "needs_review")
        self.assertTrue(candidate["conflict_scan"]["blocking"])
        self.assertEqual(evolution.scan_conflicts(self.root)["created"], [])
        self.assertEqual(evolution.list_reviews(self.root)["total"], 1)
        safe = self.payload(source["case_id"])
        safe.update(title="排除布线", rule="布线不得抽取，不生成 separate。")
        safe_candidate = evolution.propose_candidate(self.root, safe)
        self.assertFalse(any(item["memory_id"] == safe_candidate["memory_id"] and item["kind"] == "policy_scope" for item in safe_candidate["conflict_scan"]["findings"]))

    def test_rejected_state_is_visible_and_legacy_still_needs_confirmation(self):
        case = self.case()
        candidate = evolution.propose_candidate(self.root, self.payload(case["case_id"]))
        evolution.record_decision(self.root, candidate["memory_id"], 1, "rejected", "案例不支持此推广")
        self.assertEqual(store.load_candidate(self.root, candidate["memory_id"], 1)["state"], "rejected")
        report = {"evaluation_mode": "paired_labeled_v1"}
        with self.assertRaisesRegex(store.MemoryError, "内部运行器"):
            store.register_evaluation(self.root, candidate["memory_id"], 1, report)

    def test_forged_or_fake_experiment_cannot_auto_publish(self):
        case = self.case()
        candidate = evolution.propose_candidate(self.root, self.payload(case["case_id"]))
        record = {"schema_version": 1, "base_release_sha256": store.release_digest(self.root),
                  "policy_sha256": evolution.policy_digest(self.root),
                  "generation": {"model_id": "synthetic", "variant": "default", "runner_version": "test"},
                  "development_source_ids": [case["case_id"]],
                  "cases": [{"case_id": case["case_id"], "role": "development", "baseline": {"provenance": {"kind": "fake"}}, "candidate": {}}]}
        with self.assertRaisesRegex(store.MemoryError, "真实 OpenCode"):
            evolution.register_experiment(self.root, candidate["memory_id"], 1, record)
        self.assertEqual(store.list_memories(self.root)["memories"][0]["active_revision"], None)

    def experiment(self):
        cases = [self.case("dev"), self.case("reg", "regression"), self.case("counter", "regression")]
        candidate = evolution.propose_candidate(self.root, self.payload(cases[0]["case_id"]))
        record = {"schema_version": 1, "base_release_sha256": store.release_digest(self.root),
                  "policy_sha256": evolution.policy_digest(self.root),
                  "generation": {"model_id": "synthetic", "variant": "default", "runner_version": "test"},
                  "development_source_ids": [cases[0]["case_id"]], "cases": [], "artifacts": []}
        for case, role in zip(cases, ("development", "regression", "counterexample")):
            gold = evolution.get_case(self.root, case["case_id"], True)["labels_json"]
            before = copy.deepcopy(gold)
            if role == "development":
                before[0]["产品数量"] = "1台"
            baseline = self.write(role + "/before.json", before)
            result = self.write(role + "/candidate.json", gold)
            record["cases"].append({"case_id": case["case_id"], "role": role,
                                    "scope_note": "端口数量不是设备数量，检查未误合并独立采购项。",
                                    "baseline": {"result_path": str(baseline), "result_sha256": store._file_hash(baseline)},
                                    "candidate": {"result_path": str(result), "result_sha256": store._file_hash(result)},
                                    "baseline_comparison": {"status": "passed"}, "candidate_comparison": {"status": "passed"}})
        return candidate, record

    @staticmethod
    def synthetic_provenance(artifact, **kwargs):
        # 单元测试隔离真实CLI；质量评估、归档绑定和晋级仍调用实际实现。
        path = Path(artifact["result_path"])
        return store._read_json(path), [{"path": str(path), "sha256": store._file_hash(path)}]

    def test_real_comparison_recomputed_and_sealed_report_publishes_once(self):
        candidate, record = self.experiment()
        with patch.object(evolution, "_check_provenance", side_effect=self.synthetic_provenance):
            evaluated = evolution.register_experiment(self.root, candidate["memory_id"], 1, record)
        self.assertEqual(evaluated["status"], "passed")
        self.assertEqual(evaluated["summary"]["fix_improvements"], 1)
        self.assertEqual(evaluated["summary"]["success_cases"], 1)
        released = evolution.auto_promote(self.root, evaluated["evaluation_id"])
        self.assertEqual(released["state"], "published")
        with store._db(self.root) as connection:
            event = connection.execute("SELECT record_json FROM audit WHERE action='publish'").fetchone()[0]
        self.assertEqual(json.loads(event)["reviewer"], "system:auto-evolution")
        with self.assertRaises(store.MemoryError):
            evolution.auto_promote(self.root, evaluated["evaluation_id"])

    def test_forged_passed_summary_and_extra_object_cannot_bypass_quality(self):
        candidate, record = self.experiment()
        path = Path(record["cases"][0]["candidate"]["result_path"])
        result = store._read_json(path)
        result[0]["产品数量"] = "999台"
        self.write(str(path.relative_to(self.root)), result)
        record["cases"][0]["candidate"]["result_sha256"] = store._file_hash(path)
        record["summary"] = {"regressions": 0, "unfixed_target_cases": 0}
        with patch.object(evolution, "_check_provenance", side_effect=self.synthetic_provenance):
            evaluated = evolution.register_experiment(self.root, candidate["memory_id"], 1, record)
        self.assertEqual(evaluated["status"], "failed")
        self.assertEqual(evaluated["summary"]["unfixed_target_cases"], 1)
        with self.assertRaisesRegex(store.MemoryError, "发布门槛"):
            evolution.auto_promote(self.root, evaluated["evaluation_id"])

    def test_missing_independent_coverage_or_changed_policy_prevents_publication(self):
        candidate, record = self.experiment()
        record["cases"] = record["cases"][:1]
        with patch.object(evolution, "_check_provenance", side_effect=self.synthetic_provenance):
            evaluated = evolution.register_experiment(self.root, candidate["memory_id"], 1, record)
        self.assertEqual(evaluated["status"], "failed")
        self.assertTrue(any(item["code"] == "coverage_insufficient" for item in evaluated["blockers"]))
        policy = self.root / ".opencode/skills/term-extract/references/business-rules.md"
        policy.write_text("业务口径已经变更", encoding="utf-8")
        with self.assertRaises(store.MemoryError):
            evolution.auto_promote(self.root, evaluated["evaluation_id"])

    def test_event_result_session_prompt_and_candidate_binding_are_checked(self):
        case = self.case()
        info = evolution.propose_candidate(self.root, self.payload(case["case_id"]))
        candidate = store.load_candidate(self.root, info["memory_id"], 1)
        labels = evolution.get_case(self.root, case["case_id"], True)["labels_json"]
        index = store._read_json(Path(case["index_path"]))
        generation = {"model_id": "synthetic-test", "variant": "", "runner_version": "test"}
        result_path = self.write("event-test/result.json", labels)
        prompt_path = self.write("event-test/prompt.json", {"source_document": index["source_document"], "memories": [], "references": {"business-rules.md": "policy"}})
        event_path = self.root / "event-test/events.jsonl"
        event = {"type": "text", "sessionID": "ses_synthetic_fixture", "part": {"text": json.dumps({"result": labels}, ensure_ascii=False)}}
        event_path.write_text(json.dumps(event, ensure_ascii=False) + "\n", encoding="utf-8")
        provenance = {"kind": "opencode", "session_id": "ses_synthetic_fixture", "phase": "baseline", "blind": True,
                      "model_id": "synthetic-test", "variant": "", "candidate_ids": [], "base_release_sha256": store.release_digest(self.root)}
        for prefix, path in (("events", event_path), ("prompt", prompt_path), ("result", result_path)):
            provenance[prefix + "_path"] = str(path)
            provenance[prefix + "_sha256"] = store._file_hash(path)
        artifact = {"result_path": str(result_path), "result_sha256": store._file_hash(result_path), "provenance": provenance}
        checked, _ = evolution._check_provenance(artifact, phase="baseline", candidate=candidate, generation=generation,
                                               base_release=store.release_digest(self.root), expected_source=index["source_document"], expected_memories=[], expected_policy="policy")
        self.assertEqual(checked, labels)
        # 即使攻击者重新计算事件文件hash，也不能用另一个结果事件替代输出。
        event["part"]["text"] = json.dumps({"result": []})
        event_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        provenance["events_sha256"] = store._file_hash(event_path)
        with self.assertRaisesRegex(store.MemoryError, "事件中重现"):
            evolution._check_provenance(artifact, phase="baseline", candidate=candidate, generation=generation, base_release=store.release_digest(self.root))

    def test_new_revision_blocks_old_auto_experiment_and_merge_preserves_sources(self):
        candidate, record = self.experiment()
        with patch.object(evolution, "_check_provenance", side_effect=self.synthetic_provenance):
            evaluated = evolution.register_experiment(self.root, candidate["memory_id"], 1, record)
        source = self.case("another")
        newer = evolution.propose_candidate(self.root, self.payload(source["case_id"]))
        self.assertEqual(newer["revision"], 2)
        with self.assertRaisesRegex(store.MemoryError, "已有新修订"):
            evolution.auto_promote(self.root, evaluated["evaluation_id"])
        different = self.payload(source["case_id"])
        different["rule"] = "另一条核对数量的方法。"
        other = evolution.propose_candidate(self.root, different)
        decision = evolution.record_decision(self.root, other["memory_id"], 1, "merged", "人工明确认为同一方法", target_memory_id=candidate["memory_id"])
        self.assertEqual(decision["target_revision"], 2)
        self.assertEqual(store.load_candidate(self.root, other["memory_id"], 1)["state"], "merged")

    def test_conflict_scan_due_and_change_trigger(self):
        case = self.case()
        candidate = evolution.propose_candidate(self.root, self.payload(case["case_id"]))
        self.assertTrue(candidate["conflict_scan"]["due"])
        cached = evolution.scan_conflicts(self.root)
        self.assertFalse(cached["due"])
        self.assertTrue(evolution.scan_conflicts(self.root, force=True)["due"])
        policy = self.root / ".opencode/skills/term-extract/references/business-rules.md"
        policy.write_text("新业务口径", encoding="utf-8")
        self.assertTrue(evolution.scan_conflicts(self.root)["due"])

    def test_conflict_answers_update_local_candidate_gate(self):
        source = self.case()
        payload = self.payload(source["case_id"])
        payload.update(title="品牌一般要求", rule="将 Gartner 品牌资质抽取为 general。")
        candidate = evolution.propose_candidate(self.root, payload)
        status = evolution.candidate_review_status(self.root, candidate["memory_id"], 1)
        self.assertTrue(status["blocking"])
        self.assertEqual(status["open_conflicts"], 1)
        review_id = status["reviews"][0]["review_id"]
        evolution.resolve_review(self.root, review_id, "保留，条件不同", "这里实际还包括选型功能条件")
        self.assertFalse(evolution.candidate_review_status(self.root, candidate["memory_id"], 1)["blocking"])
        self.assertEqual(store.load_candidate(self.root, candidate["memory_id"], 1)["state"], "candidate")
        other = self.payload(source["case_id"])
        other.update(title="布线处理", rule="应提取布线为 separate。")
        second = evolution.propose_candidate(self.root, other)
        second_review = evolution.candidate_review_status(self.root, second["memory_id"], 1)["reviews"][0]
        evolution.resolve_review(self.root, second_review["review_id"], "不采纳", "不在设备范围")
        self.assertEqual(store.load_candidate(self.root, second["memory_id"], 1)["state"], "rejected")
        self.assertTrue(evolution.candidate_review_status(self.root, second["memory_id"], 1)["blocking"])

    def test_complete_event_backed_protocol_and_tampered_prompt(self):
        candidate_info, record = self.experiment()
        candidate = store.load_candidate(self.root, candidate_info["memory_id"], 1)
        entry = {key: candidate[key] for key in ("memory_id", "revision", "content_sha256", "payload")}
        policy = (self.root / ".opencode/skills/term-extract/references/business-rules.md").read_text(encoding="utf-8")
        for position, pair in enumerate(record["cases"]):
            case = evolution.get_case(self.root, pair["case_id"])
            index = store._read_json(Path(case["index_path"]))
            for phase in ("baseline", "candidate"):
                result = store._read_json(Path(pair[phase]["result_path"]))
                prompt_path = self.write(f"provenance-{position}-{phase}/prompt.json", {"source_document": index["source_document"], "memories": [entry] if phase == "candidate" else [], "references": {"business-rules.md": policy}})
                session = f"ses_synthetic_{position}_{phase}"
                event_path = self.root / f"provenance-{position}-{phase}/events.jsonl"
                event_path.write_text(json.dumps({"type": "text", "sessionID": session, "part": {"text": json.dumps({"result": result}, ensure_ascii=False)}}, ensure_ascii=False) + "\n", encoding="utf-8")
                provenance = {"kind": "opencode", "phase": phase, "blind": True, "session_id": session,
                              "model_id": "synthetic", "variant": "default", "base_release_sha256": record["base_release_sha256"],
                              "candidate_ids": [{key: entry[key] for key in ("memory_id", "revision", "content_sha256")}] if phase == "candidate" else []}
                for prefix, path in (("events", event_path), ("prompt", prompt_path), ("result", Path(pair[phase]["result_path"]))):
                    provenance[prefix + "_path"] = str(path)
                    provenance[prefix + "_sha256"] = store._file_hash(path)
                pair[phase]["provenance"] = provenance
        # 测试夹具仅模拟原始事件，未启动模型；注册函数按生产路径重算全部绑定和质量。
        evaluated = evolution.register_experiment(self.root, candidate_info["memory_id"], 1, record)
        self.assertEqual(evaluated["status"], "passed")
        provenance = record["cases"][1]["candidate"]["provenance"]
        prompt_path = Path(provenance["prompt_path"])
        prompt = store._read_json(prompt_path)
        prompt["source_document"] = [{"text": "<C1> 另一个输入"}]
        self.write(str(prompt_path.relative_to(self.root)), prompt)
        provenance["prompt_sha256"] = store._file_hash(prompt_path)
        with self.assertRaisesRegex(store.MemoryError, "完整原文"):
            evolution.register_experiment(self.root, candidate_info["memory_id"], 1, record)
        with self.assertRaisesRegex(store.MemoryError, "依据文件已变化"):
            evolution.auto_promote(self.root, evaluated["evaluation_id"])


if __name__ == "__main__":
    unittest.main()
