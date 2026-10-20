import { tool } from "@opencode-ai/plugin";
import { executionContext, output, projectRoot, python, resolveFile, withJsonFile } from "../skills/term-extract/scripts/opencode_bridge.ts";

type Role = "term-extractor" | "term-auditor";

function budget(value?: number): number {
  const amount = value ?? 24576;
  if (!Number.isInteger(amount) || amount < 2048 || amount > 24576) {
    throw new Error("max_bytes 必须是 2048 到 24576 的整数。");
  }
  return amount;
}

function pageLimit(value?: number): number {
  const amount = value ?? 20;
  if (!Number.isInteger(amount) || amount < 1 || amount > 20) {
    throw new Error("limit 必须是 1 到 20 的整数。");
  }
  return amount;
}

function jsonArray(value: string, name: string): unknown[] {
  let parsed: unknown;
  try { parsed = JSON.parse(value); }
  catch { throw new Error(`${name} 必须为合法 JSON 数组字符串。`); }
  if (!Array.isArray(parsed)) throw new Error(`${name} 必须为 JSON 数组。`);
  return parsed;
}

async function invoke(context: any, role: Role, action: string, args: any): Promise<any> {
  const actual = executionContext(context);
  if (!actual || actual.agent !== role) {
    throw new Error(`该工具仅供具有真实 OpenCode 会话上下文的 ${role} 使用。`);
  }
  const root = projectRoot(context.directory);
  return withJsonFile({ action, ...args, execution_context: actual }, async file => {
    const response = await python(root, "agent_audit.py", ["--project-root", root, "--request-file", file]);
    if (!response || response.ok === false || response.error) {
      throw new Error(response?.error || "独立审计工具未完成；请核对阶段、会话和文件版本。");
    }
    return response;
  });
}

function compact(response: any, maxBytes: number): string {
  if (!response?.model_view || typeof response.model_view !== "object" || Array.isArray(response.model_view)) {
    throw new Error("审计读取没有返回 compact model_view，不能将内部覆盖元数据交付给模型。");
  }
  const serialized = output(response.model_view);
  if (Buffer.byteLength(serialized, "utf8") > maxBytes) {
    throw new Error("独立审计原文视图超过字节预算，不能把截断内容当作已读原文。");
  }
  return serialized;
}

export const prepare = tool({
  description: "由主 agent 固定当前原文索引、结果和证据，创建独立内容审计票。随后用新 term-auditor 原生 task，只传 audit_id=票号；不向审计者泄露主草稿。",
  args: { index_path: tool.schema.string(), result_path: tool.schema.string(), evidence_path: tool.schema.string() },
  async execute(args, context) {
    const root = projectRoot(context.directory);
    return output(await invoke(context, "term-extractor", "prepare", {
      index_path: resolveFile(root, args.index_path), result_path: resolveFile(root, args.result_path), evidence_path: resolveFile(root, args.evidence_path),
    }));
  },
});

export const inspect = tool({
  description: "主 agent 在审计完成后分页查看独立发现；审计是业务核验建议，最终修订与取舍仍由主 agent 根据原文决定。",
  args: { audit_id: tool.schema.string(), cursor: tool.schema.string().optional(), limit: tool.schema.number().int().min(1).max(20).optional(), max_bytes: tool.schema.number().int().min(2048).max(24576).optional() },
  async execute(args, context) {
    return output(await invoke(context, "term-extractor", "inspect", { audit_id: args.audit_id, cursor: args.cursor ?? "0", limit: pageLimit(args.limit), max_bytes: budget(args.max_bytes) }));
  },
});

export const resolve = tool({
  description: "主 agent 回读发现涉及的原文、修订或说明不采纳理由后逐项处理审计。decisions_json=[{finding_id,decision:fixed|dismissed|unresolved,reason,evidence_tags:[]}]；不会替主 agent 改结果。",
  args: { audit_id: tool.schema.string(), result_path: tool.schema.string(), evidence_path: tool.schema.string(), decisions_json: tool.schema.string() },
  async execute(args, context) {
    const root = projectRoot(context.directory);
    return output(await invoke(context, "term-extractor", "resolve", {
      audit_id: args.audit_id, result_path: resolveFile(root, args.result_path), evidence_path: resolveFile(root, args.evidence_path), decisions: jsonArray(args.decisions_json, "decisions_json"),
    }));
  },
});

