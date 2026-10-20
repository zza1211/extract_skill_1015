"""独立语义核验的临时合成协议测试；不调用模型，不把核验结论当可信标签。"""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import agent_audit as audit
import document_io
import evolution_store as evolution
import memory_store as store


class AgentAuditTests(unittest.TestCase):
    def setUp(self):
        # 保留历史 v1 票据的协议回归；新 prepare 的 v2 行为另有独立测试。
        protocol = patch.object(audit, "PROTOCOL_VERSION", 1, create=True)
        protocol.start()
        self.addCleanup(protocol.stop)
        temporary = tempfile.TemporaryDirectory(prefix="term-agent-audit-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        environment = patch.dict(os.environ, {"TERM_EXTRACT_MEMORY_DIR": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.parent = {"session_id": "synthetic_parent", "message_id": "synthetic_parent_message", "agent": "term-extractor"}
        self.child = {"session_id": "synthetic_auditor", "message_id": "synthetic_child_message", "agent": "term-auditor"}
        self.references = self.root / ".opencode/skills/term-extract/references"
        self.references.mkdir(parents=True)
        for name, body in {"business-rules.md": "只抽目标设备；连续表格不算分散；机柜布线不抽。",
                           "output-format.md": "每个设备输出七字段数组，并保留真实 Cxx。",
                           "input-format.md": "text/table 字符串包含原始定位标签。"}.items():
            (self.references / name).write_text(body, encoding="utf-8")
        self.references.parent.joinpath("SKILL.md").write_text("---\nname: term-extract\ndescription: synthetic audit fixture\n---\n按原文抽取。", encoding="utf-8")
        self.source = self.write("source.json", [
            {"text": "<C1>Core Switch"},
            {"text": "<C2>GE以太网端口参数。" + "端口性能必须满足技术条件；" * 420},
            {"text": "<C3>Core Switch 采购数量2台"},
            {"text": "<C4>Wireless Access Points"},
            {"text": "<C5>无线接入设备须支持 Wi-Fi 6。"},
            {"text": "<C6>AP采购数量5台"},
        ])
        prepared = document_io.prepare(self.source, self.root / "prepared")
        self.index_path = Path(prepared["index_path"])
        self.index = store._read_json(self.index_path)
        store.create_snapshot(self.root, self.index_path)
        self.result = [{"id": 0, "条款标题": "Core Switch", "条款分类": "normal", "相关条款": [],
                        "产品分类": "盒式交换机", "定位标识": ["<C1>", "<C2>"], "产品数量": "2台"}]
        self.evidence = {"clauses": [{"id": 0, "quantity_source_tags": ["<C3>"], "related_source_tags": [], "notes": "合成协议样例，采购量位于清单。"}], "unresolved": []}
        self.result_path = self.write("draft/result.json", self.result)
        self.evidence_path = self.write("draft/evidence.json", self.evidence)
        self.parent_receipts = self.index_path.with_name("read_receipts.jsonl")
        self.parent_receipts.write_text("", encoding="utf-8")
        # 原抽取已读全文，但 finding 后仍须有针对性的父会话新回读。
        self.parent_read()

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def call(self, action, context=None, **request):
        return audit.dispatch(self.root, {"action": action, "execution_context": context if context is not None else self.parent, **request})

    def prepare(self, **changes):
        return self.call("prepare", **({"index_path": str(self.index_path), "result_path": str(self.result_path), "evidence_path": str(self.evidence_path)} | changes))

    def opened(self):
        prepared = self.prepare()
        self.call("open", self.child, audit_id=prepared["audit_id"])
        return prepared["audit_id"]

    def child_read_all(self, audit_id, max_bytes=24576):
        cursor, pages = "0", 0
        while True:
            response = self.call("read", self.child, audit_id=audit_id, cursor=cursor, max_bytes=max_bytes)
            view = response["model_view"]
            pages += 1
            if not view["has_more"]:
                return pages
            self.assertIsNotNone(view["next_cursor"])
            self.assertNotEqual(cursor, view["next_cursor"])
            cursor = view["next_cursor"]

    def inventory(self, audit_id):
        return self.call("inventory", self.child, audit_id=audit_id, objects=[
            {"title": "Core Switch", "evidence_tags": ["<C1>", "<C2>"]},
            {"title": "Wireless Access Points", "evidence_tags": ["<C4>", "<C5>"]},
        ])

    def child_draft_all(self, audit_id, limit=10):
        cursor, items = "0", []
        while True:
            page = self.call("draft", self.child, audit_id=audit_id, cursor=cursor, limit=limit, max_bytes=8192)
            items.extend(page["items"])
            if not page["has_more"]:
                return items
            self.assertNotEqual(cursor, page["next_cursor"])
            cursor = page["next_cursor"]

    def ready_to_finish(self):
        audit_id = self.opened()
        self.child_read_all(audit_id)
        self.inventory(audit_id)
        self.child_draft_all(audit_id)
        return audit_id

    def finding(self, **changes):
        return {"type": "missing_object", "clause_ids": [], "evidence_tags": ["<C4>", "<C5>", "<C6>"],
                "finding": "原文另有无线接入设备，当前草稿没有对应采购对象。", "suggested_action": "核对并补充 AP 独立条款及数量来源。",
                "certainty": "confirmed"} | changes

    def finished(self, findings=None):
        audit_id = self.ready_to_finish()
        self.call("finish", self.child, audit_id=audit_id, findings=findings if findings is not None else [self.finding()], summary="独立对象清点后比较草稿。")
        return audit_id

    def inspect_all(self, audit_id, limit=10):
        cursor, items = "0", []
        while True:
            page = self.call("inspect", audit_id=audit_id, cursor=cursor, limit=limit, max_bytes=8192)
            items.extend(page["items"])
            if not page["has_more"]:
                return items
            self.assertNotEqual(cursor, page["next_cursor"])
            cursor = page["next_cursor"]

    def parent_read(self, tags=None, context=None, partial=False):
        selected = set(tags) if tags is not None else None
        parts = []
        for unit in self.index["units"]:
            if selected is not None and unit["tag"] not in selected:
                continue
            length = unit["source_length"]
            parts.append({"unit_id": unit["unit_id"], "tag": unit["tag"], "part_start": 0,
                          "part_end": length // 2 if partial else length, "total_length": length})
        receipt = {"index_sha256": store._file_hash(self.index_path), "coverage_kind": "full_original", "time": store._now(),
                   "cursor": "0", "request": {}, "parts": parts, "execution_context": context or self.parent}
        with self.parent_receipts.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(receipt, ensure_ascii=False) + "\n")

    def fixed_result(self):
        result = copy.deepcopy(self.result)
        result.append({"id": 1, "条款标题": "Wireless Access Points", "条款分类": "normal", "相关条款": [],
                       "产品分类": "WLAN AP", "定位标识": ["<C4>", "<C5>"], "产品数量": "5台"})
        evidence = copy.deepcopy(self.evidence)
        evidence["clauses"].append({"id": 1, "quantity_source_tags": ["<C6>"], "related_source_tags": [], "notes": "合成测试补充AP原文与采购清单。"})
        self.write(str(self.result_path), result)
        self.write(str(self.evidence_path), evidence)

    def decision(self, finding_id="f0001", decision="fixed", **changes):
        return {"finding_id": finding_id, "decision": decision, "reason": "根据本轮原文证据处理独立核验发现。",
                "evidence_tags": ["<C4>", "<C5>", "<C6>"]} | changes

    def resolve(self, audit_id, decisions=None, **changes):
        return self.call("resolve", **({"audit_id": audit_id, "result_path": str(self.result_path), "evidence_path": str(self.evidence_path),
                                      "decisions": decisions if decisions is not None else [self.decision()]} | changes))

    def assert_ready(self, context=None):
        return audit.assert_ready(self.root, self.index_path, self.result_path, self.evidence_path, context or self.parent)

    def test_prepare_rejects_wrong_role_and_missing_session(self):
        for context in (self.child, self.parent | {"agent": "term-trial"}, self.parent | {"session_id": ""}, {}):
            with self.subTest(context=context), self.assertRaises((store.MemoryError, ValueError)):
                self.call("prepare", context, index_path=str(self.index_path), result_path=str(self.result_path), evidence_path=str(self.evidence_path))

    def test_child_claim_is_session_scoped_and_parent_cannot_open(self):
        prepared = self.prepare()
        with self.assertRaises((store.MemoryError, ValueError)):
            self.call("open", audit_id=prepared["audit_id"])
        opened = self.call("open", self.child, audit_id=prepared["audit_id"])
        self.assertNotIn("labels", json.dumps(opened, ensure_ascii=False))
        with self.assertRaises((store.MemoryError, ValueError)):
            self.call("open", self.child | {"session_id": "other-child"}, audit_id=prepared["audit_id"])

    def test_one_child_session_cannot_claim_two_tickets_or_read_parent_draft(self):
        first = self.opened()
        changed = copy.deepcopy(self.result)
        changed[0]["条款标题"] = "Core Switch revised title"
        alternative = self.write("other-draft.json", changed)
        second = self.prepare(result_path=str(alternative))
        self.assertNotEqual(first, second["audit_id"])
        with self.assertRaises((store.MemoryError, ValueError)):
            self.call("open", self.child, audit_id=second["audit_id"])
        with self.assertRaises((store.MemoryError, ValueError)):
            self.call("draft", self.parent, audit_id=first)

    def test_inventory_requires_independent_whole_original_read(self):
        audit_id = self.opened()
        with self.assertRaises((store.MemoryError, ValueError)):
            self.inventory(audit_id)
        self.call("read", self.child, audit_id=audit_id, start_tag="<C1>", end_tag="<C2>")
        with self.assertRaises((store.MemoryError, ValueError)):
            self.inventory(audit_id)

    def test_partial_unit_page_does_not_count_as_full_inventory_coverage(self):
        audit_id = self.opened()
        page = self.call("read", self.child, audit_id=audit_id, cursor="0", max_bytes=2048)
        self.assertTrue(page["model_view"]["has_more"])
        with self.assertRaises((store.MemoryError, ValueError)):
            self.inventory(audit_id)
        self.assertGreater(self.child_read_all(audit_id, max_bytes=2048), 1)
        self.inventory(audit_id)

    def test_draft_is_hidden_until_independent_inventory_and_parent_receipts_unchanged(self):
        before = self.parent_receipts.read_bytes()
        audit_id = self.opened()
        with self.assertRaises((store.MemoryError, ValueError)):
            self.call("draft", self.child, audit_id=audit_id)
        self.child_read_all(audit_id)
        with self.assertRaises((store.MemoryError, ValueError)):
            self.call("draft", self.child, audit_id=audit_id)
        self.inventory(audit_id)
        records = self.child_draft_all(audit_id)
        self.assertEqual(len(records), len(self.result))
        self.assertEqual(self.parent_receipts.read_bytes(), before)

    def test_search_does_not_replace_independent_full_read(self):
        audit_id = self.opened()
        self.call("search", self.child, audit_id=audit_id, query="Wireless", cursor="0", max_bytes=24576)
        with self.assertRaises((store.MemoryError, ValueError)):
            self.inventory(audit_id)

    def test_finish_requires_draft_delivery_and_valid_evidence(self):
        audit_id = self.opened()
        self.child_read_all(audit_id)
        self.inventory(audit_id)
        with self.assertRaises((store.MemoryError, ValueError)):
            self.call("finish", self.child, audit_id=audit_id, findings=[self.finding()], summary="未读取草稿不得报告核验通过。")
        self.child_draft_all(audit_id)
        with self.assertRaises((store.MemoryError, ValueError)):
            self.call("finish", self.child, audit_id=audit_id, findings=[self.finding(evidence_tags=["<C999>"])], summary="未知标签不能作依据。")

    def test_finish_requires_every_draft_page_not_only_first_product(self):
        self.fixed_result()
        audit_id = self.opened()
        self.child_read_all(audit_id)
        self.inventory(audit_id)
        first = self.call("draft", self.child, audit_id=audit_id, cursor="0", limit=1, max_bytes=8192)
        self.assertEqual(len(first["items"]), 1)
        self.assertTrue(first["has_more"])
        with self.assertRaises((store.MemoryError, ValueError)):
            self.call("finish", self.child, audit_id=audit_id, findings=[], summary="只看第一条不能完成整篇核验。")
        second = self.call("draft", self.child, audit_id=audit_id, cursor=first["next_cursor"], limit=1, max_bytes=8192)
        self.assertFalse(second["has_more"])
        self.call("finish", self.child, audit_id=audit_id, findings=[], summary="已独立清点并比较全部草稿。")

    def test_finish_assigns_stable_finding_ids_and_model_findings_are_not_labels(self):
        audit_id = self.finished()
        first = self.inspect_all(audit_id)
        self.assertEqual(first[0]["finding_id"], "f0001")
        self.assertEqual(self.inspect_all(audit_id)[0]["finding_id"], "f0001")
        self.assertEqual(evolution.list_cases(self.root)["items"], [])
        with store._db(self.root) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM learning_confirmations").fetchone()[0], 0)

    def test_parent_must_read_whole_report_before_resolving(self):
        audit_id = self.finished([self.finding(), self.finding(type="scope_uncertain", clause_ids=[0], evidence_tags=["<C2>"], certainty="uncertain")])
        page = self.call("inspect", audit_id=audit_id, cursor="0", limit=1, max_bytes=8192)
        self.assertTrue(page["has_more"])
        self.fixed_result()
        self.parent_read()
        with self.assertRaises((store.MemoryError, ValueError)):
            self.resolve(audit_id, [self.decision(), self.decision("f0002", "dismissed", evidence_tags=["<C2>"])])

    def test_missing_unknown_or_duplicate_finding_decisions_are_rejected(self):
        audit_id = self.finished()
        self.inspect_all(audit_id)
        self.fixed_result()
        self.parent_read()
        for decisions in ([], [self.decision("unknown")], [self.decision(), self.decision()]):
            with self.subTest(decisions=decisions), self.assertRaises((store.MemoryError, ValueError)):
                self.resolve(audit_id, decisions)

    def test_old_parent_read_and_child_read_cannot_replace_finding_followup(self):
        audit_id = self.finished()
        self.inspect_all(audit_id)
        self.fixed_result()
        # setUp 中父全文回执早于 report，独立子回执不能替父核验。
        with self.assertRaises((store.MemoryError, ValueError)):
            self.resolve(audit_id)
        self.parent_read(["<C4>", "<C5>", "<C6>"], context=self.child)
        with self.assertRaises((store.MemoryError, ValueError)):
            self.resolve(audit_id)

    def test_partial_evidence_reread_and_other_parent_session_are_rejected(self):
        audit_id = self.finished()
        self.inspect_all(audit_id)
        self.fixed_result()
        self.parent_read(["<C4>", "<C5>", "<C6>"], partial=True)
        with self.assertRaises((store.MemoryError, ValueError)):
            self.resolve(audit_id)
        self.parent_read(["<C4>", "<C5>", "<C6>"], context=self.parent | {"session_id": "another-parent"})
        with self.assertRaises((store.MemoryError, ValueError)):
            self.resolve(audit_id)

    def test_unrelated_decision_tag_read_cannot_dismiss_finding_evidence(self):
        audit_id = self.finished()
        self.inspect_all(audit_id)
        self.parent_read(["<C1>"])
        decision = self.decision(decision="dismissed", evidence_tags=["<C1>"], reason="仅回读交换机标题不能代替报告所指的AP原文证据。")
        with self.assertRaises((store.MemoryError, ValueError)):
            self.resolve(audit_id, [decision])
        # 发现本身的 C4-C6 与主代理新增的 C1 都已读，才满足程序交付门槛。
        self.parent_read(["<C4>", "<C5>", "<C6>"])
        self.resolve(audit_id, [decision])

    def test_fixed_requires_changed_draft_even_after_followup_read(self):
        audit_id = self.finished()
        self.inspect_all(audit_id)
        self.parent_read(["<C4>", "<C5>", "<C6>"])
        with self.assertRaises((store.MemoryError, ValueError)):
            self.resolve(audit_id)

    def test_unresolved_finding_cannot_make_production_ready(self):
        audit_id = self.finished()
        self.inspect_all(audit_id)
        try:
            self.resolve(audit_id, [self.decision(decision="unresolved")])
        except (store.MemoryError, ValueError):
            pass
        with self.assertRaises((store.MemoryError, ValueError)):
            self.assert_ready()

    def test_fixed_with_new_parent_evidence_closes_audit_and_binds_final_hashes(self):
        audit_id = self.finished()
        self.inspect_all(audit_id)
        self.fixed_result()
        self.parent_read(["<C4>", "<C5>", "<C6>"])
        self.resolve(audit_id)
        self.assertIsInstance(self.assert_ready(), dict)
        changed = store._read_json(self.result_path)
        changed[1]["产品数量"] = "6台"
        self.write(str(self.result_path), changed)
        with self.assertRaises((store.MemoryError, ValueError)):
            self.assert_ready()

    def test_dismissed_requires_read_but_does_not_require_changing_valid_result(self):
        audit_id = self.finished([self.finding(type="scope_uncertain", clause_ids=[0], evidence_tags=["<C2>"], certainty="uncertain")])
        self.inspect_all(audit_id)
        self.parent_read(["<C2>"])
        self.resolve(audit_id, [self.decision(decision="dismissed", evidence_tags=["<C2>"], reason="该原文属于相同交换机，未混入其他采购对象。")])
        self.assertIsInstance(self.assert_ready(), dict)

    def test_empty_finding_report_still_requires_inspect_but_no_reread(self):
        audit_id = self.finished([])
        with self.assertRaises((store.MemoryError, ValueError)):
            self.resolve(audit_id, [])
        self.assertEqual(self.inspect_all(audit_id), [])
        self.resolve(audit_id, [])
        self.assertIsInstance(self.assert_ready(), dict)

    def test_empty_report_cannot_clear_different_result_content(self):
        audit_id = self.finished([])
        self.inspect_all(audit_id)
        changed = copy.deepcopy(self.result)
        changed[0]["产品数量"] = "3台"
        self.write(str(self.result_path), changed)
        self.parent_read()
        with self.assertRaises((store.MemoryError, ValueError)):
            self.resolve(audit_id, [])
        with self.assertRaises((store.MemoryError, ValueError)):
            self.assert_ready()

    def test_all_dismissed_findings_cannot_clear_an_unreviewed_result_change(self):
        audit_id = self.finished([self.finding(type="scope_uncertain", clause_ids=[0], evidence_tags=["<C2>"], certainty="uncertain")])
        self.inspect_all(audit_id)
        changed = copy.deepcopy(self.result)
        changed[0]["产品数量"] = "3台"
        self.write(str(self.result_path), changed)
        self.parent_read(["<C2>"])
        with self.assertRaises((store.MemoryError, ValueError)):
            self.resolve(audit_id, [self.decision(decision="dismissed", evidence_tags=["<C2>"], reason="既有条款边界无需修正，但不能用此报告认可新的采购量。")])

    def test_cleared_report_cannot_be_resolved_again_to_new_result_or_evidence_hash(self):
        audit_id = self.finished()
        self.inspect_all(audit_id)
        self.fixed_result()
        self.parent_read(["<C4>", "<C5>", "<C6>"])
        self.resolve(audit_id)
        self.assert_ready()
        for artifact in (self.result_path, self.evidence_path):
            with self.subTest(artifact=artifact.name):
                before = artifact.read_bytes()
                content = store._read_json(artifact)
                if artifact == self.result_path:
                    content[1]["产品数量"] = "6台"
                else:
                    content["clauses"][1]["notes"] = "处置后新证据说明不能重新绑定旧闭环。"
                self.write(str(artifact), content)
                self.parent_read(["<C4>", "<C5>", "<C6>"])
                try:
                    with self.assertRaises((store.MemoryError, ValueError)):
                        self.resolve(audit_id)
                    with self.assertRaises((store.MemoryError, ValueError)):
                        self.assert_ready()
                finally:
                    artifact.write_bytes(before)
        # 相同最终工件和相同处置可幂等读取，不能产生新的审阅承诺。
        self.resolve(audit_id)
        self.assert_ready()

    def test_fixed_does_not_accept_only_an_evidence_note_change(self):
        audit_id = self.finished()
        self.inspect_all(audit_id)
        changed = copy.deepcopy(self.evidence)
        changed["clauses"][0]["notes"] = "仅改审计备注不能宣称补回漏抽产品。"
        self.write(str(self.evidence_path), changed)
        self.parent_read(["<C4>", "<C5>", "<C6>"])
        with self.assertRaises((store.MemoryError, ValueError)):
            self.resolve(audit_id)

    def test_other_parent_cannot_inspect_resolve_or_use_closed_report(self):
        audit_id = self.finished([])
        other = self.parent | {"session_id": "different-controller"}
        with self.assertRaises((store.MemoryError, ValueError)):
            self.call("inspect", other, audit_id=audit_id)
        self.inspect_all(audit_id)
        with self.assertRaises((store.MemoryError, ValueError)):
            self.call("resolve", other, audit_id=audit_id, result_path=str(self.result_path), evidence_path=str(self.evidence_path), decisions=[])
        self.resolve(audit_id, [])
        with self.assertRaises((store.MemoryError, ValueError)):
            self.assert_ready(other)

    def check_artifact_drift(self, artifact):
        audit_id = self.finished([])
        self.inspect_all(audit_id)
        self.resolve(audit_id, [])
        path = self.source if artifact == "input" else self.references / "business-rules.md" if artifact == "policy" else self.index_path.with_name("memory_snapshot.json")
        before = path.read_bytes()
        try:
            path.write_bytes(before + b"\n ")
            with self.assertRaises((store.MemoryError, ValueError)):
                self.assert_ready()
        finally:
            path.write_bytes(before)

    def test_changed_input_invalidates_audit(self):
        self.check_artifact_drift("input")

    def test_changed_policy_invalidates_audit(self):
        self.check_artifact_drift("policy")

    def test_changed_snapshot_invalidates_audit(self):
        self.check_artifact_drift("snapshot")

    def test_auditor_reads_frozen_draft_when_parent_working_copy_changes(self):
        audit_id = self.opened()
        self.child_read_all(audit_id)
        self.inventory(audit_id)
        changed = copy.deepcopy(self.result)
        changed[0]["产品数量"] = "3台"
        self.write(str(self.result_path), changed)
        page = self.call("draft", self.child, audit_id=audit_id)
        self.assertEqual(page["items"][0]["产品数量"], "2台")
        self.assertEqual(store._read_json(self.result_path)[0]["产品数量"], "3台")
        with self.assertRaises((store.MemoryError, ValueError)):
            self.assert_ready()

    def test_large_index_metadata_does_not_block_small_original_read_and_inventory(self):
        self.source = self.write("small-source.json", [{"text": "<C1>Small Switch"}, {"text": "<C2>GE端口参数要求"}])
        prepared = document_io.prepare(self.source, self.root / "large-index-task")
        self.index_path = Path(prepared["index_path"])
        self.index, _ = document_io.load_index(self.index_path)
        self.index["diagnostic_padding"] = "X" * (17 * 1024 * 1024)
        self.write(str(self.index_path), self.index)
        # 测试构造一个完整且相互绑定的大索引；不是通过改坏 manifest 绕过校验。
        manifest_path = self.index_path.with_name("manifest.json")
        manifest = store._read_json(manifest_path)
        manifest["index_sha256"] = store._file_hash(self.index_path)
        self.write(str(manifest_path), manifest)
        store.create_snapshot(self.root, self.index_path)
        self.assertGreater(self.index_path.stat().st_size, 16 * 1024 * 1024)
        self.assertLess(self.source.stat().st_size, 4096)
        self.result = [{**self.result[0], "条款标题": "Small Switch", "产品数量": "未提及"}]
        self.evidence = {"clauses": [{"id": 0, "quantity_source_tags": [], "related_source_tags": [], "notes": "合成大索引、小原文。"}], "unresolved": []}
        self.write(str(self.result_path), self.result)
        self.write(str(self.evidence_path), self.evidence)
        self.parent_receipts = self.index_path.with_name("read_receipts.jsonl")
        self.parent_receipts.write_text("", encoding="utf-8")
        self.parent_read()
        audit_id = self.opened()
        self.assertEqual(self.child_read_all(audit_id), 1)
        inventory = self.call("inventory", self.child, audit_id=audit_id,
                              objects=[{"title": "Small Switch", "evidence_tags": ["<C1>", "<C2>"]}])
        self.assertEqual(inventory["inventory_count"], 1)

    def test_oversized_single_result_is_rejected_during_prepare(self):
        oversized = copy.deepcopy(self.result)
        oversized[0]["条款标题"] = "X" * 33000
        self.write(str(self.result_path), oversized)
        with patch.object(audit, "_write", side_effect=AssertionError("预算预检应先于冻结工件写入")):
            with self.assertRaisesRegex(store.MemoryError, "24 KiB|24KiB|预算"):
                self.prepare()

    def test_resolve_does_not_bind_working_copy_changed_after_capture(self):
        audit_id = self.finished([self.finding(type="scope_uncertain", clause_ids=[0], evidence_tags=["<C2>"], certainty="uncertain")])
        self.inspect_all(audit_id)
        self.parent_read(["<C2>"])
        before = self.result_path.read_bytes()
        real_validate = audit.validate_result

        def concurrently_changed(index, result, evidence, **options):
            checked = real_validate(index, result, evidence, **options)
            changed = copy.deepcopy(result)
            changed[0]["产品数量"] = "9台"
            self.write(str(self.result_path), changed)
            return checked

        decision = self.decision(decision="dismissed", evidence_tags=["<C2>"])
        with patch.object(audit, "validate_result", side_effect=concurrently_changed) as validator:
            with self.assertRaisesRegex(store.MemoryError, "期间.*变化|发生变化|版本"):
                self.resolve(audit_id, [decision])
            self.assertEqual(validator.call_count, 1)
        self.assertEqual(store._read_json(self.result_path)[0]["产品数量"], "9台")
        with self.assertRaises((store.MemoryError, ValueError)):
            self.assert_ready()
        self.result_path.write_bytes(before)
        self.resolve(audit_id, [decision])
        self.assert_ready()


if __name__ == "__main__":
    unittest.main()
