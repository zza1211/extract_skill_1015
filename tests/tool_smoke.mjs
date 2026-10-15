// 检查真实 OpenCode 工具适配链，不调用模型；临时输入只是协议测试。
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as documents from "../.opencode/tools/term_document.ts";
import * as results from "../.opencode/tools/term_result.ts";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const temporary = await mkdtemp(path.join(os.tmpdir(), "term-extract-tools-"));
const inputPath = path.join(temporary, "input.json");
await writeFile(inputPath, JSON.stringify([
  { text: "<C100> Core Switch\n<C101>固定配置" },
  { table: { rows: [["<C2>项目", "<C3>要求"], ["<C4>接口", "<C5>24 GE ports；" + "完整技术描述🌐。".repeat(1500) + "\n<C7>另一个采购对象"]], merges: [] } },
  { text: "<C6>采购清单：Core Switch，2台。" },
]), "utf8");
const context = { directory: root };
// 直接 execute 时不走 Zod.parse；省略可选参数必须有运行时默认值。
const prepared = JSON.parse(await documents.prepare.execute({ input_path: inputPath, out_dir: path.join(temporary, "work") }, context));
assert.equal(prepared.demo, false);
const receiptPath = path.join(path.dirname(prepared.index_path), "read_receipts.jsonl");
const receipts = async () => (await readFile(receiptPath, "utf8")).split("\n").filter(Boolean).map(JSON.parse);
const lastReceipt = async () => (await receipts()).at(-1);
function modelPage(raw, budget) {
  assert.equal(typeof raw, "string");
  assert.ok(Buffer.byteLength(raw, "utf8") <= budget, "真正交付给模型的完整 JSON 必须符合字节预算");
  const view = JSON.parse(raw);
  for (const machineField of ["parts", "tags", "complete_tags", "unit_ids"]) {
    assert.ok(!(machineField in view), `模型视图不应暴露 ${machineField}`);
  }
  assert.ok(view.byte_size <= budget);
  assert.ok(Number.isInteger(view.unit_count));
  return view;
}
const draft = [{ id: 0, 条款标题: "Core Switch", 条款分类: "normal", 相关条款: [], 产品分类: "盒式交换机", 定位标识: ["<C100>", "<C5>"], 产品数量: "2台" }];
const evidence = { clauses: [{ id: 0, quantity_source_tags: ["<C6>"], related_source_tags: [], notes: "仅用于检查数量来源可在参数区段之外。" }], unresolved: [] };
const checked = JSON.parse(await results.validate.execute({ index_path: prepared.index_path, result_json: JSON.stringify(draft), evidence_json: JSON.stringify(evidence), allow_demo: false }, context));
assert.equal(checked.ok, true);
const commitArgs = { index_path: prepared.index_path, result_path: checked.result_path, evidence_path: checked.evidence_path, model_id: "协议测试，未调用模型", allow_demo: false };
const premature = JSON.parse(await results.commit.execute(commitArgs, context));
assert.equal(premature.committed, false, "未读原文时必须阻止提交");

const defaultRead = modelPage(await documents.read.execute({ index_path: prepared.index_path }, context), 24576);
assert.equal(defaultRead.cursor, "0", "省略 cursor 时必须传真实默认值，不能传 undefined");
assert.ok(defaultRead.text.includes("Core Switch"));
const defaultReceipt = await lastReceipt();
assert.equal(defaultReceipt.cursor, "0");
assert.ok(defaultReceipt.parts.length > 0, "内部 parts 仍须保存在程序回执");
const receiptCountBeforeSearch = (await receipts()).length;
const checkpointError = "先用term_result_checkpoint保存候选和具体疑点，再做搜索核验";
const blockedSearch = JSON.parse(await documents.search.execute({ index_path: prepared.index_path, query: "Core Switch" }, context));
assert.equal(blockedSearch.ok, false, "未保存候选时模型搜索工具应给出明确阶段提示");
assert.equal(blockedSearch.code, "CANDIDATE_CHECKPOINT_REQUIRED");
assert.equal(blockedSearch.error, checkpointError);
assert.equal((await receipts()).length, receiptCountBeforeSearch);
const checkpointPath = path.join(path.dirname(prepared.index_path), "draft", "candidates.json");
const indexHash = createHash("sha256").update(await readFile(prepared.index_path)).digest("hex");
const checkpoint = { status: "candidate_checkpoint", index_sha256: indexHash, candidates: [{ 条款标题: "Core Switch", doubts: ["采购数量来源及区段边界待核验"] }] };
await writeFile(checkpointPath, JSON.stringify({ ...checkpoint, index_sha256: "不对应本次输入" }), "utf8");
const staleSearch = JSON.parse(await documents.search.execute({ index_path: prepared.index_path, query: "Core Switch" }, context));
assert.equal(staleSearch.error, checkpointError, "旧输入的候选文件不能解锁本次搜索");
await writeFile(checkpointPath, JSON.stringify({ ...checkpoint, status: "final" }), "utf8");
assert.equal(JSON.parse(await documents.search.execute({ index_path: prepared.index_path, query: "Core Switch" }, context)).error, checkpointError);
await writeFile(checkpointPath, JSON.stringify({ ...checkpoint, candidates: {} }), "utf8");
assert.equal(JSON.parse(await documents.search.execute({ index_path: prepared.index_path, query: "Core Switch" }, context)).error, checkpointError);
// 不要求最终七字段或语义完备；先保存候选和具体疑点即可继续核验。
await writeFile(checkpointPath, JSON.stringify(checkpoint), "utf8");
const defaultSearch = modelPage(await documents.search.execute({ index_path: prepared.index_path, query: "Core Switch" }, context), 24576);
assert.ok(defaultSearch.matched_tags.includes("<C6>"), "省略 limit/max_bytes/cursor 的 search 应正常返回数量来源");
assert.equal((await receipts()).length, receiptCountBeforeSearch, "搜索不能被记成全文读取回执");
const stillPartial = JSON.parse(await results.commit.execute(commitArgs, context));
assert.equal(stillPartial.committed, false, "只交付超长单元的一部分仍不能提交");

