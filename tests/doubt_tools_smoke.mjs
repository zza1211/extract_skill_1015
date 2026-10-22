// 检查疑点引擎工具链；不调用模型，只使用临时输入和合成主 agent 判断。
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as documents from "../.opencode/tools/term_document.ts";
import * as doubts from "../.opencode/tools/term_doubt.ts";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const temporary = await mkdtemp(path.join(os.tmpdir(), "term-doubt-tools-"));
process.env.TERM_EXTRACT_MEMORY_DIR = path.join(temporary, "private-memory");
const context = { directory: root, sessionID: "ses_doubt_smoke", messageID: "msg_doubt_smoke", agent: "term-extractor" };
const invoke = async (tool, args) => JSON.parse(await tool.execute(args, context));
const input = path.join(temporary, "input.json");
await writeFile(input, JSON.stringify([
  { text: "<C1>Core Switch technical requirements" },
  { text: "<C2>24 GE ports and fixed configuration" },
  { text: "<C3>Wireless Access Point requirements" },
  { text: "<C4>Wi-Fi 6 and dual band" },
]), "utf8");
const prepared = await invoke(documents.prepare, { input_path: input, out_dir: path.join(temporary, "work") });
const scan = await invoke(doubts.scan, { index_path: prepared.index_path });
assert.ok(scan.signal_count > 0, JSON.stringify(scan));
assert.ok(scan.signals.some(item => item.type === "coverage_gap"), JSON.stringify(scan));
const resultPath = path.join(path.dirname(prepared.index_path), "draft", "result.json");
await writeFile(resultPath, JSON.stringify([{
  id: 0, 条款标题: "Core Switch", 条款分类: "normal", 相关条款: [], 产品分类: "盒式交换机",
  定位标识: ["<C1>", "<C2>"], 产品数量: "未提及",
}], null, 2), "utf8");
const afterDraft = await invoke(doubts.scan, { index_path: prepared.index_path, result_path: resultPath });
const signal = afterDraft.signals.find(item => item.type === "coverage_gap");
assert.ok(signal, JSON.stringify(afterDraft));
const recorded = await invoke(doubts.record, {
  index_path: prepared.index_path,
  result_path: resultPath,
  signals_path: afterDraft.signals_path,
  doubts_json: JSON.stringify([{
    type: "coverage_gap", signal_ids: [signal.id], clause_ids: [], evidence_tags: ["<C3>"],
    hypothesis: "AP 可能是未抽取的独立设备。", triggers: ["未覆盖设备名称"], impact: "high",
    confidence: "C1", next_action: "读取 AP 邻域并判断是否为独立采购对象。", status: "open", reason: "",
  }]),
});
assert.equal(recorded.open_count, 1);
const inspected = await invoke(doubts.inspect, { index_path: prepared.index_path });
assert.equal(inspected.items.length, 1);
const resolved = await invoke(doubts.resolve, {
  index_path: prepared.index_path,
  updates_json: JSON.stringify([{ doubt_id: "d0001", status: "resolved", reason: "已读取原文并补充 AP 条款。", evidence_tags: ["<C3>"] }]),
});
assert.equal(resolved.open_count, 0);
const ledger = JSON.parse(await readFile(path.join(path.dirname(prepared.index_path), "draft", "doubt-ledger.json"), "utf8"));
assert.equal(ledger.doubts[0].status, "resolved");
console.log(JSON.stringify({ ok: true, model_called: false, signals: scan.signal_count, ledger_path: recorded.ledger_path, resolved: true, semantic_accuracy_tested: false, output_directory: temporary }));
