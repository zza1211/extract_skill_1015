import { tool } from "@opencode-ai/plugin";
import { output, projectRoot, python, resolveFile } from "../skills/term-extract/scripts/opencode_bridge.ts";

const issueTypes = ["missing_product", "boundary", "quantity", "association"] as const;

export const recall = tool({
  description: "围绕当前候选的具体疑点，从本任务固定的已发布经验快照召回少量规则。返回适用与不适用条件，不能替代原文证据；不查询历史运行或候选经验。",
  args: {
    index_path: tool.schema.string(),
    query: tool.schema.string().min(1).describe("当前疑点的具体描述或原文词句，避免只写设备大类"),
    issue_type: tool.schema.enum(issueTypes).optional(),
    limit: tool.schema.number().int().min(1).max(5).optional(),
    max_bytes: tool.schema.number().int().min(1024).max(8192).optional(),
  },
  async execute(args, context) {
    const root = projectRoot(context.directory);
    const indexPath = resolveFile(root, args.index_path);
    const limit = args.limit ?? 3;
    const maxBytes = args.max_bytes ?? 6144;
    if (!Number.isInteger(limit) || limit < 1 || limit > 5) throw new Error("limit 必须为 1 到 5。");
    if (!Number.isInteger(maxBytes) || maxBytes < 1024 || maxBytes > 8192) throw new Error("max_bytes 必须为 1024 到 8192。");
    const argv = ["--project-root", root, "recall", "--index", indexPath, "--query", args.query,
      "--limit", String(limit), "--max-bytes", String(maxBytes), "--defer-receipt"];
    if (args.issue_type) argv.push("--issue-type", args.issue_type);
    const response = await python(root, "memory_store.py", argv);
    if (response.ok === false || response.error || response.errors) throw new Error(response.error || JSON.stringify(response.errors) || "经验召回失败。");
    if (!Array.isArray(response.memories) || !response.snapshot_sha256) throw new Error("经验召回缺少合法快照或条目列表。");
    const delivered = output(response);
    if (Buffer.byteLength(delivered, "utf8") > maxBytes) throw new Error("经验交付超过预算，未记录读取回执。");
    const recorded = await python(root, "memory_store.py", ["--project-root", root, "record-delivery", "--index", indexPath, "--response-json", delivered]);
    if (recorded.recorded !== true) throw new Error(recorded.error || "经验交付回执保存失败，未交付经验。");
    return delivered;
  },
});

export const correction = tool({
  description: "供经验维护任务读取人工导入纠错的元数据和文件引用，不确认新标签、不发布经验。",
  args: { correction_id: tool.schema.string().min(1) },
  async execute(args, context) {
    const root = projectRoot(context.directory);
    return output(await python(root, "memory_store.py", ["--project-root", root, "show-correction", "--correction-id", args.correction_id]));
  },
});

export const show = tool({
  description: "供经验维护任务按编号查看某个不可变修订；候选仅用于维护，不供日常抽取采用。",
  args: { memory_id: tool.schema.string().min(1), revision: tool.schema.number().int().min(1) },
  async execute(args, context) {
    const root = projectRoot(context.directory);
    return output(await python(root, "memory_store.py", ["--project-root", root, "show", "--memory-id", args.memory_id, "--revision", String(args.revision)]));
  },
});

export const propose = tool({
  description: "仅供经验维护任务：根据已有人工纠错创建候选经验修订，必须说明适用条件和不适用情况；不会发布或影响日常抽取。",
  args: {
    payload_json: tool.schema.string().describe("经验 JSON 对象，字段见 references/experience.md"),
    memory_id: tool.schema.string().optional().describe("修订已有经验时填写；不覆盖旧修订"),
  },
  async execute(args, context) {
    const root = projectRoot(context.directory);
    const payload = JSON.parse(args.payload_json);
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("经验必须是 JSON 对象。");
    const argv = ["--project-root", root, "propose", "--payload-json", JSON.stringify(payload)];
    if (args.memory_id) argv.push("--memory-id", args.memory_id);
    return output(await python(root, "memory_store.py", argv));
  },
});

export const evaluate = tool({
  description: "仅供经验维护任务：按人工关键参数断言评测给定 baseline/candidate 输出，保存绑定内容哈希的回归报告。不会调用模型生成输出，也不自动批准或发布。",
  args: {
    memory_id: tool.schema.string().min(1),
    revision: tool.schema.number().int().min(1),
    manifest_path: tool.schema.string().describe("私有评测清单路径，相对项目或绝对路径"),
  },
  async execute(args, context) {
    const root = projectRoot(context.directory);
    const result = await python(root, "memory_evaluate.py", ["--project-root", root, "--memory-id", args.memory_id,
      "--revision", String(args.revision), "--manifest", resolveFile(root, args.manifest_path)]);
    return output({
      evaluation_id: result.evaluation_id, report_sha256: result.report_sha256,
      candidate: result.candidate, base_release_sha256: result.base_release_sha256,
      status: result.status, summary: result.summary, notice: result.notice,
      error: result.error, errors: result.errors,
      next_action: "报告只验证给定输出；维护者应审阅报告、输出来源与适用边界后，通过 CLI 决定是否发布。",
    });
  },
});
