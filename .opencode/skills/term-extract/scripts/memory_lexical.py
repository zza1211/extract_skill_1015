"""轻量词法召回：只归一化明确同义词，不推断语义、不调用向量模型。"""
from __future__ import annotations

import re
import unicodedata

# 维护有限词表而非声称支持完整繁简翻译。英文多词按单词边界匹配。
ALIASES = {
    "quantity": ("quantity", "quantities", "qty", "procurement count", "采购数量", "採購數量", "数量", "數量"),
    "boundary": ("boundary", "boundaries", "clause range", "条款边界", "條款邊界", "边界", "邊界", "起止"),
    "association": ("association", "关联", "關聯", "相关条款", "相關條款"),
    "missing_product": ("missing product", "missing equipment", "漏抽", "漏产品", "漏設備", "漏设备"),
    "access_point": ("access point", "access points", "wlan ap", "无线接入点", "無線接入點"),
    "controller": ("wireless controller", "wlan ac", "无线控制器", "無線控制器"),
    "switch": ("switch", "switches", "交换机", "交換機"),
    "firewall": ("firewall", "firewalls", "防火墙", "防火牆"),
    "continuation": ("continued table", "continuation table", "续表", "續表", "连续表格", "連續表格"),
    "separate": ("separate", "分散条款", "分散條款", "不连续", "不連續"),
    "general": ("general", "通用要求", "共用要求"),
    "rack": ("rack", "racks", "cabinet", "cabinets", "机柜", "機櫃", "机架", "機架"),
    "cabling": ("cabling", "布线", "佈線", "布線"),
}


def terms(text: str) -> set[str]:
    text = unicodedata.normalize("NFKC", text).casefold()
    result = set(re.findall(r"[a-z0-9][a-z0-9_-]*", text))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        result.update(run[i:i + 2] for i in range(len(run) - 1))
    for key, aliases in ALIASES.items():
        for alias in aliases:
            pattern = r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])" if alias.isascii() else re.escape(alias)
            if re.search(pattern, text):
                result.add("alias:" + key)
                break
    return result


def document_query(index: dict) -> str:
    """扫描全部定位片段提取有限特征，避免只看文档开头的行政章节。"""
    present = terms("\n".join(str(unit.get("text", "")) for unit in index.get("units", [])))
    features = [aliases[0] for key, aliases in ALIASES.items() if "alias:" + key in present]
    return " ".join([*features, "条款边界", "采购数量", "关联", "漏抽"])[:512]
