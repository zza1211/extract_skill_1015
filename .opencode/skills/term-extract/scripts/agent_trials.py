"""OpenCode 自主实验的原子存储工具；本模块绝不调用模型。

主 agent 显式选择案例、用原生 task 创建独立 term-trial 会话，再调用这些工具
冻结结果、比较和注册实验。export 仅只读导出既有会话以核验真实工具调用。
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import subprocess
import sys
import uuid

import document_io
import evolution_store as evo
import memory_store as store
from learning_compare import compare_results, apply_judgments

MemoryError = store.MemoryError
VERSION = "opencode-agent-trials-v1"
_TABLES = ("agent_trial_plans", "agent_trial_claims", "agent_trial_results", "agent_trial_judgments", "agent_trial_evaluations")


@contextmanager
def _db(root, *, write=False):
    with evo._db(Path(root), write=write) as connection:
        if connection is not None and write:
            for table in _TABLES:
                connection.execute(f"CREATE TABLE IF NOT EXISTS {table} (record_id TEXT PRIMARY KEY, record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL, created_at TEXT NOT NULL)")
                for op in ("UPDATE", "DELETE"):
                    connection.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{op.lower()} BEFORE {op} ON {table} BEGIN SELECT RAISE(ABORT, 'immutable record'); END")
        yield connection


def _context(value, agent):
    if not isinstance(value, dict) or set(value) != {"session_id", "message_id", "agent"}:
        raise MemoryError("需要工具注入的真实 OpenCode 执行上下文。")
    for key in ("session_id", "message_id", "agent"):
        store._string(value.get(key), "execution_context." + key, 200)
    if value["agent"] != agent:
        raise MemoryError(f"此操作仅允许 {agent} agent。")
    return dict(value)


def _find(connection, table, key, value):
    return evo._find(connection, table, key, value)


def _plan(connection, experiment_id):
    plan = _find(connection, "agent_trial_plans", "experiment_id", experiment_id)
    if plan is None:
        raise MemoryError("自主实验不存在。")
    return plan


def _trial(connection, trial_id):
    for plan in evo._rows(connection, "agent_trial_plans"):
        trial = next((item for item in plan["trials"] if item["trial_id"] == trial_id), None)
        if trial is not None:
            return plan, trial
    raise MemoryError("试验票不存在。")


def _controller(plan, context):
    current = _context(context, "term-extractor")
    if current["session_id"] != plan["controller"]["session_id"]:
        raise MemoryError("只能由创建实验的主会话继续，不能跨会话读取实验标签。")
    return current


def _write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = store._canonical(value) + b"\n"
    if path.exists() and path.read_bytes() != content:
        raise MemoryError("不可变实验文件已存在，禁止覆盖不同内容。")
    if not path.exists():
        path.write_bytes(content)
    return str(path.resolve())


def _frozen(root, plan):
    if store.release_digest(Path(root)) != plan["base_release_sha256"] or evo.policy_digest(root) != plan["policy_sha256"]:
        raise MemoryError("正式经验或固定口径已改变，请由 agent 创建新实验。")
    candidate = store.load_candidate(Path(root), plan["memory_id"], plan["revision"])
    if candidate["content_sha256"] != plan["candidate"]["content_sha256"]:
        raise MemoryError("候选经验内容已变化。")
    for case in plan["cases"]:
        current = evo.get_case(root, case["case_id"])
        if current["source_sha256"] != case["source_sha256"] or current["labels_sha256"] != case["labels_sha256"]:
            raise MemoryError("实验案例依据已变化。")
    for trial in plan["trials"]:
        if store._file_hash(Path(trial["index_path"])) != trial["index_sha256"] or store._file_hash(Path(trial["input_path"])) != trial["source_sha256"]:
            raise MemoryError("试验原文或索引已变化。")


def prepare_experiment(root, memory_id, revision, cases, caller_context, memory_ids=None):
    """仅冻结主 agent 明确指定的案例和经验；不自动挑选或执行。"""
    root = Path(root)
    caller = _context(caller_context, "term-extractor")
    if not isinstance(cases, list) or not cases:
        raise MemoryError("cases 必须是主 agent 选择的非空案例角色数组。")
    with _db(root) as connection:
        candidate = store._candidate(connection, memory_id, revision)
        if candidate["state"] in {"published", "rejected", "merged", "needs_review"}:
            raise MemoryError("当前候选状态不允许创建自动晋级实验。")
        entries = store._published(connection)
        release = store._release(entries)
        source_ids = list(dict.fromkeys([*candidate["payload"].get("source_ids", []), *candidate["payload"].get("correction_ids", [])]))
        source_hashes, source_families, _ = evo._validate_sources(connection, candidate, source_ids)
    if memory_ids is not None:
        if not isinstance(memory_ids, list) or any(not isinstance(value, str) for value in memory_ids) or len(set(memory_ids)) != len(memory_ids):
            raise MemoryError("memory_ids 必须是无重复的正式经验 ID 数组。")
        if set(memory_ids) - {entry["memory_id"] for entry in entries}:
            raise MemoryError("所选基线经验不在当前正式版本中。")
        entries = [entry for entry in entries if entry["memory_id"] in memory_ids]
    if len(entries) > 4:
        raise MemoryError("基线最多四条经验；请由主 agent 用 memory_ids 明确选择相关正式经验。")
    # 纯抽取观察来源尚无 family；可信开发目标的 family 也必须排除，且不能依赖输入顺序。
    target_families = {evo.get_case(root, item.get("case_id"))["family_id"] for item in cases if isinstance(item, dict) and item.get("role") == "development"}
    selected, seen, independent = [], set(), set()
    for item in cases:
        if not isinstance(item, dict) or item.get("role") not in {"development", "regression", "counterexample"}:
            raise MemoryError("案例角色只能为 development/regression/counterexample。")
        case = evo.get_case(root, item.get("case_id"))
        if case["split"] == "holdout" or case["policy_sha256"] != evo.policy_digest(root):
            raise MemoryError("不能使用保留集或口径过期的标签。")
        if case["source_sha256"] in seen:
            raise MemoryError("同一原文不能重复充当多个案例角色。")
        seen.add(case["source_sha256"])
        if item["role"] == "development":
            if case["split"] != "development" or case["source_sha256"] not in source_hashes:
                raise MemoryError("开发目标必须与候选归纳来源绑定。")
        else:
            if case["source_sha256"] in source_hashes or case["family_id"] in source_families | target_families or case["family_id"] in independent:
                raise MemoryError("回归、反例及归纳来源必须具有独立原文和 family。")
            independent.add(case["family_id"])
        scope_note = str(item.get("scope_note", ""))[:2000]
        if item["role"] == "counterexample" and not scope_note.strip():
            raise MemoryError("反例必须说明检验的适用或排除边界。")
        selected.append({key: case[key] for key in ("case_id", "source_sha256", "labels_sha256", "family_id")} | {"role": item["role"], "scope_note": scope_note})
    if {item["role"] for item in selected} != {"development", "regression", "counterexample"} or len(independent) < 2:
        raise MemoryError("至少需开发、独立回归、独立反例三个角色，回归与反例来自两个独立 family。")
    if not source_hashes <= {item["source_sha256"] for item in selected if item["role"] == "development"}:
        raise MemoryError("实验必须覆盖候选的全部归纳来源，不能只选其中一篇开发目标。")
    identity = "agentexp_" + uuid.uuid4().hex
    directory = store.resolve_store_dir(root) / "agent-trials" / identity
    trials = []
    injected = {key: candidate[key] for key in ("memory_id", "revision", "content_sha256", "payload")}
    for case in selected:
        trusted = evo.get_case(root, case["case_id"])
        for phase in ("baseline", "candidate"):
            trial_id = "trial_" + uuid.uuid4().hex
            target = directory / trial_id
            target.mkdir(parents=True, exist_ok=True)
            source = target / "input.json"
            source.write_bytes(Path(trusted["input_archive_path"]).read_bytes())
            prepared = document_io.prepare(source, target / "document")
            index_path = Path(prepared["index_path"])
            memories = entries if phase == "baseline" else [entry for entry in entries if entry["memory_id"] != memory_id] + [injected]
            if len(store._canonical(memories)) > 22 * 1024:
                raise MemoryError("经验包超过工具交付预算；请由主 agent 减少基线经验。")
            snapshot = {"schema_version": 1, "created_at": store._now(), "index_sha256": store._file_hash(index_path), "release_sha256": store._release(memories), "memories": memories}
            snapshot["snapshot_sha256"] = store._hash(snapshot)
            _write(index_path.parent / store.SNAPSHOT_NAME, snapshot)
            (index_path.parent / "read_receipts.jsonl").write_text("", encoding="utf-8")
            trials.append({"trial_id": trial_id, "case_id": case["case_id"], "phase": phase, "index_path": str(index_path), "index_sha256": store._file_hash(index_path), "input_path": str(source.resolve()), "source_sha256": case["source_sha256"], "snapshot_sha256": snapshot["snapshot_sha256"], "memories": memories})
    references = {}
    for name in ("business-rules.md", "output-format.md", "input-format.md"):
        reference = root / ".opencode/skills/term-extract/references" / name
        if reference.is_file():
            references[name] = reference.read_text(encoding="utf-8")
    if len(store._canonical(references)) > 30 * 1024:
        raise MemoryError("试验参考规则超过交付预算，需先精简静态规范。")
    plan = {"experiment_id": identity, "memory_id": memory_id, "revision": revision, "candidate": injected, "base_release_sha256": release, "policy_sha256": evo.policy_digest(root), "controller": caller, "cases": selected, "trials": trials, "development_source_ids": source_ids, "references": references, "created_at": store._now()}
    _frozen(root, plan)
    with _db(root, write=True) as connection:
        evo._put(connection, "agent_trial_plans", identity, plan)
    return {"experiment_id": identity, "trials": [{"trial_id": trial["trial_id"], "phase": trial["phase"], "case_id": trial["case_id"]} for trial in trials], "base_release_sha256": release, "next_action": "对每个 trial_id 创建全新 term-trial task；prompt 只能为 trial_id=对应票号。不得在子任务中传递标签或已有答案。"}


def get_trial(root, trial_id, context, claim=False):
    caller = _context(context, "term-trial")
    with _db(root, write=claim) as connection:
        plan, trial = _trial(connection, trial_id)
        if caller["session_id"] == plan["controller"]["session_id"]:
            raise MemoryError("主会话不能充当盲抽子会话。")
        current = _find(connection, "agent_trial_claims", "trial_id", trial_id)
        for other in evo._rows(connection, "agent_trial_claims"):
            if other["execution_context"]["session_id"] == caller["session_id"] and other["trial_id"] != trial_id:
                raise MemoryError("同一子会话只能领取一张试验票；A/B 必须相互隔离。")
        if current and current["execution_context"]["session_id"] != caller["session_id"]:
            raise MemoryError("试验票已绑定其他子会话。")
        if not current:
            if not claim:
                raise MemoryError("请先领取试验票。")
            evo._put(connection, "agent_trial_claims", trial_id, {"trial_id": trial_id, "execution_context": caller, "created_at": store._now()})
        _frozen(root, plan)
        snapshot = store._snapshot(Path(trial["index_path"]))
        if snapshot["snapshot_sha256"] != trial["snapshot_sha256"]:
            raise MemoryError("试验经验快照已变化。")
        pack = {"schema_version": 1, **{key: snapshot[key] for key in ("snapshot_sha256", "release_sha256", "index_sha256")}, "memories": snapshot["memories"], "notice": store.NOTICE, "budget_omitted": 0}
        return {"trial_id": trial_id, "index_path": trial["index_path"], "out_dir": str(Path(trial["index_path"]).parent), "memory_pack": pack, "reference_pack": plan["references"], "trial_only": True, "next_action": "依据本次 reference_pack 连续读取全部原文；自主形成候选、核验和提交。试验不查看标签、不产生学习经验。"}


def record_trial(root, trial_id, committed, context):
    caller = _context(context, "term-trial")
    safe = get_trial(root, trial_id, caller)
    if not isinstance(committed, dict) or committed.get("committed") is not True:
        raise MemoryError("试验需要真实工具提交成功的结果。")
    directory = Path(safe["index_path"]).parent / "final"
    expected = {"result_path": directory / "result.json", "evidence_path": directory / "evidence.json", "run_path": directory / "run.json"}
    for key, path in expected.items():
        if Path(committed.get(key, "")).resolve() != path.resolve():
            raise MemoryError("试验提交路径不属于当前试验票。")
    run = store._read_json(expected["run_path"], 4 * 1024 * 1024)
    if run.get("status") != "submitted" or run.get("production_submittable") is not True or run.get("trial_only") is not True:
        raise MemoryError("试验必须是完整原文的正式格式提交且明确 trial_only，禁止回归结果进入学习来源。")
    execution = run.get("execution_context", {})
    if execution.get("session_id") != caller["session_id"] or execution.get("agent") != "term-trial":
        raise MemoryError("提交记录没有绑定当前 term-trial 会话。")
    if run.get("learning", {}).get("status") != "trial_skipped":
        raise MemoryError("试验运行禁止登记学习来源。")
    index = store._read_json(Path(safe["index_path"]), 48 * 1024 * 1024)
    if run.get("source_sha256") != index["source_sha256"]:
        raise MemoryError("试验提交的原文版本不一致。")
    if run.get("coverage", {}).get("complete") is not True or run["coverage"].get("index_sha256") != store._file_hash(Path(safe["index_path"])):
        raise MemoryError("试验原文读取覆盖不完整。")
    receipts = Path(safe["index_path"]).with_name("read_receipts.jsonl")
    from commit_result import coverage_report
    if coverage_report(Path(safe["index_path"]), receipts)["complete"] is not True:
        raise MemoryError("实际试验读取回执覆盖不完整。")
    lines = [store._parse(line) for line in receipts.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines or any(item.get("execution_context", {}).get("session_id") != caller["session_id"] or item.get("execution_context", {}).get("agent") != "term-trial" for item in lines):
        raise MemoryError("所有原文回执必须属于当前独立子会话。")
    memory = store.run_memory_record(Path(safe["index_path"]))
    identities = [{key: item[key] for key in ("memory_id", "revision", "content_sha256")} for item in safe["memory_pack"]["memories"]]
    if memory.get("snapshot_sha256") != safe["memory_pack"]["snapshot_sha256"] or sorted(memory["delivered"], key=store._canonical) != sorted(identities, key=store._canonical):
        raise MemoryError("试验没有实际收到被冻结的完整经验包。")
    for key in ("result", "evidence"):
        if store._file_hash(expected[key + "_path"]) != run.get(key + "_sha256"):
            raise MemoryError("试验结果或证据与正式提交记录不一致。")
    index_path = Path(safe["index_path"])
    files = [*expected.values(), receipts, index_path, Path(index["source_path"]), index_path.with_name(store.SNAPSHOT_NAME), index_path.with_name(store.RECEIPTS_NAME)]
    record = {"trial_id": trial_id, "execution_context": caller, "committed": committed, "files": [{"path": str(path.resolve()), "sha256": store._file_hash(path)} for path in files], "created_at": store._now()}
    with _db(root, write=True) as connection:
        previous = _find(connection, "agent_trial_results", "trial_id", trial_id)
        if previous:
            if previous["committed"] != committed or previous["files"] != record["files"]:
                raise MemoryError("试验已冻结，禁止修改或重复提交不同结果。")
            return {"recorded": True, "trial_id": trial_id, "replayed": True, "provenance_status": "awaiting_transcript_verification"}
        evo._put(connection, "agent_trial_results", trial_id, record)
    return {"recorded": True, "trial_id": trial_id, "provenance_status": "awaiting_transcript_verification", "notice": "主会话汇总时才核验已完成工具调用的真实会话导出；此处不宣称通过实验。"}


def delivery(root, trial_id, context):
    """由 wrapper 在模型视图成功序列化后调用，记录本票完整经验包。"""
    ticket = get_trial(root, trial_id, context)
    store.record_delivery(Path(ticket["index_path"]), ticket["memory_pack"])
    return {"recorded": True, "trial_id": trial_id}


def _results(connection, plan):
    records = []
    for trial in plan["trials"]:
        record = _find(connection, "agent_trial_results", "trial_id", trial["trial_id"])
        if record is None:
            raise MemoryError("必须等待所有 A/B 试验冻结，再查看标签和差异。")
        for artifact in record["files"]:
            if store._file_hash(Path(artifact["path"])) != artifact["sha256"]:
                raise MemoryError("冻结的试验结果或回执已变化。")
        records.append((trial, record))
    return records


def _inspect_all(root, experiment_id, caller_context):
    with _db(root) as connection:
        plan = _plan(connection, experiment_id)
        _controller(plan, caller_context)
        pairs = _results(connection, plan)
        stored = evo._rows(connection, "agent_trial_judgments")
    _frozen(root, plan)
    comparisons = []
    for trial, record in pairs:
        case = evo.get_case(root, trial["case_id"], include_labels=True)
        index = store._read_json(Path(case["index_path"]), 48 * 1024 * 1024)
        result = store._read_json(Path(record["committed"]["result_path"]), 2 * 1024 * 1024)
        comparison = compare_results(index, result, case["labels_json"])
        selected = [item for item in stored if item["experiment_id"] == experiment_id and item["trial_id"] == trial["trial_id"] and item["prediction_sha256"] == comparison["prediction_sha256"] and item["labels_sha256"] == comparison["labels_sha256"]]
        for saved in selected:
            comparison = apply_judgments(comparison, saved["judgments"])
        comparisons.append({"trial_id": trial["trial_id"], "case_id": trial["case_id"], "phase": trial["phase"], "comparison": comparison})
    return {"experiment_id": experiment_id, "comparisons": comparisons, "notice": "全部预测已冻结。仅局部 review 差异可由主 agent 复核；明确漏产品、数量错误等硬错误不能覆盖。"}


def inspect_experiment(root, experiment_id, caller_context, trial_id=None, difference_cursor="0", max_bytes=32768):
    """分页交付一个试验的必要差异，避免把完整对象与有效标签重复交给主 agent。"""
    full = _inspect_all(root, experiment_id, caller_context)
    if type(difference_cursor) is int and difference_cursor >= 0:
        difference_cursor = str(difference_cursor)
    if not isinstance(difference_cursor, str) or not difference_cursor.isdigit() or not 4096 <= max_bytes <= 49152:
        raise MemoryError("difference_cursor 必须为非负整数字符串，max_bytes 为4096到49152。")
    items = full["comparisons"]
    selected = next((item for item in items if item["trial_id"] == trial_id), None) if trial_id else items[0]
    if selected is None:
        raise MemoryError("trial_id 不属于本实验。")
    comparison = selected["comparison"]
    offset = int(difference_cursor)
    if offset > len(comparison["differences"]):
        raise MemoryError("difference_cursor 超出差异范围。")
    brief = {key: comparison[key] for key in ("schema_version", "status", "summary", "prediction_sha256", "labels_sha256")}
    brief["differences"] = []
    ordinal = items.index(selected)
    response = {"experiment_id": experiment_id, "trial_id": selected["trial_id"], "phase": selected["phase"], "case_id": selected["case_id"], "comparison": brief,
                "trials": [{"trial_id": item["trial_id"], "phase": item["phase"], "status": item["comparison"]["status"]} for item in items],
                "difference_cursor": difference_cursor, "next_cursor": None, "has_more": False, "remaining_differences": len(comparison["differences"]) - offset,
                "next_trial_id": items[ordinal + 1]["trial_id"] if ordinal + 1 < len(items) else None, "notice": full["notice"]}
    for difference in comparison["differences"][offset:]:
        brief["differences"].append(difference)
        remaining = len(comparison["differences"]) - offset - len(brief["differences"])
        response.update(next_cursor=str(offset + len(brief["differences"])) if remaining else None, has_more=bool(remaining), remaining_differences=remaining)
        if len(store._canonical(response)) > max_bytes:
            brief["differences"].pop()
            if not brief["differences"]:
                raise MemoryError("单个差异超过交付预算，请提高 max_bytes。")
            remaining = len(comparison["differences"]) - offset - len(brief["differences"])
            response.update(next_cursor=str(offset + len(brief["differences"])), has_more=True, remaining_differences=remaining)
            break
    if len(store._canonical(response)) > max_bytes:
        raise MemoryError("实验元信息超过交付预算，请提高 max_bytes。")
    from agent_learning import record_label_disclosure
    record_label_disclosure(root, caller_context, source_type="experiment", source_key=experiment_id)
    return response


def judge_experiment(root, experiment_id, judgments, caller_context):
    inspected = _inspect_all(root, experiment_id, caller_context)
    if not isinstance(judgments, list) or not judgments:
        raise MemoryError("需要按 trial_id 提供非空局部判断数组。")
    lookup = {item["trial_id"]: item for item in inspected["comparisons"]}
    saved, reviews = [], []
    for item in judgments:
        if not isinstance(item, dict) or item.get("trial_id") not in lookup or not isinstance(item.get("judgments"), list):
            raise MemoryError("局部判断必须绑定本实验的 trial_id。")
        source = lookup[item["trial_id"]]
        comparison = source["comparison"]
        apply_judgments(comparison, item["judgments"])
        body = {"experiment_id": experiment_id, "trial_id": item["trial_id"], "prediction_sha256": comparison["prediction_sha256"], "labels_sha256": comparison["labels_sha256"], "judgments": item["judgments"], "execution_context": dict(caller_context), "tool_input": {"experiment_id": experiment_id, "judgments": judgments}}
        record = {"judgment_id": "judgment_" + store._hash(body)[:32], **body, "created_at": store._now()}
        with _db(root, write=True) as connection:
            if not _find(connection, "agent_trial_judgments", "judgment_id", record["judgment_id"]):
                evo._put(connection, "agent_trial_judgments", record["judgment_id"], record)
        saved.append(record["judgment_id"])
        for judgment in item["judgments"]:
            if judgment.get("decision") != "uncertain":
                continue
            difference = next(d for d in comparison["differences"] if d["difference_id"] == judgment["difference_id"])
            review = evo.create_review(root, kind="semantic_difference", title="局部选型差异待确认", question=difference.get("question") or difference["message"], context={"difference": difference, "model_reason": judgment.get("reason", "")}, choices=["harmless", "error", "uncertain"], subject={"case_id": source["case_id"], "phase": source["phase"], "prediction_sha256": comparison["prediction_sha256"], "labels_sha256": comparison["labels_sha256"], "difference_id": judgment["difference_id"]}, scope="case")
            reviews.append(review["review_id"])
    return {"experiment_id": experiment_id, "saved_judgment_ids": saved, "review_ids": reviews, "notice": "这是主 agent 的局部语义判断，不是人工审核；汇总时核验对应真实工具调用。"}


def export_session(root, session_id):
    """只读 CLI 导出，不创建会话或发起模型请求。"""
    from learning_runner import _exe
    executable = _exe()
    if not executable:
        raise MemoryError("无法找到 OpenCode export；不能核验真实模型，实验保留但禁止自动晋级。")
    try:
        process = subprocess.run([executable, "export", session_id], cwd=str(root), capture_output=True, encoding="utf-8", timeout=45, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MemoryError("OpenCode 会话导出失败；禁止把未核验执行声明作为自动晋级依据。") from exc
    if process.returncode:
        raise MemoryError("OpenCode 会话尚未可导出或已不可用；请保留实验并稍后重试。")
    try:
        result = json.loads(process.stdout)
    except ValueError as exc:
        raise MemoryError("OpenCode 导出没有返回有效 JSON。") from exc
    if not isinstance(result, dict) or result.get("info", {}).get("id") != session_id or not isinstance(result.get("messages"), list):
        raise MemoryError("导出的真实会话 ID 或结构不匹配。")
    return result


def _tool_output(part):
    output = part.get("state", {}).get("output")
    if isinstance(output, str):
        try:
            return json.loads(output)
        except ValueError:
            return None
    return output


def _transcript(root, plan, trial, record):
    transcript = export_session(root, record["execution_context"]["session_id"])
    info = transcript["info"]
    if info.get("parentID") != plan["controller"]["session_id"]:
        raise MemoryError("试验必须来自本主会话新建的原生子任务，不能复用既有会话。")
    users = [m for m in transcript["messages"] if m.get("info", {}).get("role") == "user"]
    texts = [p.get("text", "") for message in users for p in message.get("parts", []) if p.get("type") == "text"]
    if len(users) != 1 or len(users[0].get("parts", [])) != 1 or texts != ["trial_id=" + trial["trial_id"]]:
        raise MemoryError("盲抽子任务只能接收试验票号，不能夹带标签、答案或追加提示。")
    models, variants, tools = set(), set(), []
    sid = info["id"]
    for message in transcript["messages"]:
        meta = message.get("info", {})
        if meta.get("sessionID") != sid:
            raise MemoryError("会话导出混入其他会话。")
        if meta.get("role") == "assistant":
            if meta.get("agent") != "term-trial" or not meta.get("providerID") or not meta.get("modelID"):
                raise MemoryError("试验没有可核验的 term-trial 模型身份。")
            models.add(meta["providerID"] + "/" + meta["modelID"])
            variants.add(meta.get("variant", ""))
        for part in message.get("parts", []):
            if part.get("type") != "tool":
                continue
            if part.get("tool") != "skill" and not str(part.get("tool", "")).startswith("term_trial_"):
                raise MemoryError("盲抽子会话调用了试验范围外工具，不能证明标签隔离。")
            if part.get("state", {}).get("status") == "completed":
                tools.append((meta, part))
    if len(models) != 1 or len(variants) != 1:
        raise MemoryError("同一试验必须具有单一已核验模型和变体。")
    read_messages = {meta["id"] for meta, part in tools if part["tool"] in {"term_trial_read", "term_trial_search"} and part.get("state", {}).get("input", {}).get("trial_id") == trial["trial_id"]}
    receipts_path = Path(trial["index_path"]).with_name("read_receipts.jsonl")
    for line in receipts_path.read_text(encoding="utf-8").splitlines():
        if line.strip() and store._parse(line).get("execution_context", {}).get("message_id") not in read_messages:
            raise MemoryError("原文读取回执未对应真实会话中已完成的本票读取工具。")
    opens = [part for _, part in tools if part["tool"] == "term_trial_open" and part.get("state", {}).get("input", {}).get("trial_id") == trial["trial_id"]]
    snapshot = store._snapshot(Path(trial["index_path"]))
    def correct_delivery(part):
        output = _tool_output(part) or {}
        pack = output.get("memory_pack", {})
        return (pack.get("memories") == trial["memories"] and
                all(pack.get(key) == snapshot[key] for key in ("snapshot_sha256", "index_sha256", "release_sha256")) and
                output.get("reference_pack") == plan["references"])
    if not opens or not any(correct_delivery(part) for part in opens):
        raise MemoryError("真实会话未收到冻结的完整试验经验包。")
    commits = [(meta, part) for meta, part in tools if part["tool"] == "term_trial_commit" and meta.get("id") == record["execution_context"]["message_id"]]
    matching = [part for _, part in commits if (_tool_output(part) or {}).get("committed") is True and all((_tool_output(part) or {}).get(key) == record["committed"][key] for key in ("result_path", "evidence_path", "run_path"))]
    if not matching:
        raise MemoryError("真实会话尚无已完成的对应 trial commit；等待子任务返回后再汇总。")
    directory = store.resolve_store_dir(root) / "agent-trials" / plan["experiment_id"] / trial["trial_id"]
    path = _write(directory / ("transcript-" + store._hash(transcript)[:24] + ".json"), transcript)
    return transcript, {"model_id": next(iter(models)), "variant": next(iter(variants)), "runner_version": VERSION}, path


def finalize(root, experiment_id, caller_context):
    with _db(root) as connection:
        plan = _plan(connection, experiment_id)
        _controller(plan, caller_context)
        pairs = _results(connection, plan)
        stored = evo._rows(connection, "agent_trial_judgments")
    _frozen(root, plan)
    cases = {item["case_id"]: {key: item[key] for key in ("case_id", "role", "scope_note")} for item in plan["cases"]}
    generation = None
    parent_transcript = None
    for trial, record in pairs:
        _, actual, transcript_path = _transcript(root, plan, trial, record)
        if generation is not None and generation != actual:
            raise MemoryError("全部 A/B 试验必须使用同一真实模型和变体。")
        generation = actual
        provenance = {"kind": "opencode_agent", "phase": trial["phase"], "blind": True, **actual, "session_id": record["execution_context"]["session_id"], "controller_session_id": plan["controller"]["session_id"], "trial_id": trial["trial_id"], "agent_experiment_id": experiment_id, "base_release_sha256": plan["base_release_sha256"], "candidate_ids": [] if trial["phase"] == "baseline" else [{key: plan["candidate"][key] for key in ("memory_id", "revision", "content_sha256")}], "transcript_path": transcript_path, "transcript_sha256": store._file_hash(Path(transcript_path)), "result_path": record["committed"]["result_path"], "result_sha256": store._file_hash(Path(record["committed"]["result_path"]))}
        artifact = {key: provenance[key] for key in ("result_path", "result_sha256")}
        artifact["provenance"] = provenance
        cases[trial["case_id"]][trial["phase"]] = artifact
        selected = [j for j in stored if j["experiment_id"] == experiment_id and j["trial_id"] == trial["trial_id"]]
        if selected:
            judgment = selected[-1]
            if parent_transcript is None:
                parent_transcript = export_session(root, plan["controller"]["session_id"])
            for previous in selected:
                _verified_judge(parent_transcript, previous)
            combined_judgments = list({item["difference_id"]: item for previous in selected for item in previous["judgments"]}.values())
            parent_path = _write(store.resolve_store_dir(root) / "agent-trials" / experiment_id / (judgment["judgment_id"] + "-" + store._hash(parent_transcript)[:24] + "-transcript.json"), parent_transcript)
            result_path = _write(Path(parent_path).with_suffix(".result.json"), {"judgments": combined_judgments})
            judge_provenance = {"kind": "opencode_agent", "phase": "judge", "blind": False, **actual, "session_id": plan["controller"]["session_id"], "agent_experiment_id": experiment_id, "trial_id": trial["trial_id"], "judgment_ids": [previous["judgment_id"] for previous in selected], "base_release_sha256": plan["base_release_sha256"], "transcript_path": parent_path, "transcript_sha256": store._file_hash(Path(parent_path)), "result_path": result_path, "result_sha256": store._file_hash(Path(result_path))}
            cases[trial["case_id"]][trial["phase"] + "_judge"] = {"result_path": result_path, "result_sha256": judge_provenance["result_sha256"], "provenance": judge_provenance, "judgments": combined_judgments, "prediction_sha256": judgment["prediction_sha256"], "labels_sha256": judgment["labels_sha256"]}
    record = {"schema_version": 1, "base_release_sha256": plan["base_release_sha256"], "policy_sha256": plan["policy_sha256"], "generation": generation, "development_source_ids": plan["development_source_ids"], "cases": list(cases.values()), "artifacts": []}
    evaluated = evo.register_experiment(root, plan["memory_id"], plan["revision"], record)
    with _db(root, write=True) as connection:
        key = experiment_id + ":" + evaluated["evaluation_id"]
        if not _find(connection, "agent_trial_evaluations", "binding_id", key):
            evo._put(connection, "agent_trial_evaluations", key, {"binding_id": key, "experiment_id": experiment_id, "evaluation_id": evaluated["evaluation_id"], "status": evaluated["status"], "execution_context": dict(caller_context), "created_at": store._now()})
    return {**evaluated, "agent_experiment_id": experiment_id, "next_action": "通过后由主 agent 调用 trial_publish；未通过则核对错误或处理局部待办，不跳过准入。"}


def publish_experiment(root, experiment_id, caller_context):
    with _db(root) as connection:
        plan = _plan(connection, experiment_id)
        _controller(plan, caller_context)
        records = [item for item in evo._rows(connection, "agent_trial_evaluations") if item["experiment_id"] == experiment_id]
        if not records:
            raise MemoryError("请先汇总并登记真实实验。")
        current = records[-1]
        if current["status"] != "passed":
            raise MemoryError("当前实验未通过，不能发布。")
        used = connection.execute("SELECT 1 FROM used_evaluations WHERE evaluation_id=?", (current["evaluation_id"],)).fetchone()
        if used:
            active = connection.execute("SELECT revision,evaluation_id FROM active WHERE memory_id=?", (plan["memory_id"],)).fetchone()
            if active is None or active["revision"] != plan["revision"] or active["evaluation_id"] != current["evaluation_id"]:
                raise MemoryError("该实验评测已用于历史发布，当前已撤回或被其他发布替换；需要重新实验，不能重用旧评测。")
            evaluation = store._evaluation(connection, current["evaluation_id"])
            if evaluation["memory_id"] != plan["memory_id"] or evaluation["revision"] != plan["revision"]:
                raise MemoryError("历史发布评测与本实验候选不一致。")
            # 重放到原发布事件，保留原回执的 release；不把后续集合变化说成重新验证。
            historical, published = {}, None
            for event in connection.execute("SELECT event_id,memory_id,revision,action,record_json,created_at FROM audit ORDER BY rowid"):
                details = store._parse(event["record_json"])
                if event["action"] in {"publish", "manual_publish"}:
                    historical[event["memory_id"]] = {"memory_id": event["memory_id"], "revision": event["revision"], "content_sha256": details["content_sha256"]}
                elif event["action"] == "withdraw":
                    historical.pop(event["memory_id"], None)
                if event["action"] == "publish" and event["memory_id"] == plan["memory_id"] and event["revision"] == plan["revision"] and details.get("evaluation_id") == current["evaluation_id"]:
                    if details.get("content_sha256") != plan["candidate"]["content_sha256"] or details.get("report_sha256") != evaluation["report_sha256"]:
                        raise MemoryError("历史发布回执与候选或评测哈希不一致。")
                    published = {"memory_id": plan["memory_id"], "revision": plan["revision"], "state": "published",
                                 "content_sha256": details["content_sha256"], "evaluation_id": current["evaluation_id"],
                                 "report_sha256": details["report_sha256"], "release_sha256": store._release(list(historical.values())),
                                 "event_id": event["event_id"], "published_at": event["created_at"]}
                    break
            if published is None:
                raise MemoryError("已使用评测缺少对应发布审计，不能生成历史发布回执。")
            return {**published, "replayed": True, "historical_receipt": True, "revalidated": False,
                    "current_release_sha256": store._release(store._published(connection)),
                    "notice": "返回既有发布的历史回执；本次未重新抽取、评测或审批，当前同一修订及评测仍处于发布状态。"}
    _frozen(root, plan)
    return {**evo.auto_promote(root, current["evaluation_id"]), "replayed": False}


def _verified_judge(transcript, judgment):
    context = judgment["execution_context"]
    if transcript.get("info", {}).get("id") != context["session_id"]:
        raise MemoryError("局部判断导出没有绑定主会话。")
    for message in transcript.get("messages", []):
        meta = message.get("info", {})
        if meta.get("id") != context["message_id"] or meta.get("role") != "assistant" or meta.get("agent") != "term-extractor":
            continue
        for part in message.get("parts", []):
            if part.get("tool") != "term_learning_trial_judge" or part.get("state", {}).get("status") != "completed":
                continue
            supplied = part["state"].get("input", {})
            try:
                values = json.loads(supplied.get("judgments_json", "null"))
            except (ValueError, TypeError):
                continue
            output = _tool_output(part) or {}
            if supplied.get("experiment_id") == judgment["experiment_id"] and values == judgment["tool_input"]["judgments"] and judgment["judgment_id"] in output.get("saved_judgment_ids", []):
                return meta
    raise MemoryError("真实主会话尚无已完成且匹配的局部判断调用，请等待工具返回后重试。")


def check_provenance(artifact, *, phase, candidate, generation, base_release, expected_source=None, expected_memories=None, expected_policy=None, root=None):
    """仅供 evolution_store 窄分派；不接受自由上传的 agent 来源声明。"""
    provenance = artifact.get("provenance", {})
    if provenance.get("kind") != "opencode_agent" or provenance.get("phase") != phase or provenance.get("blind") is not (phase != "judge"):
        raise MemoryError("自主实验的来源阶段或盲抽声明不符。")
    result_path = Path(artifact.get("result_path", ""))
    if not result_path.is_absolute() or store._file_hash(result_path) != artifact.get("result_sha256") or provenance.get("result_path") != str(result_path) or provenance.get("result_sha256") != artifact.get("result_sha256"):
        raise MemoryError("自主实验输出文件或哈希不符。")
    transcript_path = Path(provenance.get("transcript_path", ""))
    if not transcript_path.is_absolute() or store._file_hash(transcript_path) != provenance.get("transcript_sha256"):
        raise MemoryError("自主实验真实会话导出已变化。")
    transcript = store._read_json(transcript_path, 96 * 1024 * 1024)
    if root is None:
        raise MemoryError("自主来源校验只能在实验注册上下文中执行。")
    with _db(root) as connection:
        plan, trial = _trial(connection, provenance.get("trial_id"))
        record = _find(connection, "agent_trial_results", "trial_id", trial["trial_id"])
        if record is None or plan["experiment_id"] != provenance.get("agent_experiment_id"):
            raise MemoryError("自主实验来源没有对应的已冻结试验。")
        if base_release != plan["base_release_sha256"] or candidate["content_sha256"] != plan["candidate"]["content_sha256"]:
            raise MemoryError("自主实验未绑定本轮基线与候选。")
        if provenance.get("base_release_sha256") != base_release:
            raise MemoryError("自主来源声明的正式基线不符。")
        for key in ("model_id", "variant"):
            if provenance.get(key) != generation.get(key):
                raise MemoryError("自主 A/B 模型配置不一致。")
        if phase == "judge":
            if provenance.get("session_id") != plan["controller"]["session_id"]:
                raise MemoryError("局部判断声明没有绑定控制会话。")
            ids = provenance.get("judgment_ids")
            if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
                raise MemoryError("局部判断必须绑定全部不可变判断记录。")
            judgments = [_find(connection, "agent_trial_judgments", "judgment_id", key) for key in ids]
            if any(not item or item["trial_id"] != trial["trial_id"] for item in judgments):
                raise MemoryError("局部判断没有对应不可变记录。")
            for judgment in judgments:
                meta = _verified_judge(transcript, judgment)
                if meta.get("providerID", "") + "/" + meta.get("modelID", "") != generation["model_id"] or meta.get("variant", "") != generation["variant"]:
                    raise MemoryError("局部语义复核必须使用与本轮试验一致的真实模型。")
            combined = list({item["difference_id"]: item for previous in judgments for item in previous["judgments"]}.values())
            judgment = judgments[-1]
            result = store._read_json(result_path, 2 * 1024 * 1024)
            if result != {"judgments": combined} or artifact.get("prediction_sha256") != judgment["prediction_sha256"] or artifact.get("labels_sha256") != judgment["labels_sha256"]:
                raise MemoryError("局部判断结果没有绑定实际预测和标签。")
        else:
            expected_ids = [] if phase == "baseline" else [{key: candidate[key] for key in ("memory_id", "revision", "content_sha256")}]
            if provenance.get("candidate_ids") != expected_ids or provenance.get("controller_session_id") != plan["controller"]["session_id"]:
                raise MemoryError("自主盲抽未声明本轮唯一候选或控制会话。")
            if trial["phase"] != phase or record["committed"]["result_path"] != str(result_path) or provenance.get("session_id") != record["execution_context"]["session_id"]:
                raise MemoryError("试验来源与冻结提交不一致。")
            _checked, actual, actual_path = _transcript(root, plan, trial, record)
            if actual != generation or str(transcript_path) != actual_path:
                raise MemoryError("自主试验真实模型或会话文件不符。")
            index = store._read_json(Path(trial["index_path"]), 48 * 1024 * 1024)
            if expected_source is not None and index["source_document"] != expected_source or expected_memories is not None and trial["memories"] != expected_memories:
                raise MemoryError("自主试验交付的原文或经验包与实验预期不符。")
            policy_path = Path(root) / ".opencode/skills/term-extract/references/business-rules.md"
            if expected_policy is not None and (not policy_path.exists() or policy_path.read_text(encoding="utf-8") != expected_policy):
                raise MemoryError("自主试验固定口径不符。")
            result = store._read_json(result_path, 2 * 1024 * 1024)
        artifacts = [{"path": str(result_path), "sha256": artifact["result_sha256"]}, {"path": str(transcript_path), "sha256": provenance["transcript_sha256"]}, *record["files"]]
        return result, artifacts


def baseline_memories(root, provenance):
    """从不可变计划读取正式经验子集，禁止由上传 provenance 自填。"""
    with _db(root) as connection:
        plan, trial = _trial(connection, provenance.get("trial_id"))
        if trial["phase"] != "baseline" or plan["experiment_id"] != provenance.get("agent_experiment_id"):
            raise MemoryError("正式经验子集没有绑定基线试验。")
        return trial["memories"]


def _dispatch_request(root, request):
    if not isinstance(request, dict):
        raise MemoryError("请求必须为JSON对象。")
    context = request.get("execution_context")
    action = request.get("action")
    if action == "prepare":
        return prepare_experiment(root, request.get("memory_id"), request.get("revision"), request.get("cases"), context, request.get("memory_ids"))
    if action == "trial":
        return get_trial(root, request.get("trial_id"), context, bool(request.get("claim", False)))
    if action == "record":
        return record_trial(root, request.get("trial_id"), request.get("committed"), context)
    if action == "delivery":
        return delivery(root, request.get("trial_id"), context)
    if action == "inspect":
        return inspect_experiment(root, request.get("experiment_id"), context, request.get("trial_id"), request.get("difference_cursor", "0"), request.get("max_bytes", 32768))
    if action == "judge":
        return judge_experiment(root, request.get("experiment_id"), request.get("judgments"), context)
    if action == "finalize":
        return finalize(root, request.get("experiment_id"), context)
    if action == "publish":
        return publish_experiment(root, request.get("experiment_id"), context)
    raise MemoryError("不支持的自主试验动作。")


def main(argv=None):
    supplied = list(argv) if argv is not None else sys.argv[1:]
    if "--request-file" in supplied:
        short = argparse.ArgumentParser(description="自主试验JSON请求桥接")
        short.add_argument("--root", "--project-root", dest="root", type=Path, default=Path(__file__).resolve().parents[4])
        short.add_argument("--request-file", type=Path, required=True)
        args = short.parse_args(supplied)
        try:
            result = _dispatch_request(args.root, store._read_json(args.request_file, 2 * 1024 * 1024))
            print(json.dumps(result, ensure_ascii=False))
            return 0
        except (ValueError, OSError, TypeError, KeyError) as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
            return 2
    parser = argparse.ArgumentParser(description="OpenCode 自主试验原子工具，不调用模型")
    parser.add_argument("--root", "--project-root", dest="root", type=Path, default=Path(__file__).resolve().parents[4])
    parser.add_argument("--context-json", required=True)
    sub = parser.add_subparsers(dest="action", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--memory-id", required=True)
    prepare.add_argument("--revision", type=int, required=True)
    prepare.add_argument("--cases-json", required=True)
    prepare.add_argument("--memory-ids-json")
    trial = sub.add_parser("trial")
    trial.add_argument("--trial-id", required=True)
    trial.add_argument("--claim", action="store_true")
    record = sub.add_parser("record")
    record.add_argument("--trial-id", required=True)
    record.add_argument("--committed-json", required=True)
    delivery_parser = sub.add_parser("delivery")
    delivery_parser.add_argument("--trial-id", required=True)
    for action in ("inspect", "judge", "finalize", "publish"):
        command = sub.add_parser(action)
        command.add_argument("--experiment-id", required=True)
        if action == "judge":
            command.add_argument("--judgments-json", required=True)
    args = parser.parse_args(supplied)
    try:
        context = json.loads(args.context_json)
        if args.action == "prepare":
            result = prepare_experiment(args.root, args.memory_id, args.revision, json.loads(args.cases_json), context, json.loads(args.memory_ids_json) if args.memory_ids_json else None)
        elif args.action == "trial":
            result = get_trial(args.root, args.trial_id, context, args.claim)
        elif args.action == "record":
            result = record_trial(args.root, args.trial_id, json.loads(args.committed_json), context)
        elif args.action == "delivery":
            result = delivery(args.root, args.trial_id, context)
        elif args.action == "inspect":
            result = inspect_experiment(args.root, args.experiment_id, context)
        elif args.action == "judge":
            result = judge_experiment(args.root, args.experiment_id, json.loads(args.judgments_json), context)
        elif args.action == "publish":
            result = publish_experiment(args.root, args.experiment_id, context)
        else:
            result = finalize(args.root, args.experiment_id, context)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
