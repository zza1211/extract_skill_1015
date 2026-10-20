"""恢复与设备级核验映射的合成协议测试；只使用临时目录和临时库。"""
from __future__ import annotations

import copy
from pathlib import Path
import unittest
from unittest.mock import patch

import agent_audit as audit
import evolution_store as evolution
import memory_store as store
import test_agent_audit as legacy


class AgentAuditV2Tests(unittest.TestCase):
    write = legacy.AgentAuditTests.write
    call = legacy.AgentAuditTests.call
    prepare = legacy.AgentAuditTests.prepare
    opened = legacy.AgentAuditTests.opened
    child_read_all = legacy.AgentAuditTests.child_read_all
    child_draft_all = legacy.AgentAuditTests.child_draft_all
    finding = legacy.AgentAuditTests.finding
    parent_read = legacy.AgentAuditTests.parent_read
    fixed_result = legacy.AgentAuditTests.fixed_result
    decision = legacy.AgentAuditTests.decision
    resolve = legacy.AgentAuditTests.resolve
    assert_ready = legacy.AgentAuditTests.assert_ready
    inspect_all = legacy.AgentAuditTests.inspect_all

    def setUp(self):
        legacy.AgentAuditTests.setUp(self)
        protocol = patch.object(audit, "PROTOCOL_VERSION", 2, create=True)
        protocol.start()
        self.addCleanup(protocol.stop)

    def inventory(self, audit_id, objects=None):
        return self.call("inventory", self.child, audit_id=audit_id, objects=objects if objects is not None else [
            {"title": "Core Switch", "object_kind": "equipment", "evidence_tags": ["<C1>", "<C2>"]},
            {"title": "Wireless Access Points", "object_kind": "equipment", "evidence_tags": ["<C4>", "<C5>"]},
        ])

    def ready_to_finish(self, *, both_products=False):
        if both_products:
            self.fixed_result()
        audit_id = self.opened()
        self.child_read_all(audit_id)
        self.inventory(audit_id)
        self.child_draft_all(audit_id)
        return audit_id

    def coverage(self, *, both_products=False):
        return [
            {"object_id": "o0001", "status": "covered", "clause_ids": [0], "finding_ids": [],
             "reason": "交换机的连续技术要求已对应草稿条款0。"},
            {"object_id": "o0002", "status": "covered" if both_products else "missing",
             "clause_ids": [1] if both_products else [], "finding_ids": [] if both_products else ["f0001"],
             "reason": "AP已有独立设备条款。" if both_products else "原文有AP设备，草稿中没有对应采购对象。"},
        ]

    def finish(self, audit_id, *, both_products=False, findings=None, coverage=None):
        return self.call("finish", self.child, audit_id=audit_id,
                         findings=findings if findings is not None else ([] if both_products else [self.finding()]),
                         coverage=coverage if coverage is not None else self.coverage(both_products=both_products),
                         summary="合成协议核验，逐项说明独立清点对象与冻结草稿的对应关系。")

    def status(self, audit_id, context=None):
        return self.call("status", context, audit_id=audit_id)

    def plan(self, audit_id):
        with audit._db(self.root) as connection:
            return audit._plan(connection, audit_id)

    def table_counts(self):
        with store._db(self.root) as connection:
            if connection is None:
                return {}
            names = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            return {name: connection.execute('SELECT COUNT(*) FROM "' + name + '"').fetchone()[0] for name in names}

    def test_new_prepare_cannot_select_legacy_protocol_via_request(self):
        prepared = self.prepare(protocol_version=1)
        self.assertEqual(self.plan(prepared["audit_id"])["protocol_version"], 2)

    def test_v2_inventory_requires_device_kind_and_program_assigns_object_ids(self):
        audit_id = self.opened()
        self.child_read_all(audit_id)
        with self.assertRaises((store.MemoryError, ValueError)):
            self.inventory(audit_id, [{"title": "Core Switch", "evidence_tags": ["<C1>"]}])
        for kind in ("system", "Others", "", None):
            with self.subTest(kind=kind), self.assertRaises((store.MemoryError, ValueError)):
                self.inventory(audit_id, [{"title": "Core Switch", "object_kind": kind, "evidence_tags": ["<C1>"]}])
        saved = self.inventory(audit_id)
        self.assertEqual(saved["object_ids"], ["o0001", "o0002"])
        self.assertEqual(self.inventory(audit_id)["object_ids"], saved["object_ids"])

    def test_integrated_system_needs_concrete_system_basis(self):
        audit_id = self.opened()
        self.child_read_all(audit_id)
        item = {"title": "Integrated Network", "object_kind": "integrated_system", "evidence_tags": ["<C1>", "<C4>"]}
        for reason in (None, "", "  ", "X" * 601):
            with self.subTest(reason=reason), self.assertRaises((store.MemoryError, ValueError)):
                self.inventory(audit_id, [item | {"system_basis": reason}])
        saved = self.inventory(audit_id, [item | {"system_basis": "合成原文明确作为不可分割的整体采购系统。"}])
        self.assertEqual(saved["object_ids"], ["o0001"])

    def test_v2_finish_requires_coverage_even_if_findings_empty(self):
        audit_id = self.ready_to_finish(both_products=True)
        with self.assertRaises((store.MemoryError, ValueError)):
            self.call("finish", self.child, audit_id=audit_id, findings=[], summary="不能仅通过无发现跳过逐设备映射。")

    def test_coverage_requires_each_inventory_object_exactly_once(self):
        audit_id = self.ready_to_finish()
        valid = self.coverage()
        attempts = [valid[:1], valid + [copy.deepcopy(valid[0])], valid + [valid[0] | {"object_id": "o9999"}],
                    [valid[0], valid[1] | {"object_id": "o9999"}]]
        for coverage in attempts:
            with self.subTest(coverage=coverage), self.assertRaises((store.MemoryError, ValueError)):
                self.finish(audit_id, coverage=coverage)
        self.finish(audit_id)

    def test_covered_object_needs_existing_non_general_clause(self):
        audit_id = self.ready_to_finish()
        coverage = self.coverage()
        for clause_ids in ([], [100], [True]):
            with self.subTest(clause_ids=clause_ids), self.assertRaises((store.MemoryError, ValueError)):
                self.finish(audit_id, coverage=[coverage[0] | {"clause_ids": clause_ids}, coverage[1]])
        self.finish(audit_id)

    def test_covered_object_cannot_map_only_to_general(self):
        self.fixed_result()
        result = store._read_json(self.result_path)
        result[0].update({"条款分类": "general", "相关条款": [1]})
        self.write(str(self.result_path), result)
        audit_id = self.ready_to_finish()
        with self.assertRaises((store.MemoryError, ValueError)):
            self.finish(audit_id, findings=[], coverage=self.coverage(both_products=True))

    def test_missing_object_needs_missing_finding_and_no_clause_reference(self):
        audit_id = self.ready_to_finish()
        valid = self.coverage()
        attempts = [valid[1] | {"clause_ids": [0]}, valid[1] | {"finding_ids": []},
                    valid[1] | {"finding_ids": ["unknown"]}]
        for mapped in attempts:
            with self.subTest(mapped=mapped), self.assertRaises((store.MemoryError, ValueError)):
                self.finish(audit_id, coverage=[valid[0], mapped])
        with self.assertRaises((store.MemoryError, ValueError)):
            self.finish(audit_id, findings=[self.finding(type="scope_uncertain", certainty="uncertain")])
        self.finish(audit_id)

    def test_missing_and_uncertain_mapping_require_evidence_overlap_with_object(self):
        audit_id = self.ready_to_finish()
        for status in ("missing", "uncertain"):
            coverage = self.coverage()
            coverage[1]["status"] = status
            with self.subTest(status=status), self.assertRaises((store.MemoryError, ValueError)):
                self.finish(audit_id, findings=[self.finding(evidence_tags=["<C1>"])], coverage=coverage)
        coverage = self.coverage()
        coverage[1]["status"] = "uncertain"
        self.finish(audit_id, findings=[self.finding(type="scope_uncertain", certainty="uncertain")], coverage=coverage)

    def test_uncertain_object_requires_actual_finding(self):
        audit_id = self.ready_to_finish()
        coverage = self.coverage()
        coverage[1].update(status="uncertain", finding_ids=[])
        with self.assertRaises((store.MemoryError, ValueError)):
            self.finish(audit_id, findings=[], coverage=coverage)

    def test_report_preserves_inventory_and_full_coverage_inspect_is_paginated(self):
        audit_id = self.ready_to_finish()
        self.finish(audit_id)
        plan = self.plan(audit_id)
        report = store._read_json(Path(plan["directory"]) / "report.json")
        self.assertIn("inventory", report)
        self.assertEqual([row["object_id"] for row in report["object_coverage"]], ["o0001", "o0002"])
        first = self.call("inspect", audit_id=audit_id, section="coverage", cursor="0", limit=1, max_bytes=8192)
        self.assertEqual(first["items"][0]["object_id"], "o0001")
        self.assertTrue(first["has_more"])
        second = self.call("inspect", audit_id=audit_id, section="coverage", cursor=first["next_cursor"], limit=1, max_bytes=8192)
        self.assertEqual(second["items"][0]["object_id"], "o0002")
        self.assertFalse(second["has_more"])
        self.assertEqual(evolution.list_cases(self.root)["items"], [])

    def test_status_is_owner_scoped_and_leaves_all_receipts_and_db_rows_unchanged(self):
        audit_id = self.opened()
        self.call("read", self.child, audit_id=audit_id, cursor="0", max_bytes=2048)
        plan = self.plan(audit_id)
        before_parent, before_child = self.parent_receipts.read_bytes(), Path(plan["receipts_path"]).read_bytes()
        before_counts = self.table_counts()
        status = self.status(audit_id)
        self.assertEqual(status["worker_session_id"], self.child["session_id"])
        self.assertEqual(status["continue_task_id"], self.child["session_id"])
        self.assertFalse(status["read_progress"]["complete"])
        self.assertEqual(self.table_counts(), before_counts)
        self.assertEqual(self.parent_receipts.read_bytes(), before_parent)
        self.assertEqual(Path(plan["receipts_path"]).read_bytes(), before_child)
        for context in (self.child, self.parent | {"session_id": "other_parent"}):
            with self.subTest(context=context), self.assertRaises((store.MemoryError, ValueError)):
                self.status(audit_id, context)

    def test_status_resume_cursor_is_first_character_gap_even_after_later_local_read(self):
        audit_id = self.opened()
        page = self.call("read", self.child, audit_id=audit_id, cursor="0", max_bytes=2048)["model_view"]
        self.assertTrue(page["has_more"])
        expected_cursor = page["next_cursor"]
        self.assertIn(":", expected_cursor, "合成长单元格应在字符内部分页。")
        self.call("read", self.child, audit_id=audit_id, start_tag="<C6>", end_tag="<C6>")
        progress = self.status(audit_id)["read_progress"]
        self.assertFalse(progress["complete"])
        self.assertEqual(progress["next_cursor"], expected_cursor)
        cursor = progress["next_cursor"]
        while cursor is not None:
            cursor = self.call("read", self.child, audit_id=audit_id, cursor=cursor, max_bytes=2048)["model_view"]["next_cursor"]
        progress = self.status(audit_id)["read_progress"]
        self.assertTrue(progress["complete"])
        self.assertEqual(progress["covered_units"], progress["total_units"])
        self.assertIsNone(progress["next_cursor"])

    def test_reading_only_tail_does_not_skip_start_on_resume(self):
        audit_id = self.opened()
        self.call("read", self.child, audit_id=audit_id, start_tag="<C4>", end_tag="<C6>")
        self.assertEqual(self.status(audit_id)["read_progress"]["next_cursor"], "0")

    def test_reopen_same_child_keeps_progress_and_rejects_new_child(self):
        audit_id = self.opened()
        self.call("read", self.child, audit_id=audit_id, cursor="0", max_bytes=2048)
        before = self.status(audit_id)
        reopened = self.call("open", self.child, audit_id=audit_id)
        self.assertEqual(reopened["read_progress"], before["read_progress"])
        self.assertEqual(self.status(audit_id)["read_progress"], before["read_progress"])
        with self.assertRaises((store.MemoryError, ValueError)):
            self.call("open", self.child | {"session_id": "replacement_worker"}, audit_id=audit_id)

    def test_draft_progress_resumes_first_missing_position(self):
        self.fixed_result()
        audit_id = self.opened()
        self.child_read_all(audit_id)
        self.inventory(audit_id)
        self.call("draft", self.child, audit_id=audit_id, cursor="1", limit=1, max_bytes=8192)
        progress = self.status(audit_id)["draft_progress"]
        self.assertEqual(progress["delivered_count"], 1)
        self.assertEqual(progress["total"], 2)
        self.assertEqual(progress["next_cursor"], "0")
        self.assertFalse(progress["complete"])
        self.call("draft", self.child, audit_id=audit_id, cursor="0", limit=1, max_bytes=8192)
        progress = self.status(audit_id)["draft_progress"]
        self.assertTrue(progress["complete"])
        self.assertIsNone(progress["next_cursor"])

    def test_status_cannot_substitute_report_inspect(self):
        audit_id = self.ready_to_finish(both_products=True)
        self.finish(audit_id, both_products=True)
        self.assertEqual(self.status(audit_id)["phase"], "reported")
        with self.assertRaises((store.MemoryError, ValueError)):
            self.resolve(audit_id, [])
        self.inspect_all(audit_id)
        self.resolve(audit_id, [])
        self.assertEqual(self.status(audit_id)["phase"], "cleared")
        self.assert_ready()

    def test_abandon_validates_owner_reason_and_is_idempotent(self):
        audit_id = self.opened()
        for reason in ("", " ", "X" * 1201):
            with self.subTest(reason=reason), self.assertRaises((store.MemoryError, ValueError)):
                self.call("abandon", audit_id=audit_id, reason=reason)
        for context in (self.child, self.parent | {"session_id": "other_parent"}):
            with self.subTest(context=context), self.assertRaises((store.MemoryError, ValueError)):
                self.call("abandon", context, audit_id=audit_id, reason="不可由其他会话放弃。")
        first = self.call("abandon", audit_id=audit_id, reason="原子会话无法恢复，保留旧票审计轨迹。")
        before = self.table_counts()
        again = self.call("abandon", audit_id=audit_id, reason="原子会话无法恢复，保留旧票审计轨迹。")
        self.assertEqual(first["status"], "abandoned")
        self.assertEqual(again["status"], "abandoned")
        self.assertEqual(self.table_counts(), before)
        self.assertEqual(self.status(audit_id)["phase"], "abandoned")

    def test_abandoned_ticket_denies_worker_actions_inspect_resolve_and_commit(self):
        audit_id = self.ready_to_finish()
        self.finish(audit_id)
        self.call("abandon", audit_id=audit_id, reason="核验中断后显式废弃本票。")
        attempts = [
            ("open", self.child, {}),
            ("read", self.child, {"cursor": "0"}),
            ("search", self.child, {"query": "Core"}),
            ("inventory", self.child, {"objects": []}),
            ("draft", self.child, {}),
            ("finish", self.child, {"findings": [], "coverage": [], "summary": "旧票不得再报告。"}),
            ("inspect", self.parent, {}),
        ]
        for action, context, args in attempts:
            with self.subTest(action=action), self.assertRaises((store.MemoryError, ValueError)):
                self.call(action, context, audit_id=audit_id, **args)
        with self.assertRaises((store.MemoryError, ValueError)):
            self.resolve(audit_id, [])
        with self.assertRaises((store.MemoryError, ValueError)):
            self.assert_ready()

    def test_cleared_ticket_cannot_be_abandoned(self):
        audit_id = self.ready_to_finish(both_products=True)
        self.finish(audit_id, both_products=True)
        self.inspect_all(audit_id)
        self.resolve(audit_id, [])
        with self.assertRaises((store.MemoryError, ValueError)):
            self.call("abandon", audit_id=audit_id, reason="不能废弃已经用于提交门槛的关闭记录。")
        self.assert_ready()

    def test_integrity_drift_remains_visible_and_does_not_block_abandon(self):
        audit_id = self.opened()
        self.source.write_bytes(self.source.read_bytes() + b"\n ")
        status = self.status(audit_id)
        self.assertTrue(status["integrity_error"])
        self.assertNotEqual(status["phase"], "cleared")
        abandoned = self.call("abandon", audit_id=audit_id, reason="输入漂移后放弃，不能继续沿旧覆盖完成。")
        self.assertEqual(abandoned["status"], "abandoned")
        with self.assertRaises((store.MemoryError, ValueError)):
            self.assert_ready()

    def test_new_ticket_has_zero_coverage_and_requires_new_worker_after_abandon(self):
        audit_id = self.ready_to_finish()
        self.call("abandon", audit_id=audit_id, reason="恢复失败，重新建立隔离核验票。")
        replacement = self.prepare()["audit_id"]
        self.assertNotEqual(replacement, audit_id)
        with self.assertRaises((store.MemoryError, ValueError)):
            self.call("open", self.child, audit_id=replacement)
        self.child = self.child | {"session_id": "new_replacement_auditor"}
        self.call("open", self.child, audit_id=replacement)
        progress = self.status(replacement)
        self.assertEqual(progress["read_progress"]["receipt_count"], 0)
        self.assertEqual(progress["read_progress"]["covered_units"], 0)
        self.assertFalse(progress["read_progress"]["complete"])
        self.assertEqual(progress["draft_progress"]["delivered_count"], 0)
        with self.assertRaises((store.MemoryError, ValueError)):
            self.inventory(replacement)
        with self.assertRaises((store.MemoryError, ValueError)):
            self.assert_ready()

    def test_failed_replacement_prepare_does_not_reactivate_abandoned_ticket(self):
        audit_id = self.opened()
        self.call("abandon", audit_id=audit_id, reason="原票不可恢复，等待合法替代草稿。")
        invalid = self.write("invalid-result.json", [{"id": 0}])
        with self.assertRaises((store.MemoryError, ValueError)):
            self.prepare(result_path=str(invalid))
        self.assertEqual(self.status(audit_id)["phase"], "abandoned")
        with self.assertRaises((store.MemoryError, ValueError)):
            self.assert_ready()

    def test_legacy_plan_without_protocol_version_remains_readable_and_finishes_without_coverage(self):
        original_put = audit._put

        def historical_plan(connection, table, key, body):
            if table == "agent_audit_plans":
                body = {name: value for name, value in body.items() if name != "protocol_version"}
            return original_put(connection, table, key, body)

        with patch.object(audit, "PROTOCOL_VERSION", 1), patch.object(audit, "_put", side_effect=historical_plan):
            audit_id = self.opened()
        self.assertNotIn("protocol_version", self.plan(audit_id))
        self.child_read_all(audit_id)
        self.call("inventory", self.child, audit_id=audit_id,
                  objects=[{"title": "Core Switch", "evidence_tags": ["<C1>"]}])
        self.child_draft_all(audit_id)
        self.call("finish", self.child, audit_id=audit_id, findings=[], summary="旧票继续遵循原协议，不强行补写设备映射。")
        self.assertEqual(self.status(audit_id)["phase"], "reported")
        self.inspect_all(audit_id)
        self.resolve(audit_id, [])
        self.assert_ready()


if __name__ == "__main__":
    unittest.main()
