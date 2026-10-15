"""核对读取覆盖与草稿后保存主 agent 的结果；不调用模型。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def coverage_report(index_path: Path, receipts_path: Path) -> dict:
    index = load_json(index_path)
    fingerprint = hashlib.sha256(index_path.read_bytes()).hexdigest()
    units = index.get("units", [])
    expected = {}
    for number, unit in enumerate(units):
        key = str(unit.get("unit_id", number))
        expected[key] = {
            "length": unit.get("source_length", len(unit.get("source_text", unit.get("text", "")))),
            "tag": unit.get("tag"),
        }
    spans = {key: [] for key in expected}
    invalid = []
    receipt_count = 0
    for line_number, line in enumerate(receipts_path.read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip():
            continue
        item = json.loads(line)
        receipt_count += 1
        if item.get("index_sha256") != fingerprint:
            invalid.append(f"第 {line_number} 条读取回执与当前 index 版本不一致")
            continue
        if item.get("coverage_kind") != "full_original":
            invalid.append(f"第 {line_number} 条回执不是完整原文读取")
            continue
        for part in item.get("parts", []):
            key = str(part.get("unit_id"))
            if key not in expected:
                invalid.append(f"读取回执含未知原文单元 {key}")
                continue
            start, end = part.get("part_start"), part.get("part_end")
            length = expected[key]["length"]
            if type(start) is not int or type(end) is not int or not 0 <= start <= end <= length:
                invalid.append(f"原文单元 {key} 的读取范围不合法")
                continue
            if part.get("total_length") != length:
                invalid.append(f"原文单元 {key} 的长度与 index 不一致")
                continue
            spans[key].append((start, end))
    missing = []
    for key, requirement in expected.items():
        end = 0
        for start, stop in sorted(spans[key]):
            if start > end:
                break
            end = max(end, stop)
        if not spans[key] or end < requirement["length"]:
            missing.append({"unit_id": key, "tag": requirement["tag"], "covered_prefix": end, "source_length": requirement["length"]})
    return {
        "complete": not invalid and not missing,
        "unit_count": len(expected),
        "receipt_count": receipt_count,
        "missing_count": len(missing),
        "missing_units": missing[:30],
        "errors": invalid[:30],
        "index_sha256": fingerprint,
        "meaning": "仅证明这些原文部分已通过工具交付；不证明模型理解或抽取语义正确。",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="核对读取回执并保存条款抽取结果")
    parser.add_argument("--index", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--receipts", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--model-id", default="未记录")
    parser.add_argument("--allow-demo", action="store_true", help="只保存明确标注为演示的结果")
    args = parser.parse_args()
    try:
        index_path = Path(args.index).resolve()
        index = load_json(index_path)
        result = load_json(Path(args.result))
        evidence = load_json(Path(args.evidence))
        validator = Path(__file__).with_name("validate_result.py")
        argv = [sys.executable, "-X", "utf8", str(validator), "--index", str(index_path), "--result", args.result, "--evidence", args.evidence]
        if args.allow_demo:
            argv.append("--allow-demo")
        validation_process = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8")
        if not validation_process.stdout.strip():
            raise ValueError(validation_process.stderr.strip() or "结果校验没有返回报告")
        validation = json.loads(validation_process.stdout)
        coverage = coverage_report(index_path, Path(args.receipts))
        errors = []
        if validation_process.returncode:
            errors.append("草稿未通过结构或证据格式校验，请先修订")
        if not coverage["complete"]:
            errors.append("原文读取存在缺口或回执版本冲突，请补读后重新提交")
        if not isinstance(evidence, dict) or evidence.get("unresolved"):
            errors.append("仍有未解决事项，保留草稿并报告待处理问题")
        if errors:
            print(json.dumps({"committed": False, "errors": errors, "coverage": coverage, "validation": validation}, ensure_ascii=False))
            return 2
        directory = Path(args.out_dir).resolve()
        directory.mkdir(parents=True, exist_ok=True)
        result_path = directory / "result.json"
        evidence_path = directory / "evidence.json"
        record_path = directory / "run.json"
        demo = bool(index.get("demo", False))
        skill_path = Path(__file__).parent.parent / "SKILL.md"
        reference_hashes = {
            file.name: hashlib.sha256(file.read_bytes()).hexdigest()
            for file in sorted((skill_path.parent / "references").glob("*.md"))
        }
        record = {
            "status": "demo_submitted" if demo else "submitted",
            "production_submittable": not demo,
            "model_id": args.model_id,
            "skill_version": "0.1.2",
            "skill_sha256": hashlib.sha256(skill_path.read_bytes()).hexdigest(),
            "reference_sha256": reference_hashes,
            "source_path": index.get("source_path"),
            "source_sha256": index.get("source_sha256"),
            "coverage": coverage,
            "validation": validation,
            "semantic_validation": "由主 agent 核验；程序没有验证选型参数完整性、漏产品或数量语义。",
        }
        for file, content in ((result_path, result), (evidence_path, evidence), (record_path, record)):
            file.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"committed": True, "status": record["status"], "result_path": str(result_path), "evidence_path": str(evidence_path), "run_path": str(record_path), "production_submittable": not demo, "semantic_validation": record["semantic_validation"]}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(json.dumps({"committed": False, "errors": [str(error)]}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
