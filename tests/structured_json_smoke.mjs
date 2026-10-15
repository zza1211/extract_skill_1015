// 对真实解析文件检查注册与原文交付，不调用模型，也不声称抽取语义正确。
import assert from "node:assert/strict";
import { mkdtemp, readdir, readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as documents from "../.opencode/tools/term_document.ts";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const sourceDirectory = path.resolve(root, process.argv[2] || "../structured_json");
const temporary = await mkdtemp(path.join(os.tmpdir(), "term-extract-structured-"));
const context = { directory: root };
const filenames = (await readdir(sourceDirectory)).filter(name => name.endsWith(".json")).sort();
assert.ok(filenames.length > 0, "没有可检查的 JSON 文件");
const summaries = [];

function sliceCharacters(value, start, end) {
  return Array.from(value).slice(start, end).join("");
}

for (const [number, name] of filenames.entries()) {
  const sourcePath = path.join(sourceDirectory, name);
  const raw = await readFile(sourcePath);
  const source = JSON.parse(raw.toString("utf8").replace(/^\uFEFF/, ""));
  const prepared = JSON.parse(await documents.prepare.execute({ input_path: sourcePath, out_dir: path.join(temporary, String(number)), demo_tags: false }, context));
  assert.equal(prepared.demo, false);
  const index = JSON.parse(await readFile(prepared.index_path, "utf8"));
  assert.equal(index.schema_version, 2);
  assert.deepEqual(index.source_document, source);
  const chunks = new Map();
  let cursor = "0";
  let pages = 0;
  while (true) {
    const receiptPath = path.join(path.dirname(prepared.index_path), "read_receipts.jsonl");
    const page = JSON.parse(await documents.read.execute({ index_path: prepared.index_path, cursor, max_bytes: 24576 }, context));
    assert.ok(page.byte_size <= 24576);
    assert.ok(!("parts" in page), "完整机器元数据不应重复交给模型");
    const receiptLines = (await readFile(receiptPath, "utf8")).trim().split("\n");
    const receipt = JSON.parse(receiptLines.at(-1));
    for (const part of receipt.parts) {
      const previous = chunks.get(part.unit_id) || { end: 0, text: "" };
      assert.equal(part.part_start, previous.end);
      const fragment = sliceCharacters(page.text, part.content_start, part.content_end);
      assert.equal(Array.from(fragment).length, part.part_end - part.part_start);
      chunks.set(part.unit_id, { end: part.part_end, text: previous.text + fragment });
    }
    pages += 1;
    if (!page.has_more) break;
    assert.notEqual(page.next_cursor, cursor);
    cursor = page.next_cursor;
    assert.ok(pages < 2000, "全文分页必须前进并结束");
  }
  const strings = new Map();
  for (const unit of index.units) {
    assert.equal(chunks.get(unit.unit_id)?.text, unit.source_text);
    assert.equal(chunks.get(unit.unit_id)?.end, unit.source_length);
    const location = JSON.stringify([unit.block_index, unit.row, unit.col]);
    const previous = strings.get(location) || { end: 0, text: "" };
    assert.equal(unit.source_start, previous.end);
    strings.set(location, { end: unit.source_end, text: previous.text + unit.source_text });
  }
  for (const [location, rebuilt] of strings) {
    const [block, row, col] = JSON.parse(location);
    const original = row === null ? source[block].text : source[block].table.rows[row][col];
    assert.equal(rebuilt.text, original, "定位切分和工具分页后必须逐字符恢复原字符串");
    assert.equal(rebuilt.end, Array.from(original).length);
  }
  const cell = index.tables.flatMap(table => table.cell_unit_ids.flat()).find(ids => ids.length > 1);
  assert.ok(cell, "这批真实输入应包含多标签单元格");
  const selected = index.units.find(unit => unit.unit_id === cell[1]);
  const inner = JSON.parse(await documents.read.execute({ index_path: prepared.index_path, cursor: "0", start_tag: selected.tag, end_tag: selected.tag, max_bytes: 24576 }, context));
  const innerLines = (await readFile(path.join(path.dirname(prepared.index_path), "read_receipts.jsonl"), "utf8")).trim().split("\n");
  const innerReceipt = JSON.parse(innerLines.at(-1));
  assert.deepEqual(innerReceipt.tags, [selected.tag]);
  assert.equal(innerReceipt.parts[0].row, selected.row);
  assert.equal(innerReceipt.parts[0].col, selected.col);
  assert.ok(raw.equals(await readFile(sourcePath)), "源文件不能改变");
  summaries.push({ file: name, registered: true, units: prepared.unit_count, tags: prepared.tag_count, pages, original_reconstructed: true, cell_internal_range_checked: true });
}
console.log(JSON.stringify({ ok: true, files: summaries, output_directory: temporary, semantic_accuracy_tested: false }));
