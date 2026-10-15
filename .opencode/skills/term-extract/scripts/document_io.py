#!/usr/bin/env python3
"""标书原文的无模型准备、分页读取与搜索；只使用 Python 标准库。"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 2
MAX_BYTES = 24 * 1024
MAX_IPC_BYTES = 1024 * 1024
MAX_COMPACT_UNITS = 512
TAG_RE = re.compile(r"<C[0-9]+>")


class DocumentError(ValueError):
    """可直接向使用者说明的输入或完整性错误。"""


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def sized_bytes(value: dict[str, Any]) -> bytes:
    """byte_size 包括完整 JSON、转义、缩进和最后的换行。"""
    value["byte_size"] = 0
    while True:
        result = json_bytes(value)
        if len(result) == value["byte_size"]:
            return result
        value["byte_size"] = len(result)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_json(path: Path) -> tuple[Any, bytes]:
    try:
        raw = path.read_bytes()
        value = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise DocumentError(f"无法读取 UTF-8 JSON 文件 {path}：{error}") from error
    return value, raw


def check_input(source: Any) -> list[tuple[int, str, int | None, int | None, str]]:
    if not isinstance(source, list):
        raise DocumentError("输入必须是按原文顺序排列的 text/table 对象数组。")
    locations = []
    for block_no, block in enumerate(source):
        if not isinstance(block, dict) or ("text" in block) == ("table" in block):
            raise DocumentError(f"块 {block_no} 必须且只能包含 text 或 table。")
        if "text" in block:
            if not isinstance(block["text"], str):
                raise DocumentError(f"块 {block_no} 的 text 必须是字符串。")
            locations.append((block_no, "text", None, None, block["text"]))
            continue
        table = block["table"]
        if not isinstance(table, dict) or not isinstance(table.get("rows"), list):
            raise DocumentError(f"块 {block_no} 的 table.rows 必须是二维字符串数组。")
        if not isinstance(table.get("merges", []), list):
            raise DocumentError(f"块 {block_no} 的 table.merges 必须是数组。")
        for row_no, row in enumerate(table["rows"]):
            if not isinstance(row, list):
                raise DocumentError(f"块 {block_no} 的第 {row_no} 行必须是数组。")
            for col_no, cell in enumerate(row):
                if not isinstance(cell, str):
                    raise DocumentError(f"块 {block_no}、行 {row_no}、列 {col_no} 必须是字符串。")
                locations.append((block_no, "cell", row_no, col_no, cell))
    return locations


def source_segments(text: str) -> list[tuple[int, int, str | None, int | None]]:
    """按标签出现顺序分片；首片含 prefix，单标签仍对应整个字符串。"""
    matches = list(TAG_RE.finditer(text))
    if not matches:
        return [(0, len(text), None, None)]
    return [(0 if position == 0 else match.start(),
             matches[position + 1].start() if position + 1 < len(matches) else len(text),
             match.group(), match.start())
            for position, match in enumerate(matches)]


def prepare(input_path: Path, out_dir: Path, demo_tags: bool = False) -> dict[str, Any]:
    input_path = input_path.resolve()
    out_dir = out_dir.resolve()
    source, source_bytes = load_json(input_path)
    locations = check_input(source)
    found: dict[str, str] = {}
    missing = []
    for block_no, kind, row, col, text in locations:
        place = f"块 {block_no}" if kind == "text" else f"块 {block_no}、行 {row}、列 {col}"
        matches = list(TAG_RE.finditer(text))
        for match in matches:
            tag = match.group()
            occurrence = f"{place}、字符 {match.start()}"
            if tag in found:
                raise DocumentError(f"标签 {tag} 重复：{found[tag]} 与 {occurrence}。标签必须全篇唯一。")
            found[tag] = occurrence
        if not matches and text.strip():
            missing.append(place)
    if missing and not demo_tags:
        sample = "；".join(missing[:5])
        raise DocumentError(f"生产输入有 {len(missing)} 个非空段落/单元格缺少 <C数字> 标签（{sample}）。请补充真实定位标签；仅演示可显式使用 --demo-tags。")
    if not found and not demo_tags:
        raise DocumentError("生产输入没有任何 Cxx 标签。请提供真实标签；--demo-tags 仅用于演示且禁止生产提交。")

    working = copy.deepcopy(source)
    units: list[dict[str, Any]] = []
    tag_to_unit: dict[str, str] = {}
    by_location: dict[tuple[int, int | None, int | None], list[str]] = {}
    next_demo = 1
    for block_no, kind, row, col, original_text in locations:
        segments = source_segments(original_text)
        synthetic_tag = None
        if segments[0][2] is None and demo_tags:
            while f"<C{next_demo:06d}>" in found:
                next_demo += 1
            synthetic_tag = f"<C{next_demo:06d}>"
            found[synthetic_tag] = "演示生成"
            next_demo += 1
            working_text = synthetic_tag + "\n" + original_text
            if kind == "text":
                working[block_no]["text"] = working_text
            else:
                working[block_no]["table"]["rows"][row][col] = working_text
        location_ids = []
        for segment_no, (start, end, native_tag, tag_offset) in enumerate(segments):
            tag = native_tag or synthetic_tag
            source_text = original_text[start:end]
            order = len(units)
            unit_id = f"u{order + 1:06d}"
            unit = {
                "unit_id": unit_id, "order": order, "block_index": block_no,
                "kind": kind, "text": synthetic_tag + "\n" + source_text if synthetic_tag else source_text,
                "source_text": source_text, "source_length": len(source_text),
                "source_start": start, "source_end": end, "source_total_length": len(original_text),
                "source_segment_index": segment_no, "source_segment_count": len(segments),
                "tag_offset": tag_offset, "unit_tag_offset": tag_offset - start if tag_offset is not None else None,
                "tag_is_synthetic": synthetic_tag is not None, "tag": tag, "tags": [tag] if tag else [],
                "table_id": f"t{block_no + 1:04d}" if kind == "cell" else None,
                "row": row, "col": col,
            }
            units.append(unit)
            location_ids.append(unit_id)
            if tag:
                tag_to_unit[tag] = unit_id
        by_location[(block_no, row, col)] = location_ids
    tables = []
    for block_no, block in enumerate(source):
        if "table" in block:
            table = block["table"]
            tables.append({
                "table_id": f"t{block_no + 1:04d}", "block_index": block_no,
                "rows": copy.deepcopy(table["rows"]), "merges": copy.deepcopy(table.get("merges", [])),
                "unit_ids": [[by_location[(block_no, row_no, col_no)][0] for col_no in range(len(row))]
                             for row_no, row in enumerate(table["rows"])],
                "cell_unit_ids": [[by_location[(block_no, row_no, col_no)] for col_no in range(len(row))]
                                  for row_no, row in enumerate(table["rows"])],
            })
    index = {
        "schema_version": SCHEMA_VERSION, "source_path": str(input_path),
        "source_sha256": digest(source_bytes), "demo": demo_tags,
        "production_submittable": not demo_tags,
        "tag_position_policy": "tag_start_slices_with_prefix_first_single_tag_whole_string",
        "source_document": source, "units": units, "tables": tables,
        "tag_to_unit": tag_to_unit,
    }
    destinations = [out_dir / "index.json", out_dir / "manifest.json"]
    if demo_tags:
        destinations.append(out_dir / "source.demo.json")
    if input_path in destinations:
        raise DocumentError("输出路径会覆盖输入文件，请使用独立工作目录。")
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / "index.json"
    index_content = json_bytes(index)
    index_path.write_bytes(index_content)
    index_hash = digest(index_content)
    if demo_tags:
        (out_dir / "source.demo.json").write_bytes(json_bytes(working))
    views = []
    cursor = "0"
    while True:
        page = make_page(index, index_hash, list(range(len(units))), cursor, MAX_BYTES)
        view_path = out_dir / f"view-{len(views):04d}.json"
        if view_path == input_path:
            raise DocumentError("分页输出路径会覆盖输入文件，请使用独立工作目录。")
        view_path.write_bytes(sized_bytes(page))
        views.append({"path": str(view_path), "cursor": page["cursor"],
                      "next_cursor": page["next_cursor"], "byte_size": page["byte_size"],
                      "unit_ids": page["unit_ids"], "tags": page["tags"],
                      "complete_tags": page["complete_tags"], "partial_tags": page["partial_tags"]})
        if not page["has_more"]:
            break
        if page["next_cursor"] == cursor:
            raise DocumentError("分页没有前进，准备过程已停止。")
        cursor = page["next_cursor"]
    manifest = {
        "schema_version": SCHEMA_VERSION, "source_path": str(input_path),
        "source_sha256": digest(source_bytes), "index_path": str(index_path),
        "index_sha256": index_hash, "demo": demo_tags, "production_submittable": not demo_tags,
        "unit_count": len(units), "tag_count": len(tag_to_unit),
        "max_view_bytes": MAX_BYTES, "coverage_kind": "full_original", "views": views,
        "demo_source_path": str(out_dir / "source.demo.json") if demo_tags else None,
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_bytes(json_bytes(manifest))
    if input_path.read_bytes() != source_bytes:
        raise DocumentError("准备过程中源文件发生变化，输出不可提交，请重新准备。")
    result = {"schema_version": SCHEMA_VERSION, "index_path": str(index_path), "manifest_path": str(manifest_path),
              "view_paths": [v["path"] for v in views], "view_count": len(views),
              "view_paths_complete": True, "demo": demo_tags,
              "production_submittable": not demo_tags, "index_hash": index_hash,
              "unit_count": len(units), "tag_count": len(tag_to_unit)}
    if len(sized_bytes(result)) > MAX_BYTES:
        result["view_paths"] = result["view_paths"][:4]
        result["view_paths_complete"] = False
        result["all_view_paths_in_manifest"] = True
    if len(sized_bytes(result)) > MAX_BYTES:
        raise DocumentError("输出路径元数据超过 24 KiB，请使用较短的独立工作目录。")
    return result


def load_index(path: Path) -> tuple[dict[str, Any], str]:
    index, raw = load_json(path)
    if not isinstance(index, dict) or index.get("schema_version") != SCHEMA_VERSION:
        raise DocumentError("索引版本不兼容，请使用 prepare 重新生成。")
    if not isinstance(index.get("units"), list) or not isinstance(index.get("tag_to_unit"), dict):
        raise DocumentError("索引缺少 units 或 tag_to_unit。")
    index_hash = digest(raw)
    manifest_path = path.parent / "manifest.json"
    if manifest_path.exists():
        manifest, _ = load_json(manifest_path)
        if manifest.get("schema_version") != SCHEMA_VERSION:
            raise DocumentError("manifest 版本不兼容，请使用 prepare 重新生成。")
        if manifest.get("index_sha256") != index_hash:
            raise DocumentError("索引 SHA256 与 manifest 不一致，禁止使用被修改的索引。")
    return index, index_hash


def parse_cursor(cursor: str, selected: list[int], units: list[dict[str, Any]]) -> tuple[int, int]:
    if cursor == "0":
        return 0, 0
    if not re.fullmatch(r"[0-9]+:[0-9]+", cursor):
        raise DocumentError("cursor 必须为 0 或工具返回的 '单位位置:字符偏移'。")
    position, offset = map(int, cursor.split(":"))
    if position > len(selected) or (position == len(selected) and offset != 0):
        raise DocumentError("cursor 超出此次读取/搜索范围。请保持分页期间的范围或查询条件不变。")
    if position < len(selected) and offset > len(units[selected[position]]["source_text"]):
        raise DocumentError("cursor 字符偏移超过该单位原文长度。")
    return position, offset


def table_context(index: dict[str, Any], parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """仅在已证明 value 等于原锚点单元格时引用该值；其他值完整返回。"""
    contexts = []
    for table in index["tables"]:
        visible_rows = {p["row"] for p in parts if p["table_id"] == table["table_id"]}
        if not visible_rows:
            continue
        merges = []
        for merge_no, merge in enumerate(table["merges"]):
            if not isinstance(merge, dict):
                merges.append({"merge_index": merge_no, "raw_metadata": copy.deepcopy(merge)})
                continue
            top_left = merge.get("top_left", {})
            row = top_left.get("row") if isinstance(top_left, dict) else None
            span = merge.get("row_span", 1)
            if isinstance(row, int) and isinstance(span, int) and not any(row <= r < row + span for r in visible_rows):
                continue
            layout = copy.deepcopy(merge)
            layout["merge_index"] = merge_no
            col = top_left.get("col") if isinstance(top_left, dict) else None
            if isinstance(row, int) and isinstance(col, int) and 0 <= row < len(table["rows"]) and 0 <= col < len(table["rows"][row]):
                anchors = table["cell_unit_ids"][row][col]
                layout["anchor_unit_ids"] = anchors
                if len(anchors) == 1:
                    layout["anchor_unit_id"] = anchors[0]
                else:
                    layout.pop("anchor_unit_id", None)
                anchor_text = table["rows"][row][col]
                value = merge.get("value")
                if isinstance(value, str) and TAG_RE.sub("", value).strip() == TAG_RE.sub("", anchor_text).strip():
                    layout.pop("value", None)
                    layout["value_in_anchor"] = True
                    layout["duplicate_proof"] = "exact_after_removing_Cxx_and_edge_whitespace"
            if "value" in layout:
                layout["value_in_anchor"] = False
                layout["value_source"] = "merge_metadata"
            layout["value_preserved_in_index"] = True
            merges.append(layout)
        contexts.append({"table_id": table["table_id"], "block_index": table["block_index"],
                         "row_count": len(table["rows"]), "visible_rows": sorted(visible_rows),
                         "column_counts": sorted({len(r) for r in table["rows"]}),
                         "merges": merges, "original_merges_in_index": True})
    return contexts


def compact_table_context(contexts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """模型只接收布局/实际合并内容；锚点 unit IDs 和去重证明留在回执侧。"""
    internal = {"merge_index", "anchor_unit_id", "anchor_unit_ids", "value_in_anchor",
                "duplicate_proof", "value_source", "value_preserved_in_index"}
    projected = []
    for context in contexts:
        merges = []
        for merge in context["merges"]:
            item = {key: value for key, value in merge.items() if key not in internal}
            if merge.get("value_in_anchor"):
                item["value_from_cell"] = merge["top_left"]
            merges.append(item)
        projected.append({"table_id": context["table_id"], "block_index": context["block_index"],
                          "row_count": context["row_count"], "column_counts": context["column_counts"],
                          "merges": merges})
    return projected


def piece_groups(pieces: list[tuple[dict[str, Any], int, int]], view: str) -> list[list[tuple[dict[str, Any], int, int]]]:
    groups = []
    for piece in pieces:
        unit, start, _ = piece
        if groups and view == "compact":
            previous, _, previous_end = groups[-1][-1]
            location = ("block_index", "kind", "table_id", "row", "col")
            same_location = all(unit[key] == previous[key] for key in location)
            contiguous = unit["source_start"] + start == previous["source_start"] + previous_end
            if same_location and contiguous:
                groups[-1].append(piece)
                continue
        groups.append([piece])
    return groups


def piece_header(group: list[tuple[dict[str, Any], int, int]], view: str) -> str:
    unit, start, end = group[0]
    if view == "machine":
        position = f"block={unit['block_index']}"
        if unit["table_id"]:
            position += f" table={unit['table_id']} row={unit['row']} col={unit['col']}"
        label = "full" if start == 0 and end == len(unit["source_text"]) else "partial"
        return f"[{unit['unit_id']} {unit['tag'] or '(empty/no-tag)'} {position} part={label} chars={start}:{end}/{len(unit['source_text'])}]\n"
    label = f"P b{unit['block_index']}"
    if unit["table_id"]:
        label = f"T {unit['table_id']} b{unit['block_index']} r{unit['row']} c{unit['col']}"
    last, _, last_end = group[-1]
    source_start = unit["source_start"] + start
    source_end = last["source_start"] + last_end
    total = unit["source_total_length"]
    if source_start != 0 or source_end != total:
        label += f" chars={source_start}:{source_end}/{total}"
    return f"[{label}]\n"


def build_response(index: dict[str, Any], index_hash: str, cursor: str,
                   pieces: list[tuple[dict[str, Any], int, int]], next_cursor: str | None,
                   search_info: dict[str, Any] | None = None, view: str = "machine") -> dict[str, Any]:
    parts, rendered, tags, complete, partial = [], "", [], [], []
    for group in piece_groups(pieces, view):
        rendered += piece_header(group, view)
        for unit, start, end in group:
            tag = unit["tag"]
            total = len(unit["source_text"])
            content_start = len(rendered)
            rendered += unit["source_text"][start:end]
            content_end = len(rendered)
            parts.append({"unit_id": unit["unit_id"], "tag": tag, "tags": unit["tags"],
                          "order": unit["order"], "block_index": unit["block_index"], "kind": unit["kind"],
                          "table_id": unit["table_id"], "row": unit["row"], "col": unit["col"],
                          "source_start": unit["source_start"], "source_end": unit["source_end"],
                          "source_total_length": unit["source_total_length"],
                          "source_part_start": unit["source_start"] + start, "source_part_end": unit["source_start"] + end,
                          "tag_offset": unit["tag_offset"], "unit_tag_offset": unit["unit_tag_offset"],
                          "part_start": start, "part_end": end, "total_length": total,
                          "starts_unit": start == 0, "ends_unit": end == total,
                          "content_start": content_start, "content_end": content_end})
            if tag:
                tags.append(tag)
                (complete if start == 0 and end == total else partial).append(tag)
        rendered += "\n"
    result = {"schema_version": SCHEMA_VERSION, "mode": "search" if search_info else "read",
              "index_hash": index_hash, "coverage_kind": "full_original",
              "demo": index["demo"], "production_submittable": index["production_submittable"],
              "cursor": cursor, "next_cursor": next_cursor, "has_more": next_cursor is not None,
              "truncated": False, "unit_ids": [p["unit_id"] for p in parts], "tags": tags,
              "complete_tags": complete, "partial_tags": partial, "parts": parts,
              "table_context": table_context(index, parts), "text": rendered}
    if search_info:
        matched = search_info["matched"]
        result.update({"query": search_info["query"], "match_count_total": len(matched),
                       "neighbors": search_info["neighbors"],
                       "matched_unit_ids": [p["unit_id"] for p in parts if p["unit_id"] in matched]})
        result["hits"] = []
        for part in parts:
            if part["unit_id"] in matched:
                hit = matched[part["unit_id"]]
                result["hits"].append(hit)
    if view == "compact":
        model_view = {"cursor": cursor, "next_cursor": next_cursor, "has_more": next_cursor is not None,
                      "text": rendered, "table_context": compact_table_context(result["table_context"]),
                      "partial_tags": partial, "unit_count": len(parts),
                      "delivery_note": "以下是未经摘要的原文片段；partial_tags 仅表示本片未独自覆盖整单位，完整覆盖以累计回执核验。"}
        if search_info:
            model_view["match_count_total"] = len(search_info["matched"])
            model_view["matched_tags"] = [p["tag"] for p in parts if p["unit_id"] in search_info["matched"] and p["tag"]]
        sized_bytes(model_view)
        result["model_view"] = model_view
    sized_bytes(result)
    return result


def fits_page(response: dict[str, Any], max_bytes: int, view: str) -> bool:
    if view == "machine":
        return response["byte_size"] <= max_bytes
    return (response["model_view"]["byte_size"] <= max_bytes
            and response["byte_size"] <= MAX_IPC_BYTES
            and len(response["parts"]) <= MAX_COMPACT_UNITS)


def make_page(index: dict[str, Any], index_hash: str, selected: list[int], cursor: str = "0",
              max_bytes: int = MAX_BYTES, search_info: dict[str, Any] | None = None,
              limit: int = 20, view: str = "machine") -> dict[str, Any]:
    if view not in ("machine", "compact"):
        raise DocumentError("view 必须为 machine 或 compact。")
    if not 512 <= max_bytes <= MAX_BYTES:
        raise DocumentError(f"max-bytes 必须在 512 到 {MAX_BYTES} 之间；上限防止工具输出被再次截断。")
    if limit < 1:
        raise DocumentError("limit 必须大于 0。")
    units = index["units"]
    position, offset = parse_cursor(cursor, selected, units)
    pieces: list[tuple[dict[str, Any], int, int]] = []
    matched_seen: set[str] = set()
    while position < len(selected):
        if view == "compact" and len(pieces) >= MAX_COMPACT_UNITS:
            break
        unit = units[selected[position]]
        total = len(unit["source_text"])
        if offset == total and total > 0:
            position, offset = position + 1, 0
            continue
        is_match = bool(search_info and unit["unit_id"] in search_info["matched"])
        if is_match and unit["unit_id"] not in matched_seen and len(matched_seen) >= limit:
            break

        def candidate(end: int) -> dict[str, Any]:
            next_pos, next_offset = (position + 1, 0) if end == total else (position, end)
            next_value = f"{next_pos}:{next_offset}" if next_pos < len(selected) else None
            return build_response(index, index_hash, cursor, pieces + [(unit, offset, end)], next_value, search_info, view)

        whole = candidate(total)
        if fits_page(whole, max_bytes, view):
            pieces.append((unit, offset, total))
            if is_match:
                matched_seen.add(unit["unit_id"])
            position, offset = position + 1, 0
            continue
        low, high, best = offset + 1, total - 1, None
        while low <= high:
            middle = (low + high) // 2
            response = candidate(middle)
            if fits_page(response, max_bytes, view):
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        if best is not None:
            pieces.append((unit, offset, best))
            offset = best
            break
        if not pieces:
            raise DocumentError(f"max-bytes={max_bytes} 不足以容纳单位 {unit['unit_id']} 的元数据及至少一个字符；没有返回被截断内容。可提高到 {MAX_BYTES}。若独有 merge.value 本身超过该上限，初版不支持其自动分片，可查看 index.tables 原文，但本次未完成完整交付、不可提交。")
        break
    next_cursor = f"{position}:{offset}" if position < len(selected) else None
    result = build_response(index, index_hash, cursor, pieces, next_cursor, search_info, view)
    if not fits_page(result, max_bytes, view):
        raise DocumentError("此响应超过模型字节预算或内部 IPC 上限，没有输出部分 JSON。")
    return result


def read_document(index_path: Path, cursor: str = "0", max_bytes: int = MAX_BYTES,
                  start: str | None = None, end: str | None = None, view: str = "machine") -> dict[str, Any]:
    index, index_hash = load_index(index_path)
    selected = list(range(len(index["units"])))
    if bool(start) != bool(end):
        raise DocumentError("范围读取必须同时提供 --start 与 --end。")
    if start:
        positions = {u["unit_id"]: n for n, u in enumerate(index["units"])}
        try:
            first = positions[index["tag_to_unit"][start]]
            last = positions[index["tag_to_unit"][end]]
        except KeyError as error:
            raise DocumentError(f"范围标签不存在：{error.args[0]}") from error
        if first > last:
            raise DocumentError("起止标签在原文中的顺序颠倒；范围按原文顺序而非 Cxx 数字排序。")
        selected = selected[first:last + 1]
    return make_page(index, index_hash, selected, cursor, max_bytes, view=view)


def search_document(index_path: Path, query: str, cursor: str = "0", max_bytes: int = MAX_BYTES,
                    limit: int = 20, neighbors: int = 1, view: str = "machine") -> dict[str, Any]:
    if not query or not query.strip():
        raise DocumentError("query 不能为空。")
    if len(query) > 1000:
        raise DocumentError("初版 query 最长 1000 字符，请使用较短的原文关键词。")
    if not 0 <= neighbors <= 10:
        raise DocumentError("neighbors 必须在 0 到 10 之间。")
    index, index_hash = load_index(index_path)
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    matched, selected = {}, set()
    for position, unit in enumerate(index["units"]):
        hit = pattern.search(unit["source_text"])
        if not hit:
            continue
        first, last = max(0, hit.start() - 80), min(len(unit["source_text"]), hit.end() + 80)
        matched[unit["unit_id"]] = {"unit_id": unit["unit_id"], "tag": unit["tag"],
                                  "match_start": hit.start(), "match_end": hit.end(),
                                  "source_match_start": unit["source_start"] + hit.start(),
                                  "source_match_end": unit["source_start"] + hit.end(),
                                  "excerpt_start": first, "excerpt_end": last,
                                  "excerpt": unit["source_text"][first:last],
                                  "excerpt_is_full_unit": first == 0 and last == len(unit["source_text"])}
        selected.update(range(max(0, position - neighbors), min(len(index["units"]), position + neighbors + 1)))
    info = {"query": query, "matched": matched, "neighbors": neighbors}
    return make_page(index, index_hash, sorted(selected), cursor, max_bytes, info, limit, view)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="无模型标书工具：保留原文与 Cxx，准备、分页读取和原文搜索。")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare", help="生成保真索引与不超过 24 KiB 的原文分页视图")
    prepare_parser.add_argument("--input", required=True, type=Path, help="输入 JSON 路径")
    prepare_parser.add_argument("--out-dir", required=True, type=Path, help="独立工作目录")
    prepare_parser.add_argument("--demo-tags", action="store_true", help="仅演示：补充合成标签，输出禁止生产提交")
    read_parser = commands.add_parser("read", help="按 cursor 或起止 Cxx 读取完整原文片段")
    search_parser = commands.add_parser("search", help="搜索原文并返回证据及邻域，可分页")
    for sub in (read_parser, search_parser):
        sub.add_argument("--index", required=True, type=Path, help="prepare 生成的 index.json")
        sub.add_argument("--cursor", default="0", help="第一页为 0，后续使用 next_cursor")
        sub.add_argument("--max-bytes", default=MAX_BYTES, type=int, help="machine 约束完整 stdout；compact 约束 model_view 完整 UTF-8 JSON，默认 24576")
        sub.add_argument("--view", choices=("machine", "compact"), default="machine", help="compact 按模型投影预算分页，完整 parts 仅供内部回执；默认 machine")
    read_parser.add_argument("--start", help="起始标签，例如 <C12>")
    read_parser.add_argument("--end", help="终止标签，例如 <C20>")
    search_parser.add_argument("--query", required=True, help="原文关键词，大小写不敏感的字面搜索")
    search_parser.add_argument("--limit", type=int, default=20, help="每片最多出现的匹配单位数，默认 20")
    search_parser.add_argument("--neighbors", type=int, default=1, help="每个命中的相邻单位数，默认 1，最多 10")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.input, args.out_dir, args.demo_tags)
            output = json_bytes(result)
        elif args.command == "read":
            result = read_document(args.index, args.cursor, args.max_bytes, args.start, args.end, args.view)
            output = sized_bytes(result)
        else:
            result = search_document(args.index, args.query, args.cursor, args.max_bytes, args.limit, args.neighbors, args.view)
            output = sized_bytes(result)
        sys.stdout.buffer.write(output)
        return 0
    except (DocumentError, OSError, TypeError, KeyError) as error:
        print(f"文档工具错误：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
