"""风险引擎单元测试：五类风险计算的确定性口径。"""
from __future__ import annotations

from app.risk.engine import (
    compute_concentration,
    compute_correlation,
    compute_exposure,
    compute_valuation,
    compute_volatility,
    _daily_returns,
    _pearson,
    _annualized_vol_pct,
)


class TestConcentration:
    def test_single_weight_high(self, sample_holdings, sample_membership):
        fs = compute_concentration(sample_holdings, sample_membership)
        single = [f for f in fs if f["id"] == "f-conc-single"]
        assert single and single[0]["severity"] == "high"
        assert single[0]["evidence"][0]["value"] == 33
        assert single[0]["evidence"][0]["source"] == "user_input"

    def test_top3_concentration(self, sample_holdings, sample_membership):
        fs = compute_concentration(sample_holdings, sample_membership)
        top3 = [f for f in fs if f["id"] == "f-conc-top3"]
        assert top3 and top3[0]["severity"] == "high"  # 33+22+16 = 71 > 70

    def test_diversified_no_finding(self):
        hs = [{"thscode": f"c{i}", "name": f"n{i}", "weight_pct": 16} for i in range(6)]
        hs.append({"thscode": "c6", "name": "n6", "weight_pct": 4})
        fs = compute_concentration(hs, {})
        assert all(f["id"] != "f-conc-single" for f in fs), "max 16% 不应触发单标的"
        # top3 = 48% < 55%，不触发
        assert all(f["id"] != "f-conc-top3" for f in fs)

    def test_evidence_has_provenance(self, sample_holdings, sample_membership):
        fs = compute_concentration(sample_holdings, sample_membership)
        for f in fs:
            for e in f["evidence"]:
                assert e["source"], f"{f['id']} 证据缺 source"
                assert e["caliber"], f"{f['id']} 证据缺 caliber"
                assert e["data_status"] == "ok"


class TestCorrelation:
    def _series(self, a, b):
        return {
            "a": {"closes": a, "data_status": "ok", "as_of": "2026-10-01"},
            "b": {"closes": b, "data_status": "ok", "as_of": "2026-10-01"},
        }

    def test_highly_correlated(self):
        a = [100 * (1 + 0.01 * i) for i in range(30)]
        b = [200 * (1 + 0.01 * i) for i in range(30)]
        hs = [{"thscode": "a", "name": "A", "weight_pct": 50},
              {"thscode": "b", "name": "B", "weight_pct": 50}]
        fs = compute_correlation(hs, self._series(a, b))
        assert any(f["id"] == "f-corr-avg" and f["severity"] == "high" for f in fs)

    def test_insufficient_data(self):
        hs = [{"thscode": "a", "name": "A", "weight_pct": 100}]
        series = {"a": {"closes": [], "data_status": "missing", "as_of": ""}}
        fs = compute_correlation(hs, series)
        assert any(f["id"] == "f-corr-insufficient" and f["status"] == "uncertain" for f in fs)

    def test_pearson_perfect(self):
        xs = [1.0, 2.0, 3.0, 4.0, 5.0]
        assert abs(_pearson(xs, xs) - 1.0) < 1e-9


class TestValuation:
    def test_pe_extreme(self):
        vals = {"a": {"pe_ttm": 120, "pb_mrq": 3, "as_of_ms": 1700000000000, "data_status": "ok"}}
        hs = [{"thscode": "a", "name": "A", "weight_pct": 100}]
        fs = compute_valuation(hs, vals)
        assert any(f["id"] == "f-val-pe" and f["severity"] == "high" for f in fs)

    def test_negative_pe(self):
        vals = {"a": {"pe_ttm": -5, "pb_mrq": 1, "as_of_ms": 1700000000000, "data_status": "ok"}}
        fs = compute_valuation([{"thscode": "a", "name": "A", "weight_pct": 100}], vals)
        assert any(f["id"] == "f-val-loss" for f in fs)

    def test_insufficient_when_no_ok(self):
        vals = {"a": {"data_status": "error", "fetch_error": "未配置"}}
        fs = compute_valuation([{"thscode": "a", "name": "A", "weight_pct": 100}], vals)
        assert any(f["id"] == "f-val-insufficient" and f["status"] == "uncertain" for f in fs)

    def test_no_finding_when_reasonable(self):
        vals = {"a": {"pe_ttm": 15, "pb_mrq": 2, "as_of_ms": 1700000000000, "data_status": "ok"}}
        fs = compute_valuation([{"thscode": "a", "name": "A", "weight_pct": 100}], vals)
        assert fs == []


class TestVolatility:
    def test_high_volatility(self):
        import math
        # 构造大幅震荡的价格序列
        closes = [100 + 15 * math.sin(i) for i in range(60)]
        series = {"a": {"closes": closes, "data_status": "ok", "as_of": "2026-10-01"}}
        hs = [{"thscode": "a", "name": "A", "weight_pct": 100}]
        vol = _annualized_vol_pct(_daily_returns(closes))
        assert vol > 25, f"期望高波动，实际 {vol:.1f}%"
        fs = compute_volatility(hs, series)
        assert any(f["id"] in ("f-vol-high", "f-vol-med") for f in fs)

    def test_insufficient_data(self):
        series = {"a": {"closes": [100, 101], "data_status": "ok", "as_of": ""}}
        fs = compute_volatility([{"thscode": "a", "name": "A", "weight_pct": 100}], series)
        assert any(f["id"] == "f-vol-insufficient" for f in fs)


class TestExposure:
    def test_concept_clustering(self, sample_holdings, sample_membership):
        fs = compute_exposure(sample_holdings, sample_membership)
        new_energy = [f for f in fs if "新能源" in f["title"]]
        assert new_energy, "新能源概念应被识别（宁德+比亚迪+隆基）"
        assert new_energy[0]["severity"] == "high"  # 22+16+10 = 48 > 40

    def test_no_clustering_below_threshold(self):
        hs = [{"thscode": "a", "name": "A", "weight_pct": 50},
              {"thscode": "b", "name": "B", "weight_pct": 50}]
        mem = {"a": {"industry": "x", "concepts": ["c1"]}, "b": {"industry": "y", "concepts": ["c2"]}}
        assert compute_exposure(hs, mem) == []
