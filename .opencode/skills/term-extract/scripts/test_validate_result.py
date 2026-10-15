"""重点回归输出契约与不应由结构检查冒充的业务判断。"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from validate_result import validate_result


def index_fixture(demo: bool = False) -> dict:
    # 数字有意乱序：以显式 order 为准，而不是 C 后面的数字。
    return {
        "demo": demo,
        "production_submittable": not demo,
        "units": [
            {"tag": "<C100>", "order": 0, "text": "设备一"},
            {"tag": "<C2>", "order": 1, "text": "关键参数"},
            {"tag": "<C9>", "order": 2, "text": "设备二"},
            {"tag": None, "tags": [], "order": 3, "text": ""},
        ],
    }


def clause(cid: int = 0, ctype: str = "normal", links: list | None = None,
           span: list | None = None, title: str = "设备一", quantity: str = "未提及") -> dict:
    return {
        "id": cid,
        "条款标题": title,
        "条款分类": ctype,
        "相关条款": [] if links is None else links,
        "产品分类": "盒式交换机",
        "定位标识": ["<C100>", "<C2>"] if span is None else span,
        "产品数量": quantity,
    }


def codes(report: dict, key: str = "errors") -> set[str]:
    return {item["code"] for item in report[key]}


def wifi_layout_fixture() -> dict:
    # HK-M01 首表：AP 与 AC 在同一描述 cell；后两行是 Wi-Fi 性能要求。
    return {
        "schema_version": 2,
        "demo": False,
        "production_submittable": True,
        "units": [
            {"tag": f"<C{tag}>", "order": order, "text": f"原文 {tag}",
             "table_id": "t0002", "row": 1 if tag <= 50 else 2 if tag <= 55 else 3,
             "col": 0 if tag in (51, 56) else 1}
            for order, tag in enumerate(range(44, 59))
        ],
    }


class ResultValidationTests(unittest.TestCase):
    def test_valid_result_and_semantic_notice(self):
        report = validate_result(index_fixture(), [clause()])
        self.assertTrue(report["ok"])
        self.assertTrue(report["production_submittable"])
        self.assertFalse(report["semantic_verified"])
        self.assertIn("不等于", report["notice"])

    def test_bool_id_is_not_an_integer(self):
        report = validate_result(index_fixture(), [clause(cid=False)])
        self.assertIn("ID_TYPE", codes(report))

    def test_duplicate_id_and_noncontiguous_id(self):
        report = validate_result(index_fixture(), [clause(), clause(cid=0, span=["<C9>", "<C9>"])])
        self.assertIn("ID_DUPLICATE", codes(report))
        self.assertIn("ID_SEQUENCE", codes(report))
        self.assertIn("ID_SEQUENCE", codes(validate_result(index_fixture(), [clause(cid=3)])))

    def test_field_missing_extra_and_bad_type(self):
        item = clause()
        item.pop("产品数量")
        item["证据"] = []
        report = validate_result(index_fixture(), [item])
        self.assertIn("FIELDS_MISSING", codes(report))
        self.assertIn("FIELDS_EXTRA", codes(report))
        item = clause(quantity=3)
        self.assertIn("STRING_REQUIRED", codes(validate_result(index_fixture(), [item])))

    def test_invalid_category_never_crashes(self):
        item = clause()
        item["条款分类"] = ["normal"]
        self.assertIn("CLAUSE_CATEGORY", codes(validate_result(index_fixture(), [item])))

    def test_product_classification_is_reference_only(self):
        item = clause()
        item["产品分类"] = "待确认的参考分类"
        self.assertTrue(validate_result(index_fixture(), [item])["ok"])

    def test_invalid_links(self):
        item = clause(ctype="general", links=[0, 7, True, 7])
        found = codes(validate_result(index_fixture(), [item]))
        self.assertTrue({"LINK_SELF", "LINK_UNKNOWN", "LINK_ID_TYPE", "LINK_DUPLICATE"} <= found)
        self.assertIn("NORMAL_LINKS", codes(validate_result(index_fixture(), [clause(links=[1])])))
        self.assertIn("LINKS_REQUIRED", codes(validate_result(index_fixture(), [clause(ctype="separate")])))

    def test_separate_link_reciprocity_uses_explicit_links_not_titles(self):
        items = [clause(ctype="separate", links=[1]),
                 clause(cid=1, ctype="separate", links=[0], span=["<C9>", "<C9>"], title="原文另一名称")]
        self.assertTrue(validate_result(index_fixture(), items)["ok"])
        items[1]["相关条款"] = [2]
        items.append(clause(cid=2, span=["<C2>", "<C2>"]))
        self.assertIn("SEPARATE_RECIPROCAL", codes(validate_result(index_fixture(), items)))

    def test_shared_normal_requires_review_not_title_based_inference(self):
        items = [clause(), clause(cid=1, ctype="separate", links=[0], span=["<C9>", "<C9>"], title="补充一"),
                 clause(cid=2, ctype="separate", links=[0], span=["<C2>", "<C2>"], title="补充二")]
        report = validate_result(index_fixture(), items)
        self.assertTrue(report["ok"])
        self.assertIn("SEPARATE_SHARED_NORMAL_REVIEW", codes(report, "warnings"))

    def test_separate_missing_quantity_reviews_known_linked_object_quantity(self):
        for target_type in ("normal", "separate"):
            with self.subTest(target_type=target_type):
                items = [clause(ctype=target_type, links=[1] if target_type == "separate" else [], quantity="54"),
                         clause(cid=1, ctype="separate", links=[0], span=["<C9>", "<C9>"])]
                report = validate_result(index_fixture(), items)
                self.assertTrue(report["ok"])
                warnings = [item for item in report["warnings"] if item["code"] == "SEPARATE_QUANTITY_MISSING_REVIEW"]
                self.assertEqual(len(warnings), 1)
                self.assertEqual(warnings[0]["path"], "result[1].产品数量")
                self.assertIn("原文数量来源", warnings[0]["message"])
                self.assertEqual(items[1]["产品数量"], "未提及", "校验器不能自动继承关联数量")

    def test_separate_quantity_review_skips_filled_unknown_or_general_quantity(self):
        cases = [
            ("已填写", "separate", "54", "normal", "54"),
            ("关联数量未确定", "separate", "未提及", "normal", "未提及"),
            ("仅 general 有数量", "separate", "未提及", "general", "54"),
            ("当前为 general", "general", "未提及", "normal", "54"),
        ]
        for name, source_type, source_quantity, target_type, target_quantity in cases:
            with self.subTest(case=name):
                items = [clause(ctype=target_type, links=[1] if target_type == "general" else [], quantity=target_quantity),
                         clause(cid=1, ctype=source_type, links=[0], span=["<C9>", "<C9>"], quantity=source_quantity)]
                report = validate_result(index_fixture(), items)
                self.assertTrue(report["ok"])
                self.assertNotIn("SEPARATE_QUANTITY_MISSING_REVIEW", codes(report, "warnings"))

    def test_separate_adjacent_table_rows_warn_with_controller_between(self):
        items = [clause(span=["<C44>", "<C48>"], title="Access Points"),
                 clause(cid=1, ctype="separate", links=[0], span=["<C52>", "<C58>"], title="Access Points"),
                 clause(cid=2, span=["<C49>", "<C50>"], title="On-premises Access Controller")]
        report = validate_result(wifi_layout_fixture(), items)
        self.assertTrue(report["ok"])
        warnings = [item for item in report["warnings"] if item["code"] == "SEPARATE_LOCAL_LAYOUT_REVIEW"]
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["path"], "result[1].相关条款[0]")
        self.assertIn("evidence.notes", warnings[0]["message"])
        self.assertIn("general", warnings[0]["message"])

    def test_general_system_performance_has_no_separate_layout_warning(self):
        items = [clause(span=["<C44>", "<C48>"], title="Access Points"),
                 clause(cid=1, ctype="general", links=[0, 2], span=["<C52>", "<C58>"], title="Wi-Fi performance"),
                 clause(cid=2, span=["<C49>", "<C50>"], title="On-premises Access Controller")]
        report = validate_result(wifi_layout_fixture(), items)
        self.assertTrue(report["ok"])
        self.assertNotIn("SEPARATE_LOCAL_LAYOUT_REVIEW", codes(report, "warnings"))

    def test_separate_same_cell_warns_using_layout_not_title_or_tag_numbers(self):
        index = index_fixture()
        for unit in index["units"]:
            unit.update(table_id="t0001", row=7, col=1)
        items = [clause(span=["<C100>", "<C100>"]),
                 clause(cid=1, ctype="separate", links=[0], span=["<C9>", "<C9>"], title="另一原文名称")]
        report = validate_result(index, items)
        self.assertTrue(report["ok"])
        self.assertIn("SEPARATE_LOCAL_LAYOUT_REVIEW", codes(report, "warnings"))

    def test_separate_different_tables_has_no_local_layout_warning(self):
        index = wifi_layout_fixture()
        for unit in index["units"]:
            if unit["order"] >= 8:
                unit["table_id"] = "t0009"
        items = [clause(span=["<C44>", "<C48>"]),
                 clause(cid=1, ctype="separate", links=[0], span=["<C52>", "<C58>"])]
        report = validate_result(index, items)
        self.assertTrue(report["ok"])
        self.assertNotIn("SEPARATE_LOCAL_LAYOUT_REVIEW", codes(report, "warnings"))

    def test_separate_distant_rows_in_same_table_has_no_local_layout_warning(self):
        index = wifi_layout_fixture()
        for unit in index["units"]:
            if unit["order"] >= 8:
                unit["row"] += 10
        items = [clause(span=["<C44>", "<C48>"]),
                 clause(cid=1, ctype="separate", links=[0], span=["<C52>", "<C58>"])]
        report = validate_result(index, items)
        self.assertTrue(report["ok"])
        self.assertNotIn("SEPARATE_LOCAL_LAYOUT_REVIEW", codes(report, "warnings"))

    def test_separate_mixed_layout_span_does_not_use_only_endpoint_coordinates(self):
        index = wifi_layout_fixture()
        index["units"][10].update(table_id=None, row=None, col=None)
        items = [clause(span=["<C44>", "<C48>"]),
                 clause(cid=1, ctype="separate", links=[0], span=["<C52>", "<C58>"])]
        report = validate_result(index, items)
        self.assertTrue(report["ok"])
        self.assertNotIn("SEPARATE_LOCAL_LAYOUT_REVIEW", codes(report, "warnings"))

    def test_original_order_not_numeric_label_order(self):
        self.assertTrue(validate_result(index_fixture(), [clause()])["ok"])
        report = validate_result(index_fixture(), [clause(span=["<C2>", "<C100>"])])
        self.assertIn("SPAN_REVERSED", codes(report))

    def test_span_unknown_format_and_cardinality(self):
        for span, expected in [(["<C999>", "<C999>"], "SPAN_TAG_UNKNOWN"),
                               (["C100", "<C2>"], "SPAN_TAG_FORMAT"),
                               (["<C100>"], "SPAN_SHAPE"),
                               ([True, "<C2>"], "SPAN_TAG_FORMAT")]:
            with self.subTest(span=span):
                self.assertIn(expected, codes(validate_result(index_fixture(), [clause(span=span)])))

    def test_general_overlap_is_allowed(self):
        items = [clause(), clause(cid=1, ctype="general", links=[0], title="通用要求")]
        report = validate_result(index_fixture(), items)
        self.assertTrue(report["ok"])
        self.assertNotIn("SPAN_OVERLAP_REVIEW", codes(report, "warnings"))

    def test_other_overlap_warns_but_does_not_mechanically_fail(self):
        items = [clause(), clause(cid=1, title="另一设备", span=["<C2>", "<C9>"])]
        report = validate_result(index_fixture(), items)
        self.assertTrue(report["ok"])
        self.assertIn("SPAN_OVERLAP_REVIEW", codes(report, "warnings"))

    def test_demo_needs_explicit_flag_and_never_becomes_production(self):
        self.assertIn("DEMO_FORBIDDEN", codes(validate_result(index_fixture(True), [clause()])))
        report = validate_result(index_fixture(True), [clause()], allow_demo=True)
        self.assertTrue(report["ok"])
        self.assertTrue(report["demo"])
        self.assertFalse(report["production_submittable"])
        self.assertIn("DEMO_ONLY", codes(report, "warnings"))

    def test_index_tags_array_and_duplicate_labels(self):
        index = index_fixture()
        index["units"][0].pop("tag")
        index["units"][0]["tags"] = ["<C100>"]
        self.assertTrue(validate_result(index, [clause()])["ok"])
        index["units"][1]["tag"] = "<C100>"
        self.assertIn("INDEX_TAG_DUPLICATE", codes(validate_result(index, [clause()])))

    def test_evidence_optional_but_quantity_source_requested(self):
        report = validate_result(index_fixture(), [clause(quantity="2 台")])
        self.assertTrue(report["ok"])
        self.assertIn("QUANTITY_EVIDENCE_MISSING", codes(report, "warnings"))
        evidence = {"clauses": [{"id": 0, "quantity_source_tags": ["<C9>"], "related_source_tags": [], "notes": "来自区段外清单"}], "unresolved": []}
        report = validate_result(index_fixture(), [clause(quantity="2 台")], evidence)
        self.assertTrue(report["ok"])
        self.assertNotIn("QUANTITY_EVIDENCE_MISSING", codes(report, "warnings"))
        evidence["clauses"][0]["quantity_source_tags"] = ["<C999>"]
        evidence["clauses"][0]["id"] = 7
        found = codes(validate_result(index_fixture(), [clause()], evidence))
        self.assertTrue({"EVIDENCE_TAG_UNKNOWN", "EVIDENCE_ID_UNKNOWN"} <= found)

    def test_unresolved_never_implies_semantic_acceptance(self):
        report = validate_result(index_fixture(), [clause()], {"clauses": [], "unresolved": ["Others 范围待确认"]})
        self.assertTrue(report["ok"])
        self.assertFalse(report["semantic_verified"])
        self.assertIn("UNRESOLVED_REVIEW", codes(report, "warnings"))

    def test_repeated_extraction_is_rejected(self):
        items = [clause(), clause(cid=1)]
        self.assertIn("CLAUSE_DUPLICATE", codes(validate_result(index_fixture(), items)))

    def test_cli_machine_report_and_exit_codes(self):
        script = Path(__file__).with_name("validate_result.py")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            index, result, report = base / "index.json", base / "result.json", base / "report.json"
            index.write_text(json.dumps(index_fixture(True), ensure_ascii=False), encoding="utf-8")
            result.write_text(json.dumps([clause()], ensure_ascii=False), encoding="utf-8")
            command = [sys.executable, str(script), "--index", str(index), "--result", str(result), "--report", str(report)]
            run = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(run.returncode, 2)
            self.assertEqual(json.loads(run.stdout), json.loads(report.read_text(encoding="utf-8")))
            run = subprocess.run(command + ["--allow-demo"], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(run.returncode, 0)
            self.assertFalse(json.loads(run.stdout)["production_submittable"])
            result.write_text('[{"id": 0, "id": 1}]', encoding="utf-8")
            run = subprocess.run(command, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(run.returncode, 2)
            self.assertIn("INPUT_READ", codes(json.loads(run.stdout)))

    def test_report_cannot_overwrite_an_input(self):
        script = Path(__file__).with_name("validate_result.py")
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            index, result = base / "index.json", base / "result.json"
            index.write_text(json.dumps(index_fixture()), encoding="utf-8")
            original = json.dumps([clause()], ensure_ascii=False)
            result.write_text(original, encoding="utf-8")
            run = subprocess.run([sys.executable, str(script), "--index", str(index), "--result", str(result), "--report", str(result)], capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(run.returncode, 2)
            self.assertIn("REPORT_WRITE", codes(json.loads(run.stdout)))
            self.assertEqual(result.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
