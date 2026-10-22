"""规则叙事：LLM 缺省时的确定性解读模板（兼作 LLM 输出的对照基线）。

叙事只做「转述证据 + 提示关注」，遵循合规边界：
- 不生成任何新数字（只引用 finding.evidence 里已有的数值）；
- 不输出买入/卖出/目标价/收益承诺/涨跌预测；
- 措辞区分事实（数据显示）与提示（需关注/复核）。
"""
from __future__ import annotations

from typing import Any

from ..models import RISK_CATEGORY_LABELS

_CATEGORY_FRAMING: dict[str, str] = {
    "concentration": "集中度衡量组合是否过度押注个别标的或行业。",
    "correlation": "相关性衡量持仓之间是否同涨同跌、分散是否真实有效。",
    "valuation": "估值衡量当前价格相对盈利与净资产是否处于极端水平。",
    "volatility": "波动率衡量持仓价格的历史波动幅度。",
    "exposure": "题材暴露衡量组合是否在同一概念或题材上扎堆。",
}


def _evidence_numbers(finding: dict[str, Any]) -> str:
    parts = [f"{e['label']} {e['value']}{e['unit']}".rstrip()
             for e in finding.get("evidence", []) if e.get("value") is not None]
    return "、".join(parts) if parts else "（数据项）"


def narrative_for_finding(finding: dict[str, Any]) -> str:
    """确定性叙事：转述证据 + 提示。输出中文 2–4 句。"""
    cat = finding["category"]
    title = finding["title"]
    reason = (finding.get("rule") or {}).get("reason", "")
    numbers = _evidence_numbers(finding)
    framing = _CATEGORY_FRAMING.get(cat, "")
    label = RISK_CATEGORY_LABELS.get(cat, cat)

    status_hint = "（推断）" if finding.get("status") == "inference" else ""
    lines = [
        f"{framing}{status_hint}",
        f"数据显示：{numbers}。",
        f"解读：{reason}",
        "提示：以上为风险提示而非投资建议，不构成任何买卖依据，请结合自身情况复核。",
    ]
    return "\n".join(lines)


def summarize_rule(findings: list[dict[str, Any]], portfolio_name: str) -> str:
    """整体摘要（规则模板）：按严重度汇总发现，不新增数字。"""
    if not findings:
        return (f"{portfolio_name} 未发现触发阈值以外的明显风险项。"
                "提示：仅覆盖集中度/相关性/估值/波动/题材暴露五类，且结论需以实时数据为准。")

    high = [f for f in findings if f.get("severity") == "high"]
    med = [f for f in findings if f.get("severity") == "medium"]
    cats = [RISK_CATEGORY_LABELS.get(f["category"], f["category"]) for f in findings]

    parts = [f"{portfolio_name} 共触发 {len(findings)} 项风险提示"]
    if high:
        parts.append(f"其中高风险 {len(high)} 项（{'、'.join(sorted(set(RISK_CATEGORY_LABELS.get(f['category'], f['category']) for f in high)))}）")
    if med:
        parts.append(f"中等风险 {len(med)} 项")
    parts.append("覆盖：" + "、".join(sorted(set(cats))))
    parts.append("提示：以上为规则引擎产出的风险提示（待复核），不构成投资建议，不预测涨跌。")
    return "。".join(parts) + "。"
