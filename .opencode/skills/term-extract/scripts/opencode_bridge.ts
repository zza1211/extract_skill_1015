import { execFile } from "node:child_process";
import { createHash } from "node:crypto";
import { existsSync } from "node:fs";
import { appendFile, mkdir, mkdtemp, readFile, rmdir, unlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { promisify } from "node:util";

const executeFile = promisify(execFile);
const skillRelative = path.join(".opencode", "skills", "term-extract");

export function projectRoot(directory: string): string {
  let current = path.resolve(directory);
  while (true) {
    if (existsSync(path.join(current, skillRelative, "SKILL.md"))) return current;
    const parent = path.dirname(current);
    if (parent === current) throw new Error("找不到 term-extract skill，请从包含 .opencode/skills/term-extract 的项目根目录启动 OpenCode。");
    current = parent;
  }
}

export function resolveFile(root: string, file: string): string {
  return path.resolve(root, file);
}

export async function withJsonFile<T>(value: unknown, action: (file: string) => Promise<T>): Promise<T> {
  // 完整结果可能超过 Windows 的命令行长度限制，用临时 JSON 文件交给 Python。
  const directory = await mkdtemp(path.join(os.tmpdir(), "term-extract-input-"));
  const file = path.join(directory, "payload.json");
  try {
    await writeFile(file, JSON.stringify(value) + "\n", "utf8");
    return await action(file);
  } finally {
    await unlink(file).catch(error => { if (error.code !== "ENOENT") throw error; });
    await rmdir(directory);
  }
}

export async function digest(file: string): Promise<string> {
  return createHash("sha256").update(await readFile(file)).digest("hex");
}

export async function python(root: string, script: string, args: string[]): Promise<any> {
  const executable = process.env.TERM_EXTRACT_PYTHON || "python";
  const file = path.join(root, skillRelative, "scripts", script);
  let stdout: string;
  try {
    const result = await executeFile(executable, ["-X", "utf8", file, ...args], {
      cwd: root,
      windowsHide: true,
      encoding: "utf8",
      maxBuffer: 2 * 1024 * 1024,
      env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" },
    });
    stdout = result.stdout;
  } catch (error: any) {
    // 校验未通过也是工具结果，交给主 agent 处理。
    if (error.code === 2 && error.stdout?.trim().startsWith("{")) stdout = error.stdout;
    else throw new Error((error.stderr || error.message || "Python 工具执行失败").trim());
  }
  try { return JSON.parse(stdout); }
  catch { throw new Error("工具没有返回合法 JSON，请检查 Python 运行环境。"); }
}

export function output(value: unknown): string {
  const result = JSON.stringify(value);
  if (Buffer.byteLength(result, "utf8") > 48 * 1024) {
    throw new Error("本次返回超过 48 KiB，已拒绝交付；请降低读取预算或继续分页，不要把截断内容当成全文。");
  }
  return result;
}

export function executionContext(context: any): any {
  if (!context?.sessionID || !context?.messageID || !context?.agent) return null;
  return { session_id: context.sessionID, message_id: context.messageID, agent: context.agent };
}

export async function recordRead(indexPath: string, cursor: string, request: any, response: any, context?: any): Promise<void> {
  // 在返回大小校验通过后才记录。回执证明交付范围，不证明模型理解。
  const receipt = {
    index_sha256: await digest(indexPath),
    time: new Date().toISOString(),
    cursor,
    request,
    coverage_kind: response.coverage_kind,
    parts: response.parts || [],
    unit_ids: response.unit_ids || [],
    tags: response.tags || [],
    complete_tags: response.complete_tags || [],
    partial_tags: response.partial_tags || [],
    next_cursor: response.next_cursor,
    has_more: response.has_more,
    view: response.model_view ? "compact" : "machine",
    byte_size: Buffer.byteLength(JSON.stringify(response.model_view ?? response), "utf8"),
    worker_byte_size: response.byte_size,
    execution_context: executionContext(context),
  };
  await appendFile(path.join(path.dirname(indexPath), "read_receipts.jsonl"), JSON.stringify(receipt) + "\n", "utf8");
}

export async function saveDraft(indexPath: string, resultJson: string, evidenceJson?: string): Promise<{ result: string; evidence?: string }> {
  const result = JSON.parse(resultJson);
  const evidence = evidenceJson === undefined ? undefined : JSON.parse(evidenceJson);
  const directory = path.join(path.dirname(indexPath), "draft");
  await mkdir(directory, { recursive: true });
  const resultPath = path.join(directory, "result.json");
  await writeFile(resultPath, JSON.stringify(result, null, 2) + "\n", "utf8");
  const evidencePath = evidence === undefined ? undefined : path.join(directory, "evidence.json");
  if (evidencePath) await writeFile(evidencePath, JSON.stringify(evidence, null, 2) + "\n", "utf8");
  return { result: resultPath, evidence: evidencePath };
}
