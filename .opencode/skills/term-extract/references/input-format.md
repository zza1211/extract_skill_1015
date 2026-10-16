# 原文输入与读取契约

本工具不调用模型，不判断设备、标题、表头、类别或条款边界，不删除技术内容。

输入是有序数组，块形如 `{"text":"原文"}` 或 `{"table":{"rows":[["单元格原文"]],"merges":[...]}}`。文本、空单元格、每行列数、行列位置、表格次序及 merges 均保留。坐标和 block_index 从 0 开始；原文顺序为顶层块顺序，表格内部为行优先顺序，**不能按 Cxx 数字重新排序**。

生产输入的每个非空 text 字符串或单元格至少包含一个 `<C数字>` 标签，允许一个字符串中含多个标签；`<C0>` 合法。同一文档中，一个标签只能指向一份逻辑原文；不同文档可以重复使用同名标签。空字符串/仅空白字符串可无标签，但仍保留单位。merges 中重复的单元格值属于布局元数据，不再作为另一份定位标签计数。

注册时只在内部建立定位单元：单标签保留整个原字符串，兼容标签位于开头或末尾；多标签按出现顺序切分，从当前标签起点到下一个标签起点（最后到字符串末尾），首片同时保留首标签之前的所有字符。切分不删除换行、不改正文、不生成新标签。片段仍属于原 text 块或同一个表格单元格，不等于多个设备或多个条款；产品归属由主 agent 结合上下文判断。对多标签且标签写在内容末尾的其他解析约定，需先确认定位语义。

实际支持的形状例如：

```json
[
  {"text": "<C0>学校名称\n<C1>招标邀请\n<C2>技术要求"},
  {"table": {"rows": [["<C3>项目\n<C4>编号", "<C5>AP要求\n<C6>支持Wi-Fi 7"]], "merges": []}}
]
```

## 合并单元格的重复标签

解析器把合并单元格的原文展开到多个位置时，可以保留原标签，不必重排。注册只在以下条件全部满足时，把展开位置登记为左上角单元格的坐标别名：

- 重复发生在同一个表格，并由明确的 `merges` 范围连接；范围使用从 0 开始的 `top_left.row / top_left.col` 和正整数 `row_span / col_span`。
- 左上角单元格包含真实标签；展开副本与它的**整个原字符串逐字符相同**，包括标签、空格和换行。多标签单元格也必须整串一致。
- 用来解释重复的合并范围合法、不越界且不重叠。不能仅凭内容相同推断合并关系。

例如，下面两行的 `<C4>` 是同一个纵向合并单元格：

```json
[
  {"text": "<C0>交换机采购要求"},
  {"table": {
    "rows": [
      ["<C1>设备", "<C2>技术参数", "<C3>采购数量"],
      ["<C4>接入交换机", "<C5>24个GE口", "<C6>2台"],
      ["<C4>接入交换机", "<C7>支持双电源", "<C8>数量为上述设备总量"]
    ],
    "merges": [{
      "top_left": {"row": 1, "col": 0},
      "row_span": 2,
      "col_span": 1,
      "value": "<C4>接入交换机"
    }]
  }}
]
```

原始 rows 和 merges 均保留。`(2,0)` 复用锚点 `(1,0)` 的定位单元；`<C4>` 只计一次，定位顺序仍为 `<C0>` 至 `<C8>`，不会因第二次出现而把 `<C4>` 的位置延到第二行。读取或搜索时保留别名与合并范围，供主 agent 判断行列归属；这也不构成 separate 的依据。

不同段落、跨表、没有明确合并关系、同标签不同内容，以及同一个字符串内部的重复标签仍会报错。用于解释重复的合并范围无效或重叠时也会报错。不要删掉重复检查，不要自行改标签后继续沿用旧标签答案。

完整文件及准备命令见 [合并单元格示例](../../../../examples/merged-cells/README.md)。

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

compact 读取响应还包含计入字节预算的 `next_action`：有后续片段时，已有明确采购对象可先增量保存候选与疑点，再沿游标继续；本次范围读至末尾时，提示首次通读后立即保存暂定候选，不要求先敲定边界、关联与数量。该提示仅说明本次读取范围的状态；范围补读的 `has_more=false` 不能证明全文已读完，完整交付仍按累计回执核验。搜索响应不包含此阶段提示，也不能代替全文读取。

工具运行时会补 cursor=`0`、limit=`20`、max_bytes=`24576`，不依赖模型必须填所有可选参数。诊断 CLI 和 OpenCode 工具的模型输出形状不同：下面的 parts 说明是机器索引/worker/回执契约。

模型用的 term_document_search 在本次 draft/candidates.json 尚未保存或 index 哈希不匹配时，会返回先调用 term_result_checkpoint 的短提示。候选可以不完整；要求是先记录具体候选/疑点，再为这些问题搜索。Python search CLI 仍可单独诊断，无该阶段限制。

## 索引与 manifest

`index.json` 包含 `schema_version=2`、source_path/source_sha256、demo、production_submittable、完整 source_document、units、tables 和 tag_to_unit。每个 unit 对应一个真实标签的定位片段（空字符串仍有空单元），含 unit_id、order、block_index、kind（text/cell）、tag/tags、table_id、row、col，以及：

- `source_text`：当前定位单元对应的原字符串切片，内容未经改写，也是读取覆盖 offset 的依据；`source_length` 等于该切片的 Python Unicode 字符数。非 BMP 字符在 JavaScript 中必须以码点计数，不用字符串 UTF-16 length 推翻该字段。
- `text`：工作字符串。生产模式等于 source_text；演示模式只对无标签单位加合成标签。
- `source_start/source_end`：片段在原 text/单元格字符串中的 Unicode 字符区间，左闭右开；按这些位置拼接同一原字符串的片段可逐字符恢复原文。
- `tag_offset`：真实标签在原字符串中的字符位置；`unit_tag_offset` 是标签在当前片段中的位置。

