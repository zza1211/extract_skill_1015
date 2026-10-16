"""仅用合成原文验证对象闭合比较，不运行模型或修改真实标书。"""
import copy
import unittest

import learning_compare as compare


def index(texts=None):
    texts = texts or ["交换机 2台", "接口要求", "电源要求", "说明", "防火墙 1台", "安全要求", "说明", "机柜", "布线", "附注"]
    # 标签数字故意不按原文顺序，比较只能依赖 index 中的顺序。
    tags = [f"<C{value}>" for value in (90, 3, 50, 10, 1, 70, 2, 55, 4, 6)]
    units = [{"unit_id": f"u{position}", "order": position, "tag": tag, "tags": [tag],
              "text": tag + text, "block_index": position, "kind": "text", "row": None, "col": None}
             for position, (tag, text) in enumerate(zip(tags, texts))]
    return {"schema_version": 2, "demo": False, "production_submittable": True,
            "units": units, "tag_to_unit": {unit["tag"]: unit["unit_id"] for unit in units}}


def record(cid=0, start=0, end=2, category="normal", links=None, quantity="2", title="交换机"):
    tags = [unit["tag"] for unit in index()["units"]]
    return {"id": cid, "条款标题": title, "条款分类": category, "相关条款": links or [],
            "产品分类": "仅供参考", "定位标识": [tags[start], tags[end]], "产品数量": quantity}


