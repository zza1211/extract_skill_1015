#!/usr/bin/env python3
"""离线比较给定输出的人工断言；不运行模型，不推断经验的因果效果。"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import re
import sys
from pathlib import Path
from typing import Any

import document_io
import validate_result as result_validator


VERSION = 1
MAX_REPORT_BYTES = 900 * 1024
SHA = re.compile(r"[0-9a-f]{64}\Z")
TAG = re.compile(r"<C[0-9]+>\Z")
NOTICE = (
    "只评测给定 baseline/candidate 输出的这些人工断言，不证明它们确由该经验产生，"
    "不证明已运行任何模型，也不等于全面语义准确率。confirmed_by/scope_note 是人工标注声明，"
    "CLI 不认证身份或自动判定规则适用语义。真正生成两组输出由外部运行器负责。"
    "绑定的是评测时当前源码/config 哈希，不证明这些配置曾用于生成给定输出；运行来源仍需维护人确认。"
    "成功来源模式只检查对已确认成功经验的断言兼容，不证明性能收益。"
)


class EvaluationError(ValueError):
    pass


def _reject_pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvaluationError(f"JSON 字段 {key!r} 重复，不能静默覆盖。")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise EvaluationError(f"JSON 不允许非有限数字 {value}。")


def _fields(value: Any, required: set[str], optional: set[str], location: str) -> dict:
    if not isinstance(value, dict):
        raise EvaluationError(f"{location} 必须是对象。")
    missing, extra = required - value.keys(), value.keys() - required - optional
    if missing or extra:
        raise EvaluationError(f"{location} 字段不合法：缺少 {sorted(missing)}；额外 {sorted(extra)}。")
    return value


def _text(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise EvaluationError(f"{location} 必须是非空字符串。")
    return value


def _identifier(value: Any, location: str) -> str:
    value = _text(value, location)
    if value != value.strip():
        raise EvaluationError(f"{location} 不能含首尾空白。")
    return value


def _hash(value: Any, location: str) -> str:
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise EvaluationError(f"{location} 必须是小写 SHA256。")
    return value


def _path(base: Path, value: Any, location: str) -> Path:
    path = Path(_text(value, location))
    return (path if path.is_absolute() else base / path).resolve()


class Artifacts:
    def __init__(self):
        self.hashes: dict[str, str] = {}

    def read(self, path: Path) -> bytes:
        path = path.resolve()
        try:
            raw = path.read_bytes()
        except OSError as error:
            raise EvaluationError(f"无法读取工件 {path}：{error}") from error
        sha = hashlib.sha256(raw).hexdigest()
        previous = self.hashes.setdefault(str(path), sha)
        if previous != sha:
            raise EvaluationError(f"评测期间工件已变化：{path}。请重新评测。")
        return raw

    def json(self, path: Path) -> Any:
        try:
            return json.loads(self.read(path).decode("utf-8-sig"), object_pairs_hook=_reject_pairs,
                              parse_constant=_reject_constant)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise EvaluationError(f"工件不是有效 UTF-8 JSON：{path}：{error}") from error

    def verify(self) -> None:
        for name in list(self.hashes):
            self.read(Path(name))

    def list(self) -> list[dict[str, str]]:
        return [{"path": name, "sha256": sha} for name, sha in sorted(self.hashes.items())]


def _index(path: Path, artifacts: Artifacts) -> tuple[dict, dict[str, int], bool]:
    index = artifacts.json(path)
    if not isinstance(index, dict) or type(index.get("schema_version")) is not int or index["schema_version"] != 2:
        raise EvaluationError(f"{path} 不是 document_io.prepare 的 schema 2 索引。")
    if index.get("demo") is not False or index.get("production_submittable") is not True:
        raise EvaluationError("经验回归评测不接受 demo 或不可提交索引。")
    source_sha = _hash(index.get("source_sha256"), "index.source_sha256")
    manifest_path = path.parent / "manifest.json"
    if manifest_path.exists():
        manifest = artifacts.json(manifest_path)
        if (not isinstance(manifest, dict) or type(manifest.get("schema_version")) is not int
                or manifest["schema_version"] != 2 or manifest.get("index_sha256") != artifacts.hashes[str(path.resolve())]
                or manifest.get("source_sha256") != source_sha):
            raise EvaluationError(f"prepare manifest 与索引或源码哈希不一致：{manifest_path}")
    source_path = _path(path.parent, index.get("source_path"), "index.source_path")
    source_available = source_path.exists()
    if source_available:
        source = artifacts.json(source_path)
        if artifacts.hashes[str(source_path)] != source_sha or source != index.get("source_document"):
            raise EvaluationError(f"索引不匹配原始输入源码：{source_path}")
    snapshot = index.get("source_document")
    try:
        locations = document_io.check_input(snapshot)
    except (ValueError, TypeError) as error:
        raise EvaluationError(f"索引原文快照不合法：{error}") from error
    expected = []
    for block, kind, row, col, original in locations:
        for start, end, tag, offset in document_io.source_segments(original):
            if tag is None and original.strip():
                raise EvaluationError("生产索引快照含缺少真实标签的非空字符串。")
            expected.append((block, kind, row, col, original, start, end, tag, offset))
    units = index.get("units")
    if not isinstance(units, list) or len(units) != len(expected):
        raise EvaluationError("索引单位数与原文切片不一致。")
    tag_order, tag_map, unit_ids = {}, {}, set()
    for order, (unit, item) in enumerate(zip(units, expected)):
        block, kind, row, col, original, start, end, tag, offset = item
        if not isinstance(unit, dict):
            raise EvaluationError("索引 unit 必须是对象。")
        unit_id = _identifier(unit.get("unit_id"), "unit.unit_id")
        if unit_id in unit_ids:
            raise EvaluationError("索引 unit_id 重复。")
        unit_ids.add(unit_id)
        checks = {"order": order, "block_index": block, "kind": kind, "row": row, "col": col,
                  "table_id": f"t{block + 1:04d}" if kind == "cell" else None,
                  "source_start": start, "source_end": end, "source_length": end - start,
                  "source_total_length": len(original), "source_text": original[start:end],
                  "text": original[start:end], "tag": tag, "tags": [tag] if tag else [],
                  "tag_offset": offset, "unit_tag_offset": offset - start if offset is not None else None,
                  "tag_is_synthetic": False}
        for key, wanted in checks.items():
            actual = unit.get(key)
            if type(actual) is not type(wanted) or actual != wanted:
                raise EvaluationError(f"索引单位 {unit_id} 的 {key} 与原始切片不一致。")
        if tag:
            if tag in tag_order:
                raise EvaluationError(f"索引真实标签重复：{tag}")
            tag_order[tag], tag_map[tag] = order, unit_id
    if index.get("tag_to_unit") != tag_map:
        raise EvaluationError("索引 tag_to_unit 与原文真实标签不一致。")
    return index, tag_order, source_available


def _tag_list(value: Any, order: dict[str, int], location: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or (nonempty and not value):
        raise EvaluationError(f"{location} 必须是{'非空' if nonempty else ''}标签数组。")
    seen = set()
    for tag in value:
        if not isinstance(tag, str) or not TAG.fullmatch(tag) or tag not in order:
            raise EvaluationError(f"{location} 含无效或不存在的标签：{tag!r}")
        if tag in seen:
            raise EvaluationError(f"{location} 标签重复：{tag}")
        seen.add(tag)
    return value


def _assertions(values: Any, order: dict[str, int], location: str) -> list[dict]:
    if not isinstance(values, list) or not values:
        raise EvaluationError(f"{location} 必须是非空人工断言数组。")
    for number, item in enumerate(values):
        prefix = f"{location}[{number}]"
        _fields(item, {"object_key", "anchor_tag", "required_tags", "forbidden_tags"},
                {"quantity", "clause_category"}, prefix)
        _identifier(item["object_key"], prefix + ".object_key")
        anchor = item["anchor_tag"]
        _tag_list([anchor], order, prefix + ".anchor_tag")
        required = _tag_list(item["required_tags"], order, prefix + ".required_tags", nonempty=True)
        forbidden = _tag_list(item["forbidden_tags"], order, prefix + ".forbidden_tags")
        if anchor not in required:
            raise EvaluationError(f"{prefix}.required_tags 必须包含 anchor_tag。")
        if set(required) & set(forbidden):
            raise EvaluationError(f"{prefix} 的 required_tags 与 forbidden_tags 不能相交。")
        if "quantity" in item:
            _text(item["quantity"], prefix + ".quantity")
        if "clause_category" in item and (not isinstance(item["clause_category"], str)
                                          or item["clause_category"] not in result_validator.CLAUSE_TYPES):
            raise EvaluationError(f"{prefix}.clause_category 必须为 normal/general/separate。")
    return values


def _evaluate_assertion(assertion: dict, order: dict[str, int], records: list[dict], structure: dict) -> dict:
    if structure["ok"] is not True:
        return {"passed": False, "subject_id": None, "errors": [{"code": "INVALID_STRUCTURE"}]}
    spans = {record["id"]: (order[record["定位标识"][0]], order[record["定位标识"][1]]) for record in records}
    anchor = order[assertion["anchor_tag"]]
    subjects = [r for r in records if r["条款分类"] in ("normal", "separate")
                and spans[r["id"]][0] <= anchor <= spans[r["id"]][1]]
    if len(subjects) != 1:
        return {"passed": False, "subject_id": None,
                "errors": [{"code": "MISSING_OBJECT" if not subjects else "AMBIGUOUS_OBJECT",
                            "matching_ids": [r["id"] for r in subjects]}]}
    subject = subjects[0]
    objects = {r["id"]: r for r in records if r["条款分类"] in ("normal", "separate")}
    adjacent = {key: set() for key in objects}
    for record in objects.values():
        if record["条款分类"] == "separate":
            for target in record["相关条款"]:
                if target in objects:
                    adjacent[record["id"]].add(target)
                    adjacent[target].add(record["id"])
    component, pending = set(), [subject["id"]]
    while pending:
        current = pending.pop()
        if current not in component:
            component.add(current)
            pending.extend(adjacent[current] - component)
    # 只加入 general -> 同产品 normal/separate 的直接入边，不沿 general 递归到其它设备。
    generals = {r["id"] for r in records if r["条款分类"] == "general" and set(r["相关条款"]) & component}
    effective = component | generals
    contains = lambda tag: any(spans[key][0] <= order[tag] <= spans[key][1] for key in effective)
    missing = [tag for tag in assertion["required_tags"] if not contains(tag)]
    polluted = [tag for tag in assertion["forbidden_tags"] if contains(tag)]
    errors = []
    if missing:
        errors.append({"code": "REQUIRED_TAG_MISSING", "tags": missing})
    if polluted:
        errors.append({"code": "FORBIDDEN_TAG_INCLUDED", "tags": polluted})
    if "quantity" in assertion and subject["产品数量"] != assertion["quantity"]:
        errors.append({"code": "QUANTITY_MISMATCH", "expected": assertion["quantity"], "actual": subject["产品数量"]})
    if "clause_category" in assertion and subject["条款分类"] != assertion["clause_category"]:
        errors.append({"code": "CLAUSE_CATEGORY_MISMATCH", "expected": assertion["clause_category"], "actual": subject["条款分类"]})
    return {"passed": not errors, "subject_id": subject["id"], "object_clause_ids": sorted(component),
            "general_clause_ids": sorted(generals), "errors": errors}


def _development_sources(project_root: Path, payload: dict, store: Any, learning: Any,
                         artifacts: Artifacts) -> tuple[list[dict], list[dict]]:
    """Normalize confirmed sources while preserving the legacy correction API."""
    corrections, source_ids = payload.get("correction_ids"), payload.get("source_ids", [])
    for values, name in ((corrections, "correction_ids"), (source_ids, "source_ids")):
        if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip()
                                              or item != item.strip() for item in values):
            raise EvaluationError(f"candidate.{name} 必须是不含空白编号的字符串数组。")
        if len(set(values)) != len(values):
            raise EvaluationError(f"candidate.{name} 不能重复。")
    if not corrections and not source_ids:
        raise EvaluationError("candidate 必须有 correction_ids 或已确认 source_ids 来源。")
    if source_ids:
        module = learning if learning is not None else importlib.import_module("memory_learning")
        if getattr(module, "__file__", None):
            artifacts.read(Path(module.__file__))
        sources = module.evaluation_sources(project_root, payload)
        if not isinstance(sources, list) or not sources:
            raise EvaluationError("已确认评测来源必须是非空数组。")
    else:
        sources = []
        for cid in corrections:
            correction = store.get_correction(project_root, cid)
            if not isinstance(correction, dict):
                raise EvaluationError(f"纠错记录 {cid} 不合法。")
            sources.append({**correction, "source_id": cid, "kind": "correction", "correction_id": cid})
    normalized, confirmations, seen, seen_corrections = [], [], set(), set()
    for source in sources:
        if not isinstance(source, dict):
            raise EvaluationError("评测来源必须是对象。")
        sid = _identifier(source.get("source_id"), "source.source_id")
        if sid in seen:
            raise EvaluationError(f"评测来源编号重复：{sid}")
        seen.add(sid)
        kind = source.get("kind")
        if kind not in ("correction", "success"):
            raise EvaluationError("source.kind 必须为 correction/success。")
        family = _identifier(source.get("family_id"), "source.family_id")
        source_sha = _hash(source.get("source_sha256"), "source.source_sha256")
        binding = {"source_id": sid, "kind": kind, "family_id": family, "source_sha256": source_sha}
        cid = source.get("correction_id")
        if cid is not None and cid in corrections:
            seen_corrections.add(cid)
        if sid in source_ids:
            confirmation = source.get("confirmation")
            if source.get("confirmed") is not True or not isinstance(confirmation, dict):
                raise EvaluationError(f"来源 {sid} 尚无有效人工确认记录。")
            _text(confirmation.get("confirmed_by"), "source.confirmation.confirmed_by")
            _text(confirmation.get("note"), "source.confirmation.note")
            if confirmation.get("family_id") != family:
                raise EvaluationError(f"来源 {sid} 的确认 family 与来源不一致。")
            confirmation_sha = _hash(source.get("confirmation_sha256"), "source.confirmation_sha256")
            actual_sha = hashlib.sha256(json.dumps(confirmation, ensure_ascii=False, sort_keys=True,
                                                   separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
            if confirmation_sha != actual_sha:
                raise EvaluationError(f"来源 {sid} 的人工确认记录哈希不一致。")
            confirmations.append({"source_id": sid, "confirmation_sha256": confirmation_sha})
            binding["confirmation_sha256"] = confirmation_sha
        elif cid not in corrections or kind != "correction":
            raise EvaluationError(f"来源 {sid} 不属于候选 source_ids/correction_ids。")
        source_artifacts = source.get("artifacts", [])
        if not isinstance(source_artifacts, list) or (sid in source_ids and not source_artifacts):
            raise EvaluationError(f"来源 {sid} 必须绑定不可变工件。")
        for item in source_artifacts:
            _fields(item, {"path", "sha256"}, set(), "source.artifacts[]")
            path = _path(project_root, item["path"], "source.artifacts[].path")
            expected = _hash(item["sha256"], "source.artifacts[].sha256")
            if hashlib.sha256(artifacts.read(path)).hexdigest() != expected:
                raise EvaluationError(f"来源不可变工件已变化：{path}")
        input_name = source.get("input_path")
        if input_name is not None:
            input_path = _path(project_root, input_name, "source.input_path")
            if (sid in source_ids or input_path.exists()) and hashlib.sha256(artifacts.read(input_path)).hexdigest() != source_sha:
                raise EvaluationError(f"开发来源输入已变化：{input_path}，请重新确认来源。")
        elif sid in source_ids:
            raise EvaluationError(f"来源 {sid} 缺少 input_path。")
        if kind == "success":
            result_path = _path(project_root, source.get("result_path"), "source.result_path")
            result_sha = _hash(source.get("result_sha256"), "source.result_sha256")
            if hashlib.sha256(artifacts.read(result_path)).hexdigest() != result_sha:
                raise EvaluationError(f"成功来源输出已变化：{result_path}")
            binding["result_path"] = str(result_path)
            binding["result_sha256"] = result_sha
        normalized.append(binding)
    if seen_corrections != set(corrections) or {item["source_id"] for item in confirmations} != set(source_ids):
        raise EvaluationError("评测来源未精确覆盖候选 correction_ids/source_ids。")
    return sorted(normalized, key=lambda item: item["source_id"]), sorted(confirmations, key=lambda item: item["source_id"])


def evaluate_manifest(project_root: Path, memory_id: str, revision: int, manifest_path: Path,
                      *, store: Any = None, learning: Any = None) -> dict[str, Any]:
    memory_id = _identifier(memory_id, "memory_id")
    if type(revision) is not int or revision < 1:
        raise EvaluationError("revision 必须是正整数，不能是布尔值。")
    project_root, manifest_path = Path(project_root).resolve(), Path(manifest_path).resolve()
    store = store if store is not None else importlib.import_module("memory_store")
    candidate = store.load_candidate(project_root, memory_id, revision)
    if not isinstance(candidate, dict) or not isinstance(candidate.get("payload"), dict):
        raise EvaluationError("经验候选记录不合法。")
    candidate_sha = _hash(candidate.get("content_sha256"), "candidate.content_sha256")
    if candidate.get("memory_id") != memory_id or type(candidate.get("revision")) is not int or candidate["revision"] != revision:
        raise EvaluationError("加载的候选 identity 与请求不一致。")
    base_release = _hash(store.release_digest(project_root), "base_release_sha256")
    artifacts = Artifacts()
    for module in (sys.modules[__name__], document_io, result_validator):
        artifacts.read(Path(module.__file__))
    for relative in (".opencode/skills/term-extract/SKILL.md",
                     ".opencode/skills/term-extract/references/business-rules.md",
                     ".opencode/agents/term-extractor.md", "opencode.json",
                     ".opencode/tools/term_memory.ts",
                     ".opencode/skills/term-extract/scripts/commit_result.py",
                     ".opencode/skills/term-extract/scripts/memory_learning.py"):
        path = project_root / relative
        if path.exists():
            artifacts.read(path)
    sources, source_confirmations = _development_sources(project_root, candidate["payload"], store, learning, artifacts)
    correction_pairs = {(item["family_id"], item["source_sha256"]) for item in sources if item["kind"] == "correction"}
    success_pairs = {(item["family_id"], item["source_sha256"]) for item in sources if item["kind"] == "success"}
    development_families = {item["family_id"] for item in sources}
    development_sources = {item["source_sha256"] for item in sources}
    evaluation_mode = "correction" if correction_pairs else "success"
    manifest = artifacts.json(manifest_path)
    _fields(manifest, {"schema_version", "cases"}, set(), "manifest")
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != VERSION:
        raise EvaluationError("manifest.schema_version 必须为整数 1。")
    raw_cases = manifest["cases"]
    if not isinstance(raw_cases, list) or not raw_cases:
        raise EvaluationError("manifest.cases 必须是非空数组。")
    case_ids, source_families, family_splits = set(), {}, {}
    reports, structural_failures = [], []
    for number, case in enumerate(raw_cases):
        prefix = f"manifest.cases[{number}]"
        _fields(case, {"case_id", "family_id", "role", "split", "confirmed_by", "index_path",
                       "baseline_result_path", "candidate_result_path", "assertions"}, {"scope_note"}, prefix)
        case_id = _identifier(case["case_id"], prefix + ".case_id")
        family = _identifier(case["family_id"], prefix + ".family_id")
        _text(case["confirmed_by"], prefix + ".confirmed_by")
        if case_id in case_ids:
            raise EvaluationError(f"case_id 重复：{case_id}")
        case_ids.add(case_id)
        role, split = case["role"], case["split"]
        if not isinstance(role, str) or role not in ("fix", "success", "regression", "counterexample"):
            raise EvaluationError(f"{prefix}.role 不合法。")
        if not isinstance(split, str) or split not in ("development", "regression"):
            raise EvaluationError(f"{prefix}.split 只接受 development/regression；禁止 final/holdout。")
        if (role in ("fix", "success") and split != "development") or (role not in ("fix", "success") and split != "regression"):
            raise EvaluationError(f"{prefix} 的 fix/success 必须属于 development，其他角色必须属于 regression。")
        if "scope_note" in case:
            _text(case["scope_note"], prefix + ".scope_note")
        if role == "counterexample":
            _text(case.get("scope_note"), prefix + ".scope_note（人工说明为何不适用）")
        index_path = _path(manifest_path.parent, case["index_path"], prefix + ".index_path")
        index, order, source_available = _index(index_path, artifacts)
        source_sha = index["source_sha256"]
        if source_sha in source_families and source_families[source_sha] != family:
            raise EvaluationError("同一 source_sha256 被声明为不同 family，存在数据泄露。")
        source_families[source_sha] = family
        if family in family_splits and family_splits[family] != split:
            raise EvaluationError("同一 family 跨 development/regression，存在数据泄露。")
        family_splits[family] = split
        independent = family not in development_families and source_sha not in development_sources
        if role == "fix" and (family, source_sha) not in correction_pairs:
            raise EvaluationError(f"fix {case_id} 的 family/source 未被此候选的已确认纠错来源绑定。")
        if role == "success" and (family, source_sha) not in success_pairs:
            raise EvaluationError(f"success {case_id} 的 family/source 未被此候选的已确认成功来源绑定。")
        if role not in ("fix", "success") and not independent:
            raise EvaluationError(f"{case_id} 与开发纠错 family/source 不独立。")
        assertions = _assertions(case["assertions"], order, prefix + ".assertions")
        if role == "counterexample" and not any(item["forbidden_tags"] or "clause_category" in item for item in assertions):
            raise EvaluationError("counterexample 至少需要 forbidden_tags 或 clause_category 边界断言。")
        result_paths = {side: _path(manifest_path.parent, case[f"{side}_result_path"], prefix + f".{side}_result_path")
                        for side in ("baseline", "candidate")}
        results = {side: artifacts.json(path) for side, path in result_paths.items()}
        success_source = None
        if role == "success":
            matching = [item for item in sources if item["kind"] == "success"
                        and (item["family_id"], item["source_sha256"]) == (family, source_sha)]
            success_source = next((item for item in matching
                                   if artifacts.json(Path(item["result_path"])) == results["baseline"]), None)
            if success_source is None:
                raise EvaluationError(f"success {case_id} 的 baseline 必须与对应来源已确认的结果快照一致。")
        validations = {side: result_validator.validate_result(index, value) for side, value in results.items()}
        for side, validation in validations.items():
            if validation["ok"] is not True:
                structural_failures.append({"code": "INVALID_RESULT_STRUCTURE", "case_id": case_id, "side": side})
        evaluated = []
        for assertion_no, assertion in enumerate(assertions):
            before = _evaluate_assertion(assertion, order, results["baseline"], validations["baseline"])
            after = _evaluate_assertion(assertion, order, results["candidate"], validations["candidate"])
            evaluated.append({"assertion_index": assertion_no, **assertion, "baseline": before, "candidate": after,
                              "regressed": before["passed"] and not after["passed"],
                              "improved": validations["baseline"]["ok"] and not before["passed"] and after["passed"]})
        case_report = {"case_id": case_id, "family_id": family, "role": role, "split": split,
                        "confirmed_by": case["confirmed_by"], "scope_note": case.get("scope_note"),
                        "source_sha256": source_sha, "original_source_verified": source_available,
                        "independent_of_development": independent, "index_path": str(index_path),
                        "baseline_result_path": str(result_paths["baseline"]), "candidate_result_path": str(result_paths["candidate"]),
                        "baseline_validation": validations["baseline"], "candidate_validation": validations["candidate"],
                        "baseline_passed": all(a["baseline"]["passed"] for a in evaluated),
                        "candidate_passed": all(a["candidate"]["passed"] for a in evaluated), "assertions": evaluated}
        if success_source is not None:
            case_report.update(success_source_id=success_source["source_id"],
                               success_result_sha256=success_source["result_sha256"])
        reports.append(case_report)
    summary = {
        "fix_improvements": sum(c["role"] == "fix" and c["baseline_validation"]["ok"]
                                and not c["baseline_passed"] and c["candidate_passed"] for c in reports),
        "success_cases": sum(c["role"] == "success" and c["baseline_passed"] and c["candidate_passed"] for c in reports),
        "regressions": sum(a["regressed"] for c in reports for a in c["assertions"]),
        "independent_regression_cases": sum(c["role"] not in ("fix", "success") and c["independent_of_development"]
                                            and c["baseline_passed"] and c["candidate_passed"] for c in reports),
        "counterexample_cases": sum(c["role"] == "counterexample" and c["independent_of_development"]
                                    and c["baseline_passed"] and c["candidate_passed"] for c in reports),
        "unfixed_target_cases": sum(c["role"] in ("fix", "success") and not c["candidate_passed"] for c in reports),
    }
    gates = list(structural_failures)
    gates.extend({"code": "SUCCESS_BASELINE_FAILED", "case_id": c["case_id"]}
                 for c in reports if c["role"] == "success" and not c["baseline_passed"])
    target_condition = ((summary["fix_improvements"] >= 1, "NO_FIX_IMPROVEMENT") if evaluation_mode == "correction"
                        else (summary["success_cases"] >= 1, "NO_PASSING_SUCCESS_CASE"))
    conditions = [target_condition,
                  (summary["regressions"] == 0, "REGRESSION_FOUND"),
                  (summary["independent_regression_cases"] >= 1, "NO_PASSING_INDEPENDENT_REGRESSION"),
                  (summary["counterexample_cases"] >= 1, "NO_PASSING_COUNTEREXAMPLE"),
                  (summary["unfixed_target_cases"] == 0, "UNFIXED_TARGET")]
    gates.extend({"code": code} for passed, code in conditions if not passed)
    # 不缓存旧输出；注册前再次检查候选、发布基线和全部可变工件。
    current = store.load_candidate(project_root, memory_id, revision)
    if current.get("content_sha256") != candidate_sha or store.release_digest(project_root) != base_release:
        raise EvaluationError("评测期间候选内容或已发布基线发生变化，请重新评测。")
    current_sources, current_confirmations = _development_sources(project_root, candidate["payload"], store, learning, artifacts)
    if current_sources != sources or current_confirmations != source_confirmations:
        raise EvaluationError("评测期间来源或人工确认发生变化，请重新评测。")
    artifacts.verify()
    report = {"candidate": {"memory_id": memory_id, "revision": revision, "content_sha256": candidate_sha},
              "evaluation_mode": evaluation_mode, "source_confirmations": source_confirmations,
              "base_release_sha256": base_release, "status": "failed" if gates else "passed", "summary": summary,
              "cases": reports, "artifacts": artifacts.list(), "gate_errors": gates,
              "evaluator": {"version": VERSION, "mode": "offline_assertions_only"}, "notice": NOTICE}
    if len(json.dumps(report, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")) > MAX_REPORT_BYTES:
        raise EvaluationError("报告超过 900 KiB 安全预算，未注册。请缩小本轮 manifest 的断言/案例集合，并保留 fix、独立回归及反例。")
    registration = store.register_evaluation(project_root, memory_id, revision, report)
    if not isinstance(registration, dict):
        raise EvaluationError("评测报告注册器没有返回有效记录。")
    evaluation_id = _identifier(registration.get("evaluation_id"), "evaluation_id")
    report_sha = _hash(registration.get("report_sha256"), "report_sha256")
    return {**report, "evaluation_id": evaluation_id, "report_sha256": report_sha}


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="离线评测给定 baseline/candidate 工件的人工断言，不运行模型。")
    parser.add_argument("--project-root", required=True, type=Path)
    parser.add_argument("--memory-id", required=True)
    parser.add_argument("--revision", required=True, type=int)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        report = evaluate_manifest(args.project_root, args.memory_id, args.revision, args.manifest)
        print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        return 0 if report["status"] == "passed" else 2
    except (EvaluationError, OSError, ValueError, ImportError, KeyError, TypeError) as error:
        print(json.dumps({"status": "failed", "error": str(error), "notice": NOTICE}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
