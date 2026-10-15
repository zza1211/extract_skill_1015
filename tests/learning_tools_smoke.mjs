// 空库首篇、复盘和反馈的真实工具适配回归；全部为临时合成材料，不调用模型。
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import * as documents from "../.opencode/tools/term_document.ts";
import * as results from "../.opencode/tools/term_result.ts";
import * as memories from "../.opencode/tools/term_memory.ts";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const context = { directory: root };
const temporary = await mkdtemp(path.join(os.tmpdir(), "term-extract-learning-tools-"));
process.env.TERM_EXTRACT_MEMORY_DIR = path.join(temporary, "private-memory");
const execute = promisify(execFile);
const scripts = path.join(root, ".opencode", "skills", "term-extract", "scripts");
const json = async file => JSON.parse(await readFile(file, "utf8"));
const save = async (file, value) => writeFile(file, JSON.stringify(value, null, 2) + "\n", "utf8");
const invoke = async (tool, args) => JSON.parse(await tool.execute(args, context));
const noInsight = { insights: [], no_experience_reason: "合成任务没有额外可复用发现，不强行生成经验。" };
const reflection = {
  insights: [{ title: "清单数量需与技术规格中的容量区分", issue_type: "quantity", triggers: ["access point", "数量"],
    applies_when: "规格描述与采购清单分别出现，并能核对为同一独立采购对象时。",
    rule: "核对采购清单中的数量来源，避免把规格内的并发容量当作采购量。",
    exclusions: "名称相似但属于不同采购项，或清单只给系统内组件配置时不直接复用。",
    observation: "本次原文将采购数量与支持并发用户数分别列出。", evidence_tags: ["<C2>", "<C3>"] }],
  no_experience_reason: "",
};

async function cli(script, args, success = true) {
  let stdout, code = 0;
  try {
    ({ stdout } = await execute(process.env.TERM_EXTRACT_PYTHON || "python",
      ["-B", "-X", "utf8", path.join(scripts, script), "--project-root", root, ...args],
      { cwd: root, windowsHide: true, encoding: "utf8", env: { ...process.env, PYTHONUTF8: "1" } }));
  } catch (error) { stdout = error.stdout; code = error.code; }
  assert.equal(code, success ? 0 : 2, stdout);
  return JSON.parse(stdout);
}

const input = path.join(temporary, "source.json");
await save(input, [{ text: "<C1>Access Point requirements<C2>Support 200 concurrent users.<C3>Procurement list: Access Point, quantity 2." }]);
const prepared = await invoke(documents.prepare, { input_path: input, out_dir: path.join(temporary, "first") });
assert.equal(prepared.memory_snapshot.memory_count, 0);
let cursor = "0";
for (;;) {
  const page = await invoke(documents.read, { index_path: prepared.index_path, cursor });
  if (!page.has_more) break;
  cursor = page.next_cursor;
}
const clause = quantity => [{ id: 0, 条款标题: "Access Point", 条款分类: "normal", 相关条款: [],
  产品分类: "WLAN AP", 定位标识: ["<C1>", "<C2>"], 产品数量: quantity }];
const evidence = { clauses: [{ id: 0, quantity_source_tags: ["<C3>"], related_source_tags: [],
  notes: "合成协议测试；完整原文明示两种不同含义的数字。" }], unresolved: [] };
async function checked(quantity) {
  const draft = await invoke(results.validate, { index_path: prepared.index_path,
    result_json: JSON.stringify(clause(quantity)), evidence_json: JSON.stringify(evidence) });
  assert.equal(draft.ok, true);
  return draft;
}
async function submit(draft, value) {
  return invoke(results.commit, { index_path: prepared.index_path, result_path: draft.result_path,
    evidence_path: draft.evidence_path, reflection_json: JSON.stringify(value), model_id: "synthetic-learning-protocol-test" });
}

const firstDraft = await checked("200");
await assert.rejects(results.commit.execute({ index_path: prepared.index_path, result_path: firstDraft.result_path,
  evidence_path: firstDraft.evidence_path }, context), /复盘/);
const invalid = structuredClone(reflection);
invalid.insights[0].evidence_tags = ["<C999>"];
const invalidCommit = await submit(firstDraft, invalid);
assert.equal(invalidCommit.committed, false, "不存在的来源标签不得生成候选或提交结果");