class LearningComparisonTests(unittest.TestCase):
    def setUp(self):
        self.index = index()

    def test_ids_titles_categories_are_not_business_identity(self):
        prediction = [record(42, title="Core Switching Requirement")]
        prediction[0]["产品分类"] = "任意分类也不影响比较"
        result = compare.compare_results(self.index, prediction, [record()])
        self.assertEqual("passed", result["status"])

    def test_reordered_ids_and_general_references_are_remapped(self):
        prediction = [record(80, 0, 0, "general", [25], "未提及"), record(25, 1, 2)]
        self.assertEqual("passed", compare.compare_results(self.index, prediction, [record()])["status"])

    def test_contiguous_and_connected_separate_effective_scope_are_equivalent(self):
        prediction = [record(0, 0, 0, "separate", [1]), record(1, 1, 2, "separate", [0])]
        self.assertEqual("passed", compare.compare_results(self.index, prediction, [record()])["status"])

    def test_inline_and_direct_general_effective_scope_are_equivalent(self):
        labels = [record(0, 0, 0, "general", [1], "未提及"), record(1, 1, 2)]
        self.assertEqual("passed", compare.compare_results(self.index, [record()], labels)["status"])

    def test_shared_general_does_not_join_two_devices(self):
        labels = [record(0, 3, 3, "general", [1, 2], "未提及"),
                  record(1), record(2, 4, 5, quantity="1", title="防火墙")]
        result = compare.compare_results(self.index, labels, labels)
        self.assertEqual("passed", result["status"])
        self.assertEqual(2, len(result["objects"]))

    def test_extra_cabinet_is_detected_by_closed_object_set(self):
        prediction = [record(), record(1, 7, 7, quantity="未提及", title="机柜")]
        result = compare.compare_results(self.index, prediction, [record()])
        self.assertEqual("failed", result["status"])
        self.assertEqual(1, result["summary"]["extra_objects"])

    def test_missing_object_is_hard_error(self):
        result = compare.compare_results(self.index, [], [record()])
        self.assertEqual(1, result["summary"]["missing_objects"])
        with self.assertRaises(compare.ComparisonError):
            compare.apply_judgments(result, [{"difference_id": result["differences"][0]["difference_id"],
                                             "decision": "harmless", "reason": "不能覆盖标签漏项"}])

    def test_empty_gold_is_valid_and_rejects_any_extra_device(self):
        self.assertEqual("passed", compare.compare_results(self.index, [], [])["status"])
        self.assertEqual("failed", compare.compare_results(self.index, [record()], [])["status"])

    def test_separate_connecting_two_gold_objects_is_hard_association_error(self):
        labels = [record(), record(1, 4, 5, quantity="1", title="防火墙")]
        prediction = [record(0, 0, 2, "separate", [1]), record(1, 4, 5, "separate", [0], "1")]
        result = compare.compare_results(self.index, prediction, labels)
        self.assertEqual("failed", result["status"])
        self.assertEqual(1, result["summary"]["association_errors"])
        self.assertEqual(0, result["summary"]["missing_objects"])

    def test_normal_spanning_two_devices_requires_business_review(self):
        result = compare.compare_results(self.index, [record(0, 0, 5)], [record(), record(1, 4, 5)])
        self.assertEqual("needs_review", result["status"])
        self.assertEqual(1, result["summary"]["ambiguous_differences"])

    def test_same_title_does_not_merge_distinct_objects(self):
        labels = [record(), record(1, 4, 5)]
        result = compare.compare_results(self.index, [record()], labels)
        self.assertEqual(1, result["summary"]["missing_objects"])

    def test_general_scope_expansion_is_detected(self):
        labels = [record(0, 3, 3, "general", [1], "未提及"), record(1), record(2, 4, 5, quantity="1")]
        prediction = copy.deepcopy(labels)
        prediction[0]["相关条款"].append(2)
        result = compare.compare_results(self.index, prediction, labels)
        self.assertEqual("failed", result["status"])
        self.assertEqual(1, result["summary"]["association_errors"])

    def test_general_scope_loss_is_detected(self):
        labels = [record(0, 3, 3, "general", [1, 2], "未提及"), record(1), record(2, 4, 5, quantity="1")]
        prediction = copy.deepcopy(labels)
        prediction[0]["相关条款"] = [1]
        result = compare.compare_results(self.index, prediction, labels)
        self.assertEqual(1, result["summary"]["association_errors"])

    def test_repeated_general_rows_with_equal_combined_scope_are_equivalent(self):
        labels = [record(0, 3, 3, "general", [1, 2], "未提及"), record(1), record(2, 4, 5, quantity="1")]
        prediction = [record(0, 3, 3, "general", [2], "未提及"), record(1, 3, 3, "general", [3], "未提及"),
                      record(2), record(3, 4, 5, quantity="1")]
        self.assertEqual("passed", compare.compare_results(self.index, prediction, labels)["status"])

    def test_nonoverlapping_same_name_is_alignment_review_not_assumed_identity(self):
        result = compare.compare_results(self.index, [record(start=4, end=5)], [record()])
        self.assertEqual("needs_review", result["status"])
        self.assertEqual(0, result["summary"]["missing_objects"])
        self.assertEqual(0, result["summary"]["extra_objects"])
        self.assertEqual("object_alignment", result["differences"][0]["context"]["issue"])

    def test_quantity_bare_number_and_supported_unit_are_equivalent(self):
        result = compare.compare_results(self.index, [record(quantity="2台")], [record(quantity="2")])
        self.assertEqual("passed", result["status"])

    def test_unit_addition_without_local_source_support_requires_review(self):
        document = index(["交换机 数量2", "端口", "电源", "说明", "其他2台", "其他", "注", "机柜", "线", "注"])
        result = compare.compare_results(document, [record(quantity="2台")], [record(quantity="2")])
        self.assertEqual("needs_review", result["status"])

    def test_counting_units_are_not_automatically_equated(self):
        result = compare.compare_results(self.index, [record(quantity="2套")], [record(quantity="2台")])
        self.assertEqual("needs_review", result["status"])

    def test_numeric_quantity_error_cannot_be_overridden(self):
        result = compare.compare_results(self.index, [record(quantity="3")], [record(quantity="2")])
        self.assertEqual(1, result["summary"]["quantity_errors"])
        with self.assertRaises(compare.ComparisonError):
            compare.apply_judgments(result, [{"difference_id": result["differences"][0]["difference_id"],
                                             "decision": "harmless", "reason": "模型觉得一样"}])

    def test_missing_known_quantity_is_error(self):
        result = compare.compare_results(self.index, [record(quantity="未提及")], [record(quantity="2")])
        self.assertEqual("failed", result["status"])
        self.assertEqual(1, result["summary"]["quantity_errors"])

    def test_range_omission_requires_judgment_not_per_character_error(self):
        result = compare.compare_results(self.index, [record(end=1)], [record()])
        self.assertEqual("needs_review", result["status"])
        self.assertEqual(0, result["summary"]["parameter_omissions"])
        key = result["differences"][0]["difference_id"]
        resolved = compare.apply_judgments(result, [{"difference_id": key, "decision": "error", "reason": "遗漏双电源选型要求"}])
        self.assertEqual(1, resolved["summary"]["parameter_omissions"])
        self.assertEqual("failed", resolved["status"])
        self.assertEqual("needs_review", result["status"], "不得原地修改原始报告")

    def test_harmless_redundancy_can_be_resolved(self):
        result = compare.compare_results(self.index, [record(end=3)], [record()])
        key = result["differences"][0]["difference_id"]
        resolved = compare.compare_results(self.index, [record(end=3)], [record()],
            decisions={key: {"decision": "harmless", "reason": "多出的说明不影响选型"}})
        self.assertEqual("passed", resolved["status"])
        self.assertEqual(1, resolved["summary"]["harmless_differences"])

    def test_uncertain_stays_in_review(self):
        result = compare.compare_results(self.index, [record(end=1)], [record()])
        resolved = compare.apply_judgments(result, [{"difference_id": result["differences"][0]["difference_id"],
                                                    "decision": "uncertain", "reason": "需要业务确认"}])
        self.assertEqual("needs_review", resolved["status"])

    def test_duplicate_range_objects_are_not_silently_collapsed(self):
        result = compare.compare_results(self.index, [record(), record(1, title="不同标题重复范围")], [record()])
        self.assertEqual("needs_review", result["status"])
        self.assertNotEqual(result["objects"][0]["prediction"][0]["object_key"],
                            result["objects"][0]["prediction"][1]["object_key"])

    def test_context_is_limited_and_reports_truncation(self):
        document = index(["说明" * 10000] * 10)
        result = compare.compare_results(document, [record(end=1)], [record()])
        self.assertTrue(result["context_truncated"])
        snippets = [snippet for item in result["differences"] for snippet in item["context"]["snippets"]]
        self.assertLessEqual(sum(len(value["text"]) for value in snippets), compare.MAX_CONTEXT_CHARS)
        self.assertTrue(all(len(value["text"]) <= compare.MAX_SNIPPET_CHARS for value in snippets))

    def test_difference_ids_are_deterministic(self):
        left = compare.compare_results(self.index, [record(end=1)], [record()])
        right = compare.compare_results(self.index, [record(end=1)], [record()])
        self.assertEqual(left["differences"][0]["difference_id"], right["differences"][0]["difference_id"])

    def test_invalid_references_are_rejected(self):
        with self.assertRaises(compare.ComparisonError):
            compare.compare_results(self.index, [record(category="separate", links=[99])], [record()])

    def test_unknown_or_duplicate_judgments_are_rejected(self):
        result = compare.compare_results(self.index, [record(end=1)], [record()])
        key = result["differences"][0]["difference_id"]
        judgment = {"difference_id": key, "decision": "harmless", "reason": "无害说明"}
        with self.assertRaises(compare.ComparisonError):
            compare.apply_judgments(result, [judgment, judgment])
        with self.assertRaises(compare.ComparisonError):
            compare.apply_judgments(result, [{**judgment, "difference_id": "unknown"}])


if __name__ == "__main__":
    unittest.main()
