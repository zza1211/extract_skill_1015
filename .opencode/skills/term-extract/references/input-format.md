# 原文输入与读取契约

本工具不调用模型，不判断设备、标题、表头、类别或条款边界，不删除技术内容。

输入是有序数组，块形如 `{"text":"原文"}` 或 `{"table":{"rows":[["单元格原文"]],"merges":[...]}}`。文本、空单元格、每行列数、行列位置、表格次序及 merges 均保留。坐标和 block_index 从 0 开始；原文顺序为顶层块顺序，表格内部为行优先顺序，**不能按 Cxx 数字重新排序**。

生产输入的每个非空 text 字符串或单元格至少包含一个 `<C数字>` 标签，允许一个字符串中含多个标签；所有主文本标签全篇唯一，`<C0>` 合法。空字符串/仅空白字符串可无标签，但仍保留单位。merges 中重复的单元格值属于布局元数据，不再作为另一份定位标签计数。

注册时只在内部建立定位单元：单标签保留整个原字符串，兼容标签位于开头或末尾；多标签按出现顺序切分，从当前标签起点到下一个标签起点（最后到字符串末尾），首片同时保留首标签之前的所有字符。切分不删除换行、不改正文、不生成新标签。片段仍属于原 text 块或同一个表格单元格，不等于多个设备或多个条款；产品归属由主 agent 结合上下文判断。对多标签且标签写在内容末尾的其他解析约定，需先确认定位语义。

实际支持的形状例如：

```json
[
  {"text": "<C0>学校名称\n<C1>招标邀请\n<C2>技术要求"},
  {"table": {"rows": [["<C3>项目\n<C4>编号", "<C5>AP要求\n<C6>支持Wi-Fi 7"]], "merges": []}}
]
```

## 命令

```text
python scripts/document_io.py prepare --input 输入.json --out-dir 工作目录
python scripts/document_io.py prepare --input 演示输入.json --out-dir 演示目录 --demo-tags
python scripts/document_io.py read --index 工作目录/index.json --cursor 0
python scripts/document_io.py read --index 工作目录/index.json --start "<C12>" --end "<C20>" --cursor 0
python scripts/document_io.py search --index 工作目录/index.json --query "Virtual Firewall" --cursor 0 --limit 20 --neighbors 1
```

stdout 为 UTF-8 JSON；错误为中文 stderr，退出码 2。默认诊断 CLI 使用 `--view machine`，read/search 的 `--max-bytes` 默认 24576，可以下调到 512；太小而装不下必要元数据时明确报错，不输出半份 JSON。machine 模式完整 stdout 的 `byte_size` 包括 JSON 转义、缩进和末尾换行，始终不超过所请求上限。分页必须保持 index、范围或 query/neighbors/view 不变；cursor 是所选单位序列内的 `位置:字符偏移`，首页可用 `0`，后续只使用返回的 next_cursor。

OpenCode 工具默认使用 `--view compact`。worker 返回包含完整 parts 的内部 JSON 和 `model_view`；只按 `model_view` 的完整 UTF-8 JSON 大小装包，详细 parts 不挤占模型预算。内部 worker 最大 1 MiB、每次最多 512 个定位单元，超过也继续分页；它不是应直接加载到模型的文件。OpenCode 仅交付 `model_view`，保留原文、短位置头、table_context、游标和部分标签提示，省去重复标签数组、所有原始偏移和完整 parts。同段落/单元格的连续片段拼接，非连续搜索片段不拼接。compact 模式的 `--max-bytes` 约束 `model_view.byte_size`，不能用内部 worker.byte_size 推算模型上下文开销。

工具运行时会补 cursor=`0`、limit=`20`、max_bytes=`24576`，不依赖模型必须填所有可选参数。诊断 CLI 和 OpenCode 工具的模型输出形状不同：下面的 parts 说明是机器索引/worker/回执契约。

模型用的 term_document_search 在本次 draft/candidates.json 尚未保存或 index 哈希不匹配时，会返回先调用 term_result_checkpoint 的短提示。候选可以不完整；要求是先记录具体候选/疑点，再为这些问题搜索。Python search CLI 仍可单独诊断，无该阶段限制。

## 索引与 manifest

`index.json` 包含 `schema_version=2`、source_path/source_sha256、demo、production_submittable、完整 source_document、units、tables 和 tag_to_unit。每个 unit 对应一个真实标签的定位片段（空字符串仍有空单元），含 unit_id、order、block_index、kind（text/cell）、tag/tags、table_id、row、col，以及：

