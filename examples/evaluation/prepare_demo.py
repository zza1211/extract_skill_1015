#!/usr/bin/env python3
"""仅准备手写合成数据的隔离离线评测演示；不运行模型、评测或发布。"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import tempfile


PACKAGE = Path(__file__).resolve().parent
REPO = PACKAGE.parents[1]
SCRIPTS = REPO / ".opencode/skills/term-extract/scripts"
sys.path.insert(0, str(SCRIPTS))

import document_io
import memory_store


NOTICE = (
    "这是手写合成 fixture 的接口演示。baseline.json/candidate.json 都不是模型生成，"
    "不是模型效果或经验收益证据；labels.json 仅示意正确标签。"
    "不要用本清单评测生产库中的真实候选。本脚本只准备，不评测、不发布。"
)


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


@contextmanager
def isolated_memory(root: Path):
    """覆盖可能指向真实库的外部设置，退出时原样恢复。"""
    previous = os.environ.get("TERM_EXTRACT_MEMORY_DIR")
    os.environ["TERM_EXTRACT_MEMORY_DIR"] = str(root / ".term-memory")
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("TERM_EXTRACT_MEMORY_DIR", None)
        else:
            os.environ["TERM_EXTRACT_MEMORY_DIR"] = previous


def _overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def _new_root(out_dir: Path | None) -> Path:
    root = (Path(tempfile.mkdtemp(prefix="term-evaluation-demo-")) if out_dir is None
            else Path(out_dir).expanduser()).resolve()
    # 不覆盖代码、真实记忆库（包括外部配置），也不接受其父目录。
    stores = {REPO / ".term-memory", memory_store.resolve_store_dir(REPO)}
    if _overlap(root, REPO) or any(_overlap(root, store.resolve()) for store in stores):
        raise ValueError("演示目录必须位于仓库及实际记忆库之外，不能使用它们的父目录。")
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError("演示目录必须不存在或为空；脚本不会清空已有数据。")
    root.mkdir(parents=True, exist_ok=True)
    return root


def _command(memory_dir: Path, argv: list[str]) -> str:
    """命令显式指定隔离库，避免用户环境变量重定向到生产库。"""
    if os.name == "nt":
        quote = lambda value: "'" + str(value).replace("'", "''") + "'"
        return ("& { $termEvaluationPrevious = [Environment]::GetEnvironmentVariable('TERM_EXTRACT_MEMORY_DIR', 'Process'); "
                "try { $env:TERM_EXTRACT_MEMORY_DIR = " + quote(memory_dir) + "; & " + " ".join(quote(v) for v in argv) +
                " } finally { if ($null -eq $termEvaluationPrevious) { "
                "Remove-Item -LiteralPath Env:TERM_EXTRACT_MEMORY_DIR -ErrorAction SilentlyContinue "
                "} else { $env:TERM_EXTRACT_MEMORY_DIR = $termEvaluationPrevious } } }")
    return "env TERM_EXTRACT_MEMORY_DIR=" + shlex.quote(str(memory_dir)) + " " + shlex.join(argv)


def prepare_demo(out_dir: Path | None = None) -> dict:
    inherited_override = bool(os.environ.get("TERM_EXTRACT_MEMORY_DIR", "").strip())
    root = _new_root(out_dir)
    manifest = _read(PACKAGE / "manifest.example.json")
    for case in manifest["cases"]:
        name = Path(case["index_path"]).parts[0]
        target = root / name
        target.mkdir()
        # 每个工件来自独立静态文件；不把标签复制成声称由模型生成的输出。
        for filename in ("input.json", "labels.json", "baseline.json", "candidate.json", "annotations.json"):
            shutil.copyfile(PACKAGE / name / filename, target / filename)
        annotations = _read(target / "annotations.json")
        if annotations != case["assertions"]:
            raise ValueError(f"{name}/annotations.json 与规范清单的断言不一致。")
        document_io.prepare(target / "input.json", target / "prepared")
    shutil.copyfile(PACKAGE / "rule.json", root / "rule.template.json")
    manifest_path = root / "evaluation.manifest.json"
    _write(manifest_path, manifest)
    # UI 从项目根读取静态页面与唯一的规范示例文件；使用8766避免替换生产8765。
    shutil.copytree(REPO / "web/memory", root / "web/memory")
    template = root / "examples/evaluation/manifest.example.json"
    template.parent.mkdir(parents=True)
    shutil.copyfile(PACKAGE / "manifest.example.json", template)
    with isolated_memory(root):
        correction = memory_store.import_correction(
            root, root / "development/input.json", root / "development/baseline.json",
            root / "development/labels.json",
            "合成演示：原边界漏掉同一设备的射频及供电参数；正确边界应包含C7。不是模型运行记录。",
            "合成示例作者（仅示意人工标注）", "synthetic-school-a")
        payload = _read(PACKAGE / "rule.json")
        payload["correction_ids"] = [correction["correction_id"]]
        candidate = memory_store.propose(root, payload)
        _write(root / "rule.bound.json", payload)
    memory_dir = root / ".term-memory"
    result = {
        "notice": NOTICE, "demo_root": str(root), "manifest_path": str(manifest_path),
        "memory_id": candidate["memory_id"], "revision": candidate["revision"],
        "correction_id": correction["correction_id"], "memory_dir": str(memory_dir),
        "environment_override_ignored": inherited_override,
        "evaluation_command": _command(memory_dir, [sys.executable, str(SCRIPTS / "memory_evaluate.py"),
            "--project-root", str(root), "--memory-id", candidate["memory_id"], "--revision", str(candidate["revision"]),
            "--manifest", str(manifest_path)]),
        "ui_command": _command(memory_dir, [sys.executable, str(SCRIPTS / "memory_ui.py"),
            "--project-root", str(root), "--port", "8766", "--no-maintenance"]),
    }
    _write(root / "demo-info.json", result)
    (root / "SYNTHETIC-DEMO.txt").write_text(NOTICE + "\n", encoding="utf-8")
    return result


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=NOTICE)
    parser.add_argument("--out-dir", type=Path, help="仓库和实际记忆库之外的空目录；省略则新建系统临时目录")
    args = parser.parse_args(argv)
    try:
        result = prepare_demo(args.out_dir)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
