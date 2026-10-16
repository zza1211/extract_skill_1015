"""验证原文保真、定位与分页边界；无需网络或额外依赖。"""

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import document_io as doc


class DocumentIOTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, source, demo=False):
        path = self.root / "input.json"
        path.write_bytes(doc.json_bytes(source))
        return doc.prepare(path, self.root / "prepared", demo)

    def collect(self, path, max_bytes=doc.MAX_BYTES, search=None, limit=20, view="machine"):
        cursor, pages = "0", []
        for _ in range(1000):
            page = (doc.search_document(path, search, cursor, max_bytes, limit, neighbors=0, view=view)
                    if search is not None else doc.read_document(path, cursor, max_bytes, view=view))
            self.assertEqual(len(doc.sized_bytes(page)), page["byte_size"])
            if view == "compact":
                model_view = page["model_view"]
                self.assertEqual(len(doc.sized_bytes(model_view)), model_view["byte_size"])
                self.assertLessEqual(model_view["byte_size"], max_bytes)
                self.assertLessEqual(page["byte_size"], doc.MAX_IPC_BYTES)
                self.assertLessEqual(len(page["parts"]), doc.MAX_COMPACT_UNITS)
                for key in ("cursor", "next_cursor", "has_more", "partial_tags", "text"):
                    self.assertEqual(model_view[key], page[key])
                if search is None:
                    self.assertIn("本代理的候选保存工具", model_view["next_action"])
                else:
                    self.assertNotIn("next_action", model_view)
            else:
                self.assertLessEqual(page["byte_size"], max_bytes)
            self.assertFalse(page["truncated"])
            pages.append(page)
            if not page["has_more"]:
                return pages
            self.assertNotEqual(cursor, page["next_cursor"])
            cursor = page["next_cursor"]
        self.fail("分页没有结束")

    def reconstructed(self, pages):
        chunks = {}
        for page in pages:
            for part in page["parts"]:
                text = page["text"][part["content_start"]:part["content_end"]]
                self.assertEqual(len(text), part["part_end"] - part["part_start"])
                chunks.setdefault(part["unit_id"], []).append((part["part_start"], part["part_end"], text))
        result = {}
        for unit_id, parts in chunks.items():
            position, text = 0, ""
            for start, end, fragment in parts:
                self.assertEqual(start, position)
                position = end
                text += fragment
            result[unit_id] = text
        return result

    def test_cross_table_fixture_and_demo_only(self):
        example = self.root / "untagged.json"
        source = [
            {"text": "设备技术规格"},
            {"table": {
                "rows": [["序号", "设备", "参数", "数量", "备注"],
                         ["1", "设备一", "参数甲", "2", ""]],
                "merges": [{"id": "M1", "value": "设备与参数",
                            "top_left": {"row": 0, "col": 1},
                            "row_span": 1, "col_span": 2}],
            }},
            {"text": "续表"},
            {"table": {"rows": [["2", "设备二", "参数乙"],
                                 ["3", "设备三", "参数丙"]], "merges": []}},
        ]
        example.write_bytes(doc.json_bytes(source))
        raw = example.read_bytes()
        with self.assertRaisesRegex(doc.DocumentError, "缺少"):
            doc.prepare(example, self.root / "production")
        result = doc.prepare(example, self.root / "demo", True)
        self.assertEqual(example.read_bytes(), raw)
        index, _ = doc.load_index(Path(result["index_path"]))
        self.assertTrue(index["demo"])
        self.assertFalse(index["production_submittable"])
        self.assertEqual([len(t["rows"]) for t in index["tables"]], [2, 2])
        original = json.loads(raw)
        self.assertEqual(index["source_document"], original)
        self.assertEqual(index["tables"][0]["merges"], original[1]["table"]["merges"])
        cells = [u for u in index["units"] if u["kind"] == "cell"]
        self.assertEqual([(u["row"], u["col"]) for u in cells[:5]], [(0, n) for n in range(5)])
        second = [u for u in cells if u["block_index"] == 3]
        self.assertEqual(second[0]["source_text"], "2")
        self.assertEqual(second[-3]["source_text"], "3")
        seen = self.reconstructed(self.collect(Path(result["index_path"])))
        self.assertEqual(seen, {u["unit_id"]: u["source_text"] for u in index["units"]})
        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertTrue(manifest["demo"])
        self.assertFalse(manifest["production_submittable"])
        for view in manifest["views"]:
            self.assertEqual(Path(view["path"]).stat().st_size, view["byte_size"])
            self.assertLessEqual(view["byte_size"], doc.MAX_BYTES)

    def test_long_cell_is_lossless_and_last_fragment_not_complete(self):
        value = "引号\"、反斜线\\、换行\n" * 600 + "<C77>"
        result = self.prepare([{"table": {"rows": [[value, ""]], "merges": []}}])
        pages = self.collect(Path(result["index_path"]), max_bytes=2200)
        self.assertGreater(len(pages), 1)
        fragments = [(page, p) for page in pages for p in page["parts"] if p["tag"] == "<C77>"]
        self.assertTrue(fragments[-1][1]["ends_unit"])
        self.assertGreater(fragments[-1][1]["part_start"], 0)
        self.assertIn("<C77>", fragments[-1][0]["partial_tags"])
        self.assertNotIn("<C77>", fragments[-1][0]["complete_tags"])
        self.assertEqual(self.reconstructed(pages)["u000001"], value)

    def test_reject_duplicate_and_missing_tags(self):
        for text, message in [("<C1>a<C1>b", "重复"), ("plain", "缺少")]:
            with self.subTest(text=text), self.assertRaisesRegex(doc.DocumentError, message):
                self.prepare([{"text": text}])
        with self.assertRaisesRegex(doc.DocumentError, "重复"):
            self.prepare([{"text": "a<C1>"}, {"table": {"rows": [["<C1>b"]], "merges": []}}])

    def merged_source(self):
        shared = "<C3>接口要求\n<C4>供电要求"
        return [{"table": {
            "rows": [["<C90>总要求", shared, shared, ""],
                     ["<C8>设备名称", "<C9>本设备参数", "<C10>说明", ""],
                     ["<C8>设备名称", "<C2>下一设备", "<C1>另一设备参数", ""]],
            "merges": [{"top_left": {"row": 0, "col": 1}, "row_span": 1, "col_span": 2, "value": shared},
                       {"top_left": {"row": 1, "col": 0}, "row_span": 2, "col_span": 1}],
        }}]

    def test_horizontal_vertical_multitag_aliases_preserve_source_and_layout(self):
        source = self.merged_source()
        result = self.prepare(source)
        index, _ = doc.load_index(Path(result["index_path"]))
        self.assertEqual(index["source_document"], source)
        self.assertEqual(index["tables"][0]["rows"], source[0]["table"]["rows"])
        self.assertEqual(index["merged_cell_alias_policy"], "exact_merged_cell_v1")
        self.assertEqual(index["merged_cell_aliases"], [
            {"block_index": 0, "row": 0, "col": 2, "anchor_row": 0, "anchor_col": 1, "merge_index": 0},
            {"block_index": 0, "row": 2, "col": 0, "anchor_row": 1, "anchor_col": 0, "merge_index": 1}])
        self.assertEqual(result["tag_count"], 8)
        self.assertEqual(result["unit_count"], 11)  # 三个空位置也保留。
        self.assertEqual([u["tag"] for u in index["units"] if u["tag"]],
                         ["<C90>", "<C3>", "<C4>", "<C8>", "<C9>", "<C10>", "<C2>", "<C1>"])
        cells = index["tables"][0]["cell_unit_ids"]
        self.assertEqual(cells[0][1], cells[0][2])
        self.assertEqual(len(cells[0][2]), 2)
        self.assertEqual(cells[1][0], cells[2][0])
        for view in ("machine", "compact"):
            pages = self.collect(Path(result["index_path"]), max_bytes=4096, view=view)
            self.assertEqual(self.reconstructed(pages), {u["unit_id"]: u["source_text"] for u in index["units"]})
            contexts = [context for page in pages for context in (page["model_view"]["table_context"] if view == "compact" else page["table_context"])]
            self.assertTrue(any(context.get("cell_aliases") for context in contexts))
        self.assertEqual(json.loads((self.root / "input.json").read_text(encoding="utf-8")), source)

    def test_vertical_alias_never_moves_tag_endpoint_or_search_hit_count(self):
        result = self.prepare(self.merged_source())
        path = Path(result["index_path"])
        page = doc.read_document(path, start="<C8>", end="<C8>", view="compact")
        self.assertEqual(page["tags"], ["<C8>"])
        self.assertNotIn("下一设备", page["text"])
        search = doc.search_document(path, "设备名称", neighbors=0, view="compact")
        self.assertEqual(search["match_count_total"], 1)
        self.assertEqual(search["model_view"]["matched_tags"], ["<C8>"])

    def test_long_merged_anchor_is_read_once_with_lossless_unicode_pagination(self):
        shared = "<C30>接口🌐及供电。" + "长段原文🌐\n" * 400
        result = self.prepare([{"table": {"rows": [[shared, shared]], "merges": [
            {"top_left": {"row": 0, "col": 0}, "row_span": 1, "col_span": 2}]}}])
        path = Path(result["index_path"])
        pages = self.collect(path, max_bytes=2048, view="compact")
        self.assertGreater(len(pages), 1)
        self.assertEqual(self.reconstructed(pages), {"u000001": shared})

    def test_large_vertical_merge_does_not_repeat_all_alias_coordinates_in_each_page(self):
        result = self.prepare([{"table": {"rows": [["<C1>共同要求"] for _ in range(1000)], "merges": [
            {"top_left": {"row": 0, "col": 0}, "row_span": 1000, "col_span": 1}]}}])
        self.assertEqual(result["unit_count"], 1)
        self.assertEqual(result["merged_cell_alias_count"], 999)
        pages = self.collect(Path(result["index_path"]), max_bytes=2048, view="compact")
        self.assertEqual(len(pages), 1)
        context = pages[0]["model_view"]["table_context"][0]
        self.assertEqual(context["merged_cell_alias_count"], 999)
        self.assertEqual(context["merges"][0]["row_span"], 1000)
        self.assertNotIn("cell_aliases", context)

    def test_wide_horizontal_merge_keeps_read_budget_and_neighbor_alias_reference(self):
        result = self.prepare([{"table": {"rows": [["<C1>共同要求"] * 1000 + ["<C2>下一设备"]], "merges": [
            {"top_left": {"row": 0, "col": 0}, "row_span": 1, "col_span": 1000}]}}])
        self.assertEqual(result["unit_count"], 2)
        pages = self.collect(Path(result["index_path"]), max_bytes=2048, view="compact")
        self.assertEqual(self.reconstructed(pages), {"u000001": "<C1>共同要求", "u000002": "<C2>下一设备"})
        self.assertTrue(all(len(page["model_view"]["table_context"][0].get("cell_aliases", [])) <= 2 for page in pages))

    def test_duplicate_alias_requires_complete_string_equality_and_explicit_merge(self):
        base = [{"table": {"rows": [["<C1>名称", "<C1>名称"]], "merges": [
            {"top_left": {"row": 0, "col": 0}, "row_span": 1, "col_span": 2}]}}]
        cases = []
        missing = copy.deepcopy(base)
        missing[0]["table"]["merges"] = []
        cases.append(missing)
        different = copy.deepcopy(base)
        different[0]["table"]["rows"][0][1] += " "
        cases.append(different)
        cases.append([{"text": "<C1>名称<C1>名称"}])
        cases.append([{"table": {"rows": [["<C1>名称"]]}}, {"table": {"rows": [["<C1>名称"]]}}])
        # 合并区域中出现另一个不同非空内容，不能以部分副本冒充整格。
        inconsistent = copy.deepcopy(base)
        inconsistent[0]["table"]["rows"][0].append("<C2>不同设备")
        inconsistent[0]["table"]["merges"][0]["col_span"] = 3
        cases.append(inconsistent)
        for source in cases:
            with self.subTest(source=source), self.assertRaisesRegex(doc.DocumentError, "重复"):
                self.prepare(source)

    def test_invalid_or_overlapping_merge_cannot_prove_duplicate_tags(self):
        merge = {"top_left": {"row": 0, "col": 0}, "row_span": 1, "col_span": 2}
        bad = [dict(merge, col_span=3), dict(merge, row_span=True), dict(merge, row_span=0),
               dict(merge, col_span=2.0), dict(merge, top_left={"row": -1, "col": 0})]
        for item in bad:
            with self.subTest(merge=item), self.assertRaisesRegex(doc.DocumentError, "重复"):
                self.prepare([{"table": {"rows": [["<C1>x", "<C1>x"]], "merges": [item]}}])
        for extra in (merge, {"top_left": {"row": 0, "col": 1}, "row_span": 1, "col_span": 1}):
            with self.subTest(extra=extra), self.assertRaisesRegex(doc.DocumentError, "重复"):
                self.prepare([{"table": {"rows": [["<C1>x", "<C1>x"]], "merges": [merge, extra]}}])
        with self.assertRaisesRegex(doc.DocumentError, "重复"):
            self.prepare([{"table": {"rows": [["<C1>x", "<C2>y"], ["<C1>x"]],
                "merges": [{"top_left": {"row": 0, "col": 0}, "row_span": 2, "col_span": 2}]}}])

    def test_alias_validation_rejects_tampering_even_when_outer_hash_is_updated(self):
        result = self.prepare(self.merged_source())
        path = Path(result["index_path"])
        original = json.loads(path.read_text(encoding="utf-8"))
        for field in ("merged_cell_aliases", "merged_cell_alias_policy", "tables", "units", "tag_to_unit"):
            changed = copy.deepcopy(original)
            if field in ("merged_cell_aliases", "merged_cell_alias_policy"):
                changed.pop(field)
            elif field == "tables":
                changed[field][0]["cell_unit_ids"][2][0] = changed[field][0]["cell_unit_ids"][2][1]
            elif field == "units":
                changed[field][0]["source_text"] = "伪造原文"
            else:
                changed[field]["<C8>"] = changed[field]["<C2>"]
            with self.subTest(field=field), self.assertRaises(doc.DocumentError):
                doc.validate_merged_aliases(changed)
            path.write_bytes(doc.json_bytes(changed))
            with self.assertRaises(doc.DocumentError):
                doc.load_index(path)

    def test_no_alias_keeps_legacy_schema_and_demo_with_aliases_is_explicit(self):
        result = self.prepare([{"table": {"rows": [["<C1>同名", "<C2>同名"]],
            "merges": [{"raw_legacy_metadata": "尚未标准化的历史布局"}]}}])
        index, _ = doc.load_index(Path(result["index_path"]))
        self.assertNotIn("merged_cell_aliases", index)
        self.assertEqual(result["tag_count"], 2)
        source = self.merged_source() + [{"text": "没有标签的演示内容"}]
        result = self.prepare(source, demo=True)
        index, _ = doc.load_index(Path(result["index_path"]))
        self.assertTrue(index["demo"])
        self.assertFalse(index["production_submittable"])
        self.assertEqual(len(index["merged_cell_aliases"]), 2)

    def test_range_uses_source_order_and_empty_cell_kept(self):
        result = self.prepare([{"text": "正文<C99>"}, {"table": {"rows": [["<C2>参数", ""]], "merges": []}}, {"text": "<C1>末段"}])
        page = doc.read_document(Path(result["index_path"]), start="<C99>", end="<C2>")
        self.assertEqual(page["tags"], ["<C99>", "<C2>"])
        with self.assertRaisesRegex(doc.DocumentError, "颠倒"):
            doc.read_document(Path(result["index_path"]), start="<C1>", end="<C99>")
        index, _ = doc.load_index(Path(result["index_path"]))
        self.assertEqual(index["units"][2]["source_text"], "")
        self.assertIsNone(index["units"][2]["tag"])

    def test_search_paginates_all_matches_without_losing_tags(self):
        result = self.prepare([{"text": f"<C{n}>Firewall 参数 {n}"} for n in range(1, 18)])
        pages = self.collect(Path(result["index_path"]), search="firewall", limit=3)
        self.assertGreater(len(pages), 1)
        self.assertTrue(pages[0]["has_more"])
        all_tags = []
        for page in pages:
            self.assertLessEqual(len(page["matched_unit_ids"]), 3)
            self.assertEqual(page["match_count_total"], 17)
            all_tags.extend(page["tags"])
        self.assertEqual(all_tags, [f"<C{n}>" for n in range(1, 18)])
        missing = doc.search_document(Path(result["index_path"]), "不存在")
        self.assertFalse(missing["has_more"])
        self.assertEqual(missing["parts"], [])

    def test_demo_avoids_existing_tags_and_keeps_raw_text(self):
        source = [{"text": "<C000001>已有"}, {"text": "未标记"}]
        result = self.prepare(source, demo=True)
        index, _ = doc.load_index(Path(result["index_path"]))
        self.assertEqual(index["units"][1]["tag"], "<C000002>")
        self.assertEqual(index["units"][1]["source_text"], "未标记")
        self.assertTrue(index["units"][1]["text"].startswith("<C000002>\n"))
        self.assertEqual(json.loads((self.root / "input.json").read_text(encoding="utf-8")), source)

    def test_tampered_index_and_too_small_output_are_errors(self):
        result = self.prepare([{"text": "<C1>test"}])
        path = Path(result["index_path"])
        with self.assertRaises(doc.DocumentError):
            doc.read_document(path, max_bytes=512)
        path.write_bytes(path.read_bytes() + b" ")
        with self.assertRaisesRegex(doc.DocumentError, "SHA256"):
            doc.read_document(path)

    def test_unique_merge_value_visible_and_large_unique_value_rejected(self):
        merge = {"id": "M1", "value": "独有要求：必须具备双电源", "top_left": {"row": 0, "col": 0}, "row_span": 1, "col_span": 2}
        result = self.prepare([{"table": {"rows": [["<C1>", ""]], "merges": [merge]}}])
        page = doc.read_document(Path(result["index_path"]))
        seen = page["table_context"][0]["merges"][0]
        self.assertEqual(seen["value"], merge["value"])
        self.assertFalse(seen["value_in_anchor"])
        self.assertEqual(seen["anchor_unit_id"], "u000001")
        merge["value"] = "独有要求" * 15000
        with self.assertRaisesRegex(doc.DocumentError, "merge.value"):
            self.prepare([{"table": {"rows": [["<C1>", ""]], "merges": [merge]}}])

    def test_long_duplicate_merge_value_uses_anchor_without_content_loss(self):
        body = "原文😀\n" * 2000
        merge = {"id": "M1", "value": body, "top_left": {"row": 0, "col": 0}, "row_span": 1, "col_span": 1}
        result = self.prepare([{"table": {"rows": [["<C1>" + body]], "merges": [merge]}}])
        path = Path(result["index_path"])
        index, _ = doc.load_index(path)
        self.assertEqual(index["units"][0]["source_length"], len("<C1>" + body))
        pages = self.collect(path, max_bytes=2500)
        for page in pages:
            seen = page["table_context"][0]["merges"][0]
            self.assertTrue(seen["value_in_anchor"])
            self.assertNotIn("value", seen)
        self.assertEqual(self.reconstructed(pages)["u000001"], "<C1>" + body)

    def test_cli_utf8_and_exact_stdout_size(self):
        result = self.prepare([{"text": "<C1>中文😀证据\n" * 1}])
        run = subprocess.run([sys.executable, str(Path(doc.__file__)), "read", "--index", result["index_path"], "--max-bytes", "1800"], capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr.decode("utf-8"))
        response = json.loads(run.stdout.decode("utf-8"))
        self.assertEqual(len(run.stdout), response["byte_size"])
        self.assertLessEqual(len(run.stdout), 1800)
        part = response["parts"][0]
        self.assertEqual(response["text"][part["content_start"]:part["content_end"]], "<C1>中文😀证据\n")

    def test_multi_tag_text_preserves_prefix_and_source_coordinates(self):
        original = "  前言😀\n<C90>Firewall\n<C3>WAN Switch\n<C11>末尾\n"
        source = [{"text": original}]
        result = self.prepare(source)
        path = Path(result["index_path"])
        index, _ = doc.load_index(path)
        self.assertEqual(index["schema_version"], 2)
        self.assertFalse(index["demo"])
        self.assertTrue(index["production_submittable"])
        self.assertEqual(index["source_document"], source)
        units = index["units"]
        self.assertEqual([u["tag"] for u in units], ["<C90>", "<C3>", "<C11>"])
        self.assertEqual([u["order"] for u in units], [0, 1, 2])
        self.assertEqual([u["source_start"] for u in units], [0, original.index("<C3>"), original.index("<C11>")])
        self.assertEqual([u["source_end"] for u in units], [original.index("<C3>"), original.index("<C11>"), len(original)])
        for unit in units:
            self.assertEqual(unit["source_text"], original[unit["source_start"]:unit["source_end"]])
            self.assertEqual(unit["source_length"], unit["source_end"] - unit["source_start"])
            self.assertEqual(unit["tag_offset"], original.index(unit["tag"]))
            self.assertEqual(unit["unit_tag_offset"], unit["tag_offset"] - unit["source_start"])
            self.assertFalse(unit["tag_is_synthetic"])
            self.assertEqual(index["tag_to_unit"][unit["tag"]], unit["unit_id"])
        seen = self.reconstructed(self.collect(path))
        self.assertEqual("".join(seen[u["unit_id"]] for u in units), original)
        self.assertEqual(json.loads((self.root / "input.json").read_text(encoding="utf-8")), source)

    def test_multi_tag_cell_range_search_and_merge_anchor_ids(self):
        cell = "prefix <C91>Alpha\n<C2>Béta\n<C15>Gamma"
        value = doc.TAG_RE.sub("", cell).strip()
        merge = {"id": "M1", "value": value, "top_left": {"row": 0, "col": 0}, "row_span": 1, "col_span": 2}
        source = [{"table": {"rows": [[cell, "<C70>CAT-D"]], "merges": [merge]}}]
        result = self.prepare(source)
        path = Path(result["index_path"])
        index, _ = doc.load_index(path)
        table = index["tables"][0]
        self.assertEqual(table["rows"], source[0]["table"]["rows"])
        self.assertEqual(table["merges"], [merge])
        self.assertEqual(table["cell_unit_ids"], [[["u000001", "u000002", "u000003"], ["u000004"]]])
        self.assertEqual(table["unit_ids"], [["u000001", "u000004"]])
        page = doc.read_document(path, start="<C2>", end="<C15>")
        self.assertEqual(page["tags"], ["<C2>", "<C15>"])
        seen = self.reconstructed([page])
        self.assertEqual("".join(seen[p["unit_id"]] for p in page["parts"]), cell[cell.index("<C2>"):])
        for part in page["parts"]:
            self.assertEqual((part["row"], part["col"]), (0, 0))
        layout = page["table_context"][0]["merges"][0]
        self.assertEqual(layout["anchor_unit_ids"], ["u000001", "u000002", "u000003"])
        self.assertNotIn("anchor_unit_id", layout)
        self.assertTrue(layout["value_in_anchor"])
        self.assertNotIn("value", layout)
        internal = doc.search_document(path, "<C2>", neighbors=0)
        self.assertEqual(internal["tags"], ["<C2>"])
        self.assertEqual(internal["hits"][0]["source_match_start"], cell.index("<C2>"))
        term = doc.search_document(path, "BÉTA", neighbors=0)
        self.assertEqual(term["matched_unit_ids"], ["u000002"])
        self.assertEqual(term["hits"][0]["source_match_end"], cell.index("Béta") + len("Béta"))
        with self.assertRaisesRegex(doc.DocumentError, "颠倒"):
            doc.read_document(path, start="<C15>", end="<C2>")

    def test_multi_tag_long_cell_pages_reconstruct_whole_original(self):
        original = "  prefix\n<C81>" + "中文😀\\\"\n" * 700 + "<C7>" + "续段\n" * 500 + "<C1000>末尾  "
        result = self.prepare([{"table": {"rows": [[original, "   "]], "merges": []}}])
        path = Path(result["index_path"])
        index, _ = doc.load_index(path)
        pages = self.collect(path, max_bytes=3000)
        seen = self.reconstructed(pages)
        cell_units = index["tables"][0]["cell_unit_ids"][0][0]
        self.assertEqual("".join(seen[unit_id] for unit_id in cell_units), original)
        self.assertEqual(seen[index["tables"][0]["cell_unit_ids"][0][1][0]], "   ")
        units = {unit["unit_id"]: unit for unit in index["units"]}
        for page in pages:
            for part in page["parts"]:
                unit = units[part["unit_id"]]
                self.assertEqual(part["total_length"], unit["source_length"])
                self.assertEqual(part["source_part_start"], unit["source_start"] + part["part_start"])
                self.assertEqual(part["source_part_end"], unit["source_start"] + part["part_end"])
                if part["part_start"] > 0 and part["tag"]:
                    self.assertIn(part["tag"], page["partial_tags"])
                    self.assertNotIn(part["tag"], page["complete_tags"])

    def test_single_tag_at_end_stays_whole_and_legacy_index_rejected(self):
        original = "前置正文😀\n <C9>  "
        result = self.prepare([{"text": original}])
        path = Path(result["index_path"])
        index, _ = doc.load_index(path)
        unit = index["units"][0]
        self.assertEqual(unit["source_text"], original)
        self.assertEqual((unit["source_start"], unit["source_end"]), (0, len(original)))
        self.assertEqual(unit["tag_offset"], original.index("<C9>"))
        legacy = self.root / "legacy.json"
        legacy.write_bytes(doc.json_bytes({"schema_version": 1, "units": [], "tag_to_unit": {}}))
        with self.assertRaisesRegex(doc.DocumentError, "版本不兼容"):
            doc.load_index(legacy)

    def test_compact_groups_cell_without_duplicating_unit_headers(self):
        cell = " prefix😀\n<C90>Alpha  \n<C2>Béta\n<C15>Gamma "
        merge = {"id": "M1", "value": doc.TAG_RE.sub("", cell).strip(), "top_left": {"row": 0, "col": 0}, "row_span": 1, "col_span": 2}
        result = self.prepare([{"table": {"rows": [[cell, ""]], "merges": [merge]}}])
        path = Path(result["index_path"])
        page = self.collect(path, view="compact")[0]
        model_view = page["model_view"]
        self.assertEqual(model_view["text"].count("[T t0001 b0 r0 c0]"), 1)
        self.assertIn(cell, model_view["text"])
        self.assertNotIn("u000001", model_view["text"])
        self.assertEqual(model_view["unit_count"], 4)
        self.assertEqual(set(model_view), {"cursor", "next_cursor", "has_more", "text", "table_context", "partial_tags", "unit_count", "delivery_note", "next_action", "byte_size"})
        index, _ = doc.load_index(path)
        self.assertEqual(self.reconstructed([page]), {u["unit_id"]: u["source_text"] for u in index["units"]})
        layout = model_view["table_context"][0]["merges"][0]
        self.assertEqual(layout["value_from_cell"], {"row": 0, "col": 0})
        self.assertNotIn("anchor_unit_ids", layout)
        self.assertNotIn("duplicate_proof", layout)
        self.assertEqual(page["table_context"][0]["merges"][0]["anchor_unit_ids"], ["u000001", "u000002", "u000003"])

    def test_compact_long_unit_multiple_pages_use_exact_source_offsets(self):
        original = "<C91>" + "中文🌐\"\\\n  " * 1400 + "tail"
        result = self.prepare([{"table": {"rows": [[original]], "merges": []}}])
        path = Path(result["index_path"])
        pages = self.collect(path, max_bytes=2048, view="compact")
        self.assertGreater(len(pages), 1)
        self.assertEqual(self.reconstructed(pages)["u000001"], original)
        for page in pages:
            self.assertIn("chars=", page["model_view"]["text"])
            self.assertEqual(page["model_view"]["partial_tags"], ["<C91>"])
            self.assertNotIn("<C91>", page["complete_tags"])
            action = page["model_view"]["next_action"]
            if page["has_more"]:
                self.assertIn("增量保存暂定候选和疑点", action)
                self.assertIn("next_cursor", action)
            else:
                self.assertIn("本次读取范围已到末尾", action)
                self.assertIn("下一步立即调用本代理的候选保存工具", action)
                self.assertIn("可待核验", action)
        self.assertFalse(pages[-1]["has_more"])
        self.assertTrue(pages[-1]["parts"][0]["ends_unit"])
        self.assertGreater(pages[-1]["parts"][0]["part_start"], 0)

    def test_compact_range_end_does_not_claim_full_document_read(self):
        result = self.prepare([{"text": "<C1>前文"}, {"text": "<C2>待核验设备"}, {"text": "<C3>后文"}])
        page = doc.read_document(Path(result["index_path"]), start="<C2>", end="<C2>", view="compact")
        self.assertFalse(page["has_more"])
        self.assertEqual(page["tags"], ["<C2>"])
        self.assertEqual(self.reconstructed([page]), {"u000002": "<C2>待核验设备"})
        action = page["model_view"]["next_action"]
        self.assertIn("本次读取范围已到末尾", action)
        self.assertIn("范围补读结束不等于全文读完", action)
        self.assertIn("首次通读至此时", action)
        self.assertLessEqual(len(doc.sized_bytes(page["model_view"])), doc.MAX_BYTES)

    def test_compact_search_does_not_stitch_noncontiguous_segments(self):
        original = "prefix<C91>MATCH one\n<C2>quiet middle\n<C15>MATCH two"
        result = self.prepare([{"text": original}])
        path = Path(result["index_path"])
        pages = self.collect(path, search="MATCH", view="compact", limit=1)
        self.assertEqual(len(pages), 2)
        for page in pages:
            model_view = page["model_view"]
            self.assertEqual(model_view["match_count_total"], 2)
            self.assertEqual(len(model_view["matched_tags"]), 1)
            self.assertNotIn("hits", model_view)
            self.assertNotIn("excerpt", model_view)
            self.assertEqual(model_view["matched_tags"], page["tags"])
        combined = doc.search_document(path, "MATCH", neighbors=0, view="compact")["model_view"]["text"]
        self.assertEqual(combined.count("[P b0 chars="), 2)
        self.assertNotIn("quiet middle", combined)

    def test_compact_unique_merge_value_remains_visible(self):
        merge = {"id": "M1", "value": "独有配置参数🌐", "top_left": {"row": 0, "col": 0}, "row_span": 1, "col_span": 1}
        result = self.prepare([{"table": {"rows": [["<C1>"]], "merges": [merge]}}])
        page = doc.read_document(Path(result["index_path"]), view="compact")
        self.assertEqual(page["model_view"]["table_context"][0]["merges"][0]["value"], merge["value"])

    def test_compact_unit_count_and_ipc_caps_cause_pagination(self):
        source = [{"text": f"<C{n}>x"} for n in range(600)]
        result = self.prepare(source)
        path = Path(result["index_path"])
        pages = self.collect(path, view="compact")
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0]["model_view"]["unit_count"], 512)
        self.assertEqual(pages[0]["next_cursor"], "512:0")
        index, _ = doc.load_index(path)
        self.assertEqual(self.reconstructed(pages), {u["unit_id"]: u["source_text"] for u in index["units"]})
        with mock.patch.object(doc, "MAX_IPC_BYTES", 8192):
            capped = self.collect(path, view="compact")
            self.assertGreater(len(capped), 2)
            self.assertEqual(self.reconstructed(capped), {u["unit_id"]: u["source_text"] for u in index["units"]})

    def test_compact_table_is_one_page_with_full_original_coverage(self):
        rows = [["\n".join(f"<C{row * 6 + part + 1}>设备规格" for part in range(6))]
                for row in range(50)]
        result = self.prepare([{"table": {"rows": rows, "merges": []}}])
        path = Path(result["index_path"])
        index, _ = doc.load_index(path)
        pages = self.collect(path, view="compact")
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["model_view"]["unit_count"], len(index["units"]))
        self.assertEqual(self.reconstructed(pages), {u["unit_id"]: u["source_text"] for u in index["units"]})
        self.assertEqual(pages[0]["partial_tags"], [])
        self.assertGreater(pages[0]["byte_size"], doc.MAX_BYTES)
        self.assertLessEqual(pages[0]["model_view"]["byte_size"], doc.MAX_BYTES)

    def test_compact_cli_budget_applies_to_model_view_not_full_ipc(self):
        result = self.prepare([{"text": f"<C{n}>短句"} for n in range(50)])
        run = subprocess.run([sys.executable, str(Path(doc.__file__)), "read", "--index", result["index_path"], "--view", "compact", "--max-bytes", "2048"], capture_output=True)
        self.assertEqual(run.returncode, 0, run.stderr.decode("utf-8"))
        response = json.loads(run.stdout.decode("utf-8"))
        self.assertEqual(len(run.stdout), response["byte_size"])
        self.assertGreater(len(run.stdout), 2048)
        self.assertLessEqual(response["model_view"]["byte_size"], 2048)
        self.assertLessEqual(len(run.stdout), doc.MAX_IPC_BYTES)


if __name__ == "__main__":
    unittest.main()
