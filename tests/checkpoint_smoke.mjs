// 候选检查点在模型正式输出前保留工作产物；不能冒充最终结果。
import assert from "node:assert/strict";
import { access, mkdtemp, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as documents from "../.opencode/tools/term_document.ts";
import * as results from "../.opencode/tools/term_result.ts";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const temporary = await mkdtemp(path.join(os.tmpdir(), "term-extract-checkpoint-"));
const inputPath = path.join(temporary, "input.json");
await writeFile(inputPath, JSON.stringify([{ text: "<C0>Access Points\n<C1>支持Wi-Fi 7" }]), "utf8");
const context = { directory: root };
const prepared = JSON.parse(await documents.prepare.execute({ input_path: inputPath, out_dir: path.join(temporary, "work") }, context));
const candidates = [{ 条款标题: "Access Points", 定位标识: ["<C0>", "<C1>"], 疑点: ["数量尚未确认"] }];
const reply = JSON.parse(await results.checkpoint.execute({ index_path: prepared.index_path, candidates_json: JSON.stringify(candidates) }, context));
assert.equal(reply.stage, "candidate");
const stored = JSON.parse(await readFile(reply.checkpoint_path, "utf8"));
assert.deepEqual(stored.candidates, candidates);
assert.equal(stored.status, "candidate_checkpoint");
assert.equal(stored.index_sha256.length, 64);
await assert.rejects(access(path.join(path.dirname(prepared.index_path), "final", "result.json")), { code: "ENOENT" });
console.log(JSON.stringify({ ok: true, candidate_saved: true, final_not_created: true, output_directory: temporary }));