const first = await submit(firstDraft, reflection);
assert.equal(first.committed, true);
assert.equal(typeof first.learning.run_id, "string", JSON.stringify(first.learning));
assert.notEqual(first.learning.status, "learning_failed", JSON.stringify(first.learning));
const runId = first.learning.run_id;
assert.deepEqual((await json(first.run_path)).learning, first.learning);
const entries = await cli("memory_store.py", ["list"]);
assert.equal(entries.memories.length, 1, "空库首篇有发现时应自动生成候选");
assert.equal(entries.memories[0].state, "candidate");
assert.equal(entries.memories[0].active_revision, null);
const proposed = await invoke(memories.show, { memory_id: entries.memories[0].memory_id, revision: 1 });
assert.equal(proposed.payload.correction_ids.length, 0);
assert.equal(proposed.payload.source_ids.length, 1);
const source = await invoke(memories.source, { source_id: proposed.payload.source_ids[0] });
assert.ok(JSON.stringify(source).includes(runId));
const replay = await submit(firstDraft, reflection);
assert.equal(replay.learning.run_id, runId);
assert.equal((await cli("memory_store.py", ["list"])).memories.length, 1, "重试不能重复生成候选");
const sameReflection = await invoke(memories.reflect, { run_id: runId, reflection_json: JSON.stringify(reflection) });
assert.notEqual(sameReflection.ok, false);

const second = await invoke(documents.prepare, { input_path: input, out_dir: path.join(temporary, "second") });
assert.equal(second.memory_snapshot.memory_count, 0, "未确认、未发布的首篇经验不能污染第二篇");
assert.deepEqual((await invoke(memories.recall, { index_path: second.index_path, query: "access point 数量" })).memories, []);

const feedbackText = "采购数量是2，不是200；200是并发容量。";
const preliminaryFeedback = await invoke(memories.feedback, { run_id: runId, feedback_text: feedbackText,
  reflection_json: JSON.stringify(noInsight) });
assert.equal(typeof preliminaryFeedback.feedback_id, "string");
const feedback = await invoke(memories.feedback, { run_id: runId, feedback_text: feedbackText,
  after_result_json: JSON.stringify(clause("2")), reflection_json: JSON.stringify(reflection) });
assert.notEqual(feedback.ok, false, JSON.stringify(feedback));
assert.equal(typeof feedback.feedback_id, "string");
assert.notEqual(feedback.feedback_id, preliminaryFeedback.feedback_id,
  "先存原话后补完整修正稿应创建新的反馈，不能改写原先记录");
assert.equal((await cli("memory_store.py", ["list"])).memories.length, 2);
const repeatedFeedback = await invoke(memories.feedback, { run_id: runId, feedback_text: feedbackText,
  after_result_json: JSON.stringify(clause("2")) + " ".repeat(50000), reflection_json: JSON.stringify(reflection) });
assert.equal(repeatedFeedback.feedback_id, feedback.feedback_id);
assert.equal((await cli("memory_store.py", ["list"])).memories.length, 2);
assert.equal((await json(first.result_path))[0].产品数量, "200", "反馈不能偷偷覆盖已提交结果");

const updated = await submit(await checked("2"), noInsight);
assert.equal(updated.committed, true);
assert.notEqual(updated.learning.run_id, runId, "修正结果应生成新的不可变运行身份");
assert.equal((await json(updated.result_path))[0].产品数量, "2");
const original = await cli("memory_learning.py", ["show-run", "--run-id", runId]);
assert.ok(JSON.stringify(original).includes(runId), "旧运行仍能追溯");
assert.equal((await cli("memory_store.py", ["list"])).memories.length, 2, "无新经验不应机械生成候选");

// 用真实模块贯通人工确认、两种评测和发布；不是模型质量评测。
const commonArgs = ["--confirmed-by", "synthetic-human-fixture", "--family-id", "synthetic-development",
  "--note", "Synthetic protocol fixture; no real human annotation or model accuracy claim."];
await cli("memory_learning.py", ["confirm-feedback", "--feedback-id", preliminaryFeedback.feedback_id, ...commonArgs], false);
await cli("memory_learning.py", ["confirm-feedback", "--feedback-id", feedback.feedback_id, ...commonArgs]);
await cli("memory_learning.py", ["confirm-run", "--run-id", updated.learning.run_id, ...commonArgs]);

async function caseFixture(name, role, family, sourceText, indexPath, baselineQuantity = "2") {
  if (!indexPath) {
    const sourcePath = path.join(temporary, name + ".json");
    await save(sourcePath, [{ text: sourceText }]);
    indexPath = (await invoke(documents.prepare, { input_path: sourcePath, out_dir: path.join(temporary, name) })).index_path;
  }
  const baseline = path.join(temporary, name + "-baseline.json");
  const candidate = path.join(temporary, name + "-candidate.json");
  await save(baseline, clause(baselineQuantity));
  await save(candidate, clause("2"));
  return { case_id: name, family_id: family, role, split: ["fix", "success"].includes(role) ? "development" : "regression",
    confirmed_by: "synthetic-human-fixture", index_path: indexPath,
    baseline_result_path: baseline, candidate_result_path: candidate,
    assertions: [{ object_key: "access-point", anchor_tag: "<C1>", required_tags: ["<C1>", "<C2>"],
      forbidden_tags: ["<C3>"], quantity: "2", clause_category: "normal" }],
    ...(role === "counterexample" ? { scope_note: "Synthetic counterexample: quantity is inline; a separate other-device list must not override it." } : {}) };
}
const regression = await caseFixture("independent-regression", "regression", "synthetic-independent",
  "<C1>Independent Access Point<C2>Requirements and procurement quantity 2.<C3>Another product list.");
