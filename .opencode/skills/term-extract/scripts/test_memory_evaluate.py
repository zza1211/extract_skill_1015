"""仅用临时合成工件测试离线断言和发布门槛，不调用任何模型。"""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import document_io
import memory_evaluate as evaluation


def record(cid=0, start="<C90>", end="<C3>", category="normal", links=None, quantity="2"):
    return {"id": cid, "条款标题": "相同标题也不用于对象匹配", "条款分类": category,
            "相关条款": links or [], "产品分类": "仅供参考的任意分类", "定位标识": [start, end], "产品数量": quantity}


class FakeStore:
    def __init__(self, correction):
        self.corrections = {"corr-dev": correction}
        self.candidate = {"memory_id": "mem-test", "revision": 1, "content_sha256": "a" * 64,
                          "state": "candidate", "payload": {"correction_ids": ["corr-dev"]}}
        self.reports = []
        self.load_count = 0
        self.drift = False

    def load_candidate(self, root, memory_id, revision):
        self.load_count += 1
        value = copy.deepcopy(self.candidate)
        if self.drift and self.load_count > 1:
            value["content_sha256"] = "c" * 64
        return value

    def get_correction(self, root, cid):
        return copy.deepcopy(self.corrections[cid])

    def release_digest(self, root):
        return "b" * 64

    def register_evaluation(self, root, memory_id, revision, report):
        self.reports.append(copy.deepcopy(report))
        sha = hashlib.sha256(json.dumps(report, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return {"evaluation_id": f"eval-{len(self.reports)}", "report_sha256": sha}


class FakeLearning:
    def __init__(self, sources):
        self.sources = sources
        self.calls = 0
        self.drift = False

    def evaluation_sources(self, root, payload):
        self.calls += 1
        result = copy.deepcopy(self.sources)
        if self.drift and self.calls > 1:
            result[-1]["confirmation"]["note"] += "（已变更）"
            result[-1]["confirmation_sha256"] = hashlib.sha256(json.dumps(result[-1]["confirmation"],
                sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        return result


class MemoryEvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manifest_path = self.root / "evaluation.json"
        self.cases = []
        for name, family, role, split in [("fix", "dev-a", "fix", "development"),
                                          ("reg", "ind-b", "regression", "regression"),
                                          ("counter", "ind-c", "counterexample", "regression")]:
            self.cases.append(self.make_case(name, family, role, split))
        fix_index = json.loads((self.root / self.cases[0]["index_path"]).read_text(encoding="utf-8"))
        self.store = FakeStore({"correction_id": "corr-dev", "source_sha256": fix_index["source_sha256"],
                                "family_id": "dev-a", "input_path": fix_index["source_path"]})
        self.learning = None
        self.write_manifest()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def make_case(self, name, family, role, split):
        folder = self.root / name
        source = [{"text": f"<C90>{family} 主体<C3>关键参数<C50>无害冗余<C8>另一设备<C2>通用参数<C70>分散参数<C1>其他通用"}]
        self.write(folder / "source.json", source)
        prepared = document_io.prepare(folder / "source.json", folder / "prepared")
        before = [record(end="<C90>" if role == "fix" else "<C3>")]
        after = [record(end="<C50>" if role == "fix" else "<C3>")]
        self.write(folder / "baseline.json", before)
        self.write(folder / "candidate.json", after)
        case = {"case_id": name, "family_id": family, "role": role, "split": split, "confirmed_by": "人工测试标注",
                "index_path": str(Path(prepared["index_path"]).relative_to(self.root)),
                "baseline_result_path": str((folder / "baseline.json").relative_to(self.root)),
                "candidate_result_path": str((folder / "candidate.json").relative_to(self.root)),
                "assertions": [{"object_key": "device-a", "anchor_tag": "<C90>", "required_tags": ["<C90>", "<C3>"],
                                "forbidden_tags": ["<C8>"], "quantity": "2", "clause_category": "normal"}]}
        if role == "counterexample":
            case["scope_note"] = "独立采购项，经验不能把相邻另一设备的要求合进来。"
        return case

    def write_manifest(self, value=None):
        self.write(self.manifest_path, value if value is not None else {"schema_version": 1, "cases": self.cases})

    def run_evaluation(self):
        self.write_manifest()
        return evaluation.evaluate_manifest(self.root, "mem-test", 1, self.manifest_path,
                                            store=self.store, learning=self.learning)

    def configure_success(self, mixed=False):
        if mixed:
            case = self.make_case("success", "dev-success", "success", "development")
            self.cases.append(case)
            legacy = self.store.get_correction(self.root, "corr-dev")
            sources = [{**legacy, "source_id": "corr-dev", "correction_id": "corr-dev", "kind": "correction"}]
        else:
            case = self.cases[0]
            case["role"] = "success"
            self.write(self.root / case["baseline_result_path"], [record()])
            self.store.candidate["payload"]["correction_ids"] = []
            sources = []
        index = json.loads((self.root / case["index_path"]).read_text(encoding="utf-8"))
        result_path = self.root / "immutable-success.json"
        self.write(result_path, [record()])
        confirmation = {"confirmed_by": "人工验收", "family_id": case["family_id"], "note": "该次抽取正确且可复用"}
        confirmation_path = self.root / "immutable-confirmation.json"
        self.write(confirmation_path, confirmation)
        sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
        confirmation_sha = hashlib.sha256(json.dumps(confirmation, sort_keys=True, ensure_ascii=False,
                                                      separators=(",", ":")).encode()).hexdigest()
        source = {"source_id": "source-success", "kind": "success", "confirmed": True, "family_id": case["family_id"],
                  "source_sha256": index["source_sha256"], "input_path": index["source_path"],
                  "index_path": str(self.root / case["index_path"]), "result_path": str(result_path),
                  "result_sha256": sha(result_path), "confirmation": confirmation,
                  "confirmation_sha256": confirmation_sha,
                  "artifacts": [{"path": str(path), "sha256": sha(path)} for path in (result_path, confirmation_path)]}
        self.store.candidate["payload"]["source_ids"] = [source["source_id"]]
        self.learning = FakeLearning(sources + [source])
        return source, case

    def candidate_output(self, case_number, records):
        self.write(self.root / self.cases[case_number]["candidate_result_path"], records)

    def use_merged_case(self, case_number):
        """真实 prepare 合并单元格；三个角色仍使用各自独立的完整输入。"""
        case = self.cases[case_number]
        folder = (self.root / case["index_path"]).parent.parent
        anchor = f"<C90>{case['family_id']} 主体2台"
        source = [{"table": {
            "rows": [[anchor, "<C3>关键参数", "<C50>无害冗余"],
                     [anchor, "<C8>另一设备", "<C2>通用参数"],
                     ["<C70>分散参数", "<C1>其他通用"]],
            "merges": [{"top_left": {"row": 0, "col": 0}, "row_span": 2,
                        "col_span": 1, "value": anchor}],
        }}]
        self.write(folder / "source.json", source)
        prepared = document_io.prepare(folder / "source.json", folder / "prepared")
        index = json.loads(Path(prepared["index_path"]).read_text(encoding="utf-8"))
        if case_number == 0:
            self.store.corrections["corr-dev"].update(source_sha256=index["source_sha256"],
                                                       input_path=index["source_path"])
        return index

    def rewrite_prepared_index(self, index, case_number=0):
        """同时刷新外层 SHA，以验证别名证明本身，而非仅检测文件哈希变化。"""
        path = self.root / self.cases[case_number]["index_path"]
        self.write(path, index)
        manifest_path = path.parent / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["index_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        self.write(manifest_path, manifest)

    def test_merged_cell_aliases_pass_three_case_roles_without_duplicate_tag_units(self):
        for number in range(3):
            index = self.use_merged_case(number)
            self.assertEqual(index["merged_cell_alias_policy"], "exact_merged_cell_v1")
            self.assertEqual(len(index["units"]), 7)
            self.assertEqual(index["tables"][0]["cell_unit_ids"][1][0],
                             index["tables"][0]["cell_unit_ids"][0][0])
        report = self.run_evaluation()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["summary"]["fix_improvements"], 1)
        self.assertTrue(all(case["candidate_validation"]["ok"] for case in report["cases"]))

    def test_merged_alias_does_not_move_endpoint_or_absorb_other_device(self):
        self.use_merged_case(0)
        self.candidate_output(0, [record(end="<C90>")])
        report = self.run_evaluation()
        errors = report["cases"][0]["assertions"][0]["candidate"]["errors"]
        self.assertEqual(errors[0]["code"], "REQUIRED_TAG_MISSING")
        self.assertIn("<C3>", errors[0]["tags"])
        self.candidate_output(0, [record(end="<C8>")])
        report = self.run_evaluation()
        errors = report["cases"][0]["assertions"][0]["candidate"]["errors"]
        self.assertIn("FORBIDDEN_TAG_INCLUDED", {error["code"] for error in errors})
        self.assertEqual(report["status"], "failed")

    def test_reject_forged_missing_or_drifted_merged_alias_metadata(self):
        original = self.use_merged_case(0)
        for mutation in ("missing", "empty", "policy", "coordinate", "bool", "duplicate", "extra", "mapping"):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(original)
                if mutation == "missing":
                    changed.pop("merged_cell_aliases")
                    changed.pop("merged_cell_alias_policy")
                elif mutation == "empty":
                    changed["merged_cell_aliases"] = []
                elif mutation == "policy":
                    changed["merged_cell_alias_policy"] = "accept_all_repeated_tags"
                elif mutation == "coordinate":
                    changed["merged_cell_aliases"][0]["col"] = 1
                elif mutation == "bool":
                    changed["merged_cell_aliases"][0]["anchor_row"] = False
                elif mutation == "duplicate":
                    changed["merged_cell_aliases"].append(copy.deepcopy(changed["merged_cell_aliases"][0]))
                elif mutation == "extra":
                    changed["merged_cell_aliases"][0]["unverified"] = True
                else:
                    changed["tables"][0]["cell_unit_ids"][1][0] = changed["tables"][0]["cell_unit_ids"][1][1]
                self.rewrite_prepared_index(changed)
                with self.assertRaises(evaluation.EvaluationError):
                    self.run_evaluation()
                self.assertEqual(self.store.reports, [])

    def test_reject_alias_claim_without_merge_evidence(self):
        path = self.root / self.cases[0]["index_path"]
        index = json.loads(path.read_text(encoding="utf-8"))
        index["merged_cell_alias_policy"] = "exact_merged_cell_v1"
        index["merged_cell_aliases"] = [{"block_index": 0, "row": 1, "col": 0,
                                        "anchor_row": 0, "anchor_col": 0, "merge_index": 0}]
        self.rewrite_prepared_index(index)
        with self.assertRaises(evaluation.EvaluationError):
            self.run_evaluation()
        self.assertEqual(self.store.reports, [])

    def test_merged_alias_cannot_hide_an_unrelated_tag_unit(self):
        index = self.use_merged_case(0)
        index["units"] = [unit for unit in index["units"] if unit["tag"] != "<C8>"]
        index["tag_to_unit"].pop("<C8>")
        for number, unit in enumerate(index["units"]):
            unit["order"] = number
        self.rewrite_prepared_index(index)
        with self.assertRaises(evaluation.EvaluationError):
            self.run_evaluation()
        self.assertEqual(self.store.reports, [])

    def test_passes_key_parameter_assertions_with_legal_redundancy(self):
        report = self.run_evaluation()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["summary"], {"fix_improvements": 1, "success_cases": 0, "regressions": 0,
                         "independent_regression_cases": 2, "counterexample_cases": 1, "unfixed_target_cases": 0})
        self.assertEqual(report["evaluation_mode"], "correction")
        self.assertEqual(len(self.store.reports), 1)
        self.assertNotIn("evaluation_id", self.store.reports[0])
        self.assertIn("不证明", report["notice"])
        self.assertTrue(all(Path(item["path"]).is_absolute() for item in report["artifacts"]))
        self.assertIn(str(self.manifest_path.resolve()), {item["path"] for item in report["artifacts"]})
        for case in report["cases"]:
            self.assertTrue(case["original_source_verified"])
            self.assertTrue(case["candidate_passed"])

    def test_missing_product_fails_instead_of_passing_empty_result(self):
        self.candidate_output(0, [])
        report = self.run_evaluation()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["summary"]["unfixed_target_cases"], 1)
        self.assertEqual(report["cases"][0]["assertions"][0]["candidate"]["errors"][0]["code"], "MISSING_OBJECT")

    def test_key_parameter_missing_and_other_device_pollution_fail(self):
        for end, code in [("<C90>", "REQUIRED_TAG_MISSING"), ("<C8>", "FORBIDDEN_TAG_INCLUDED")]:
            with self.subTest(end=end):
                self.candidate_output(0, [record(end=end)])
                report = self.run_evaluation()
                self.assertEqual(report["status"], "failed")
                self.assertIn(code, [error["code"] for error in report["cases"][0]["assertions"][0]["candidate"]["errors"]])

    def test_quantity_regression_is_checked_on_subject_without_summing(self):
        self.candidate_output(1, [record(quantity="3")])
        report = self.run_evaluation()
        self.assertEqual(report["summary"]["regressions"], 1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["cases"][1]["assertions"][0]["candidate"]["errors"][0]["code"], "QUANTITY_MISMATCH")
        self.candidate_output(1, [record(), record(1, "<C70>", "<C70>", "separate", [0], "2")])
        self.cases[1]["assertions"][0]["required_tags"].append("<C70>")
        report = self.run_evaluation()
        self.assertTrue(report["cases"][1]["candidate_passed"])
        self.assertEqual(report["cases"][1]["assertions"][0]["candidate"]["object_clause_ids"], [0, 1])

    def test_general_is_direct_incoming_only_not_a_route_to_other_product(self):
        assertion = self.cases[0]["assertions"][0]
        assertion["required_tags"] = ["<C90>", "<C3>", "<C70>", "<C2>"]
        assertion["forbidden_tags"] = ["<C8>", "<C1>", "<C50>"]
        candidate = [record(), record(1, "<C8>", "<C8>"),
                     record(2, "<C70>", "<C70>", "separate", [0]),
                     record(3, "<C2>", "<C2>", "general", [2]),
                     record(4, "<C1>", "<C1>", "general", [1]),
                     record(5, "<C50>", "<C50>", "general", [3])]
        self.candidate_output(0, candidate)
        report = self.run_evaluation()
        judged = report["cases"][0]["assertions"][0]["candidate"]
        self.assertTrue(judged["passed"])
        self.assertEqual(judged["object_clause_ids"], [0, 2])
        self.assertEqual(judged["general_clause_ids"], [3])
        candidate[3]["相关条款"] = [1]
        self.candidate_output(0, candidate)
        report = self.run_evaluation()
        judged = report["cases"][0]["assertions"][0]["candidate"]
        self.assertFalse(judged["passed"])
        self.assertIn("<C2>", judged["errors"][0]["tags"])

    def test_separate_chain_is_connected_and_general_does_not_join_objects(self):
        assertion = self.cases[0]["assertions"][0]
        assertion["required_tags"] = ["<C90>", "<C70>", "<C2>"]
        candidate = [record(end="<C90>"), record(1, "<C8>", "<C8>"),
                     record(2, "<C70>", "<C70>", "separate", [0, 3]),
                     record(3, "<C2>", "<C2>", "separate", [2]),
                     record(4, "<C1>", "<C1>", "general", [0, 1])]
        self.candidate_output(0, candidate)
        report = self.run_evaluation()
        judged = report["cases"][0]["assertions"][0]["candidate"]
        self.assertTrue(judged["passed"])
        self.assertEqual(judged["object_clause_ids"], [0, 2, 3])
        self.assertEqual(judged["general_clause_ids"], [4])

    def test_anchor_ambiguity_is_failure_even_when_union_has_required_tags(self):
        self.candidate_output(0, [record(end="<C90>"), record(1, "<C90>", "<C3>", "separate", [0])])
        report = self.run_evaluation()
        error = report["cases"][0]["assertions"][0]["candidate"]["errors"][0]
        self.assertEqual(error["code"], "AMBIGUOUS_OBJECT")
        self.assertEqual(report["status"], "failed")

    def test_required_tags_use_index_order_not_numeric_order(self):
        report = self.run_evaluation()
        judged = report["cases"][0]["assertions"][0]["candidate"]
        self.assertTrue(judged["passed"])  # C90 -> C3 -> C50 in source, C8 lies later.
        self.candidate_output(0, [record(start="<C3>", end="<C90>")])
        report = self.run_evaluation()
        self.assertFalse(report["cases"][0]["candidate_validation"]["ok"])
        self.assertEqual(report["status"], "failed")

    def test_reject_duplicate_case_cross_family_and_holdout(self):
        for mutation in ("case", "family", "source", "holdout", "final"):
            with self.subTest(mutation=mutation):
                saved = copy.deepcopy(self.cases)
                if mutation == "case":
                    self.cases[1]["case_id"] = self.cases[0]["case_id"]
                elif mutation == "family":
                    self.cases[1]["family_id"] = "dev-a"
                elif mutation == "source":
                    self.cases[1]["index_path"] = self.cases[0]["index_path"]
                else:
                    self.cases[1]["split"] = mutation
                with self.assertRaises(evaluation.EvaluationError):
                    self.run_evaluation()
                self.cases = saved

    def test_all_candidate_development_corrections_are_isolated(self):
        reg_index = json.loads((self.root / self.cases[1]["index_path"]).read_text(encoding="utf-8"))
        self.store.corrections["unlisted-fix"] = {"family_id": "ind-b", "source_sha256": reg_index["source_sha256"]}
        self.store.candidate["payload"]["correction_ids"].append("unlisted-fix")
        with self.assertRaisesRegex(evaluation.EvaluationError, "不独立"):
            self.run_evaluation()

    def test_reject_empty_none_bool_unknown_and_missing_scope_metadata(self):
        for mutation in ("empty", "none", "bool_quantity", "extra", "anchor", "scope", "boundary"):
            with self.subTest(mutation=mutation):
                saved = copy.deepcopy(self.cases)
                assertion = self.cases[0]["assertions"][0]
                if mutation == "empty":
                    self.cases[0]["assertions"] = []
                elif mutation == "none":
                    assertion["required_tags"] = None
                elif mutation == "bool_quantity":
                    assertion["quantity"] = False
                elif mutation == "extra":
                    assertion["passed"] = True
                elif mutation == "anchor":
                    assertion["required_tags"] = ["<C3>"]
                elif mutation == "scope":
                    self.cases[2]["scope_note"] = "  "
                else:
                    self.cases[2]["assertions"][0].pop("clause_category")
                    self.cases[2]["assertions"][0]["forbidden_tags"] = []
                with self.assertRaises(evaluation.EvaluationError):
                    self.run_evaluation()
                self.cases = saved
        self.write_manifest({"schema_version": True, "cases": self.cases})
        with self.assertRaises(evaluation.EvaluationError):
            evaluation.evaluate_manifest(self.root, "mem-test", 1, self.manifest_path, store=self.store)
        with self.assertRaises(evaluation.EvaluationError):
            evaluation.evaluate_manifest(self.root, "mem-test", True, self.manifest_path, store=self.store)

    def test_result_extra_fields_and_boolean_ids_cannot_fake_pass(self):
        for extra in ("extra", "boolean"):
            bad = record()
            if extra == "extra":
                bad["passed"] = True
            else:
                bad["id"] = True
            self.candidate_output(0, [bad])
            report = self.run_evaluation()
            self.assertEqual(report["status"], "failed")
            self.assertFalse(report["cases"][0]["candidate_validation"]["ok"])

    def test_candidate_hash_and_artifact_changes_are_not_cached(self):
        before = self.run_evaluation()
        self.candidate_output(1, [record(quantity="3")])
        after = self.run_evaluation()
        self.assertNotEqual(before["report_sha256"], after["report_sha256"])
        self.assertEqual(after["status"], "failed")
        path = str((self.root / self.cases[1]["candidate_result_path"]).resolve())
        hashes = [{a["path"]: a["sha256"] for a in report["artifacts"]} for report in (before, after)]
        self.assertNotEqual(hashes[0][path], hashes[1][path])
        self.store.load_count, self.store.drift = 0, True
        with self.assertRaisesRegex(evaluation.EvaluationError, "发生变化"):
            self.run_evaluation()

    def test_failed_counterexample_cannot_satisfy_count_gate(self):
        # Both runs fail this counterexample; zero regressions alone must not allow publication.
        self.write(self.root / self.cases[2]["baseline_result_path"], [record(quantity="3")])
        self.candidate_output(2, [record(quantity="3")])
        report = self.run_evaluation()
        self.assertEqual(report["summary"]["regressions"], 0)
        self.assertEqual(report["summary"]["counterexample_cases"], 0)
        self.assertEqual(report["status"], "failed")

    def test_independent_baseline_semantic_failures_cannot_fill_protection_gate(self):
        for number in (1, 2):
            self.write(self.root / self.cases[number]["baseline_result_path"], [record(end="<C90>")])
        report = self.run_evaluation()
        self.assertEqual(report["summary"]["fix_improvements"], 1)
        self.assertEqual(report["summary"]["regressions"], 0)
        self.assertEqual(report["summary"]["independent_regression_cases"], 0)
        self.assertEqual(report["summary"]["counterexample_cases"], 0)
        self.assertTrue(all(c["candidate_passed"] for c in report["cases"]))
        self.assertFalse(report["cases"][1]["baseline_passed"])
        self.assertTrue(report["cases"][1]["baseline_validation"]["ok"])
        self.assertEqual(report["status"], "failed")

    def test_current_rule_files_are_bound_without_claiming_model_provenance(self):
        config = self.root / "opencode.json"
        self.write(config, {"model": "fixture"})
        report = self.run_evaluation()
        self.assertIn(str(config.resolve()), {a["path"] for a in report["artifacts"]})
        self.assertIn("不证明这些配置曾用于生成", report["notice"])
        self.assertTrue(all(type(value) is int for value in report["summary"].values()))
        with mock.patch.object(evaluation, "MAX_REPORT_BYTES", 100):
            with self.assertRaisesRegex(evaluation.EvaluationError, "未注册"):
                self.run_evaluation()


    def test_confirmed_success_passes_without_fake_fix_improvement(self):
        source, _ = self.configure_success()
        report = self.run_evaluation()
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["evaluation_mode"], "success")
        self.assertEqual(report["summary"]["fix_improvements"], 0)
        self.assertEqual(report["summary"]["success_cases"], 1)
        self.assertEqual(report["summary"]["unfixed_target_cases"], 0)
        self.assertEqual(report["source_confirmations"], [{"source_id": "source-success",
                         "confirmation_sha256": source["confirmation_sha256"]}])
        self.assertEqual(report["cases"][0]["success_source_id"], source["source_id"])
        self.assertEqual(report["cases"][0]["success_result_sha256"], source["result_sha256"])
        paths = {item["path"] for item in report["artifacts"]}
        self.assertTrue(all(item["path"] in paths for item in source["artifacts"]))
        self.assertIn("不证明性能收益", report["notice"])

    def test_unconfirmed_or_incomplete_success_source_is_rejected(self):
        self.configure_success()
        original = copy.deepcopy(self.learning.sources)
        for field in ("confirmed", "confirmation", "confirmation_sha256", "artifacts"):
            with self.subTest(field=field):
                self.learning.sources = copy.deepcopy(original)
                self.learning.sources[-1].pop(field)
                with self.assertRaises(evaluation.EvaluationError):
                    self.run_evaluation()
        self.assertEqual(self.store.reports, [])

    def test_success_still_requires_independent_counterexample(self):
        self.configure_success()
        self.cases = self.cases[:2]
        report = self.run_evaluation()
        self.assertEqual(report["summary"]["success_cases"], 1)
        self.assertEqual(report["summary"]["counterexample_cases"], 0)
        self.assertEqual(report["status"], "failed")
        self.assertIn("NO_PASSING_COUNTEREXAMPLE", {item["code"] for item in report["gate_errors"]})

    def test_success_development_family_and_source_cannot_be_regression(self):
        self.configure_success()
        for mutation in ("family", "source"):
            with self.subTest(mutation=mutation):
                original = copy.deepcopy(self.cases[1])
                if mutation == "family":
                    self.cases[1]["family_id"] = self.cases[0]["family_id"]
                else:
                    self.cases[1]["index_path"] = self.cases[0]["index_path"]
                with self.assertRaises(evaluation.EvaluationError):
                    self.run_evaluation()
                self.cases[1] = original

    def test_success_candidate_regression_fails_and_baseline_must_already_pass(self):
        self.configure_success()
        self.candidate_output(0, [record(end="<C90>")])
        report = self.run_evaluation()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["summary"]["success_cases"], 0)
        self.assertEqual(report["summary"]["regressions"], 1)
        self.assertEqual(report["summary"]["unfixed_target_cases"], 1)
        self.candidate_output(0, [record()])
        self.write(self.root / self.cases[0]["baseline_result_path"], [record(end="<C90>")])
        # A source can be confirmed while the new assertions reveal an omitted key requirement.
        source = self.learning.sources[-1]
        self.write(Path(source["result_path"]), [record(end="<C90>")])
        source["result_sha256"] = hashlib.sha256(Path(source["result_path"]).read_bytes()).hexdigest()
        source["artifacts"][0]["sha256"] = source["result_sha256"]
        report = self.run_evaluation()
        self.assertEqual(report["summary"]["unfixed_target_cases"], 0)
        self.assertEqual(report["status"], "failed")
        self.assertIn("SUCCESS_BASELINE_FAILED", {item["code"] for item in report["gate_errors"]})

    def test_success_role_must_match_success_source_and_development_split(self):
        self.configure_success()
        self.cases[0]["split"] = "regression"
        with self.assertRaises(evaluation.EvaluationError):
            self.run_evaluation()
        self.cases[0]["split"] = "development"
        self.cases[0]["family_id"] = "other-family"
        with self.assertRaisesRegex(evaluation.EvaluationError, "成功来源"):
            self.run_evaluation()

    def test_mixed_sources_keep_correction_gate_and_isolate_success_sources(self):
        self.configure_success(mixed=True)
        report = self.run_evaluation()
        self.assertEqual(report["evaluation_mode"], "correction")
        self.assertEqual(report["summary"]["fix_improvements"], 1)
        self.assertEqual(report["summary"]["success_cases"], 1)
        self.assertEqual(report["status"], "passed")
        self.write(self.root / self.cases[0]["baseline_result_path"], [record()])
        report = self.run_evaluation()
        self.assertEqual(report["status"], "failed")
        self.assertIn("NO_FIX_IMPROVEMENT", {item["code"] for item in report["gate_errors"]})

    def test_success_confirmation_and_immutable_result_cannot_drift(self):
        source, _ = self.configure_success()
        self.learning.drift = True
        with self.assertRaisesRegex(evaluation.EvaluationError, "来源或人工确认发生变化"):
            self.run_evaluation()
        self.assertEqual(self.store.reports, [])
        self.learning.drift = False
        self.write(Path(source["result_path"]), [record(quantity="3")])
        with self.assertRaisesRegex(evaluation.EvaluationError, "不可变工件已变化"):
            self.run_evaluation()

    def test_success_baseline_is_bound_to_confirmed_result_not_just_passing_assertions(self):
        self.configure_success()
        # Harmless extra tags still pass the assertions, but cannot impersonate the saved baseline.
        self.write(self.root / self.cases[0]["baseline_result_path"], [record(end="<C50>")])
        with self.assertRaisesRegex(evaluation.EvaluationError, "已确认的结果快照一致"):
            self.run_evaluation()
        self.assertEqual(self.store.reports, [])
        # File formatting is not part of the seven-field output identity.
        path = self.root / self.cases[0]["baseline_result_path"]
        path.write_text(json.dumps([record()], ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.run_evaluation()["status"], "passed")

    def test_real_confirmed_learning_source_evaluates_and_publishes_without_model(self):
        import memory_learning
        import memory_store

        for number in range(3):
            self.use_merged_case(number)
        case = self.cases[0]
        case["role"] = "success"
        self.write(self.root / case["baseline_result_path"], [record()])
        index_path = self.root / case["index_path"]
        index = json.loads(index_path.read_text(encoding="utf-8"))
        fingerprint = hashlib.sha256(index_path.read_bytes()).hexdigest()
        evidence_path, run_path = self.root / "evidence.json", self.root / "run.json"
        self.write(evidence_path, {"clauses": [{"id": 0, "notes": "合成来源核验",
                   "quantity_source_tags": ["<C90>"], "related_source_tags": []}], "unresolved": []})
        self.write(run_path, {"status": "submitted", "production_submittable": True,
                   "source_sha256": index["source_sha256"],
                   "coverage": {"complete": True, "index_sha256": fingerprint}})
        run = memory_learning.register_run(self.root, index_path,
            self.root / case["baseline_result_path"], evidence_path, run_path)
        payload = {"title": "合成成功经验", "issue_type": "boundary", "triggers": ["同一产品"],
                   "applies_when": "同一采购对象的连续要求", "rule": "保留关键参数和完整产品边界",
                   "exclusions": "独立采购的其他设备", "correction_ids": [], "source_ids": [run["run_id"]]}
        candidate = memory_store.propose(self.root, payload)
        self.write_manifest()
        with self.assertRaisesRegex(memory_store.MemoryError, "未经人工确认"):
            evaluation.evaluate_manifest(self.root, candidate["memory_id"], candidate["revision"], self.manifest_path)
        memory_learning.confirm_run(self.root, run["run_id"], "合成人工", "dev-a", "已确认合成成功来源")
        report = evaluation.evaluate_manifest(self.root, candidate["memory_id"], candidate["revision"], self.manifest_path)
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["evaluation_mode"], "success")
        published = memory_store.publish(self.root, candidate["memory_id"], candidate["revision"],
                                         report["evaluation_id"], "合成人工", "通过成功兼容回归")
        self.assertEqual(published["state"], "published")


if __name__ == "__main__":
    unittest.main()