export const open = tool({
  description: "独立审计者认领新审计票，仅接收原文入口和业务规范。一个新会话只能认领一票；独立读完全文并固定采购对象清单后，才查看主草稿。",
  args: { audit_id: tool.schema.string() },
  async execute(args, context) { return output(await invoke(context, "term-auditor", "open", args)); },
});

export const read = tool({
  description: "独立审计者分页读取本票原文，程序记录独立覆盖回执。沿 next_cursor 连续读取至全文完整，不能借用主 agent 的读取记录。",
  args: { audit_id: tool.schema.string(), cursor: tool.schema.string().optional(), start_tag: tool.schema.string().optional(), end_tag: tool.schema.string().optional(), max_bytes: tool.schema.number().int().min(2048).max(24576).optional() },
  async execute(args, context) {
    if (Boolean(args.start_tag) !== Boolean(args.end_tag)) throw new Error("范围读取必须同时提供 start_tag 和 end_tag。");
    const maxBytes = budget(args.max_bytes);
    const response = await invoke(context, "term-auditor", "read", { ...args, cursor: args.cursor ?? "0", max_bytes: maxBytes });
    return compact(response, maxBytes);
  },
});

export const search = tool({
  description: "独立审计者围绕具体疑点搜索本票原文；命中不能代替全文读取，不污染主 agent 的原文覆盖记录。",
  args: { audit_id: tool.schema.string(), query: tool.schema.string().min(1), cursor: tool.schema.string().optional(), limit: tool.schema.number().int().min(1).max(20).optional(), max_bytes: tool.schema.number().int().min(2048).max(24576).optional() },
  async execute(args, context) {
    if (typeof args.query !== "string" || !args.query.trim()) throw new Error("query 不能为空。");
    const maxBytes = budget(args.max_bytes);
    return compact(await invoke(context, "term-auditor", "search", { ...args, cursor: args.cursor ?? "0", limit: pageLimit(args.limit), max_bytes: maxBytes }), maxBytes);
  },
});

export const inventory = tool({
  description: "全文独立读取后固定审计者识别的采购对象清单，再解封主草稿。objects_json=[{title,evidence_tags:[]}]，依据原文列目标设备，不复制主 agent 答案。",
  args: { audit_id: tool.schema.string(), objects_json: tool.schema.string() },
  async execute(args, context) {
    return output(await invoke(context, "term-auditor", "inventory", { audit_id: args.audit_id, objects: jsonArray(args.objects_json, "objects_json") }));
  },
});

export const draft = tool({
  description: "独立采购对象清单固定后，分页读取主 agent 七字段草稿，与自己先前识别的目标设备和原文核对。不交付主 agent 的解释性 sidecar；全部草稿分页交付后才能提交发现。",
  args: { audit_id: tool.schema.string(), cursor: tool.schema.string().optional(), limit: tool.schema.number().int().min(1).max(20).optional(), max_bytes: tool.schema.number().int().min(2048).max(24576).optional() },
  async execute(args, context) {
    return output(await invoke(context, "term-auditor", "draft", { audit_id: args.audit_id, cursor: args.cursor ?? "0", limit: pageLimit(args.limit), max_bytes: budget(args.max_bytes) }));
  },
});

export const finish = tool({
  description: "独立核验后提交发现：findings_json=[{type:missing_object|boundary_truncated|parameter_pollution|association_error|scope_uncertain,clause_ids:[],evidence_tags:[],finding,suggested_action,certainty:confirmed|uncertain}]。程序生成 finding_id；没有问题可传空数组，不编造问题。",
  args: { audit_id: tool.schema.string(), findings_json: tool.schema.string(), summary: tool.schema.string().optional() },
  async execute(args, context) {
    return output(await invoke(context, "term-auditor", "finish", { audit_id: args.audit_id, findings: jsonArray(args.findings_json, "findings_json"), summary: args.summary ?? "" }));
  },
});
