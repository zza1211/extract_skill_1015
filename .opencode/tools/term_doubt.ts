import { tool } from "@opencode-ai/plugin";
import { executionContext, output, projectRoot, python, resolveFile, withJsonFile } from "../skills/term-extract/scripts/opencode_bridge.ts";

async function invoke(context: any, action: string, body: any): Promise<string> {
  const actual = executionContext(context);
  if (!actual || actual.agent !== "term-extractor") {
    throw new Error("疑点工具仅允许具有真实上下文的 term-extractor 主 agent 使用。");
  }
  const root = projectRoot(context.directory);
  return withJsonFile({ action, ...body, execution_context: actual }, async file => {
    const result = await python(root, "doubt_engine.py", ["--project-root", root, "--request-file", file]);
    if (result?.ok === false || result?.error) throw new Error(result.error || "疑点引擎未完成。");
    return output(result);
  });
}

export const scan = tool({
  description: "从当前 index 和可选结果/evidence 生成确定性疑点信号。信号只表示需要检查的风险，不是业务错误结论；保存到本次工作目录 draft/doubt-signals.json。",
  args: {
    index_path: tool.schema.string(),
    result_path: tool.schema.string().optional(),
    evidence_path: tool.schema.string().optional(),
    signals_path: tool.schema.string().optional(),
  },
  async execute(args, context) {
    const root = projectRoot(context.directory);
    return invoke(context, "scan", {
      index_path: resolveFile(root, args.index_path),
      result_path: args.result_path ? resolveFile(root, args.result_path) : undefined,
      evidence_path: args.evidence_path ? resolveFile(root, args.evidence_path) : undefined,
      signals_path: args.signals_path ? resolveFile(root, args.signals_path) : undefined,
    });
  },
});

export const record = tool({
  description: "把主 agent 根据风险信号和原文形成的疑点保存为结构化账本。每条含类型、证据、影响、C0-C3置信等级、处理动作和状态；程序只校验来源，不替 agent 判定语义。",
  args: {
    index_path: tool.schema.string(),
    result_path: tool.schema.string().optional(),
    signals_path: tool.schema.string().optional(),
    ledger_path: tool.schema.string().optional(),
    doubts_json: tool.schema.string().describe("疑点数组：type/signal_ids/clause_ids/evidence_tags/hypothesis/triggers/impact/confidence/next_action/status/reason"),
  },
  async execute(args, context) {
    const root = projectRoot(context.directory);
    return withJsonFile(JSON.parse(args.doubts_json), async file => invoke(context, "record", {
      index_path: resolveFile(root, args.index_path),
      result_path: args.result_path ? resolveFile(root, args.result_path) : undefined,
      signals_path: args.signals_path ? resolveFile(root, args.signals_path) : undefined,
      ledger_path: args.ledger_path ? resolveFile(root, args.ledger_path) : undefined,
      doubts_file: file,
    }));
  },
});

export const inspect = tool({
  description: "分页查看当前疑点账本，不读取历史运行或经验库。",
  args: { index_path: tool.schema.string(), ledger_path: tool.schema.string().optional(), cursor: tool.schema.string().optional(), limit: tool.schema.number().int().min(1).max(50).optional() },
  async execute(args, context) {
    const root = projectRoot(context.directory);
    return invoke(context, "inspect", { index_path: resolveFile(root, args.index_path), ledger_path: args.ledger_path ? resolveFile(root, args.ledger_path) : undefined, cursor: args.cursor ?? "0", limit: args.limit ?? 20 });
  },
});

export const resolve = tool({
  description: "记录主 agent 对疑点的原文核验结果。resolved/dismissed 必须给理由和证据标签；open 表示仍待处理。高影响未关闭疑点应在提交前解决。",
  args: { index_path: tool.schema.string(), ledger_path: tool.schema.string().optional(), updates_json: tool.schema.string().describe("处理数组：[{doubt_id,status:resolved|dismissed|open,reason,evidence_tags:[]}]"), },
  async execute(args, context) {
    const root = projectRoot(context.directory);
    return withJsonFile(JSON.parse(args.updates_json), async file => invoke(context, "resolve", {
      index_path: resolveFile(root, args.index_path),
      ledger_path: args.ledger_path ? resolveFile(root, args.ledger_path) : undefined,
      updates_file: file,
    }));
  },
});