- `source_text`：当前定位单元对应的原字符串切片，内容未经改写，也是读取覆盖 offset 的依据；`source_length` 等于该切片的 Python Unicode 字符数。非 BMP 字符在 JavaScript 中必须以码点计数，不用字符串 UTF-16 length 推翻该字段。
- `text`：工作字符串。生产模式等于 source_text；演示模式只对无标签单位加合成标签。
- `source_start/source_end`：片段在原 text/单元格字符串中的 Unicode 字符区间，左闭右开；按这些位置拼接同一原字符串的片段可逐字符恢复原文。
- `tag_offset`：真实标签在原字符串中的字符位置；`unit_tag_offset` 是标签在当前片段中的位置。

tables 保留原始 rows、原始 merges；`cell_unit_ids[row][col]` 是该单元格所有定位片段的 unit_id 数组，二维 `unit_ids` 仅保留每个单元格的首片作兼容引用。不能用首片代表整个多标签单元格，也不把相同 CAT 值去重或广播。`manifest.json` 含 source_sha256、index_sha256、单位/标签计数、demo/production_submittable、分页清单。每片是 `view-NNNN.json`，包含 cursor、next_cursor、覆盖元数据和完整原文片段。

版本 1 索引与新定位粒度不兼容，读取时会明确拒绝；使用原始输入重新 prepare，不手动修改旧索引或沿用旧读取回执。最终七字段结果接口不变。

prepare 的 stdout 返回 index_path、manifest_path、view_paths、view_count、view_paths_complete、demo、production_submittable、index_hash、unit_count、tag_count、byte_size。通常 view_paths 完整；列表太长时仅返回前四个路径，并明确 view_paths_complete=false/all_view_paths_in_manifest=true，完整列表仍在 manifest，继续读取使用 cursor 不受影响。prepare 的完整 stdout 同样不超过 24 KiB。

`--demo-tags` 只生成 `source.demo.json` 工作副本，绝不改输入文件。demo=true、production_submittable=false；即使只有部分标签被补充，该整次运行也不能生产提交。当前 example 无真实标签，只能用演示模式。

## 读取完整性

read/search 返回 `index_hash`、`coverage_kind="full_original"`、cursor/next_cursor、has_more、truncated=false、byte_size、text、parts、unit_ids、tags、complete_tags、partial_tags。truncated=false 表示本片返回内容未被静默裁掉；has_more=true 表示仍有后续原文，**不代表全文已经读完**。

compact 的 model_view 不返回 parts、unit_ids、tags、complete_tags 或 SHA。这些保留在内部 worker 和 read_receipts.jsonl。先确认模型返回在大小预算内再写回执；回执 byte_size 是实际交付的紧凑 JSON 大小，worker_byte_size 是内部机器 JSON 大小。覆盖仍使用完整 parts，而非仅凭 matched_tags 或标签出现次数。模型视图把每个原段落/单元格只标一次位置，位于相邻行的要求应结合原表判断，不能把单元格内 C 片段误当独立条款。

parts 的 `part_start`、`part_end` 是当前定位片段 source_text 的 Python Unicode 字符偏移，左闭右开；`total_length` 是该定位片段的长度。`source_start/source_end/tag_offset` 仍以原字符串为参照，不能混用两种坐标。`content_start/content_end` 标出本次交付原文在响应 text 内的字符区间，可用于逐字符核对。parts 同时携带原始 order/block/table/row/col。同一标签的超长定位片段可跨多次读取，标签/坐标始终相同；不截掉标签后就假装读完。

`complete_tags` 只包含**本响应独自从 0 覆盖到 total_length** 的单位。部分单位的最后一片仍属于 partial_tags；应核对所有读取 receipts 的 part 区间并集覆盖 `[0,total_length)`，才能认定该标签原文完整。空单位也有 part（0:0），从而可核对空表格位置。

table_context 提供涉及表格的形状、相关合并范围、merge_index 和 anchor_unit_ids（锚点单元格的全部定位片段）；只有单片锚点才额外提供 anchor_unit_id。只有 merge.value 与锚点原单元格在去 Cxx 标签、首尾空白后精确相等时，才用 value_in_anchor=true/duplicate_proof 引用锚点；其他 merge.value 原封不动返回，标明 value_source=merge_metadata，不装作它是已读取的单元格文本。所有原始 merges 始终保存在 index.tables，不自动判定表头、跨页续表或 CAT 适用关系。独有 merge.value 若单独超过返回预算，初版明确报错而不省略；本次不能声称完整读取/提交，长 merge 元数据单独分片是尚不支持的情况。

search 是大小写不敏感的字面搜索，不是语义检索。返回所有命中单位及所选相邻单位，按原文顺序分页；limit 只限制每片匹配单位数，不丢弃后续结果。hits 中的 excerpt 有明确 offset 和 excerpt_is_full_unit；这个证据摘要不等于整单位已读，完整性仍以 parts 为准。搜索没有命中不能作为相关内容不存在的证明。
