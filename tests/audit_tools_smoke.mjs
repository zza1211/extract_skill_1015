// 临时合成协议验证：真实工具适配和存储链，不调用模型，不使用私有标书。
import assert from "node:assert/strict";
import { mkdtemp, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import * as documents from "../.opencode/tools/term_document.ts";
import * as results from "../.opencode/tools/term_result.ts";
import * as audits from "../.opencode/tools/term_audit.ts";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const temporary = await mkdtemp(path.join(os.tmpdir(), "term-audit-tools-"));
process.env.TERM_EXTRACT_MEMORY_DIR = path.join(temporary, "private-memory");
let sequence = 0;
const main = () => ({ directory: root, sessionID: "ses_synthetic_audit_main", messageID: `msg_main_${++sequence}`, agent: "term-extractor" });
const worker = () => ({ directory: root, sessionID: "ses_synthetic_auditor", messageID: `msg_auditor_${++sequence}`, agent: "term-auditor" });
const anotherWorker = () => ({ directory: root, sessionID: "ses_synthetic_other_auditor", messageID: `msg_other_${++sequence}`, agent: "term-auditor" });
const invoke = async (tool, args, context) => JSON.parse(await tool.execute(args, context));
const json = async file => JSON.parse(await readFile(file, "utf8"));
const save = async (file, value) => writeFile(file, JSON.stringify(value, null, 2) + "\n", "utf8");

const inputPath = path.join(temporary, "input.json");
await save(inputPath, [
  { text: "<C1>Access Switch requirements" },
  { text: "<C2>24 GE ports; fixed configuration." },
  { text: "<C3>PoE power budget shall be at least 370 W." + " Each interface shall remain available during power transition.".repeat(100) },
  { text: "<C4>Procurement: Access Switch, quantity 2 units." },
  { text: "<C5>Wireless Access Point requirements" },
  { text: "<C6>Wi-Fi 6, dual band, four spatial streams." },
  { text: "<C7>Procurement: Wireless Access Point, quantity 3 units." },
  { text: "<C8>Common environmental specification: network equipment shall operate at 0 to 45 degrees Celsius." },
]);
const prepared = await invoke(documents.prepare, { input_path: inputPath, out_dir: path.join(temporary, "work") }, main());
const receiptsPath = path.join(path.dirname(prepared.index_path), "read_receipts.jsonl");
let cursor = "0";
do {
  const page = await invoke(documents.read, { index_path: prepared.index_path, cursor }, main());
  cursor = page.has_more ? page.next_cursor : null;
} while (cursor !== null);

const initial = [
  { id: 0, 条款标题: "Access Switch", 条款分类: "normal", 相关条款: [], 产品分类: "盒式交换机", 定位标识: ["<C1>", "<C2>"], 产品数量: "2" },
  { id: 1, 条款标题: "Common environmental specification", 条款分类: "general", 相关条款: [0], 产品分类: "未提及", 定位标识: ["<C8>", "<C8>"], 产品数量: "未提及" },
];
const evidence = { clauses: [
  { id: 0, quantity_source_tags: ["<C4>"], related_source_tags: [], notes: "Synthetic draft intentionally omits PoE power and the second equipment object." },
  { id: 1, quantity_source_tags: [], related_source_tags: ["<C8>"], notes: "Synthetic common operating temperature requirement." },
], unresolved: [] };
const draft = await invoke(results.validate, { index_path: prepared.index_path, result_json: JSON.stringify(initial), evidence_json: JSON.stringify(evidence) }, main());
assert.equal(draft.ok, true, JSON.stringify(draft));
const commitArgs = { index_path: prepared.index_path, result_path: draft.result_path, evidence_path: draft.evidence_path,
  reflection_json: JSON.stringify({ insights: [], no_experience_reason: "合成协议测试，不归纳业务经验。" }), model_id: "synthetic-audit-protocol-test" };
const unaudited = await invoke(results.commit, commitArgs, main());
assert.equal(unaudited.committed, false, "真实主 agent 上下文不能跳过独立审计直接提交");
assert.match(JSON.stringify(unaudited), /审计|audit/i);
await assert.rejects(results.commit.execute(commitArgs, { ...main(), messageID: undefined }), /执行上下文|身份/);

const ticket = await invoke(audits.prepare, { index_path: prepared.index_path, result_path: draft.result_path, evidence_path: draft.evidence_path }, main());
assert.equal(typeof ticket.audit_id, "string", JSON.stringify(ticket));
const auditId = ticket.audit_id;
const workerReceiptsPath = path.join(process.env.TERM_EXTRACT_MEMORY_DIR, "agent-audits", auditId, "source", "read_receipts.jsonl");
await assert.rejects(audits.open.execute({ audit_id: auditId }, main()), /term-auditor/);
await assert.rejects(audits.prepare.execute({ index_path: prepared.index_path, result_path: draft.result_path, evidence_path: draft.evidence_path }, worker()), /term-extractor/);
const opened = await invoke(audits.open, { audit_id: auditId }, worker());
assert.equal(opened.audit_id, auditId);
await assert.rejects(audits.open.execute({ audit_id: auditId }, anotherWorker()), /会话|认领|绑定/);
const workerReceiptsAtOpen = await readFile(workerReceiptsPath, "utf8");
const claimedStatus = await invoke(audits.status, { audit_id: auditId }, main());
assert.equal(claimedStatus.worker_session_id, "ses_synthetic_auditor");
assert.equal(claimedStatus.continue_task_id, claimedStatus.worker_session_id, "可续接task标识必须是同一独立子会话");
assert.equal(claimedStatus.read_progress.complete, false);
assert.equal(claimedStatus.report_available, false);
assert.equal(await readFile(workerReceiptsPath, "utf8"), workerReceiptsAtOpen, "status不能产生原文读取回执");

const inventory = [
  { title: "Access Switch", object_kind: "equipment", evidence_tags: ["<C1>", "<C2>", "<C3>"] },
  { title: "Wireless Access Point", object_kind: "equipment", evidence_tags: ["<C5>", "<C6>"] },
];
await assert.rejects(audits.inventory.execute({ audit_id: auditId, objects_json: JSON.stringify(inventory) }, worker()), /全文|读取|覆盖/);
await assert.rejects(audits.draft.execute({ audit_id: auditId }, worker()), /清单|inventory|独立|全文/);
const mainReceiptsBefore = await readFile(receiptsPath, "utf8");
cursor = "0";
let independentPages = 0;
do {
  const page = await invoke(audits.read, { audit_id: auditId, cursor }, worker());
  assert.ok(Buffer.byteLength(JSON.stringify(page), "utf8") <= 24576);
  for (const field of ["parts", "tags", "unit_ids", "complete_tags"]) assert.ok(!(field in page), `不得向模型交付内部 ${field}`);
  assert.equal(typeof page.text, "string");
  independentPages += 1;
  cursor = page.has_more ? page.next_cursor : null;
} while (cursor !== null);
assert.equal(await readFile(receiptsPath, "utf8"), mainReceiptsBefore, "独立读取不能改写主 agent 的覆盖回执");
const independentReceipts = (await readFile(workerReceiptsPath, "utf8")).split("\n").filter(Boolean).map(JSON.parse);
assert.equal(independentReceipts.length, independentPages, "每次独立原文交付都应写入独立回执");
assert.ok(independentReceipts.every(item => item.execution_context.agent === "term-auditor" && item.execution_context.session_id === "ses_synthetic_auditor" && item.parts.length > 0));
assert.ok((await readFile(receiptsPath, "utf8")).split("\n").filter(Boolean).map(JSON.parse).every(item => item.execution_context.agent === "term-extractor"));
await assert.rejects(audits.read.execute({ audit_id: auditId, max_bytes: 2000 }, worker()), /max_bytes/);
await assert.rejects(audits.read.execute({ audit_id: auditId, start_tag: "<C1>" }, worker()), /同时/);
await assert.rejects(audits.inventory.execute({ audit_id: auditId, objects_json: JSON.stringify([{ ...inventory[0], object_kind: "integrated_system" }]) }, worker()), /系统|system_basis|依据/);
const inventorySaved = await invoke(audits.inventory, { audit_id: auditId, objects_json: JSON.stringify(inventory) }, worker());
assert.deepEqual(inventorySaved.object_ids, ["o0001", "o0002"]);
await assert.rejects(audits.finish.execute({ audit_id: auditId, findings_json: "[]" }, worker()), /草稿|分页|draft|读取|coverage|覆盖/);
const sourceSearch = await invoke(audits.search, { audit_id: auditId, query: "PoE" }, worker());
assert.ok(sourceSearch.text.includes("370 W"), "search 的运行时默认参数必须生效");
assert.equal(await readFile(receiptsPath, "utf8"), mainReceiptsBefore, "独立搜索不能记成主 agent 全文读取");

cursor = "0";
let draftPages = 0;
do {
  const page = await invoke(audits.draft, { audit_id: auditId, cursor, limit: 1 }, worker());
  draftPages += 1;
  cursor = page.has_more ? page.next_cursor : null;
} while (cursor !== null);
const findings = [
  { type: "boundary_truncated", clause_ids: [0], evidence_tags: ["<C3>"], finding: "交换机范围在接口要求后结束，遗漏影响供电配置的 PoE 功率。", suggested_action: "核对并保留 C3 的供电预算要求。", certainty: "confirmed" },
  { type: "missing_object", clause_ids: [], evidence_tags: ["<C5>", "<C6>"], finding: "原文单列 Wireless Access Point，当前草稿缺少该采购对象。", suggested_action: "核对并抽取该 AP 条款及独立采购数量。", certainty: "confirmed" },
];
const coverage = [
  { object_id: "o0001", status: "covered", clause_ids: [0], finding_ids: ["f0001"], reason: "交换机已有独立条款，另报告供电参数截断。" },
  { object_id: "o0002", status: "missing", clause_ids: [], finding_ids: ["f0002"], reason: "AP为独立设备采购对象，草稿未包含对应条款。" },
];
const finishArgs = { audit_id: auditId, findings_json: JSON.stringify(findings), summary: "独立识别两种设备，发现一个范围截断及一个漏项。" };
await assert.rejects(audits.finish.execute(finishArgs, worker()), /coverage|覆盖|清点/);
await assert.rejects(audits.finish.execute({ ...finishArgs, coverage_json: JSON.stringify(coverage.slice(0, 1)) }, worker()), /完整|每个|覆盖|清点/);
await assert.rejects(audits.finish.execute({ ...finishArgs, coverage_json: JSON.stringify([coverage[0], { ...coverage[1], status: "covered", clause_ids: [1], finding_ids: [] }]) }, worker()), /general|独立|设备|条款/);
await assert.rejects(audits.finish.execute({ ...finishArgs, coverage_json: JSON.stringify([coverage[0], { ...coverage[1], finding_ids: [] }]) }, worker()), /missing|漏|发现|问题|引用/);
await assert.rejects(audits.finish.execute({ ...finishArgs, coverage_json: JSON.stringify([coverage[0], { ...coverage[1], status: "uncertain", finding_ids: ["f0001"] }]) }, worker()), /依据|证据|交集|清点|对象/);
await invoke(audits.finish, { ...finishArgs, coverage_json: JSON.stringify(coverage) }, worker());

const reportStatus = await invoke(audits.status, { audit_id: auditId }, main());
assert.equal(reportStatus.report_available, true);
assert.equal(reportStatus.read_progress.complete, true);
assert.equal(reportStatus.draft_progress.complete, true);
assert.equal(reportStatus.findings, undefined, "status不交付报告正文");
assert.equal(reportStatus.items, undefined);
await assert.rejects(audits.status.execute({ audit_id: auditId }, worker()), /term-extractor/);

cursor = "0";
const inspectedCoverage = [];
do {
  const page = await invoke(audits.inspect, { audit_id: auditId, section: "coverage", cursor, limit: 1 }, main());
  inspectedCoverage.push(...page.items);
  cursor = page.has_more ? page.next_cursor : null;
} while (cursor !== null);
assert.equal(inspectedCoverage.length, 2);
assert.deepEqual(inspectedCoverage.map(item => item.object_id ?? item.object?.object_id ?? item.coverage?.object_id), ["o0001", "o0002"]);
const earlyDecisions = coverage.map((item, i) => ({ finding_id: `f000${i + 1}`, decision: "dismissed", reason: "合成测试不能只读状态或覆盖就算读过报告。", evidence_tags: inventory[i].evidence_tags }));
await assert.rejects(audits.resolve.execute({ audit_id: auditId, result_path: draft.result_path, evidence_path: draft.evidence_path, decisions_json: JSON.stringify(earlyDecisions) }, main()), /报告|完整读取|处置/);

cursor = "0";
const inspectedFindings = [];
do {
  const inspected = await invoke(audits.inspect, { audit_id: auditId, cursor, limit: 1 }, main());
  const items = inspected.findings ?? inspected.items ?? inspected.report?.findings;
  assert.ok(Array.isArray(items), JSON.stringify(inspected));
  inspectedFindings.push(...items);
  cursor = inspected.has_more ? inspected.next_cursor : null;
} while (cursor !== null);
assert.equal(inspectedFindings.length, 2);
assert.deepEqual(inspectedFindings.map(f => f.finding_id), ["f0001", "f0002"]);

const corrected = [
  { ...initial[0], 定位标识: ["<C1>", "<C3>"] },
  initial[1],
  { id: 2, 条款标题: "Wireless Access Point", 条款分类: "normal", 相关条款: [], 产品分类: "WLAN AP", 定位标识: ["<C5>", "<C6>"], 产品数量: "3" },
];
const correctedEvidence = { clauses: [
  { id: 0, quantity_source_tags: ["<C4>"], related_source_tags: [], notes: "复核供电要求，采购量来自独立清单。" },
  evidence.clauses[1],
  { id: 2, quantity_source_tags: ["<C7>"], related_source_tags: [], notes: "独立 AP 采购项，数量来自对应清单。" },
], unresolved: [] };
const revised = await invoke(results.validate, { index_path: prepared.index_path, result_json: JSON.stringify(corrected), evidence_json: JSON.stringify(correctedEvidence) }, main());
assert.equal(revised.ok, true, JSON.stringify(revised));
const decisions = [
  { finding_id: "f0001", decision: "fixed", reason: "主 agent 核对 PoE 预算，区间扩至 C3。", evidence_tags: ["<C3>"] },
  { finding_id: "f0002", decision: "fixed", reason: "主 agent 核对 AP 独立规格并补充采购对象。", evidence_tags: ["<C5>", "<C6>"] },
];
const resolveArgs = { audit_id: auditId, result_path: revised.result_path, evidence_path: revised.evidence_path, decisions_json: JSON.stringify(decisions) };
await assert.rejects(audits.resolve.execute(resolveArgs, main()), /回读|重新读取|复读|交付/);
await invoke(documents.read, { index_path: prepared.index_path, start_tag: "<C3>", end_tag: "<C7>" }, main());
await invoke(audits.resolve, resolveArgs, main());

const finalArgs = { ...commitArgs, result_path: revised.result_path, evidence_path: revised.evidence_path };
const resolvedBytes = await readFile(revised.result_path);
await save(revised.result_path, corrected.map(item => item.id === 2 ? { ...item, 产品数量: "4" } : item));
const stale = await invoke(results.commit, finalArgs, main());
assert.equal(stale.committed, false, "审计处理后再次改变结果必须使审计依据失效");
assert.match(JSON.stringify(stale), /审计|audit|版本|变化|hash/i);
await assert.rejects(audits.resolve.execute(resolveArgs, main()), /新|变化|改变|核验|绑定/);
await writeFile(revised.result_path, resolvedBytes);
const committed = await invoke(results.commit, finalArgs, main());
assert.equal(committed.committed, true, JSON.stringify(committed));
assert.deepEqual(await json(committed.result_path), corrected);
assert.equal((await json(committed.run_path)).agent_audit.audit_id, auditId, "最终运行归档必须绑定实际通过的独立核验");

const anotherTicket = await invoke(audits.prepare, { index_path: prepared.index_path, result_path: revised.result_path, evidence_path: revised.evidence_path }, main());
await assert.rejects(audits.open.execute({ audit_id: anotherTicket.audit_id }, worker()), /一张|新会话|一个.*会话/);
await assert.rejects(audits.abandon.execute({ audit_id: auditId, reason: "不能取消已经清除的审计记录。" }, main()), /cleared|关闭|完成|已处理/);

const recoveryWorker = () => ({ ...worker(), sessionID: "ses_synthetic_recovery_auditor" });
const freshWorker = () => ({ ...worker(), sessionID: "ses_synthetic_fresh_auditor" });
const audit2 = anotherTicket.audit_id;
await invoke(audits.open, { audit_id: audit2 }, recoveryWorker());
const interrupted = await invoke(audits.read, { audit_id: audit2, max_bytes: 2048 }, recoveryWorker());
assert.equal(interrupted.has_more, true, "超长单元格在中断前应只交付一部分");
const recoveryReceiptsPath = path.join(process.env.TERM_EXTRACT_MEMORY_DIR, "agent-audits", audit2, "source", "read_receipts.jsonl");
const recoveryReceipts = await readFile(recoveryReceiptsPath, "utf8");
const interruptedStatus = await invoke(audits.status, { audit_id: audit2 }, main());
assert.equal(interruptedStatus.read_progress.receipt_count, 1);
assert.equal(interruptedStatus.read_progress.complete, false);
assert.equal(interruptedStatus.read_progress.next_cursor, interrupted.next_cursor);
assert.equal(interruptedStatus.continue_task_id, "ses_synthetic_recovery_auditor");
assert.equal(await readFile(recoveryReceiptsPath, "utf8"), recoveryReceipts, "恢复状态查询不能重复记原文已读");
await assert.rejects(audits.read.execute({ audit_id: audit2, cursor: interrupted.next_cursor }, anotherWorker()), /领取|会话|票/);
let recoveryCursor = interruptedStatus.read_progress.next_cursor;
while (recoveryCursor !== null) {
  const page = await invoke(audits.read, { audit_id: audit2, cursor: recoveryCursor, max_bytes: 2048 }, recoveryWorker());
  recoveryCursor = page.has_more ? page.next_cursor : null;
}
const resumedStatus = await invoke(audits.status, { audit_id: audit2 }, main());
assert.equal(resumedStatus.read_progress.complete, true, "原独立会话可沿持久游标继续而无需重读开头");
assert.equal(resumedStatus.draft_progress.delivered_count, 0);
await assert.rejects(audits.abandon.execute({ audit_id: audit2, reason: " " }, main()), /reason/);
const abandonArgs = { audit_id: audit2, reason: "合成中断恢复验证：显式终止旧票，新票不继承独立阅读或设备清点。" };
await invoke(audits.abandon, abandonArgs, main());
await invoke(audits.abandon, abandonArgs, main());
const abandonedStatus = await invoke(audits.status, { audit_id: audit2 }, main());
assert.match(abandonedStatus.phase, /abandon|终止|取消/);
await assert.rejects(audits.read.execute({ audit_id: audit2 }, recoveryWorker()), /abandon|废弃|终止|取消/);
await assert.rejects(audits.resolve.execute({ ...resolveArgs, audit_id: audit2 }, main()), /abandon|废弃|终止|取消/);
const abandonedCommit = await invoke(results.commit, finalArgs, main());
assert.equal(abandonedCommit.committed, false, "终止票据后不能依赖该票提交");

const replacement = await invoke(audits.prepare, { index_path: prepared.index_path, result_path: revised.result_path, evidence_path: revised.evidence_path }, main());
const replacementStatus = await invoke(audits.status, { audit_id: replacement.audit_id }, main());
assert.equal(replacementStatus.read_progress.receipt_count, 0);
assert.equal(replacementStatus.read_progress.covered_units, 0);
assert.equal(replacementStatus.draft_progress.delivered_count, 0);
assert.equal(replacementStatus.worker_session_id, null);
await assert.rejects(audits.open.execute({ audit_id: replacement.audit_id }, recoveryWorker()), /一张|新会话|一个.*会话/);
await invoke(audits.open, { audit_id: replacement.audit_id }, freshWorker());
await assert.rejects(audits.inventory.execute({ audit_id: replacement.audit_id, objects_json: JSON.stringify(inventory) }, freshWorker()), /全文|读取|覆盖/);
await invoke(audits.abandon, { audit_id: replacement.audit_id, reason: "合成测试结束，未完成的替代票明确终止。" }, main());

console.log(JSON.stringify({ ok: true, synthetic_only: true, model_called: false, independent_pages: independentPages, draft_pages: draftPages,
  audit_required_before_commit: true, inventory_before_draft: true, draft_before_findings: true, session_bound: true,
  main_receipts_not_polluted: true, separate_worker_receipts_checked: true, post_audit_reread_required: true,
  stale_resolution_rejected: true, one_ticket_per_worker: true, run_bound_to_audit: true,
  device_inventory_closed: true, general_alone_not_coverage: true, incomplete_coverage_rejected: true,
  status_does_not_deliver_report: true, same_session_cursor_recovery: true, cross_session_recovery_rejected: true,
  abandonment_idempotent: true, replacement_does_not_inherit_progress: true, output_directory: temporary }));
