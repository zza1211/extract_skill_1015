import { tool } from "@opencode-ai/plugin";
import { executionContext, output, projectRoot, python, withJsonFile } from "../skills/term-extract/scripts/opencode_bridge.ts";

async function invoke(context: any, action: string, body: any) {
  const root = projectRoot(context.directory), actual = executionContext(context);
  if (!actual) throw new Error("需要真实 OpenCode 会话上下文；不能由模型填写校验人或会话身份。");
  return withJsonFile({ ...body, execution_context: actual }, async file =>
    output(await python(root, "agent_review.py", ["--project-root", root, action, "--request-file", file])));
}

export const pending = tool({
  description: "用户要求从网页已校验结果学习时，列出待学习修订的安全编号与状态，不返回标签。未校验、修改后待重校验、整篇弃用的标书不会参与。不会代用户点击已校验。",
  args: { cursor: tool.schema.number().int().optional(), limit: tool.schema.number().int().min(1).max(100).optional() },
  execute: (args, context) => invoke(context, "pending", args),
});

export const read = tool({
  description: "显式读取用户在网页完成整篇校验的当前修订：分页交付原始抽取、已校验结果、用户操作和原文索引。按 next_cursor 读完后核验与归纳；会记录已接触标签，之后新盲抽需新会话。",
  args: { run_id: tool.schema.string(), revision: tool.schema.number().int().min(1), cursor: tool.schema.number().int().optional(), max_bytes: tool.schema.number().int().min(4096).max(49152).optional() },
  execute: (args, context) => invoke(context, "read", args),
});

export const reflect = tool({
  description: "读取全部校验分页并核验原文后，提交最多三条有真实标签证据的可迁移经验；无新经验用空 insights 并说明原因。归档已校验快照为可信开发案例，去重生成候选并标记本修订已学习，不直接发布、不代人确认。",
  args: { run_id: tool.schema.string(), revision: tool.schema.number().int().min(1), reflection_json: tool.schema.string() },
  execute: (args, context) => invoke(context, "reflect", { run_id: args.run_id, revision: args.revision, reflection: JSON.parse(args.reflection_json) }),
});
