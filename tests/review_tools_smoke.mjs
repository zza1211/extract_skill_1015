// 真实工具传输链：合成提交 → 模拟网页确认 → OpenCode 学习；不调用模型。
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import * as documents from "../.opencode/tools/term_document.ts";
import { closeSyntheticAudit } from "./audit_fixture.mjs";
import * as results from "../.opencode/tools/term_result.ts";
import * as reviews from "../.opencode/tools/term_review.ts";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const temporary = await mkdtemp(path.join(os.tmpdir(), "term-review-tools-"));
process.env.TERM_EXTRACT_MEMORY_DIR = path.join(temporary, "private-memory");
const context = { directory: root, sessionID: "synthetic-review-session", messageID: "synthetic-review-message", agent: "term-extractor" };
const invoke = async (tool, args = {}) => JSON.parse(await tool.execute(args, context));
const noInsight = { insights: [], no_experience_reason: "合成协议验收，无需生成业务经验。" };
const input = path.join(temporary, "synthetic.json");
await writeFile(input, JSON.stringify([{ text: "<C1>Access Point<C2>Support 24 concurrent users.<C3>Procurement quantity: 2." }]), "utf8");
const prepared = await invoke(documents.prepare, { input_path: input, out_dir: path.join(temporary, "run") });
let cursor = "0";
for (;;) {
  const page = await invoke(documents.read, { index_path: prepared.index_path, cursor });
  if (!page.has_more) break;
  cursor = page.next_cursor;
}
const predicted = [{ id: 0, 条款标题: "Access Point", 条款分类: "normal", 相关条款: [], 产品分类: "WLAN AP", 定位标识: ["<C1>", "<C2>"], 产品数量: "24" }];
const evidence = { clauses: [{ id: 0, quantity_source_tags: ["<C3>"], related_source_tags: [], notes: "Synthetic incorrect quantity for protocol testing." }], unresolved: [] };
const checked = await invoke(results.validate, { index_path: prepared.index_path, result_json: JSON.stringify(predicted), evidence_json: JSON.stringify(evidence) });
assert.equal(checked.ok, true);
await closeSyntheticAudit({ index_path: prepared.index_path, result_path: checked.result_path, evidence_path: checked.evidence_path }, context);
const committed = await invoke(results.commit, { index_path: prepared.index_path, result_path: checked.result_path, evidence_path: checked.evidence_path,
  reflection_json: JSON.stringify(noInsight), model_id: "synthetic-review-protocol-test" });
assert.equal(committed.committed, true);
assert.equal(committed.result_review.status, "pending");
assert.ok(Buffer.byteLength(JSON.stringify(committed)) < 49152);
const runId = committed.learning.run_id;
assert.deepEqual((await invoke(reviews.pending)).reviews, []);
assert.match((await invoke(reviews.read, { run_id: runId, revision: 1 })).error, /尚未人工校验/);

// 仅在临时合成库中模拟网页用户操作，生产 Agent 没有代人确认的工具。
const execute = promisify(execFile);
const scripts = path.join(root, ".opencode", "skills", "term-extract", "scripts");
const { stdout } = await execute(process.env.TERM_EXTRACT_PYTHON || "python", ["-B", "-X", "utf8", "-c", `
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import document_review as reviews
root, run_id = Path(sys.argv[2]), sys.argv[3]
current = reviews.get_review(root, run_id)
doc_id = current['document_id']
current = reviews.handle_post(root, [doc_id, 'edit'], {'run_id': run_id, 'expected_revision': current['revision'], 'action': 'update', 'clause_key': current['clauses'][0]['clause_key'], 'patch': {'产品数量': '2'}, 'reason': '合成测试：采购数量不是并发容量。'})
current = reviews.handle_post(root, [doc_id, 'verify'], {'run_id': run_id, 'expected_revision': current['revision'], 'acknowledged': True, 'reviewer': 'synthetic-fixture', 'family_id': 'synthetic-review-family'})
print(json.dumps({'revision': current['revision'], 'current_result_path': current['current_result_path']}))
`, scripts, root, runId], { cwd: root, windowsHide: true, encoding: "utf8", env: { ...process.env, PYTHONUTF8: "1" } });
const verified = JSON.parse(stdout);
const pending = await invoke(reviews.pending);
assert.equal(pending.reviews[0].run_id, runId);
assert.ok(!JSON.stringify(pending).includes("before_result"));
assert.ok(!(pending.reviews[0].labels_path));
const unread = await invoke(reviews.reflect, { run_id: runId, revision: verified.revision, reflection_json: JSON.stringify(noInsight) });
assert.match(unread.error, /完整读取/);
let reviewCursor = 0, seenAfter = false;
for (;;) {
  const page = await invoke(reviews.read, { run_id: runId, revision: verified.revision, cursor: reviewCursor, max_bytes: 4096 });
  assert.ok(Buffer.byteLength(JSON.stringify(page)) <= 4096);
  seenAfter ||= page.items.some(item => item.kind === "after" && item.clause.产品数量 === "2");
  if (page.next_cursor === null) break;
  reviewCursor = page.next_cursor;
}
assert.ok(seenAfter);
const reflected = await invoke(reviews.reflect, { run_id: runId, revision: verified.revision, reflection_json: JSON.stringify(noInsight) });
assert.equal(reflected.status, "learned");
assert.deepEqual(reflected.candidates, []);
assert.equal((await invoke(reviews.reflect, { run_id: runId, revision: verified.revision, reflection_json: JSON.stringify(noInsight) })).replayed, true);
assert.deepEqual((await invoke(reviews.pending)).reviews, []);
assert.equal(JSON.parse(await readFile(verified.current_result_path, "utf8"))[0].产品数量, "2");
assert.equal(JSON.parse(await readFile(committed.result_path, "utf8"))[0].产品数量, "24");
assert.ok(!("verify" in reviews) && !("publish" in reviews));
console.log(JSON.stringify({ ok: true, human_review_required: true, working_file_sync: true, original_preserved: true,
  paginated_review_delivery: true, no_insight_learning: true, idempotent_learning: true, semantic_accuracy_tested: false, output_directory: temporary }));