const inner = modelPage(await documents.read.execute({ index_path: prepared.index_path, start_tag: "<C7>", end_tag: "<C7>", max_bytes: 8192 }, context), 8192);
const innerReceipt = await lastReceipt();
assert.deepEqual(innerReceipt.tags, ["<C7>"], "单元格内部的标签必须可以独立定位");
assert.ok(inner.text.includes("另一个采购对象"));
assert.ok(!inner.text.includes("完整技术描述"), "内部标签范围不能强制包括整格");
assert.equal(innerReceipt.parts[0].row, 1);
assert.equal(innerReceipt.parts[0].col, 1);
assert.ok(inner.table_context.length > 0, "模型视图必须保留必要表格上下文");
const index = JSON.parse(await readFile(prepared.index_path, "utf8"));
const deliveredFragments = new Map();

let cursor = "0";
let pages = 0;
let partialSeen = false;
while (true) {
  // 更紧的分页预算也必须保持原文和完整交付后的提交能力。
  const page = modelPage(await documents.read.execute({ index_path: prepared.index_path, cursor, max_bytes: 2048 }, context), 2048);
  const receipt = await lastReceipt();
  pages += 1;
  partialSeen ||= page.partial_tags.length > 0;
  assert.equal(page.unit_count, receipt.parts.length);
  const characters = Array.from(page.text);
  for (const part of receipt.parts) {
    const fragment = characters.slice(part.content_start, part.content_end).join("");
    const expected = index.units.find(unit => unit.unit_id === part.unit_id);
    assert.ok(expected, "读取回执必须对应真实 unit");
    assert.equal(part.row, expected.row);
    assert.equal(part.col, expected.col);
    assert.equal(part.table_id, expected.table_id);
    assert.equal(fragment, Array.from(expected.source_text).slice(part.part_start, part.part_end).join(""), "实际模型视图应交付原文精确切片，含 Unicode 非 BMP 字符");
    if (!deliveredFragments.has(part.unit_id)) deliveredFragments.set(part.unit_id, []);
    deliveredFragments.get(part.unit_id).push({ start: part.part_start, end: part.part_end, text: fragment });
  }
  if (!page.has_more) break;
  assert.notEqual(page.next_cursor, cursor);
  cursor = page.next_cursor;
  assert.ok(pages < 500, "分页应当持续前进");
}
assert.ok(partialSeen, "超长单元格应当分片");
for (const unit of index.units) {
  if (!unit.source_text) continue;
  const parts = deliveredFragments.get(unit.unit_id) || [];
  let end = 0;
  for (const part of parts) {
    assert.equal(part.start, end, "同一个 unit 的紧预算分页不能漏读或重复字符");
    end = part.end;
  }
  assert.equal(end, Array.from(unit.source_text).length);
  assert.equal(parts.map(part => part.text).join(""), unit.source_text);
}
let searchCursor = "0";
let searchPages = 0;
const searchTags = new Set();
while (true) {
  const search = modelPage(await documents.search.execute({ index_path: prepared.index_path, query: "采购清单", cursor: searchCursor, max_bytes: 2048 }, context), 2048);
  searchPages += 1;
  for (const tag of search.matched_tags) searchTags.add(tag);
  if (!search.has_more) break;
  assert.notEqual(search.next_cursor, searchCursor);
  searchCursor = search.next_cursor;
  assert.ok(searchPages < 100, "搜索分页应当持续前进");
}
assert.ok(searchTags.has("<C6>"));
const committed = JSON.parse(await results.commit.execute(commitArgs, context));
assert.equal(committed.committed, true);
const saved = JSON.parse(await readFile(committed.result_path, "utf8"));
assert.deepEqual(saved, draft);
console.log(JSON.stringify({ ok: true, checkpoint_stage_checked: true, incomplete_candidate_allowed: true, stale_checkpoint_rejected: true, runtime_defaults_checked: true, compact_budget_checked: true, machine_parts_only_in_receipts: true, multi_tag_cell_range_checked: true, read_pages: pages, search_pages: searchPages, partial_unit_checked: partialSeen, unread_commit_rejected: true, output_directory: temporary, semantic_accuracy_tested: false }));
