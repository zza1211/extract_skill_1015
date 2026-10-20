"""手动清理本项目评测运行态；默认只预览，不会被 OpenCode agent 调用。

用法：
    python scripts/reset_eval_environment.py                 # 只预览
    python scripts/reset_eval_environment.py --apply         # 预览后输入 DELETE 才删除
    python scripts/reset_eval_environment.py --apply --yes  # 明确跳过交互确认

默认只删除项目内的 runs、.term-memory 和 reports，不触碰代码、examples、测试、
输入标书或标签。若 TERM_EXTRACT_MEMORY_DIR 指向项目外目录，必须另加
--allow-external-memory 才会在 --apply 时删除该目录。
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import sys


LOCAL_RUNTIME_NAMES = ("runs", ".term-memory", "reports")


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def _safe_local_target(root: Path, name: str) -> Path:
    target = _resolved(root / name)
    expected = _resolved(root) / name
    if target != expected or target == _resolved(root):
        raise RuntimeError(f"拒绝不安全的本地清理目标：{target}")
    return target


def targets(root: Path, *, allow_external_memory: bool = False) -> tuple[list[Path], list[str]]:
    root = _resolved(root)
    selected = [_safe_local_target(root, name) for name in LOCAL_RUNTIME_NAMES]
    notices: list[str] = []
    configured = os.environ.get("TERM_EXTRACT_MEMORY_DIR", "").strip()
    if not configured:
        return selected, notices
    memory = _resolved(Path(configured))
    if memory in selected:
        return selected, notices
    if memory == root or memory in root.parents:
        raise RuntimeError(
            "拒绝把项目根目录或其父目录作为记忆清理目标："
            f"{memory}；请检查 TERM_EXTRACT_MEMORY_DIR。"
        )
    protected = (root / ".git").resolve(), (root / ".opencode").resolve()
    if any(memory == path or path in memory.parents for path in protected):
        raise RuntimeError(
            "拒绝把代码仓库或 OpenCode 配置目录作为记忆清理目标："
            f"{memory}"
        )
    try:
        memory.relative_to(root)
    except ValueError:
        if allow_external_memory:
            if memory == memory.anchor or memory == Path(memory.anchor):
                raise RuntimeError(f"拒绝删除磁盘根目录：{memory}")
            selected.append(memory)
        else:
            notices.append(
                "检测到项目外 TERM_EXTRACT_MEMORY_DIR，默认未纳入清理："
                f"{memory}；确认后可使用 --allow-external-memory。"
            )
    else:
        # 项目内的自定义记忆目录不属于默认目录，避免误把任意路径当成标准目录。
        if allow_external_memory:
            selected.append(memory)
        else:
            notices.append(
                "检测到项目内自定义 TERM_EXTRACT_MEMORY_DIR，默认未纳入清理："
                f"{memory}；确认后可使用 --allow-external-memory。"
            )
    return list(dict.fromkeys(selected)), notices


def describe(path: Path) -> str:
    if not path.exists():
        return f"[不存在] {path}"
    if path.is_dir():
        count = sum(1 for item in path.rglob("*") if item.is_file())
        return f"[目录，{count} 个文件] {path}"
    return f"[文件，{path.stat().st_size} bytes] {path}"


def remove(path: Path) -> None:
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="清理 term-extract 的评测运行态；默认只预览。")
    parser.add_argument("--project-root", type=Path, default=project_root(), help="项目根目录，默认脚本所在仓库")
    parser.add_argument("--apply", action="store_true", help="执行删除；没有此参数时只预览")
    parser.add_argument("--yes", action="store_true", help="跳过 DELETE 确认，仅建议用于人工已核对预览后")
    parser.add_argument("--allow-external-memory", action="store_true", help="纳入 TERM_EXTRACT_MEMORY_DIR 指向的自定义目录")
    args = parser.parse_args(argv)

    root = _resolved(args.project_root)
    if not root.is_dir() or not (root / ".opencode").is_dir():
        print(f"错误：不是 term-extract 项目根目录：{root}", file=sys.stderr)
        return 2
    try:
        selected, notices = targets(root, allow_external_memory=args.allow_external_memory)
    except (OSError, RuntimeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2

    print("将清理以下运行态目标：")
    for path in selected:
        print("  " + describe(path))
    for notice in notices:
        print("提示：" + notice)
    print("保留：代码、examples、tests、输入标书和标签源文件。")
    if not args.apply:
        print("当前为预览模式；确认范围后加 --apply 执行。")
        return 0
    if not args.yes:
        try:
            answer = input("确认删除以上目标？请输入 DELETE：").strip()
        except (EOFError, KeyboardInterrupt):
            print("已取消。")
            return 1
        if answer != "DELETE":
            print("未输入 DELETE，已取消。")
            return 1
    for path in selected:
        remove(path)
        print("已删除：" + str(path))
    print("评测运行态已清空。下次抽取请使用新的 runs 子目录；若使用外置记忆目录，请保持环境变量配置一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
