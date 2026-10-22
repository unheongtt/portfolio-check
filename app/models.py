"""持仓体检 PortfolioCheck 领域模型。

设计原则（对应题目统一规则）：
- 每个数字必须带来源、时点、单位、口径；缺失/过期/失败显式标记（ok/stale/missing/error），
  绝不静默生成"正常"结论，绝不编造数字；
- 每个结论带状态标签：fact（事实）/ inference（推断）/ uncertain（不确定）；
- AI 只做"解读合成"，永不生成数字；所有 AI 产出默认"待复核"。
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class DataStatus(str, Enum):
    OK = "ok"          # 新鲜可用
    STALE = "stale"    # 超过新鲜度阈值（带旧值 + 失败原因）
    MISSING = "missing"  # 数据源无此数据（显式呈现，不兜底）
    ERROR = "error"    # 调用失败/被拒/超时


class ClaimStatus(str, Enum):
    FACT = "fact"          # 有原始数据/出处直接支撑
    INFERENCE = "inference"  # 由多源信息推理得出
    UNCERTAIN = "uncertain"  # 无法确认，显式提示


class Severity(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RiskCategory(str, Enum):
    CONCENTRATION = "concentration"   # 集中度：单标的/行业权重过重
    CORRELATION = "correlation"       # 相关性：持仓同涨同跌、分散失效
    VALUATION = "valuation"           # 估值：PE/PB 极端值、亏损
    VOLATILITY = "volatility"         # 波动：历史波动率过高
    EXPOSURE = "exposure"             # 题材暴露：同一概念/题材扎堆


RISK_CATEGORY_LABELS: dict[RiskCategory, str] = {
    RiskCategory.CONCENTRATION: "集中度风险",
    RiskCategory.CORRELATION: "相关性风险",
    RiskCategory.VALUATION: "估值风险",
    RiskCategory.VOLATILITY: "波动风险",
    RiskCategory.EXPOSURE: "题材暴露风险",
}


# ---------------------------------------------------------------------------
# 输入
# ---------------------------------------------------------------------------

class Holding(BaseModel):
    """一条持仓。thscode 形如 600519.SH。"""
    thscode: str
    name: str = ""
    weight_pct: float = Field(gt=0, le=100, description="组合权重（百分比）")
    market: str = "A股"


class PortfolioInput(BaseModel):
    name: str = "我的组合"
    holdings: list[Holding]


# ---------------------------------------------------------------------------
# 证据点（数字，完整溯源）
# ---------------------------------------------------------------------------

class EvidencePoint(BaseModel):
    label: str                       # 人话标签，如「贵州茅台权重」
    value: Optional[float] = None
    unit: str = ""                   # % / 元 / 倍
    caliber: str = ""                # 统计口径说明
    source: str = ""                 # user_input / fuyao / ifind / seed
    as_of: str = ""                  # 数据时点
    data_status: DataStatus = DataStatus.MISSING
    note: str = ""                   # 失败/缺失时的显式原因


class RiskRule(BaseModel):
    """触发该发现所用的确定性规则（可复现、可测试）。"""
    rule_id: str
    threshold: str
    reason: str


class Narrative(BaseModel):
    """AI 解读（待复核）。引擎缺省时为规则模板。"""
    text: str
    engine: str = "rules"            # rules / llm
    model: str = ""
    review_status: str = "pending"   # pending / reviewed
    ai_output_id: str = ""


class RiskFinding(BaseModel):
    id: str
    category: RiskCategory
    title: str
    severity: Severity
    status: ClaimStatus = ClaimStatus.FACT
    evidence: list[EvidencePoint] = Field(default_factory=list)
    rule: RiskRule
    narrative: Optional[Narrative] = None
    affected_holdings: list[str] = Field(default_factory=list)


class Report(BaseModel):
    id: str
    portfolio: PortfolioInput
    generated_at: str = Field(default_factory=now_iso)
    data_sources: dict[str, Any] = Field(default_factory=dict)
    findings: list[RiskFinding] = Field(default_factory=list)
    overall: Optional[Narrative] = None


# ---------------------------------------------------------------------------
# AI 产出（全部带状态与追溯）
# ---------------------------------------------------------------------------

class AIOutput(BaseModel):
    id: str
    kind: str                      # risk_narrative / overall_summary
    model: str                     # 模型名或 "rules-engine"
    prompt_hash: str = ""
    input_refs: list[str] = Field(default_factory=list)  # 引用证据点 / 数据点 id
    output: dict[str, Any] = Field(default_factory=dict)
    review_status: str = "pending"
    created_at: str = Field(default_factory=now_iso)
