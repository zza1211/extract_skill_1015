"""两种学习模式的本地工作台接口。模型任务显式启动，GET 不写库。"""
from __future__ import annotations

from pathlib import Path

import memory_store as store


def get(root: Path, parts: list[str], query: dict):
    if parts[:2] == ["api", "learning"]:
        import learning_runner as runner
        import evolution_store as evolution
        if parts == ["api", "learning", "settings"]:
            return runner.get_settings(root)
        if parts == ["api", "learning", "models"]:
            return runner.list_models(root)
        if parts == ["api", "learning", "jobs"]:
            return runner.list_jobs(root)
        if len(parts) == 4 and parts[2] == "jobs":
            return runner.get_job(root, parts[3])
        if parts == ["api", "learning", "cases"]:
            value = evolution.list_cases(root)
            return value if isinstance(value, dict) else {"items": value, "total": len(value)}
        if len(parts) == 4 and parts[2] == "cases":
            return evolution.get_case(root, parts[3], include_labels=True)
    if parts[:2] == ["api", "reviews"]:
        import evolution_store as evolution
        if len(parts) == 2:
            value = evolution.list_reviews(root, state=query.get("state", "open"))
            return value if isinstance(value, dict) else {"items": value, "total": len(value)}
        if len(parts) == 3:
            return evolution.get_review(root, parts[2])
    return None


def post(root: Path, parts: list[str], body: dict):
    if parts[:2] == ["api", "learning"]:
        import learning_runner as runner
        import evolution_store as evolution
        if parts == ["api", "learning", "settings"]:
            return runner.save_settings(root, body.get("settings", {}))
        if parts == ["api", "learning", "jobs"]:
            allowed = {"mode", "input_path", "labels_path", "family_id", "split", "declared_by", "auto_evolve"}
            if set(body) - allowed:
                raise store.MemoryError("任务包含未知字段。")
            return runner.create_job(root, **body)
        if len(parts) == 5 and parts[2] == "jobs" and parts[4] == "start":
            return runner.start_job(root, parts[3])
        if parts == ["api", "learning", "cases"]:
            allowed = {"input_path", "labels_path", "family_id", "split", "declared_by", "note"}
            if set(body) - allowed:
                raise store.MemoryError("案例包含未知字段。")
            arguments = dict(body)
            for key in ("input_path", "labels_path"):
                path = Path(store._string(arguments.get(key), key, 4096)).expanduser()
                arguments[key] = path if path.is_absolute() else root / path
            return evolution.import_case(root, **arguments)
    if len(parts) == 4 and parts[:2] == ["api", "reviews"] and parts[3] == "resolve":
        import evolution_store as evolution
        allowed = {"decision", "answer", "actor", "expected_revision"}
        if set(body) - allowed:
            raise store.MemoryError("人工答复包含未知字段。")
        return evolution.resolve_review(root, parts[2], channel="dashboard", **body)
    if parts == ["api", "experiences", "scan"]:
        import evolution_store as evolution
        return evolution.scan_conflicts(root, force=True)
    return None
