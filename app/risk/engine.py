"""风险引擎：持仓体检的五类风险确定性计算。

设计边界（写入 README）：
- 引擎只做**确定性计算**（集中度/相关性/估值/波动/题材暴露），不调用 LLM、
  不生成自然语言结论——解读由 ai/analyzer.py 单独合成；
- 所有计算都从结构化数据（用户输入权重、种子行业/概念、扶摇行情/估值/日线）推导，
  每个数字带 source/caliber/as_of/data_status，可回溯到原始字段；
- 阈值全部集中在本文件顶部常量，供测试固化口径。

五类风险：
1. 集中度 concentration —— 单标的/行业/前 N 大权重过重；
2. 相关性 correlation —— 持仓两两相关性过高，分散失效；
3. 估值 valuation —— PE/PB 极端值、亏损（负 PE）；
4. 波动 volatility —— 历史日收益年化波动率过高；
5. 题材暴露 exposure —— 同一概念覆盖多只持仓且权重过重。
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone
from typing import Any

from ..models import ClaimStatus, DataStatus, Severity, now_iso

# ---------------------------------------------------------------------------
# 阈值口径（可测试固化）
# ---------------------------------------------------------------------------
SINGLE_WEIGHT_HIGH = 30.0   # 单一标的权重（%）高/中阈值
SINGLE_WEIGHT_MED = 20.0
INDUSTRY_WEIGHT_HIGH = 50.0
INDUSTRY_WEIGHT_MED = 35.0
TOP3_WEIGHT_HIGH = 70.0
TOP3_WEIGHT_MED = 55.0
CORR_AVG_HIGH = 0.7
CORR_AVG_MED = 0.5
CORR_MAX_HIGH = 0.85
PE_HIGH = 80.0
PE_MED = 50.0
PB_HIGH = 10.0
PB_MED = 6.0
VOL_HIGH = 40.0          # 年化波动率（%）高/中阈值
VOL_MED = 25.0
EXPOSURE_COUNT = 3       # 同一概念覆盖的最小持仓数
EXPOSURE_WEIGHT_HIGH = 40.0
EXPOSURE_WEIGHT_MED = 25.0
MIN_BARS = 20            # 相关性/波动率计算所需最少交易日数


def _ev(label: str, value: float | None, unit: str, caliber: str, source: str,
        as_of: str, data_status: str, note: str = "") -> dict[str, Any]:
    return {"label": label, "value": value, "unit": unit, "caliber": caliber,
            "source": source, "as_of": as_of, "data_status": data_status, "note": note}


def _daily_returns(closes: list[float]) -> list[float]:
    return [(closes[i] / closes[i - 1] - 1.0) for i in range(1, len(closes)) if closes[i - 1]]


def _pearson(xs: list[float], ys: list[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 2:
        return 0.0
    x, y = xs[:n], ys[:n]
    mx, my = sum(x) / n, sum(y) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(x, y))
    sx = math.sqrt(sum((a - mx) ** 2 for a in x))
    sy = math.sqrt(sum((b - my) ** 2 for b in y))
    if sx == 0 or sy == 0:
        return 0.0
    return cov / (sx * sy)


def _annualized_vol_pct(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    return statistics.stdev(returns) * math.sqrt(252) * 100.0


# ---------------------------------------------------------------------------
# 1. 集中度
# ---------------------------------------------------------------------------

def compute_concentration(holdings: list[dict[str, Any]],
                          membership: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """集中度：单标的权重、行业集中、前三大集中。仅依赖用户输入 + 种子行业归属。"""
    now = now_iso()
    findings: list[dict[str, Any]] = []
    if not holdings:
        return findings

    # 单标的权重
    top = max(holdings, key=lambda h: h["weight_pct"])
    if top["weight_pct"] > SINGLE_WEIGHT_HIGH:
        sev, thr = Severity.HIGH.value, f"> {SINGLE_WEIGHT_HIGH:g}%"
    elif top["weight_pct"] > SINGLE_WEIGHT_MED:
        sev, thr = Severity.MEDIUM.value, f"> {SINGLE_WEIGHT_MED:g}%"
    else:
        sev = None
    if sev:
        findings.append({
            "id": "f-conc-single", "category": "concentration",
            "title": f"单一标的权重过高：{top['name'] or top['thscode']} {top['weight_pct']:g}%",
            "severity": sev, "status": ClaimStatus.FACT.value,
            "evidence": [_ev("组合中权重最高的标的", top["weight_pct"], "%",
                             "用户输入持仓权重", "user_input", now, DataStatus.OK.value)],
            "rule": {"rule_id": "max_single_weight", "threshold": thr,
                     "reason": "单一标的权重过高会放大个股自身风险，一次黑天鹅即显著影响组合。"},
            "affected_holdings": [top["thscode"]],
        })

    # 行业集中（按同花顺行业聚合；无行业归属的标的记为"未分类"，不参与行业集中）
    ind_weights: dict[str, float] = {}
    for h in holdings:
        ind = (membership.get(h["thscode"]) or {}).get("industry") or ""
        if not ind:
            continue
        ind_weights[ind] = ind_weights.get(ind, 0.0) + h["weight_pct"]
    if ind_weights:
        top_ind, top_ind_w = max(ind_weights.items(), key=lambda kv: kv[1])
        if top_ind_w > INDUSTRY_WEIGHT_HIGH:
            sev, thr = Severity.HIGH.value, f"> {INDUSTRY_WEIGHT_HIGH:g}%"
        elif top_ind_w > INDUSTRY_WEIGHT_MED:
            sev, thr = Severity.MEDIUM.value, f"> {INDUSTRY_WEIGHT_MED:g}%"
        else:
            sev = None
        if sev:
            affected = [h["thscode"] for h in holdings
                        if (membership.get(h["thscode"]) or {}).get("industry") == top_ind]
            findings.append({
                "id": "f-conc-industry", "category": "concentration",
                "title": f"行业集中度偏高：{top_ind} 合计 {top_ind_w:g}%",
                "severity": sev, "status": ClaimStatus.FACT.value,
                "evidence": [_ev(f"行业「{top_ind}」合计权重", top_ind_w, "%",
                                 "同花顺行业归属（种子数据，静态）+ 用户输入权重", "seed+user_input",
                                 now, DataStatus.OK.value)],
                "rule": {"rule_id": "industry_concentration", "threshold": thr,
                         "reason": "同一行业权重过重，行业景气下行或政策变化时组合缺乏对冲。"},
                "affected_holdings": affected,
            })

    # 前三大集中
    sorted_h = sorted(holdings, key=lambda h: h["weight_pct"], reverse=True)
    top3_w = sum(h["weight_pct"] for h in sorted_h[:3])
    if top3_w > TOP3_WEIGHT_HIGH:
        sev, thr = Severity.HIGH.value, f"> {TOP3_WEIGHT_HIGH:g}%"
    elif top3_w > TOP3_WEIGHT_MED:
        sev, thr = Severity.MEDIUM.value, f"> {TOP3_WEIGHT_MED:g}%"
    else:
        sev = None
    if sev:
        findings.append({
            "id": "f-conc-top3", "category": "concentration",
            "title": f"前三大标的合计 {top3_w:g}%，组合缺乏分散",
            "severity": sev, "status": ClaimStatus.FACT.value,
            "evidence": [_ev("前三大标的权重之和", top3_w, "%",
                             "用户输入持仓权重", "user_input", now, DataStatus.OK.value)],
            "rule": {"rule_id": "top3_concentration", "threshold": thr,
                     "reason": "前三大标的权重过高，剩余仓位对组合的边际分散作用有限。"},
            "affected_holdings": [h["thscode"] for h in sorted_h[:3]],
        })
    return findings


# ---------------------------------------------------------------------------
# 2. 相关性
# ---------------------------------------------------------------------------

def compute_correlation(holdings: list[dict[str, Any]],
                        close_series: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """相关性：持仓两两日收益相关性。依赖扶摇历史日线（前复权收盘价）。"""
    name_of = {h["thscode"]: (h.get("name") or h["thscode"]) for h in holdings}
    returns: dict[str, list[float]] = {}
    series_status: dict[str, str] = {}
    as_of = ""
    for h in holdings:
        s = close_series.get(h["thscode"]) or {}
        closes = s.get("closes") or []
        st = s.get("data_status") or DataStatus.MISSING.value
        series_status[h["thscode"]] = st
        if st == DataStatus.OK.value and len(closes) >= MIN_BARS:
            returns[h["thscode"]] = _daily_returns(closes)
        as_of = s.get("as_of") or as_of

    if len(returns) < 2:
        # 有效历史数据不足两标的：显式说明不产相关性发现
        missing = [h["thscode"] for h in holdings if h["thscode"] not in returns]
        note = f"有效历史日线不足（{len(returns)}/{len(holdings)} 只标的可用，需 ≥{MIN_BARS} 个交易日）"
        if missing:
            note += f"；缺失：{', '.join(missing)}"
        return [{
            "id": "f-corr-insufficient", "category": "correlation",
            "title": "相关性数据不足，暂无法评估分散有效性",
            "severity": Severity.LOW.value, "status": ClaimStatus.UNCERTAIN.value,
            "evidence": [_ev("可计算相关性的标的数", None, "只",
                             "扶摇历史日线（前复权收盘价）", "fuyao", as_of,
                             DataStatus.MISSING.value, note)],
            "rule": {"rule_id": "corr_insufficient_data", "threshold": f"≥2 只标的各 ≥{MIN_BARS} 交易日",
                     "reason": "数据不足时不做相关性判断，显式提示而非静默忽略。"},
            "affected_holdings": [],
        }]

    pairs: list[tuple[str, str, float]] = []
    codes = list(returns.keys())
    for i in range(len(codes)):
        for j in range(i + 1, len(codes)):
            r = _pearson(returns[codes[i]], returns[codes[j]])
            pairs.append((codes[i], codes[j], r))

    avg = sum(p[2] for p in pairs) / len(pairs)
    maxp = max(pairs, key=lambda p: p[2])
    findings: list[dict[str, Any]] = []
    if avg > CORR_AVG_HIGH:
        sev, thr = Severity.HIGH.value, f"平均相关性 > {CORR_AVG_HIGH:g}"
    elif avg > CORR_AVG_MED:
        sev, thr = Severity.MEDIUM.value, f"平均相关性 > {CORR_AVG_MED:g}"
    else:
        sev = None
    if sev:
        top_pairs = sorted(pairs, key=lambda p: p[2], reverse=True)[:3]
        findings.append({
            "id": "f-corr-avg", "category": "correlation",
            "title": f"持仓平均相关性 {avg:.2f}，分散效果有限",
            "severity": sev, "status": ClaimStatus.INFERENCE.value,
            "evidence": [
                _ev("两两日收益平均相关系数", round(avg, 3), "",
                    "扶摇历史日线（前复权）近约 120 自然日日收益 Pearson 相关系数均值",
                    "fuyao", as_of, _worst_status(series_status)),
                _ev("最高相关性持仓对", round(maxp[2], 3), "",
                    f"{name_of[maxp[0]]} × {name_of[maxp[1]]}", "fuyao", as_of,
                    _worst_status(series_status)),
            ],
            "rule": {"rule_id": "avg_pairwise_correlation", "threshold": thr,
                     "reason": "持仓同涨同跌说明分散无效，组合层面没有真正降低风险。"},
            "affected_holdings": [maxp[0], maxp[1]] + [p[0] for p in top_pairs] + [p[1] for p in top_pairs],
        })
    return findings


def _worst_status(statuses: dict[str, str]) -> str:
    order = {DataStatus.ERROR.value: 0, DataStatus.STALE.value: 1,
             DataStatus.MISSING.value: 2, DataStatus.OK.value: 3}
    return min(statuses.values(), key=lambda s: order.get(s, 4)) if statuses else DataStatus.OK.value


# ---------------------------------------------------------------------------
# 3. 估值
# ---------------------------------------------------------------------------

def compute_valuation(holdings: list[dict[str, Any]],
                      valuations: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """估值：PE/PB 极端值与亏损（负 PE）。依赖扶摇估值快照。"""
    findings: list[dict[str, Any]] = []
    if not valuations or not holdings:
        return findings

    # 无任何 ok 估值数据：显式提示缺失，而非静默省略该维度
    ok_statuses = [v.get("data_status") for v in valuations.values() if isinstance(v, dict)]
    if DataStatus.OK.value not in ok_statuses:
        note = "估值数据未配置或不可用（需扶摇 API Key）"
        findings.append({
            "id": "f-val-insufficient", "category": "valuation",
            "title": "估值数据缺失，暂无法评估估值风险",
            "severity": Severity.LOW.value, "status": ClaimStatus.UNCERTAIN.value,
            "evidence": [_ev("可用估值数据的标的数", None, "只", "扶摇估值快照", "fuyao", "",
                             DataStatus.MISSING.value, note)],
            "rule": {"rule_id": "val_insufficient_data", "threshold": "≥1 只标的估值可用",
                     "reason": "估值数据缺失时不评估估值风险，显式提示。"},
            "affected_holdings": [],
        })
        return findings

    pe_high: list[dict[str, Any]] = []
    pb_high: list[dict[str, Any]] = []
    loss: list[dict[str, Any]] = []
    for h in holdings:
        v = valuations.get(h["thscode"]) or {}
        st = v.get("data_status") or DataStatus.MISSING.value
        name = h.get("name") or v.get("name") or h["thscode"]
        as_of = _as_of_ms(v.get("as_of_ms"))
        if st != DataStatus.OK.value:
            continue
        pe = v.get("pe_ttm")
        pb = v.get("pb_mrq")
        if pe is not None and pe < 0:
            loss.append({"name": name, "pe": pe, "st": st, "as_of": as_of})
        elif pe is not None and pe > PE_HIGH:
            pe_high.append({"name": name, "pe": pe, "st": st, "as_of": as_of})
        if pb is not None and pb > PB_HIGH:
            pb_high.append({"name": name, "pb": pb, "st": st, "as_of": as_of})

    if pe_high:
        m = max(pe_high, key=lambda x: x["pe"])
        findings.append({
            "id": "f-val-pe", "category": "valuation",
            "title": f"{len(pe_high)} 只标的 PE(TTM) 处于极端高位（最高 {m['name']} {m['pe']:.0f}x）",
            "severity": Severity.HIGH.value, "status": ClaimStatus.FACT.value,
            "evidence": [_ev(f"{x['name']} PE(TTM)", x["pe"], "倍",
                             "扶摇估值快照·PE(TTM)", "fuyao", x["as_of"], x["st"]) for x in pe_high[:5]],
            "rule": {"rule_id": "pe_extreme", "threshold": f"PE(TTM) > {PE_HIGH:g}",
                     "reason": "PE 处于极端高位意味着价格已计入较高增长预期，估值回归风险大。"},
            "affected_holdings": [h["thscode"] for h in holdings if (h.get("name") or h["thscode"]) in {x["name"] for x in pe_high}],
        })
    if loss:
        findings.append({
            "id": "f-val-loss", "category": "valuation",
            "title": f"{len(loss)} 只标的处于亏损状态（PE 为负）",
            "severity": Severity.HIGH.value, "status": ClaimStatus.FACT.value,
            "evidence": [_ev(f"{x['name']} PE(TTM)", x["pe"], "倍",
                             "扶摇估值快照·PE(TTM)（负值=最近报告期亏损）", "fuyao", x["as_of"], x["st"]) for x in loss],
            "rule": {"rule_id": "negative_pe", "threshold": "PE(TTM) < 0",
                     "reason": "亏损标的无法用市盈率估值，盈利前景存在不确定性。"},
            "affected_holdings": [h["thscode"] for h in holdings if (h.get("name") or h["thscode"]) in {x["name"] for x in loss}],
        })
    if pb_high:
        m = max(pb_high, key=lambda x: x["pb"])
        findings.append({
            "id": "f-val-pb", "category": "valuation",
            "title": f"{len(pb_high)} 只标的 PB 超过 {PB_HIGH:g}x（最高 {m['name']} {m['pb']:.1f}x）",
            "severity": Severity.MEDIUM.value, "status": ClaimStatus.FACT.value,
            "evidence": [_ev(f"{x['name']} PB(MRQ)", x["pb"], "倍",
                             "扶摇估值快照·PB(MRQ)", "fuyao", x["as_of"], x["st"]) for x in pb_high[:5]],
            "rule": {"rule_id": "pb_extreme", "threshold": f"PB > {PB_HIGH:g}",
                     "reason": "PB 过高时，账面价值对股价的支撑有限。"},
            "affected_holdings": [h["thscode"] for h in holdings if (h.get("name") or h["thscode"]) in {x["name"] for x in pb_high}],
        })
    return findings


def _as_of_ms(ms: Any) -> str:
    if not ms:
        return ""
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OSError):
        return str(ms)


# ---------------------------------------------------------------------------
# 4. 波动
# ---------------------------------------------------------------------------

def compute_volatility(holdings: list[dict[str, Any]],
                       close_series: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """波动：历史日收益年化波动率。依赖扶摇历史日线。"""
    vols: list[dict[str, Any]] = []
    for h in holdings:
        s = close_series.get(h["thscode"]) or {}
        closes = s.get("closes") or []
        if (s.get("data_status") or DataStatus.MISSING.value) == DataStatus.OK.value and len(closes) >= MIN_BARS:
            r = _daily_returns(closes)
            vols.append({"name": h.get("name") or h["thscode"], "vol": _annualized_vol_pct(r),
                         "as_of": s.get("as_of") or ""})
    if not vols:
        return [{
            "id": "f-vol-insufficient", "category": "volatility",
            "title": "波动率数据不足，暂无法评估",
            "severity": Severity.LOW.value, "status": ClaimStatus.UNCERTAIN.value,
            "evidence": [_ev("可计算波动率的标的数", None, "只",
                             "扶摇历史日线（前复权收盘价）", "fuyao", "", DataStatus.MISSING.value,
                             "历史日线不足或数据源未配置")],
            "rule": {"rule_id": "vol_insufficient_data", "threshold": f"≥{MIN_BARS} 交易日",
                     "reason": "数据不足时不做波动判断。"},
            "affected_holdings": [],
        }]

    high = [v for v in vols if v["vol"] > VOL_HIGH]
    med = [v for v in vols if VOL_MED < v["vol"] <= VOL_HIGH]
    findings: list[dict[str, Any]] = []
    if high:
        m = max(high, key=lambda x: x["vol"])
        findings.append({
            "id": "f-vol-high", "category": "volatility",
            "title": f"{len(high)} 只标的年化波动率 > {VOL_HIGH:g}%（最高 {m['name']} {m['vol']:.0f}%）",
            "severity": Severity.HIGH.value, "status": ClaimStatus.FACT.value,
            "evidence": [_ev(f"{x['name']} 年化波动率", round(x["vol"], 1), "%",
                             "由扶摇历史日线（前复权）日收益标准差 ×√252 年化", "fuyao", x["as_of"], DataStatus.OK.value)
                         for x in sorted(high, key=lambda v: -v["vol"])[:5]],
            "rule": {"rule_id": "annualized_vol_high", "threshold": f"年化波动率 > {VOL_HIGH:g}%",
                     "reason": "高波动标的回撤风险大，对持仓体验和组合净值稳定性不利。"},
            "affected_holdings": [h["thscode"] for h in holdings if (h.get("name") or h["thscode"]) in {x["name"] for x in high}],
        })
    if med:
        findings.append({
            "id": "f-vol-med", "category": "volatility",
            "title": f"{len(med)} 只标的年化波动率在 {VOL_MED:g}%–{VOL_HIGH:g}% 之间",
            "severity": Severity.MEDIUM.value, "status": ClaimStatus.FACT.value,
            "evidence": [_ev(f"{x['name']} 年化波动率", round(x["vol"], 1), "%",
                             "由扶摇历史日线（前复权）日收益标准差 ×√252 年化", "fuyao", x["as_of"], DataStatus.OK.value)
                         for x in sorted(med, key=lambda v: -v["vol"])[:5]],
            "rule": {"rule_id": "annualized_vol_medium", "threshold": f"{VOL_MED:g}% < 年化波动率 ≤ {VOL_HIGH:g}%",
                     "reason": "中等偏高的波动率会在市场下行时放大组合回撤。"},
            "affected_holdings": [h["thscode"] for h in holdings if (h.get("name") or h["thscode"]) in {x["name"] for x in med}],
        })
    return findings


# ---------------------------------------------------------------------------
# 5. 题材暴露
# ---------------------------------------------------------------------------

def compute_exposure(holdings: list[dict[str, Any]],
                     membership: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """题材暴露：同一概念覆盖多只持仓且权重过重。仅依赖种子概念归属 + 用户权重。"""
    now = now_iso()
    concept_holdings: dict[str, list[dict[str, Any]]] = {}
    for h in holdings:
        for c in (membership.get(h["thscode"]) or {}).get("concepts") or []:
            concept_holdings.setdefault(c, []).append(h)
    findings: list[dict[str, Any]] = []
    for concept, hs in concept_holdings.items():
        if len(hs) < EXPOSURE_COUNT:
            continue
        w = sum(h["weight_pct"] for h in hs)
        if w > EXPOSURE_WEIGHT_HIGH:
            sev, thr = Severity.HIGH.value, f"≥{EXPOSURE_COUNT} 只且权重 > {EXPOSURE_WEIGHT_HIGH:g}%"
        elif w > EXPOSURE_WEIGHT_MED:
            sev, thr = Severity.MEDIUM.value, f"≥{EXPOSURE_COUNT} 只且权重 > {EXPOSURE_WEIGHT_MED:g}%"
        else:
            continue
        names = "、".join(h.get("name") or h["thscode"] for h in hs)
        findings.append({
            "id": f"f-exp-{concept}", "category": "exposure",
            "title": f"「{concept}」概念扎堆：{len(hs)} 只持仓合计 {w:g}%",
            "severity": sev, "status": ClaimStatus.FACT.value,
            "evidence": [
                _ev(f"概念「{concept}」覆盖持仓数", float(len(hs)), "只",
                    "同花顺概念归属（种子数据，静态）", "seed", now, DataStatus.OK.value),
                _ev(f"概念「{concept}」合计权重", w, "%",
                    "用户输入持仓权重", "user_input", now, DataStatus.OK.value),
            ],
            "rule": {"rule_id": "concept_clustering", "threshold": thr,
                     "reason": f"同一题材多只持仓：{names} 高度同源，题材降温时组合缺乏对冲。"},
            "affected_holdings": [h["thscode"] for h in hs],
        })
    # 按严重度与权重排序，最多返回前 5 条
    order = {Severity.HIGH.value: 0, Severity.MEDIUM.value: 1, Severity.LOW.value: 2}
    findings.sort(key=lambda f: (order.get(f["severity"], 9), -sum(h["weight_pct"] for h in concept_holdings.get(
        f["id"].replace("f-exp-", ""), []))))
    return findings[:5]


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------

def analyze(holdings: list[dict[str, Any]], membership: dict[str, dict[str, Any]],
            valuations: dict[str, dict[str, Any]],
            close_series: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """执行全部五类风险计算，返回 finding 列表。

    参数：
    - holdings: [{thscode, name, weight_pct, market}]
    - membership: {thscode: {industry, industry_source, concepts:[..]}}
    - valuations: {thscode: {pe_ttm, pb_mrq, ps_ttm, as_of_ms, data_status}}
    - close_series: {thscode: {closes:[..], dates_ms:[..], data_status, as_of}}
    """
    findings: list[dict[str, Any]] = []
    findings += compute_concentration(holdings, membership)
    findings += compute_correlation(holdings, close_series)
    findings += compute_valuation(holdings, valuations)
    findings += compute_volatility(holdings, close_series)
    findings += compute_exposure(holdings, membership)
    return findings
