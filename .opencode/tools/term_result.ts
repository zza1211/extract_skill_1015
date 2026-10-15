import { tool } from "@opencode-ai/plugin";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { digest, output, projectRoot, python, resolveFile, saveDraft } from "../skills/term-extract/scripts/opencode_bridge.ts";

export const checkpoint = tool({
  description: "保存主 agent 的简短采购对象候选、区间、数量来源和疑点，供后续核验或中断恢复；不是最终提交，不判断候选语义。",
  args: {
    index_path: tool.schema.string(),
    candidates_json: tool.schema.string().describe("候选对象 JSON 数组字符串；可含条款标题、拟定区间、数量来源和疑点，不复述全部参数"),
    notes: tool.schema.string().optional().describe("本阶段的简短工作说明"),
  },
  async execute(args, context) {
    const root = projectRoot(context.directory);
    const indexPath = resolveFile(root, args.index_path);
    const index = JSON.parse(await readFile(indexPath, "utf8"));
    if (index.schema_version !== 2 || !Array.isArray(index.units)) throw new Error("请先注册文档并使用版本 2 的 index。");
    const candidates = JSON.parse(args.candidates_json);
    if (!Array.isArray(candidates) || candidates.some(value => !value || typeof value !== "object" || Array.isArray(value))) {
      throw new Error("候选必须是对象数组，可以保留待确认字段和疑点。");
    }
    const directory = path.join(path.dirname(indexPath), "draft");
    await mkdir(directory, { recursive: true });
    const checkpointPath = path.join(directory, "candidates.json");
    await writeFile(checkpointPath, JSON.stringify({
      status: "candidate_checkpoint",
      index_sha256: await digest(indexPath),
      source_path: index.source_path,
      skill_version: "0.2.0",
      candidates,
      notes: args.notes ?? "",
    }, null, 2) + "\n", "utf8");
    return output({ saved: true, stage: "candidate", candidate_count: candidates.length, checkpoint_path: checkpointPath, next_action: "围绕候选中的具体疑点核验，再生成正式结果并校验。" });
  },
});

export const validate = tool({
  description: "保存主 agent 的草稿并检查七字段 JSON、原文标签和关联。不会替主 agent 决定产品、边界或数量。",
  args: {
    index_path: tool.schema.string(),
    result_json: tool.schema.string().describe("合法 JSON 数组字符串，不带 Markdown 围栏"),
    evidence_json: tool.schema.string().describe("含 clauses 和 unresolved 的证据 JSON 字符串，详见输出协议"),
    allow_demo: tool.schema.boolean().default(false),
  },
  async execute(args, context) {
    const root = projectRoot(context.directory);
    const indexPath = resolveFile(root, args.index_path);
    const draft = await saveDraft(indexPath, args.result_json, args.evidence_json);
    const argv = ["--index", indexPath, "--result", draft.result, "--evidence", draft.evidence!, "--report", path.join(path.dirname(indexPath), "validation.json")];
    if (args.allow_demo) argv.push("--allow-demo");
    return output({ ...(await python(root, "validate_result.py", argv)), result_path: draft.result, evidence_path: draft.evidence });
  },
});

export const commit = tool({
  description: "核对草稿、读取回执及未解决事项后保存最终结果。仅记录主 agent 的决定，不调用其他抽取模型。",
  args: {
    index_path: tool.schema.string(),
    result_path: tool.schema.string().describe("term_result_validate 返回的草稿路径"),
    evidence_path: tool.schema.string(),
    model_id: tool.schema.string().optional().describe("当前 OpenCode 主模型标识，用于回放记录"),
    allow_demo: tool.schema.boolean().default(false),
  },
  async execute(args, context) {
    const root = projectRoot(context.directory);
    const indexPath = resolveFile(root, args.index_path);
    const argv = ["--index", indexPath, "--result", resolveFile(root, args.result_path), "--evidence", resolveFile(root, args.evidence_path), "--receipts", path.join(path.dirname(indexPath), "read_receipts.jsonl"), "--out-dir", path.join(path.dirname(indexPath), "final"), "--model-id", args.model_id || "未记录"];
    if (args.allow_demo) argv.push("--allow-demo");
    return output(await python(root, "commit_result.py", argv));
  },
});
