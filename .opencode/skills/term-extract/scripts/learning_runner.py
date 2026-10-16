"""0.5 历史任务只读与旧事件解析。抽取和学习统一由 OpenCode agent 编排。

保留旧记录与报告校验能力；create/start/run 等执行入口已停用，不调用模型。
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import shutil
import memory_store as store

MemoryError = store.MemoryError
RETIRED = "Python 自动编排入口已停用。请在项目根目录启动 opencode --agent term-extractor，由 agent 加载 term-extract skill；网页仅管理经验。"

def _exe(configured=""):
    supplied = configured.strip() if isinstance(configured, str) else ""
    found = supplied or shutil.which("opencode") or shutil.which("opencode.exe") or shutil.which("opencode.ps1")
    if not found:
        return None
    target = Path(found).expanduser().resolve()
    if target.suffix.lower() in {".ps1", ".cmd", ".bat"}:
        candidates = [target.parent / "node_modules/opencode-ai/bin/opencode.exe",
                      target.parent / "node_modules/opencode-ai/bin/opencode"]
        target = next((path for path in candidates if path.is_file()), target)
    if target.suffix.lower() in {".ps1", ".cmd", ".bat"} or not target.is_file():
        return None
    return str(target)

def parse_model_output(text):
    if not isinstance(text, str):
        raise MemoryError("模型没有返回文本。")
    cleaned = text.strip()
    match = re.fullmatch(r"```(?:json)?\s*([\s\S]*?)\s*```", cleaned, re.IGNORECASE)
    cleaned = match.group(1) if match else cleaned
    value = store._parse(cleaned)
    if not isinstance(value, dict):
        raise MemoryError("模型输出必须是 JSON 对象。")
    return value

def extract_event_output(events_text):
    texts, sessions, steps = [], set(), []
    for line in events_text.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue  # 启动日志不作为模型结果；原样事件流仍然保存。
        if not isinstance(event, dict):
            continue
        session = event.get("sessionID") or event.get("session_id")
        if session:
            sessions.add(str(session))
        if event.get("type") == "text":
            part = event.get("part", {})
            text = part.get("text") if isinstance(part, dict) else None
            if isinstance(text, str):
                texts.append(text)
        if event.get("type") == "step_finish":
            steps.append(event.get("part", {}))
    if len(sessions) > 1:
        raise MemoryError("模型事件流混入多个会话，不能绑定为一次盲抽。")
    usage = {}
    for key, source in (("input_tokens", "input"), ("output_tokens", "output")):
        values = [step.get("tokens", {}).get(source) for step in steps if isinstance(step, dict)]
        usage[key] = sum(values) if values and all(isinstance(value, (int, float)) for value in values) else None
    costs = [step.get("cost") for step in steps if isinstance(step, dict)]
    usage["cost"] = sum(costs) if costs and all(isinstance(value, (int, float)) for value in costs) else None
    return {"output": parse_model_output("\n".join(texts)), "session_id": next(iter(sessions), None), "usage": usage}

def _rows(connection, table):
    if connection is None or not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return []
    items = []
    for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid"):
        item = store._parse(row["record_json"])
        if store._hash(item) != row["record_sha256"]:
            raise MemoryError("历史任务记录哈希不一致。")
        items.append(item)
    return items


def _job(connection, job_id):
    record = next((x for x in _rows(connection, "workflow_jobs") if x["job_id"] == job_id), None)
    if record is None:
        raise MemoryError("历史任务不存在。")
    events = [x for x in _rows(connection, "workflow_job_events") if x["job_id"] == job_id]
    result = {**record, "state": "queued", "events": events, "candidates": [], "experiments": []}
    calls = []
    for event in events:
        if event["kind"] in {"state", "checkpoint"}:
            result.update({k:v for k,v in event.items() if k not in {"event_id","job_id","kind","created_at"}})
        if event["kind"] == "call_finished":
            calls.append(event)
    result.update(execution_disabled=True, legacy=True, next_action="历史记录仅供追溯；新任务请由 OpenCode agent 执行。")
    result["usage"] = {"calls": sum(x["kind"]=="call_started" for x in events),
                       "completed_calls": len(calls),
                       "elapsed_seconds": sum(float(x.get("usage",{}).get("elapsed_seconds") or 0) for x in calls)}
    for key in ("input_tokens","output_tokens","cost"):
        values=[x.get("usage",{}).get(key) for x in calls]
        result["usage"][key]=sum(values) if values and all(v is not None for v in values) else None
    return result


def get_job(root, job_id, include_result=True):
    with store._db(Path(root)) as connection:
        result = _job(connection, store._string(job_id,"job_id",128))
    if include_result and result.get("result_path"):
        result["result"] = store._read_json(Path(result["result_path"]),2*1024*1024)
    return result


def list_jobs(root):
    with store._db(Path(root)) as connection:
        items=[_job(connection,row["job_id"]) for row in reversed(_rows(connection,"workflow_jobs"))]
    for item in items:
        item.pop("events",None)
    return {"items":items,"total":len(items)}


def _retired(*args, **kwargs):
    raise MemoryError(RETIRED)


create_job = run_job = start_job = save_settings = list_models = _retired


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root",type=Path,default=Path(__file__).resolve().parents[4])
    parser.add_argument("action",choices=("list","show","create","start","run","settings","models"))
    parser.add_argument("--job-id")
    parser.add_argument("--file")
    args=parser.parse_args(argv)
    try:
        if args.action not in {"list","show"}:
            raise MemoryError(RETIRED)
        value=list_jobs(args.project_root) if args.action=="list" else get_job(args.project_root,args.job_id)
        print(json.dumps(value,ensure_ascii=False))
        return 0
    except (ValueError,OSError,TypeError,KeyError) as exc:
        print(json.dumps({"ok":False,"error":str(exc)},ensure_ascii=False))
        return 2


if __name__=="__main__":
    raise SystemExit(main())