tables 保留原始 rows、原始 merges；`cell_unit_ids[row][col]` 是该单元格所有定位片段的 unit_id 数组，二维 `unit_ids` 仅保留每个单元格的首片作兼容引用。不能用首片代表整个多标签单元格，也不把相同 CAT 值去重或广播。`manifest.json` 含 source_sha256、index_sha256、单位/标签计数、demo/production_submittable、分页清单。每片是 `view-NNNN.json`，包含 cursor、next_cursor、覆盖元数据和完整原文片段。

存在上述合并副本时，schema 2 索引还包含 `merged_cell_alias_policy="exact_merged_cell_v1"` 和 `merged_cell_aliases` 坐标别名列表；副本的 `cell_unit_ids` / `unit_ids` 复用锚点引用，`tag_to_unit` 仍为唯一映射。别名不创建新的定位单元、不重复计算读取覆盖，也不按最后出现位置扩大条款区间。完整 `source_document`、表格 rows / merges 不变，读取和搜索返回相关别名与合并上下文。

读取的 table_context 通过 `merged_cell_alias_count` 说明该表的副本数；`cell_aliases` 只列本片可见行、已交付列范围及紧邻列的副本坐标和 `value_from_cell`，完整别名保存在索引。合并的完整范围仍由 merges 提供，避免每一页都重复输出跨大量行列的副本清单。

版本 1 索引与新定位粒度不兼容，读取时会明确拒绝；使用原始输入重新 prepare，不手动修改旧索引或沿用旧读取回执。最终七字段结果接口不变。

0.6.4 的合并别名支持兼容原先没有重复标签的 schema 2 索引；已有任务不自动迁移。此前因合并副本重复标签而失败的输入，更新代码后使用**新的工作目录**重新 prepare，不重排原标签，也不修改对应正确标签或历史输出。

prepare 的 stdout 返回 index_path、manifest_path、view_paths、view_count、view_paths_complete、demo、production_submittable、index_hash、unit_count、tag_count、merged_cell_alias_count、byte_size。merged_cell_alias_count 是本次确认的合并副本坐标数，unit_count / tag_count 按去重后的逻辑原文计数；OpenCode 注册工具也返回这三个计数。通常 view_paths 完整；列表太长时仅返回前四个路径，并明确 view_paths_complete=false/all_view_paths_in_manifest=true，完整列表仍在 manifest，继续读取使用 cursor 不受影响。prepare 的完整 stdout 同样不超过 24 KiB。

`--demo-tags` 只生成 `source.demo.json` 工作副本，绝不改输入文件。demo=true、production_submittable=false；即使只有部分标签被补充，该整次运行也不能生产提交。当前 example 无真实标签，只能用演示模式。

## 读取完整性

read/search 返回 `index_hash`、`coverage_kind="full_original"`、cursor/next_cursor、has_more、truncated=false、byte_size、text、parts、unit_ids、tags、complete_tags、partial_tags。truncated=false 表示本片返回内容未被静默裁掉；has_more=true 表示仍有后续原文，**不代表全文已经读完**。

compact 的 model_view 不返回 parts、unit_ids、tags、complete_tags 或 SHA。这些保留在内部 worker 和 read_receipts.jsonl。先确认模型返回在大小预算内再写回执；回执 byte_size 是实际交付的紧凑 JSON 大小，worker_byte_size 是内部机器 JSON 大小。覆盖仍使用完整 parts，而非仅凭 matched_tags 或标签出现次数。模型视图把每个原段落/单元格只标一次位置，位于相邻行的要求应结合原表判断，不能把单元格内 C 片段误当独立条款。

parts 的 `part_start`、`part_end` 是当前定位片段 source_text 的 Python Unicode 字符偏移，左闭右开；`total_length` 是该定位片段的长度。`source_start/source_end/tag_offset` 仍以原字符串为参照，不能混用两种坐标。`content_start/content_end` 标出本次交付原文在响应 text 内的字符区间，可用于逐字符核对。parts 同时携带原始 order/block/table/row/col。同一标签的超长定位片段可跨多次读取，标签/坐标始终相同；不截掉标签后就假装读完。

`complete_tags` 只包含**本响应独自从 0 覆盖到 total_length** 的单位。部分单位的最后一片仍属于 partial_tags；应核对所有读取 receipts 的 part 区间并集覆盖 `[0,total_length)`，才能认定该标签原文完整。空单位也有 part（0:0），从而可核对空表格位置。

table_context 提供涉及表格的形状、相关合并范围、merge_index 和 anchor_unit_ids（锚点单元格的全部定位片段）；只有单片锚点才额外提供 anchor_unit_id。只有 merge.value 与锚点原单元格在去 Cxx 标签、首尾空白后精确相等时，才用 value_in_anchor=true/duplicate_proof 引用锚点；其他 merge.value 原封不动返回，标明 value_source=merge_metadata，不装作它是已读取的单元格文本。所有原始 merges 始终保存在 index.tables，不自动判定表头、跨页续表或 CAT 适用关系。独有 merge.value 若单独超过返回预算，初版明确报错而不省略；本次不能声称完整读取/提交，长 merge 元数据单独分片是尚不支持的情况。

search 是大小写不敏感的字面搜索，不是语义检索。返回所有命中单位及所选相邻单位，按原文顺序分页；limit 只限制每片匹配单位数，不丢弃后续结果。hits 中的 excerpt 有明确 offset 和 excerpt_is_full_unit；这个证据摘要不等于整单位已读，完整性仍以 parts 为准。搜索没有命中不能作为相关内容不存在的证明。
