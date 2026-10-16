import { tool } from "@opencode-ai/plugin";
import { output, projectRoot, python, withJsonFile } from "../skills/term-extract/scripts/opencode_bridge.ts";

async function invoke(context: any, action: string, body: any) {
  const root = projectRoot(context.directory);
  return withJsonFile(body, async file => output(await python(root, "evolution_cli.py", ["--project-root", root, action, "--request-file", file])));
}

export const create = tool({
  description: "创建纯抽取或标签学习任务。标签模式先隔离盲抽并冻结，再由服务比较标签；不要先用 read 读取标签。工具只接收用户提供的路径，创建后调用 start。",
  args: {
    mode: tool.schema.enum(["extract", "learn_labels"]),
    input_path: tool.schema.string().min(1),
    labels_path: tool.schema.string().optional(),
    family_id: tool.schema.string().optional().describe("标签模式必填；同模板、同项目变体使用相同案例族"),
    auto_evolve: tool.schema.boolean().optional(),
  },
  execute: (args, context) => invoke(context, "create", args),
});

export const start = tool({
  description: "后台启动或继续学习任务；复用已冻结的盲抽，不覆盖历史。运行中无需高频轮询。已完成任务需另建。",
  args: { job_id: tool.schema.string().min(1) },
  execute: (args, context) => invoke(context, "start", args),
});

export const status = tool({
  description: "查看任务阶段、结果路径、候选、验证与待确认事项。标签仅在盲抽冻结后用于比较。",
  args: { job_id: tool.schema.string().min(1), include_result: tool.schema.boolean().optional() },
  execute: (args, context) => invoke(context, "status", args),
});

export const import_case = tool({
  description: "归档用户明确提供的可信输入和正确标签；标签应与输入同套 Cxx 且按当前口径确认。独立回归与学习使用不同案例族；保留测试不用于经验归纳。禁止把 agent 修正稿冒充用户标签。",
  args: {
    input_path: tool.schema.string().min(1), labels_path: tool.schema.string().min(1), family_id: tool.schema.string().min(1),
    split: tool.schema.enum(["development", "regression", "holdout"]).optional(),
  },
  execute: (args, context) => invoke(context, "import-case", args),
});

export const reviews = tool({
  description: "查看需要人工判断的局部差异或经验冲突；呈现原问题、选项和适用范围。",
  args: { state: tool.schema.enum(["open", "resolved", "all"]).optional() },
  execute: (args, context) => invoke(context, "reviews", args),
});

export const reply = tool({
  description: "仅在用户明确答复某个待办时使用。保存其选项和原话到与工作台相同的记录；agent 不替用户选择，不将局部答复变成整篇验收或全局规则。",
  args: {
    review_id: tool.schema.string().min(1), decision: tool.schema.string().min(1),
    answer: tool.schema.string(), source_ref: tool.schema.string().min(1).describe("对应用户原话或消息引用"),
    actor: tool.schema.string().optional(), expected_revision: tool.schema.number().int().optional(),
  },
  execute: (args, context) => invoke(context, "reply", args),
});
