// 为已有标签学习/人工校验运输测试准备合成核验工件，不调用模型或断言语义准确。
import * as audits from "../.opencode/tools/term_audit.ts";

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
  await invoke(audits.inventory, { audit_id, objects_json: "[]" }, child);
  cursor = "0";
  for (;;) {
    const page = await invoke(audits.draft, { audit_id, cursor }, child);
    if (!page.has_more) break;
    cursor = page.next_cursor;
  }
  await invoke(audits.finish, { audit_id, findings_json: "[]", summary: "合成运输测试空发现，不代表真实模型核验。" }, child);
  await invoke(audits.inspect, { audit_id });
  await invoke(audits.resolve, { audit_id, result_path: draft.result_path, evidence_path: draft.evidence_path, decisions_json: "[]" });
}
