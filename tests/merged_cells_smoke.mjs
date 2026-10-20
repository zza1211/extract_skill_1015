// 合并单元格重复标签的真实工具链回归；仅合成输入，不调用模型。
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as documents from "../.opencode/tools/term_document.ts";
import * as results from "../.opencode/tools/term_result.ts";
import * as learning from "../.opencode/tools/term_learning.ts";
import { closeSyntheticAudit } from "./audit_fixture.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const directory = await mkdtemp(path.join(os.tmpdir(), "term-merged-cells-tools-"));
process.env.TERM_EXTRACT_MEMORY_DIR = path.join(directory, "private-memory");
const context = { directory: root, sessionID: "ses_merged_protocol", messageID: "msg_merged_protocol", agent: "term-extractor" };
const invoke = async (tool, args) => JSON.parse(await tool.execute(args, context));
const save = async (file, value) => writeFile(file, JSON.stringify(value, null, 2) + "\n", "utf8");
const json = async file => JSON.parse(await readFile(file, "utf8"));
const shared = "<C5>支持Wi-Fi 6。<C6>PoE供电：" + "接口与供电🌐。".repeat(200);
const source = [
  { text: "<C90>Access Points，采购数量2台。" },
  { table: {
    rows: [
      ["<C3>接口要求", shared, shared],
      ["<C8>设备安装说明", "<C10>AP支持双频并发。", "<C13>AP应提供配套供电附件。"],
      ["<C8>设备安装说明", "<C21>防火墙，采购数量1台。", "<C22>吞吐量至少5Gbps。"],
    ],
    merges: [
      { top_left: { row: 0, col: 1 }, row_span: 1, col_span: 2, value: shared },
      { top_left: { row: 1, col: 0 }, row_span: 2, col_span: 1, value: "<C8>设备安装说明" },
    ],
  } },
];
const clauses = [
  { id: 0, 条款标题: "Access Points", 条款分类: "normal", 相关条款: [], 产品分类: "WLAN AP", 定位标识: ["<C90>", "<C13>"], 产品数量: "2台" },
  { id: 1, 条款标题: "防火墙", 条款分类: "normal", 相关条款: [], 产品分类: "安全产品", 定位标识: ["<C21>", "<C22>"], 产品数量: "1台" },
];
const evidence = { clauses: clauses.map(clause => ({ id: clause.id,
  quantity_source_tags: [clause.id === 0 ? "<C90>" : "<C21>"], related_source_tags: [], notes: "合成原文明示采购量；合并副本只记录坐标。" })), unresolved: [] };
const reflection = { insights: [], no_experience_reason: "合成运输测试不生成经验。" };
const input = path.join(directory, "input.json"), labels = path.join(directory, "labels.json");
await save(input, source);
await save(labels, clauses);
const original = await readFile(input);
const prepared = await invoke(documents.prepare, { input_path: input, out_dir: path.join(directory, "work") });
const index = await json(prepared.index_path);
assert.equal(index.schema_version, 2);
assert.deepEqual(index.source_document, source);
assert.equal(index.merged_cell_alias_policy, "exact_merged_cell_v1");
assert.equal(index.merged_cell_aliases.length, 2);
assert.equal(index.units.length, 9);
assert.equal(prepared.tag_count, 9);
assert.equal(prepared.merged_cell_alias_count, 2);
assert.equal(new Set(index.units.map(unit => unit.tag)).size, 9);
assert.deepEqual(index.tables[0].cell_unit_ids[0][1], index.tables[0].cell_unit_ids[0][2]);
assert.deepEqual(index.tables[0].cell_unit_ids[1][0], index.tables[0].cell_unit_ids[2][0]);
const study = await invoke(learning.begin, { input_path: input, labels_path: labels, family_id: "merged-synthetic-family", index_path: prepared.index_path });
assert.equal(study.state, "awaiting_blind_extraction", JSON.stringify(study));
const validated = await invoke(results.validate, { index_path: prepared.index_path, result_json: JSON.stringify(clauses), evidence_json: JSON.stringify(evidence) });
assert.equal(validated.ok, true, JSON.stringify(validated));
const commitArgs = { index_path: prepared.index_path, result_path: validated.result_path, evidence_path: validated.evidence_path,
  reflection_json: JSON.stringify(reflection), model_id: "synthetic-protocol-no-model" };
const unread = await invoke(results.commit, commitArgs);
assert.equal(unread.committed, false, "合并副本不能免除原文实际读取要求");
// 纵向副本出现在下一设备行，也不能把单标签区间延长到该设备。
const single = await invoke(documents.read, { index_path: prepared.index_path, start_tag: "<C8>", end_tag: "<C8>" });
assert.ok(single.text.includes("设备安装说明"));
assert.ok(!single.text.includes("防火墙"));
let cursor = "0", pages = 0;
const chunks = new Map();
for (;;) {
  const page = await invoke(documents.read, { index_path: prepared.index_path, cursor, max_bytes: 2048 });
  assert.ok(page.byte_size <= 2048);
  const lines = (await readFile(path.join(path.dirname(prepared.index_path), "read_receipts.jsonl"), "utf8")).trim().split("\n");
  const receipt = JSON.parse(lines.at(-1));
  for (const part of receipt.parts) {
    const fragment = Array.from(page.text).slice(part.content_start, part.content_end).join("");
    const previous = chunks.get(part.unit_id) || { end: 0, text: "" };
    assert.equal(part.part_start, previous.end);
    chunks.set(part.unit_id, { end: part.part_end, text: previous.text + fragment });
  }
  pages += 1;
  if (!page.has_more) break;
  assert.notEqual(page.next_cursor, cursor);
  cursor = page.next_cursor;
  assert.ok(pages < 100);
}
assert.ok(pages > 1);
for (const unit of index.units) assert.equal(chunks.get(unit.unit_id)?.text, unit.source_text);
await invoke(results.checkpoint, { index_path: prepared.index_path, candidates_json: JSON.stringify(clauses) });
const search = await invoke(documents.search, { index_path: prepared.index_path, query: "设备安装说明" });
assert.equal(search.match_count_total, 1, "合并副本不增加搜索命中数量");
await closeSyntheticAudit({ index_path: prepared.index_path, result_path: validated.result_path, evidence_path: validated.evidence_path }, context);
const committed = await invoke(results.commit, commitArgs);
assert.equal(committed.committed, true, JSON.stringify(committed));
assert.equal(committed.learning.status, "no_experience");
assert.deepEqual(await json(committed.result_path), clauses);
const compared = await invoke(learning.compare, { study_id: study.study_id, run_id: committed.learning.run_id });
assert.equal(compared.error, undefined, JSON.stringify(compared));
assert.equal(compared.comparison.status, "passed", JSON.stringify(compared));
assert.deepEqual(await readFile(input), original, "注册、读取和学习不得改动原始标签或源文件");
console.log(JSON.stringify({ ok: true, merged_alias_registration: true, original_tags_preserved: true,
  read_pages: pages, logical_coverage_and_commit: true, labels_compare: true, vertical_range_not_extended: true,
  output_directory: directory, semantic_accuracy_tested: false }));
