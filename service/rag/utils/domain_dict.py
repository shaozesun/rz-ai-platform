"""
数据中心基础设施领域同义词词典
用于 RAG Query Expansion，仅在 BM25 稀疏检索阶段使用扩展后的查询。
"""

DOMAIN_SYNONYMS: dict[str, list[str]] = {
    "UPS": ["不间断电源", "UPS电源", "不间断供电系统"],
    "ATS": ["自动转换开关", "双电源切换", "自动切换开关"],
    "PDU": ["电源分配单元", "配电单元"],
    "EPS": ["应急电源", "消防应急电源"],
    "10kV": ["10千伏", "高压"],
    "400V": ["低压", "400伏"],
    "直流屏": ["直流配电屏", "DC屏"],
    "蓄电池": ["电池组", "储能电池", "蓄电池组"],
    "CDU": ["冷量分配单元", "冷冻水分配单元", "冷却液分配"],
    "BA": ["楼宇自控", "建筑自动化", "楼控系统"],
    "VRV": ["变制冷剂流量", "多联机空调", "VRV空调"],
    "CRAC": ["精密空调", "机房空调", "恒温恒湿空调"],
    "CRAH": ["冷冻水精密空调", "水冷精密空调"],
    "HVAC": ["暖通空调", "供热通风与空调"],
    "板换": ["板式换热器", "板式热交换器"],
    "冷塔": ["冷却塔", "冷却水塔"],
    "冷冻泵": ["冷冻水泵", "冷冻水循环泵"],
    "冷却泵": ["冷却水泵", "冷却水循环泵"],
    "蓄冷罐": ["蓄冷罐", "冷量储存罐"],
    "VESDA": ["极早期烟雾探测", "空气采样探测器", "吸气式烟雾探测"],
    "门禁": ["出入口控制", "门禁系统", "门禁控制"],
    "闭路": ["视频监控", "CCTV", "闭路电视"],
    "动环": ["动力环境监控", "动环监控"],
    "BMS": ["楼宇管理系统", "建筑管理系统"],
    "EPMS": ["电力监控系统", "电能管理系统"],
    "消防": ["火灾报警", "消防系统"],
    "气体灭火": ["气体消防", "七氟丙烷", "IG541"],
    "喷淋": ["自动喷水灭火", "水喷淋"],
    "SOP": ["标准操作流程", "标准操作规程"],
    "EOP": ["应急操作流程", "应急操作规程", "应急处置"],
    "MOP": ["维护操作流程", "维护操作规程", "保养规程"],
    "巡检": ["巡查", "巡视检查", "例行检查"],
    "维保": ["维护保养", "维修保养"],
    "切换": ["倒换", "切换操作", "主备切换"],
    "应急预案": ["应急处理", "应急处置方案", "应急预案"],
    "故障": ["异常", "告警", "报警"],
}

_FULL_TO_ABBR: dict[str, str] = {}
for _abbr, _fulls in DOMAIN_SYNONYMS.items():
    for _f in _fulls:
        _FULL_TO_ABBR[_f] = _abbr


def expand_query(query: str) -> str:
    if not query:
        return query
    expanded_terms: list[str] = []
    matched_abbrs: set[str] = set()

    for abbr, synonyms in DOMAIN_SYNONYMS.items():
        lower_q = query.lower()
        abbr_lower = abbr.lower()
        if abbr_lower in lower_q or abbr in query:
            matched_abbrs.add(abbr)
        for syn in synonyms:
            if syn in query or syn.lower() in lower_q:
                matched_abbrs.add(abbr)
                break

    for abbr in matched_abbrs:
        expanded_terms.append(abbr)
        synonyms = DOMAIN_SYNONYMS[abbr]
        expanded_terms.extend(synonyms[:2])

    if not expanded_terms:
        return query

    seen: set[str] = set()
    deduped: list[str] = []
    for t in expanded_terms:
        if t not in seen:
            seen.add(t)
            deduped.append(t)

    return query + " " + " ".join(deduped)
