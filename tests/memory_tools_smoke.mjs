// 经验工具的协议回归：全部输入、人工身份和发布仅为临时合成夹具，不调用模型。
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { access, mkdtemp, readFile, rename, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import * as documents from "../.opencode/tools/term_document.ts";
import * as results from "../.opencode/tools/term_result.ts";
import * as memories from "../.opencode/tools/term_memory.ts";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const context = { directory: root };
const temporary = await mkdtemp(path.join(os.tmpdir(), "term-extract-memory-tools-"));
const previousStore = process.env.TERM_EXTRACT_MEMORY_DIR;
process.env.TERM_EXTRACT_MEMORY_DIR = path.join(temporary, "private-memory");
const execute = promisify(execFile);
const scripts = path.join(root, ".opencode", "skills", "term-extract", "scripts");

const json = async file => JSON.parse(await readFile(file, "utf8"));
const save = async (file, value) => writeFile(file, JSON.stringify(value, null, 2) + "\n", "utf8");
const invoke = async (tool, args) => JSON.parse(await tool.execute(args, context));
const exists = async file => access(file).then(() => true, () => false);

async function storeCLI(action, args, successful = true) {
  let stdout, code;
  try {
    ({ stdout } = await execute(process.env.TERM_EXTRACT_PYTHON || "python",
      ["-B", "-X", "utf8", path.join(scripts, "memory_store.py"), "--project-root", root, action, ...args],
      { cwd: root, windowsHide: true, encoding: "utf8", env: { ...process.env, PYTHONUTF8: "1" } }));
    code = 0;
  } catch (error) {
    stdout = error.stdout;
    code = error.code;
  }
  assert.equal(typeof stdout, "string", "人工 CLI 必须返回可诊断的 JSON");
  const value = JSON.parse(stdout);
  if (successful) {
    assert.equal(code, 0, value.error);
    assert.notEqual(value.ok, false);
  } else {
    assert.equal(code, 2, "无效发布或交付必须被拒绝");
    assert.equal(value.ok, false);
  }
  return value;
}

function source(name) {
  return [{ text: `<C9>${name}: target device\n<C2>Required technical parameter; procurement quantity 2.\n<C7>Another independently purchased device.` }];
}

function clauses(complete = true) {
  return [{ id: 0, 条款标题: "target device", 条款分类: "normal", 相关条款: [], 产品分类: "参考",
    定位标识: ["<C9>", complete ? "<C2>" : "<C9>"], 产品数量: "2" }];
}

async function prepare(name) {
  const input = path.join(temporary, name + ".json");
  await save(input, source(name));
  const prepared = await invoke(documents.prepare, { input_path: input, out_dir: path.join(temporary, name) });
  assert.equal(typeof prepared.memory_snapshot.snapshot_path, "string");
  assert.match(prepared.memory_snapshot.snapshot_sha256, /^[a-f0-9]{64}$/);
  assert.ok(!("memories" in prepared.memory_snapshot), "prepare 只返回快照元数据，不能塞入整个经验库");
  return { ...prepared, input_path: input };
}

async function readAll(prepared) {
  let cursor = "0";
  for (let pageCount = 0; pageCount < 100; pageCount++) {
    const page = await invoke(documents.read, { index_path: prepared.index_path, cursor });
    if (!page.has_more) return;
    assert.notEqual(page.next_cursor, cursor);
    cursor = page.next_cursor;
  }
  assert.fail("合成输入的全文读取未结束");
}

async function draft(prepared) {
  return invoke(results.validate, { index_path: prepared.index_path, result_json: JSON.stringify(clauses()),
    evidence_json: JSON.stringify({ clauses: [{ id: 0, quantity_source_tags: ["<C2>"], related_source_tags: [],
      notes: "仅验证工具协议；合成输入明确写有采购数量。" }], unresolved: [] }) });
}

async function commit(prepared, checked) {
  return invoke(results.commit, { index_path: prepared.index_path, result_path: checked.result_path,
    evidence_path: checked.evidence_path, model_id: "synthetic-memory-protocol-test",
    reflection_json: JSON.stringify({ insights: [], no_experience_reason: "合成协议测试，不归纳新的业务经验。" }) });
}

async function memoryReceipts(prepared) {
  const file = path.join(path.dirname(prepared.index_path), "memory_receipts.jsonl");
  if (!await exists(file)) return [];
  return (await readFile(file, "utf8")).split("\n").filter(Boolean).map(JSON.parse);
}

async function recall(prepared, options = {}) {
  const raw = await memories.recall.execute({ index_path: prepared.index_path,
    query: "continuity parameter", ...options }, context);
  const response = JSON.parse(raw);
  assert.ok(Buffer.byteLength(raw, "utf8") <= (options.max_bytes ?? 6144));
  assert.ok(response.memories.length <= (options.limit ?? 3));
  assert.equal(response.snapshot_sha256, prepared.memory_snapshot.snapshot_sha256);
  assert.ok(!("adopted" in response), "交付记录不能被称为已采用");
  const receipts = await memoryReceipts(prepared);
  assert.equal(receipts.at(-1).snapshot_sha256, response.snapshot_sha256);
  assert.deepEqual(receipts.at(-1).delivered, response.memories.map(item => ({
    memory_id: item.memory_id, revision: item.revision, content_sha256: item.content_sha256,
  })));
  return response;
}

async function rejectedSnapshot(prepared, checked) {
  const before = (await memoryReceipts(prepared)).length;
  await assert.rejects(memories.recall.execute({ index_path: prepared.index_path, query: "continuity parameter" }, context));
  assert.equal((await memoryReceipts(prepared)).length, before, "失败召回不能记录成功交付");
  const rejected = await commit(prepared, checked);
  assert.equal(rejected.committed, false, "坏快照不得正式提交");
  assert.equal(await exists(path.join(path.dirname(prepared.index_path), "final", "result.json")), false);
}

try {
  // 检查配置与工具导出边界；它们依赖 OpenCode 权限执行，不冒称 OS 身份认证测试。
  const extractor = await readFile(path.join(root, ".opencode", "agents", "term-extractor.md"), "utf8");
  const curator = await readFile(path.join(root, ".opencode", "agents", "term-memory-curator.md"), "utf8");
  for (const settings of [extractor, curator]) {
    assert.match(settings, /term_memory_\*:\s*deny/);
    for (const permission of ["bash", "edit", "task"]) assert.match(settings, new RegExp(`${permission}:\\s*deny`));
  }
  assert.match(extractor, /term_memory_recall:\s*allow/);
  for (const tool of ["correction", "show", "propose", "evaluate", "publish", "withdraw", "import_correction"]) {
    assert.doesNotMatch(extractor, new RegExp(`term_memory_${tool}:\\s*allow`));
  }
  for (const tool of ["publish", "withdraw", "import_correction"]) assert.ok(!(tool in memories));

  const empty = await prepare("fix-family");
  assert.equal(empty.memory_snapshot.memory_count, 0);
  assert.deepEqual((await recall(empty)).memories, [], "空库必须可正常召回并继续抽取");
  const emptyDraft = await draft(empty);
  assert.equal((await commit(empty, emptyDraft)).committed, false, "经验快照不能替代全文读取");
  await readAll(empty);
  const emptyCommit = await commit(empty, emptyDraft);
  assert.equal(emptyCommit.committed, true);
  const emptyRun = await json(emptyCommit.run_path);
  assert.equal(emptyRun.memory.snapshot_sha256, empty.memory_snapshot.snapshot_sha256);
  assert.deepEqual(emptyRun.memory.delivered, []);

  const regression = await prepare("regression-family");
  const counterexample = await prepare("counterexample-family");
  const baseline = path.join(temporary, "fix-before.json"), candidateOutput = path.join(temporary, "fix-after.json");
  await save(baseline, clauses(false));
  await save(candidateOutput, clauses());
  const correction = await storeCLI("import-correction", ["--input", empty.input_path, "--before", baseline,
    "--after", candidateOutput, "--reason", "Synthetic fixture: retain the required target parameter.",
    "--confirmed-by", "synthetic-human-fixture", "--family-id", "fix-family"]);
  const payload = { title: "Retain continuous target parameters", issue_type: "boundary",
    triggers: ["continuity", "parameter"], applies_when: "Continuous requirements belong to the same procurement object.",
    rule: "Retain target technical parameters while excluding specifications of adjacent independent devices.",
    exclusions: "Do not combine independently purchased adjacent objects or infer identity from names alone.",
    correction_ids: [correction.correction_id] };
  const proposed = await invoke(memories.propose, { payload_json: JSON.stringify(payload) });
  assert.equal(proposed.state, "candidate");
  const revision2 = await invoke(memories.propose, { memory_id: proposed.memory_id,
    payload_json: JSON.stringify({ ...payload, rule: "UNPUBLISHED revision must never be recalled." }) });
  assert.equal(revision2.revision, proposed.revision + 1);
  const shown = await invoke(memories.show, { memory_id: proposed.memory_id, revision: proposed.revision });
  assert.equal(shown.content_sha256, proposed.content_sha256, "新修订不能覆盖旧修订");
  const candidatesOnly = await prepare("candidate-only");
  assert.equal(candidatesOnly.memory_snapshot.memory_count, 0);
  assert.deepEqual((await recall(candidatesOnly)).memories, [], "候选及新候选修订都不能召回");
  await storeCLI("publish", ["--memory-id", proposed.memory_id, "--revision", String(proposed.revision),
    "--evaluation-id", "eval_nonexistent", "--reviewer", "synthetic-human-fixture", "--note", "negative gate fixture"], false);

  const cases = [];
  for (const [prepared, role, family] of [[empty, "fix", "fix-family"], [regression, "regression", "regression-family"],
    [counterexample, "counterexample", "counterexample-family"]]) {
    const before = role === "fix" ? baseline : path.join(temporary, role + "-before.json");
    const after = role === "fix" ? candidateOutput : path.join(temporary, role + "-after.json");
    if (role !== "fix") { await save(before, clauses()); await save(after, clauses()); }
    cases.push({ case_id: role + "-fixture", family_id: family, role,
      split: role === "fix" ? "development" : "regression", confirmed_by: "synthetic-human-fixture",
      index_path: prepared.index_path, baseline_result_path: before, candidate_result_path: after,
      ...(role === "counterexample" ? { scope_note: "Independent neighboring equipment must not enter the target span." } : {}),
      assertions: [{ object_key: "target", anchor_tag: "<C9>", required_tags: ["<C9>", "<C2>"],
        forbidden_tags: ["<C7>"], quantity: "2", clause_category: "normal" }] });
  }
  const manifest = path.join(temporary, "evaluation.json");
  await save(manifest, { schema_version: 1, cases });
  const evaluation = await invoke(memories.evaluate, { memory_id: proposed.memory_id, revision: proposed.revision, manifest_path: manifest });
  assert.equal(evaluation.status, "passed", JSON.stringify(evaluation));
  assert.ok(evaluation.evaluation_id);
  assert.ok(evaluation.summary.fix_improvements >= 1);
  assert.equal(evaluation.summary.regressions, 0);
  const published = await storeCLI("publish", ["--memory-id", proposed.memory_id, "--revision", String(proposed.revision),
    "--evaluation-id", evaluation.evaluation_id, "--reviewer", "synthetic-human-fixture", "--note", "Synthetic protocol fixture only; no model accuracy claim."]);
  assert.equal(published.state, "published");
  assert.deepEqual((await recall(empty)).memories, [], "旧任务必须保持开始时的空快照");

  const active = await prepare("active-runtime");
  assert.equal(active.memory_snapshot.memory_count, 1);
  const recalled = await recall(active, { issue_type: "boundary" });
  assert.equal(recalled.memories.length, 1);
  assert.equal(recalled.memories[0].revision, proposed.revision);
  assert.equal(recalled.memories[0].content_sha256, proposed.content_sha256);
  const existingMemoryReceipts = await readFile(path.join(path.dirname(active.index_path), "memory_receipts.jsonl"));
  const existingReadReceipts = await readFile(path.join(path.dirname(active.index_path), "read_receipts.jsonl"));
  const existingIndex = await readFile(active.index_path);
  await assert.rejects(documents.prepare.execute({ input_path: active.input_path, out_dir: path.dirname(active.index_path) }, context),
    "已冻结任务不能通过重复 prepare 重置读取回执或改用另一规则版本");
  assert.ok(existingMemoryReceipts.equals(await readFile(path.join(path.dirname(active.index_path), "memory_receipts.jsonl"))));
  assert.ok(existingReadReceipts.equals(await readFile(path.join(path.dirname(active.index_path), "read_receipts.jsonl"))));
  assert.ok(existingIndex.equals(await readFile(active.index_path)));
  for (const key of ["input_path", "before_path", "after_path", "before_json", "after_json"]) {
    assert.ok(!JSON.stringify(recalled).includes(`"${key}"`), "抽取侧不应收到纠错原文或私有文件引用");
  }
  await recall(active, { max_bytes: 1024 });
  const receiptCount = (await memoryReceipts(active)).length;
  await assert.rejects(memories.recall.execute({ index_path: active.index_path, query: "parameter", max_bytes: 1023 }, context));
  assert.equal((await memoryReceipts(active)).length, receiptCount);
  await readAll(active);
  const activeDraft = await draft(active);
  const activeCommit = await commit(active, activeDraft);
  assert.equal(activeCommit.committed, true);
  const activeRun = await json(activeCommit.run_path);
  assert.equal(activeRun.memory.snapshot_sha256, active.memory_snapshot.snapshot_sha256);
  assert.deepEqual(activeRun.memory.delivered, [{ memory_id: proposed.memory_id, revision: proposed.revision,
    content_sha256: proposed.content_sha256 }]);

  const broken = await prepare("broken-snapshot");
  await readAll(broken);
  const brokenDraft = await draft(broken);
  const snapshotPath = broken.memory_snapshot.snapshot_path;
  const originalSnapshot = await readFile(snapshotPath);
  await writeFile(snapshotPath, await readFile(active.memory_snapshot.snapshot_path));
  await rejectedSnapshot(broken, brokenDraft);
  await writeFile(snapshotPath, originalSnapshot);
  const edited = await json(snapshotPath);
  edited.memories[0].payload.rule += " forged content";
  await save(snapshotPath, edited);
  await rejectedSnapshot(broken, brokenDraft);
  await writeFile(snapshotPath, originalSnapshot);
  const backup = snapshotPath + ".backup";
  await rename(snapshotPath, backup);
  await rejectedSnapshot(broken, brokenDraft);
  await rename(backup, snapshotPath);

  await storeCLI("withdraw", ["--memory-id", proposed.memory_id, "--reviewer", "synthetic-human-fixture", "--note", "Synthetic withdrawal fixture."]);
  const afterWithdrawal = await prepare("after-withdrawal");
  assert.equal(afterWithdrawal.memory_snapshot.memory_count, 0);
  assert.deepEqual((await recall(afterWithdrawal)).memories, []);
  assert.equal((await recall(active)).memories[0].revision, proposed.revision, "撤回只影响新任务，旧快照可以回放");
  assert.equal((await commit(active, activeDraft)).committed, true);

  console.log(JSON.stringify({ ok: true, empty_store_checked: true, candidate_isolation_checked: true,
    immutable_revisions_checked: true, offline_evaluation_and_manual_cli_publish_checked: true,
    frozen_snapshot_and_withdrawal_checked: true, actual_recall_budget_checked: true,
    snapshot_tamper_missing_and_index_binding_rejected: true, commit_memory_trace_checked: true,
    unread_commit_rejected: true, agent_permission_contract_checked: true,
    output_directory: temporary, semantic_accuracy_tested: false }));
} finally {
  if (previousStore === undefined) delete process.env.TERM_EXTRACT_MEMORY_DIR;
  else process.env.TERM_EXTRACT_MEMORY_DIR = previousStore;
}
