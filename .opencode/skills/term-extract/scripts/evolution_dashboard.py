"""记忆管理页面接口：读学习记录、管理案例与待办，不启动模型执行。"""
from __future__ import annotations

from pathlib import Path

import memory_store as store


def get(root: Path, parts: list[str], query: dict):
    if parts[:2] == ["api", "learning"]:
        import evolution_store as evolution
        if len(parts) >= 3 and parts[2] in {"settings", "models"}:
            raise store.MemoryError("模型由 OpenCode 会话选择和管理；网页只管理记忆，不提供模型设置。")
        if parts == ["api", "learning", "studies"]:
            import agent_learning
            return agent_learning.list_studies(root)
        if len(parts) == 4 and parts[2] == "studies":
            import agent_learning
            return agent_learning.get_study(root, parts[3])
        if parts == ["api", "learning", "jobs"]:
            import learning_runner as runner
            return runner.list_jobs(root)
        if len(parts) == 4 and parts[2] == "jobs":
            import learning_runner as runner
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
        import evolution_store as evolution
        if len(parts) >= 3 and parts[2] in {"jobs", "settings", "models", "studies"}:
            raise store.MemoryError("网页不创建、启动或继续抽取任务，也不修改模型配置。请在 OpenCode 中加载 term-extract 技能执行；本页仅展示记录和管理记忆。")
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
    if parts == ["api", "experiences", "discard"]:
        import evolution_store as evolution
        allowed = {"memory_id", "revision", "reason", "actor", "acknowledged"}
        if set(body) - allowed:
            raise store.MemoryError("丢弃候选请求包含未知字段。")
        if body.get("acknowledged") is not True:
            raise store.MemoryError("请明确勾选丢弃确认；acknowledged 必须为 true。")
        return evolution.discard_candidate(root, body.get("memory_id"), body.get("revision"),
                                           body.get("reason"), body.get("actor", "维护者"))
    return None
