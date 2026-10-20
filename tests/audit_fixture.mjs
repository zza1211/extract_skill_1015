// 为已有标签学习/人工校验运输测试准备合成核验工件，不调用模型或断言语义准确。
import * as audits from "../.opencode/tools/term_audit.ts";
import { readFile } from "node:fs/promises";

export async function closeSyntheticAudit(draft, context) {
  const invoke = async (tool, args, caller = context) => JSON.parse(await tool.execute(args, caller));
  const prepared = await invoke(audits.prepare, draft);
  const audit_id = prepared.audit_id;
  const child = { ...context, agent: "term-auditor", sessionID: `fixture-${audit_id}`, messageID: "fixture-audit-message" };
  await invoke(audits.open, { audit_id }, child);
  let cursor = "0";
  for (;;) {
    const page = await invoke(audits.read, { audit_id, cursor }, child);
    if (!page.has_more) break;
    cursor = page.next_cursor;
  }
  // 仅运输夹具：从调用者给定的合成草稿构造清点与对应，不模拟独立模型识别，
  // 不将此固定映射作为质量或召回测试的证据。
  const clauses = JSON.parse(await readFile(draft.result_path, "utf8")).filter(item => item.条款分类 !== "general");
  const inventory = clauses.map(item => ({ title: item.条款标题, object_kind: "equipment", evidence_tags: [...new Set(item.定位标识)] }));
  const saved = await invoke(audits.inventory, { audit_id, objects_json: JSON.stringify(inventory) }, child);
  cursor = "0";
  for (;;) {
    const page = await invoke(audits.draft, { audit_id, cursor }, child);
    if (!page.has_more) break;
    cursor = page.next_cursor;
  }
  const coverage = saved.object_ids.map((object_id, i) => ({ object_id, status: "covered", clause_ids: [clauses[i].id], finding_ids: [], reason: "合成夹具逐条映射，只用于验证工具运输协议，不是模型质量结论。" }));
  await invoke(audits.finish, { audit_id, findings_json: "[]", coverage_json: JSON.stringify(coverage), summary: "合成运输测试空发现，不代表真实模型核验。" }, child);
  await invoke(audits.inspect, { audit_id });
  await invoke(audits.resolve, { audit_id, result_path: draft.result_path, evidence_path: draft.evidence_path, decisions_json: "[]" });
}
