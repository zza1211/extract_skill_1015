import { tool } from "@opencode-ai/plugin";
import { existsSync } from "node:fs";
import { readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { digest, output, projectRoot, python, recordRead, resolveFile } from "../skills/term-extract/scripts/opencode_bridge.ts";

function compactOutput(response: any, maxBytes: number): string {
  if (!response?.model_view || typeof response.model_view !== "object") {
    throw new Error("文档工具未返回 compact model_view；请检查 Python 工具版本，不能交付内部覆盖元数据。");
  }
  const result = output(response.model_view);
  if (Buffer.byteLength(result, "utf8") > maxBytes) {
    throw new Error("本次模型视图超过读取预算，已拒绝交付且不会记录读取回执；请降低预算或继续分页。");
  }
  return result;
}

function byteBudget(value: number | undefined): number {
  const budget = value ?? 24576;
  if (!Number.isInteger(budget) || budget < 2048 || budget > 24576) {
    throw new Error("max_bytes 必须是 2048 到 24576 的整数。");
  }
  return budget;
}

async function hasCandidateCheckpoint(indexPath: string): Promise<boolean> {
  try {
    const checkpointPath = path.join(path.dirname(indexPath), "draft", "candidates.json");
    const checkpoint = JSON.parse(await readFile(checkpointPath, "utf8"));
    // 这里只核对阶段和本次输入身份，不要求候选完备或最终语义已正确。
    return checkpoint?.status === "candidate_checkpoint"
      && Array.isArray(checkpoint.candidates)
      && checkpoint.index_sha256 === await digest(indexPath);
  } catch {
    return false;
  }
}

export const prepare = tool({
  description: "注册标书 JSON；允许一个 text 或表格单元格包含多个 Cxx，保留每个标签的位置、表格行列和原文，建立可分页读取的索引。不做语义抽取。",
  args: {
    input_path: tool.schema.string().describe("原始 JSON 文件路径，相对项目根目录或绝对路径"),
    out_dir: tool.schema.string().optional().describe("本次独立工作目录；省略时自动创建 runs 下的新目录"),
    demo_tags: tool.schema.boolean().default(false).describe("仅显式演示时为缺标签样例生成模拟标签，结果标记为演示"),
  },
  async execute(args, context) {
    const root = projectRoot(context.directory);
    const demoTags = args.demo_tags ?? false;
    const directory = resolveFile(root, args.out_dir || path.join("runs", new Date().toISOString().replace(/[:.]/g, "-") + "-" + Math.random().toString(16).slice(2, 8)));
    if (existsSync(path.join(directory, "memory_snapshot.json"))) {
      throw new Error("此任务目录已固定经验快照。继续任务请使用现有 index；重新抽取或切换经验版本请指定新的 out_dir，避免混用历史回执。");
    }
    const argv = ["prepare", "--input", resolveFile(root, args.input_path), "--out-dir", directory];
    if (demoTags) argv.push("--demo-tags");
    const result = await python(root, "document_io.py", argv);
    // 每次 prepare 开始新的读取记录，不沿用上一轮会话的覆盖。
    await writeFile(path.join(path.dirname(result.index_path), "read_receipts.jsonl"), "", "utf8");
    const memory = await python(root, "memory_store.py", ["--project-root", root, "create-snapshot", "--index", result.index_path]);
    if (memory.ok === false || memory.error || !memory.snapshot_sha256) throw new Error(memory.error || "无法固定本任务的经验快照。");
    return output({ index_path: result.index_path, manifest_path: result.manifest_path, demo: result.demo, unit_count: result.unit_count, tag_count: result.tag_count,
      memory_snapshot: { snapshot_path: memory.snapshot_path, snapshot_sha256: memory.snapshot_sha256, release_sha256: memory.release_sha256,
        memory_count: memory.memory_count ?? memory.memories?.length ?? 0 },
      next_action: "使用 term_document_read 连续读取；原始 Cxx 保持不变。形成候选后，只针对具体疑点调用 term_memory_recall。" });
  },
});

export const read = tool({
  description: "按游标或 Cxx 范围读取原文，保留表格位置；明确返回分页和部分单元格信息，并记录实际交付回执。",
  args: {
    index_path: tool.schema.string(),
    cursor: tool.schema.string().default("0").describe("上一批 next_cursor，首批为 0"),
    start_tag: tool.schema.string().optional(),
    end_tag: tool.schema.string().optional(),
    max_bytes: tool.schema.number().int().min(2048).max(24576).default(24576),
  },
  async execute(args, context) {
    const root = projectRoot(context.directory);
    const indexPath = resolveFile(root, args.index_path);
    // OpenCode 可以直接调用 execute；Zod.default 不保证在此时已经应用。
    const cursor = args.cursor ?? "0";
    const maxBytes = byteBudget(args.max_bytes);
    const argv = ["read", "--index", indexPath, "--cursor", cursor, "--max-bytes", String(maxBytes), "--view", "compact"];
    if (Boolean(args.start_tag) !== Boolean(args.end_tag)) throw new Error("范围读取需要同时提供 start_tag 和 end_tag。");
    if (args.start_tag) argv.push("--start", args.start_tag, "--end", args.end_tag!);
    const response = await python(root, "document_io.py", argv);
    const result = compactOutput(response, maxBytes);
    // 视图序列化和字节检查成功后，使用内部完整 parts 记录实际覆盖。
    await recordRead(indexPath, cursor, { start_tag: args.start_tag, end_tag: args.end_tag }, response, context);
    return result;
  },
});

export const search = tool({
  description: "先用 term_result_checkpoint 保存本次候选和具体疑点，再按原文词句搜索核验设备名称、型号、采购数量或引用。搜索不代表全文已读取。",
  args: {
    index_path: tool.schema.string(),
    query: tool.schema.string().min(1),
    cursor: tool.schema.string().default("0"),
    limit: tool.schema.number().int().min(1).max(50).default(20),
    max_bytes: tool.schema.number().int().min(2048).max(24576).default(24576),
  },
  async execute(args, context) {
    const root = projectRoot(context.directory);
    const indexPath = resolveFile(root, args.index_path);
    if (!await hasCandidateCheckpoint(indexPath)) {
      return output({ ok: false, code: "CANDIDATE_CHECKPOINT_REQUIRED", error: "先用term_result_checkpoint保存候选和具体疑点，再做搜索核验" });
    }
    const cursor = args.cursor ?? "0";
    const maxBytes = byteBudget(args.max_bytes);
    const limit = args.limit ?? 20;
    if (!Number.isInteger(limit) || limit < 1 || limit > 50) {
      throw new Error("limit 必须是 1 到 50 的整数。");
    }
    const response = await python(root, "document_io.py", ["search", "--index", indexPath, "--query", args.query, "--cursor", cursor, "--limit", String(limit), "--max-bytes", String(maxBytes), "--view", "compact"]);
    // 搜索结果只交付证据，不计入全文读取回执。
    return compactOutput(response, maxBytes);
  },
});