const counterexample = await caseFixture("independent-counterexample", "counterexample", "synthetic-counterexample",
  "<C1>Access Point, procurement quantity 2.<C2>Required 200 concurrent users.<C3>Different device: quantity 5.");
const manifest = path.join(temporary, "evaluation.json");
const fix = await caseFixture("feedback-fix", "fix", "synthetic-development", null, prepared.index_path, "200");
await save(manifest, { schema_version: 1, cases: [fix, regression, counterexample] });
const feedbackCandidate = feedback.candidates[0];
const fixEvaluation = await invoke(memories.evaluate, { memory_id: feedbackCandidate.memory_id,
  revision: feedbackCandidate.revision, manifest_path: manifest });
assert.equal(fixEvaluation.status, "passed", JSON.stringify(fixEvaluation));
assert.equal(fixEvaluation.summary.fix_improvements, 1);
await cli("memory_store.py", ["publish", "--memory-id", feedbackCandidate.memory_id, "--revision", String(feedbackCandidate.revision),
  "--evaluation-id", fixEvaluation.evaluation_id, "--reviewer", "synthetic-human-fixture", "--note", "Synthetic feedback publication fixture."]);

const { observation, evidence_tags, ...rule } = reflection.insights[0];
const successCandidate = await invoke(memories.propose, { payload_json: JSON.stringify({ ...rule,
  correction_ids: [], source_ids: [updated.learning.run_id] }) });
const successCase = await caseFixture("success-source", "success", "synthetic-development", null, prepared.index_path);
await save(manifest, { schema_version: 1, cases: [successCase, regression, counterexample] });
const successEvaluation = await invoke(memories.evaluate, { memory_id: successCandidate.memory_id,
  revision: successCandidate.revision, manifest_path: manifest });
assert.equal(successEvaluation.status, "passed", JSON.stringify(successEvaluation));
assert.equal(successEvaluation.summary.fix_improvements, 0, "成功经验不得伪造错误改善");
assert.equal(successEvaluation.summary.success_cases, 1);
await cli("memory_store.py", ["publish", "--memory-id", successCandidate.memory_id, "--revision", String(successCandidate.revision),
  "--evaluation-id", successEvaluation.evaluation_id, "--reviewer", "synthetic-human-fixture", "--note", "Synthetic successful-case publication fixture."]);
const third = await invoke(documents.prepare, { input_path: input, out_dir: path.join(temporary, "after-publication") });
assert.equal(third.memory_snapshot.memory_count, 2);
assert.equal((await invoke(memories.recall, { index_path: third.index_path, query: "access point 数量" })).memories.length, 2);
assert.deepEqual((await invoke(memories.recall, { index_path: second.index_path, query: "access point 数量" })).memories, [],
  "发布不会改变已冻结的旧任务");

// 引号转义会放大 Windows argv，整份反馈请求必须走文件而非字符串参数。
const quotedReflection = { insights: [0, 1, 2].map(index => ({ ...reflection.insights[0],
  title: `Synthetic quoted payload ${index}`, rule: '"'.repeat(1000), observation: '"'.repeat(900) })), no_experience_reason: "" };
const quotedFeedback = await invoke(memories.feedback, { run_id: updated.learning.run_id,
  feedback_text: '"'.repeat(8000), reflection_json: JSON.stringify(quotedReflection) });
assert.notEqual(quotedFeedback.ok, false, JSON.stringify(quotedFeedback));
assert.equal(quotedFeedback.candidates.length, 3);

for (const exported of ["confirm_run", "confirm_feedback", "publish", "withdraw"]) {
  assert.ok(!(exported in memories), "日常工具不得自填人工确认或发布");
}
console.log(JSON.stringify({ ok: true, empty_store_first_run_candidates: true, required_reflection: true,
  invalid_source_tags_rejected: true, candidate_isolation: true, idempotent_commit_and_feedback: true,
  corrections_preserve_original_run: true, two_phase_feedback_uses_new_complete_record: true,
  no_insight_no_fake_rule: true, feedback_confirm_evaluate_publish: true,
  success_confirm_evaluate_publish_without_badcase: true, new_task_recall_and_frozen_snapshot: true,
  windows_large_and_quoted_json_file_transport: true, semantic_accuracy_tested: false,
  output_directory: temporary }));
