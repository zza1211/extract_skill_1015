"""OpenCode 高层工具的 JSON 文件桥接；不向盲抽返回标签内容。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import evolution_store as evolution
import learning_runner as runner
import memory_store as store


def dispatch(root, action, body):
    if action in {"create", "start", "settings"}:
        raise store.MemoryError(runner.RETIRED)
    if action == "create":
        return runner.create_job(root, **body)
    if action == "start":
        return runner.start_job(root, body["job_id"])
    if action == "status":
        result = runner.get_job(root, body["job_id"], include_result=body.get("include_result", False))
        # 工具交付维持原有 48 KiB 上限；详细工件在工作台中查看。
        if len(store._canonical(result)) > 40 * 1024:
            result = {key: result.get(key) for key in ("job_id", "mode", "state", "next_action", "result_path", "run_id", "review_ids", "error")}
            result["notice"] = "详细执行记录超过工具预算，请在工作台查看；未截断原文或伪造交付。"
        return result
    if action == "jobs":
        return runner.list_jobs(root)
    if action == "import-case":
        return evolution.import_case(root, **body)
    if action == "cases":
        return evolution.list_cases(root)
    if action == "reviews":
        return evolution.list_reviews(root, state=body.get("state", "open"))
    if action == "reply":
        source_ref = store._string(body.pop("source_ref", None), "用户原话或消息引用", 2000)
        return evolution.resolve_review(root, **body, channel="conversation", source_ref=source_ref)
    if action == "scan":
        return evolution.scan_conflicts(root, force=True)
    if action == "settings":
        return runner.get_settings(root) if not body else runner.save_settings(root, body)
    raise store.MemoryError("未知学习操作。")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, required=True)
    parser.add_argument("action", choices=("create", "start", "status", "jobs", "import-case", "cases", "reviews", "reply", "scan", "settings"))
    parser.add_argument("--request-file", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        body = store._read_json(args.request_file, 3 * 1024 * 1024)
        if not isinstance(body, dict):
            raise store.MemoryError("学习请求必须为 JSON 对象。")
        print(store._canonical(dispatch(args.project_root.resolve(), args.action, body)).decode("utf-8"))
        return 0
    except (ValueError, OSError, TypeError, KeyError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
