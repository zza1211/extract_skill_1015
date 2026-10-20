import { tool } from "@opencode-ai/plugin";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { digest, executionContext, output, projectRoot, python, resolveFile, saveDraft, withJsonFile } from "../skills/term-extract/scripts/opencode_bridge.ts";

export const checkpoint = tool({
  description: "增量保存已读原文中的简短采购对象候选、暂定区间、数量来源和疑点。出现明确对象即可保存，不必等全文或所有判断完成；全文最后一批读取后应先保存，再围绕疑点核验。不是最终提交，不判断候选语义。",
  args: {
    index_path: tool.schema.string(),
    candidates_json: tool.schema.string().describe("候选对象 JSON 数组字符串；可含条款标题、暂定区间、数量来源和具体疑点，待定归属/边界/数量可以标明待确认；不复述全部参数。尚未识别到对象时可传 [] 并在 notes 写待核查范围"),
    notes: tool.schema.string().optional().describe("简短记录已读范围、下一读取位置和待核查事项，不保存内部思维链"),
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
      skill_version: "0.7.0",
      candidates,
      notes: args.notes ?? "",
    }, null, 2) + "\n", "utf8");
    return output({ saved: true, stage: "candidate", candidate_count: candidates.length, checkpoint_path: checkpointPath, next_action: "若全文尚未读完，沿 next_cursor 继续完整读取，再更新候选；若已读完，批量补读或搜索候选中的具体疑点，逐步确定归属、边界和数量，再生成正式结果并校验。候选保存不代表提交，最终仍须完成覆盖与语义自检。" });
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
  description: "核对当前主会话全文读取、独立核验处置和最终草稿版本后提交结果，登记运行与简短复盘。未完成独立核验或仍有发现未处理时拒绝提交；不调用模型，不自动发布。",
  args: {
    index_path: tool.schema.string(),
    result_path: tool.schema.string().describe("term_result_validate 返回的草稿路径"),
    evidence_path: tool.schema.string(),
    reflection_json: tool.schema.string().describe("本次复盘 JSON：{insights:[{title,issue_type,triggers,applies_when,rule,exclusions,observation,evidence_tags}],no_experience_reason}。最多三条；无新经验时 insights=[] 并填写原因。依据须来自本次原文和实际核验，不复制具体答案。"),
    model_id: tool.schema.string().optional().describe("当前 OpenCode 主模型标识，用于回放记录"),
    allow_demo: tool.schema.boolean().default(false),
  },
  async execute(args, context) {
    const root = projectRoot(context.directory);
    const indexPath = resolveFile(root, args.index_path);
    if (typeof args.reflection_json !== "string" || !args.reflection_json.trim()) {
      throw new Error("提交需附带本次简短复盘；没有新经验时传 insights=[] 和 no_experience_reason，不得编造经验。");
    }
    const reflection = JSON.parse(args.reflection_json);
    const argv = ["--project-root", root, "--index", indexPath, "--result", resolveFile(root, args.result_path), "--evidence", resolveFile(root, args.evidence_path), "--receipts", path.join(path.dirname(indexPath), "read_receipts.jsonl"), "--out-dir", path.join(path.dirname(indexPath), "final"), "--model-id", args.model_id || "未记录"];
    const actualContext = executionContext(context);
    if (!actualContext && (context.agent || context.sessionID || context.messageID)) {
      throw new Error("提交需要完整的真实 OpenCode 执行上下文，不能缺少身份后退为历史 CLI 路径。");
    }
    if (actualContext) argv.push("--execution-context-json", JSON.stringify(actualContext));
    if (context.agent === "term-trial") argv.push("--trial-only");
    if (args.allow_demo) argv.push("--allow-demo");
    return withJsonFile(reflection, async file =>
      output(await python(root, "commit_result.py", [...argv, "--reflection-file", file])));
  },
});
