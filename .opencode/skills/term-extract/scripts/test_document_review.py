"""合成标书验证网页反馈的版本、文件同步及可信学习闭环；不调用模型。"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import document_io
import document_review as review
import memory_learning as learning
import memory_store as store


class DocumentReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="term-document-review-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"TERM_EXTRACT_MEMORY_DIR": ""})
        env.start()
        self.addCleanup(env.stop)
        self.input = self.root / "设备采购.json"
        self.source = [{"text": "<C9> Core Switch"}, {"text": "<C2> 采购2台，48口"},
                       {"table": {"rows": [["<C3> AP", "<C4> 5台"]], "merges": []}}]
        self.write(self.input, self.source)
        prepared = document_io.prepare(self.input, self.root / "task")
        self.index_path = Path(prepared["index_path"])
        self.index = store._read_json(self.index_path)
        self.result = [{"id": 0, "条款标题": "Core Switch", "条款分类": "normal", "相关条款": [],
                        "产品分类": "盒式交换机", "定位标识": ["<C9>", "<C2>"], "产品数量": "48台"},
                       {"id": 1, "条款标题": "AP", "条款分类": "normal", "相关条款": [],
                        "产品分类": "WLAN AP", "定位标识": ["<C3>", "<C4>"], "产品数量": "5台"}]
        self.evidence = {"clauses": [{"id": 0, "quantity_source_tags": ["<C2>"], "related_source_tags": [], "notes": "合成测试"},
                                     {"id": 1, "quantity_source_tags": ["<C4>"], "related_source_tags": [], "notes": "合成测试"}], "unresolved": []}
        self.result_path, self.evidence_path, self.run_path = [self.root / name for name in ("result.json", "evidence.json", "run.json")]
        self.write(self.result_path, self.result)
        self.write(self.evidence_path, self.evidence)
        self.run = {"status": "submitted", "production_submittable": True, "source_sha256": self.index["source_sha256"],
                    "coverage": {"complete": True, "index_sha256": store._file_hash(self.index_path)},
                    "result_sha256": store._file_hash(self.result_path), "evidence_sha256": store._file_hash(self.evidence_path)}
        self.write(self.run_path, self.run)
        registered = learning.register_run(self.root, self.index_path, self.result_path, self.evidence_path, self.run_path)
        self.run_id = registered["run_id"]
        self.docid = "doc_" + self.index["source_sha256"][:32]

    def write(self, path, value):
        Path(path).write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def detail(self):
        return review.get_review(self.root, self.run_id)

    def post(self, endpoint, **body):
        body.setdefault("run_id", self.run_id)
        body.setdefault("expected_revision", self.detail()["revision"])
        return review.handle_post(self.root, [self.docid, endpoint], body)

    def verify(self):
        return self.post("verify", reviewer="合成人工校验者", family_id="synthetic-family-A", note="整篇已核验", acknowledged=True)

    def edit_quantity(self):
        return self.post("edit", action="update", clause_key=self.detail()["clauses"][0]["clause_key"], patch={"产品数量": "2台"})

    def test_get_never_initializes_review_store_and_groups_runs(self):
        path = store.resolve_store_dir(self.root) / "document-review.sqlite3"
        before = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        value = review.handle_get(self.root, [], {})
        self.assertEqual(value["counts"]["pending"], 1)
        self.assertEqual(value["documents"][0]["run_count"], 1)
        detail = review.handle_get(self.root, [self.docid], {})
        self.assertEqual(detail["revision"], 0)
        self.assertEqual(detail["result"], self.result)
        self.assertFalse(path.exists())
        after = {p: p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_empty_project_reads_do_not_create_any_directory(self):
        empty = self.root / "missing"
        self.assertEqual(review.list_documents(empty)["documents"], [])
        self.assertEqual(review.list_verified(empty), {"reviews": []})
        self.assertFalse(empty.exists())

    def test_ensure_creates_small_isolated_working_copy_idempotently(self):
        value = review.ensure_run(self.root, self.run_id)
        self.assertLess(len(json.dumps(value, ensure_ascii=False).encode()), 2048)
        self.assertEqual(value["revision"], 1)
        self.assertEqual(value, review.ensure_run(self.root, self.run_id))
        path = Path(value["current_result_path"])
        self.assertEqual(path.parent.name, self.run_id)
        self.assertEqual(path.parent.parent.name, "review")
        self.assertEqual(store._read_json(path), self.result)
        self.assertNotEqual(str(path), self.detail()["original_result_path"])

    def test_edit_boundary_quantity_non_target_and_restore_keep_keys(self):
        original = self.detail()["original_result_path"]
        original_bytes = Path(original).read_bytes()
        initial = self.detail()
        key0, key1 = [item["clause_key"] for item in initial["clauses"]]
        changed = self.edit_quantity()
        self.assertEqual(changed["status"], "modified")
        self.assertEqual(changed["result"][0]["产品数量"], "2台")
        removed = self.post("edit", action="non_target", clause_key=key0, reason="测试非目标")
        self.assertEqual(removed["result"][0]["id"], 0)
        self.assertEqual(removed["result"][0]["条款标题"], "AP")
        self.assertEqual(removed["clauses"][1]["clause_key"], key1)
        restored = self.post("edit", action="restore", clause_key=key0)
        self.assertEqual([item["clause_key"] for item in restored["clauses"]], [key0, key1])
        self.assertEqual(restored["result"][0]["产品数量"], "2台")
        self.assertEqual(Path(original).read_bytes(), original_bytes)
        self.assertEqual(store._read_json(self.result_path), self.result)

    def test_relationship_keys_survive_id_reindexing(self):
        first, second = self.detail()["clauses"]
        self.post("edit", action="update", clause_key=first["clause_key"], patch={"条款分类": "general"}, related_keys=[second["clause_key"]])
        changed = self.post("edit", action="delete", clause_key=second["clause_key"])
        self.assertEqual(changed["result"][0]["相关条款"], [])
        self.assertFalse(changed["validation"]["ok"])
        restored = self.post("edit", action="restore", clause_key=second["clause_key"])
        self.assertEqual(restored["result"][0]["相关条款"], [1])

    def test_drafts_can_be_invalid_but_verify_requires_structure(self):
        key = self.detail()["clauses"][0]["clause_key"]
        draft = self.post("edit", action="update", clause_key=key, patch={"定位标识": ["<C999>", "<C2>"]})
        self.assertFalse(draft["validation"]["ok"])
        with self.assertRaisesRegex(store.MemoryError, "尚未通过"):
            self.verify()
        self.assertEqual(review.list_verified(self.root), {"reviews": []})

    def test_explicit_verification_freezes_labels_and_lists_learning(self):
        self.edit_quantity()
        with self.assertRaisesRegex(store.MemoryError, "主动确认"):
            self.post("verify", reviewer="test", family_id="family-A")
        value = self.verify()
        self.assertEqual(value["status"], "verified")
        self.assertEqual(value["learning"]["status"], "pending")
        source = review.verified_for_learning(self.root, self.run_id, value["revision"])
        self.assertEqual(source["after_result"][0]["产品数量"], "2台")
        self.assertEqual(source["before_result"][0]["产品数量"], "48台")
        self.assertEqual(store._read_json(Path(source["labels_path"])), source["after_result"])
        self.assertEqual(source["operations"][0]["action"], "update")
        self.assertEqual(len(review.list_verified(self.root)["reviews"]), 1)
        changed = self.post("edit", action="update", clause_key=value["clauses"][0]["clause_key"], patch={"产品数量": "3台"})
        self.assertEqual(changed["learning"]["status"], "not_ready")
        with self.assertRaisesRegex(store.MemoryError, "尚未人工校验"):
            review.verified_for_learning(self.root, self.run_id, value["revision"])

    def test_reviewer_limit_matches_trusted_case_declaration(self):
        with self.assertRaisesRegex(store.MemoryError, "160"):
            self.post("verify", reviewer="审" * 161, family_id="synthetic-family-A", acknowledged=True)
        self.assertEqual(self.detail()["revision"], 0)
        value = self.post("verify", reviewer="审" * 160, family_id="synthetic-family-A", acknowledged=True)
        self.assertEqual(value["reviewer"], "审" * 160)

    def test_learning_record_is_idempotent_and_stale_rejected(self):
        value = self.verify()
        args = (self.root, self.run_id, value["revision"], [], {"session_id": "test-session"}, "合成数据未形成普适经验")
        learned = review.record_learning(*args)
        self.assertEqual(review.record_learning(*args), learned)
        self.assertEqual(self.detail()["learning"]["status"], "learned")
        self.assertEqual(review.list_verified(self.root), {"reviews": []})
        self.assertEqual(review.verified_for_learning(self.root, self.run_id, value["revision"])["learning"], learned)
        with self.assertRaisesRegex(store.MemoryError, "不同学习结论"):
            review.record_learning(self.root, self.run_id, value["revision"], [], {}, "不同原因")
        self.edit_quantity()
        with self.assertRaisesRegex(store.MemoryError, "尚未人工校验"):
            review.record_learning(*args)

    def test_stale_revision_cannot_overwrite_current_draft(self):
        old = self.detail()
        self.edit_quantity()
        with self.assertRaisesRegex(store.MemoryError, "expected_revision"):
            self.post("edit", expected_revision=old["revision"], action="delete", clause_key=old["clauses"][0]["clause_key"])
        self.assertEqual(len(self.detail()["result"]), 2)

    def test_external_file_change_requires_explicit_sync_and_reverification(self):
        verified = self.verify()
        path = Path(verified["current_result_path"])
        external = copy.deepcopy(self.result)
        external[0]["产品数量"] = "2台"
        self.write(path, external)
        dirty = self.detail()
        self.assertTrue(dirty["sync"]["dirty"])
        self.assertEqual(dirty["status"], "modified")
        self.assertEqual(dirty["result"][0]["产品数量"], "48台")
        with self.assertRaisesRegex(store.MemoryError, "先点击同步"):
            self.verify()
        self.assertEqual(store._read_json(path), external)
        synced = self.post("sync")
        self.assertFalse(synced["sync"]["dirty"])
        self.assertEqual(synced["result"][0]["产品数量"], "2台")
        self.assertEqual(synced["status"], "modified")
        self.assertEqual(synced["clauses"][0]["clause_key"], verified["clauses"][0]["clause_key"])
        self.assertEqual(self.verify()["status"], "verified")

    def test_external_unknown_links_are_not_silently_lost(self):
        value = review.ensure_run(self.root, self.run_id)
        path = Path(value["current_result_path"])
        external = copy.deepcopy(self.result)
        external[0]["相关条款"] = [999]
        self.write(path, external)
        before = path.read_bytes()
        with self.assertRaisesRegex(store.MemoryError, "禁止静默丢弃关联"):
            self.post("sync")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.detail()["revision"], value["revision"])

    def test_edit_notes_are_preserved_in_history_and_learning_operations(self):
        key = self.detail()["clauses"][0]["clause_key"]
        value = self.post("edit", action="update", clause_key=key, patch={"产品数量": "2台"}, note="48是端口数，采购数量为2台")
        self.assertEqual(value["history"][0]["note"], "48是端口数，采购数量为2台")
        verified = self.verify()
        source = review.verified_for_learning(self.root, self.run_id, verified["revision"])
        self.assertEqual(source["operations"][0]["note"], "48是端口数，采购数量为2台")

    def test_malformed_external_file_blocks_without_overwriting(self):
        self.verify()
        path = Path(self.detail()["current_result_path"])
        path.write_text('{"broken":', encoding="utf-8")
        self.assertTrue(self.detail()["sync"]["blocked"])
        self.assertEqual(self.detail()["status"], "modified")
        self.assertEqual(self.detail()["learning"]["status"], "not_ready")
        with self.assertRaisesRegex(store.MemoryError, "无法读取"):
            self.post("sync")
        self.assertEqual(path.read_text(encoding="utf-8"), '{"broken":')

    def test_source_drift_blocks_verify_and_learning_but_archive_can_be_read(self):
        value = self.verify()
        self.write(self.input, [{"text": "<C9> changed"}])
        detail = self.detail()
        self.assertTrue(detail["sync"]["blocked"])
        source = review.handle_get(self.root, [self.docid, "source"], {"tag": "<C2>"})
        self.assertIn("采购2台", source["text"])
        with self.assertRaisesRegex(store.MemoryError, "源标书"):
            self.verify()
        with self.assertRaisesRegex(store.MemoryError, "源标书"):
            review.verified_for_learning(self.root, self.run_id, value["revision"])
        self.assertEqual(review.list_verified(self.root), {"reviews": []})

    def test_add_missing_clause_then_delete_restore_preserves_audit(self):
        added = self.post("edit", action="add", clause={"条款标题": "遗漏设备", "条款分类": "normal", "相关条款": [],
                             "产品分类": "参考分类", "定位标识": ["<C2>", "<C2>"], "产品数量": "未提及"})
        key = added["clauses"][-1]["clause_key"]
        self.assertEqual(len(added["result"]), 3)
        removed = self.post("edit", action="delete", clause_key=key)
        self.assertEqual(len(removed["result"]), 2)
        restored = self.post("edit", action="restore", clause_key=key)
        self.assertEqual(len(restored["result"]), 3)
        self.assertEqual([item["action"] for item in restored["history"][:3]], ["edit", "edit", "edit"])

    def test_discard_whole_document_is_recoverable_and_not_learning_feedback(self):
        value = self.verify()
        labels = review.verified_for_learning(self.root, self.run_id, value["revision"])["labels_path"]
        original = Path(value["original_result_path"]).read_bytes()
        discarded = self.post("discard", note="用户不需要这篇结果")
        self.assertEqual(discarded["status"], "discarded")
        self.assertEqual(discarded["result"], [])
        self.assertEqual(store._read_json(Path(discarded["current_result_path"])), [])
        self.assertEqual(review.list_documents(self.root)["counts"]["discarded"], 1)
        self.assertEqual(review.list_verified(self.root), {"reviews": []})
        self.assertEqual(Path(value["original_result_path"]).read_bytes(), original)
        self.assertTrue(Path(labels).is_file())
        with self.assertRaisesRegex(store.MemoryError, "尚未人工校验"):
            review.verified_for_learning(self.root, self.run_id, value["revision"])
        restored = self.post("restore")
        self.assertEqual(len(restored["result"]), 2)
        self.assertIn(restored["status"], {"pending", "modified"})
        self.assertEqual(restored["learning"]["status"], "not_ready")
        self.assertFalse(restored["history"][0]["operations"][0]["learning_feedback"])

    def test_multiple_runs_group_and_discard_all_then_restore(self):
        first = review.ensure_run(self.root, self.run_id)
        self.run["model_id"] = "different-model"
        self.write(self.run_path, self.run)
        second = learning.register_run(self.root, self.index_path, self.result_path, self.evidence_path, self.run_path)
        other_id = second["run_id"]
        other = review.ensure_run(self.root, other_id)
        self.assertNotEqual(first["current_result_path"], other["current_result_path"])
        grouped = review.list_documents(self.root)["documents"]
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["run_count"], 2)
        self.assertEqual(grouped[0]["run_id"], other_id)
        self.post("discard")
        self.assertEqual(review.get_review(self.root, other_id)["status"], "discarded")
        self.assertEqual(store._read_json(Path(other["current_result_path"])), [])
        self.post("restore")
        self.assertEqual(len(review.get_review(self.root, other_id)["result"]), 2)

    def test_discard_preserves_dirty_work_files_and_hides_all_runs(self):
        first = review.ensure_run(self.root, self.run_id)
        self.run["model_id"] = "different-model"
        self.write(self.run_path, self.run)
        second = learning.register_run(self.root, self.index_path, self.result_path, self.evidence_path, self.run_path)
        other = review.ensure_run(self.root, second["run_id"])
        Path(first["current_result_path"]).write_text("[]", encoding="utf-8")
        before = Path(other["current_result_path"]).read_bytes()
        value = self.post("discard")
        self.assertEqual(Path(first["current_result_path"]).read_text(encoding="utf-8"), "[]")
        self.assertEqual(value["status"], "discarded")
        self.assertTrue(value["history"][0]["operations"][0]["working_file_preserved"])
        self.assertEqual(store._read_json(Path(other["current_result_path"])), [])
        self.assertEqual(review.get_review(self.root, second["run_id"])["status"], "discarded")

    def test_missing_source_can_be_discarded_and_restored_without_rewriting_work_file(self):
        value = self.verify()
        path = Path(value["current_result_path"])
        before = path.read_bytes()
        self.input.unlink()
        discarded = self.post("discard")
        self.assertEqual(discarded["status"], "discarded")
        self.assertEqual(path.read_bytes(), before)
        self.assertTrue(discarded["history"][0]["operations"][0]["working_file_preserved"])
        restored = self.post("restore")
        self.assertEqual(restored["status"], "modified")
        self.assertTrue(restored["sync"]["blocked"])
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(restored["learning"]["status"], "not_ready")

    def test_malformed_work_file_can_be_discarded_without_loss(self):
        value = review.ensure_run(self.root, self.run_id)
        path = Path(value["current_result_path"])
        path.write_text("broken local file", encoding="utf-8")
        self.assertEqual(self.post("discard")["status"], "discarded")
        self.assertEqual(path.read_text(encoding="utf-8"), "broken local file")
        self.assertTrue(self.post("restore")["sync"]["blocked"])

    def test_failed_second_discard_write_restores_first_and_database(self):
        first = review.ensure_run(self.root, self.run_id)
        self.run["model_id"] = "different-model"
        self.write(self.run_path, self.run)
        second = learning.register_run(self.root, self.index_path, self.result_path, self.evidence_path, self.run_path)
        other = review.ensure_run(self.root, second["run_id"])
        paths = [Path(item["current_result_path"]) for item in (first, other)]
        before = [path.read_bytes() for path in paths]
        actual = review._atomic
        count = 0
        def fail_second(path, data, digest):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("synthetic disk failure")
            return actual(path, data, digest)
        with patch.object(review, "_atomic", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "synthetic disk failure"):
                self.post("discard")
        self.assertEqual([path.read_bytes() for path in paths], before)
        self.assertEqual(self.detail()["revision"], first["revision"])
        self.assertEqual(review.get_review(self.root, second["run_id"])["revision"], other["revision"])
        self.assertEqual(review.list_documents(self.root)["counts"]["discarded"], 0)
        self.assertFalse(any(review.get_review(self.root, run_id)["sync"]["dirty"] for run_id in (self.run_id, second["run_id"])))

    def test_formatted_json_has_highlightable_tags_and_global_jump_offset(self):
        value = review.handle_get(self.root, [self.docid, "source"], {"tag": "<C3>"})
        self.assertEqual(value["text"][value["tag_offset"] - value["start_cursor"]:][:4], "<C3>")
        self.assertIsNone(value["next_cursor"])
        self.assertIn('"rows"', value["text"])
        self.assertLess(value["text"].index('"rows"'), value["text"].index('"merges"'))
        with self.assertRaisesRegex(store.MemoryError, "不存在该 C"):
            review.handle_get(self.root, [self.docid, "source"], {"tag": "<C999>"})

    def test_database_revisions_are_append_only(self):
        self.edit_quantity()
        path = store.resolve_store_dir(self.root) / "document-review.sqlite3"
        connection = sqlite3.connect(path)
        self.addCleanup(connection.close)
        with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
            connection.execute("UPDATE review_revisions SET revision=99")

    def test_failed_initial_record_insert_removes_new_work_file(self):
        target = Path(self.detail()["current_result_path"])
        actual = store._hash
        def fail_revision(value):
            if isinstance(value, dict) and value.get("action") == "initialize" and "working_sha256" in value:
                raise store.MemoryError("synthetic audit failure")
            return actual(value)
        with patch.object(store, "_hash", side_effect=fail_revision):
            with self.assertRaisesRegex(store.MemoryError, "synthetic audit failure"):
                review.ensure_run(self.root, self.run_id)
        self.assertFalse(target.exists())
        self.assertEqual(self.detail()["revision"], 0)

    def test_source_index_drift_is_not_misrepresented_as_archived_source(self):
        index = copy.deepcopy(self.index)
        index["source_document"] = [{"text": "<C9> 不同原文"}]
        self.write(self.index_path, index)
        self.assertTrue(self.detail()["sync"]["blocked"])
        with self.assertRaisesRegex(store.MemoryError, "index 已变化"):
            review.handle_get(self.root, [self.docid, "source"], {})

    def test_external_missing_file_and_wrong_document_block_safely(self):
        value = review.ensure_run(self.root, self.run_id)
        Path(value["current_result_path"]).unlink()
        self.assertTrue(self.detail()["sync"]["blocked"])
        with self.assertRaisesRegex(store.MemoryError, "缺失"):
            self.edit_quantity()
        with self.assertRaisesRegex(store.MemoryError, "不对应"):
            review.handle_post(self.root, ["doc_wrong", "verify"], {"run_id": self.run_id, "expected_revision": 1})


if __name__ == "__main__":
    unittest.main()
