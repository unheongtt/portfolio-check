"""主链路测试：示例组合 → 体检 → 报告 → 追溯。无凭证环境下验证显式失败。"""
from __future__ import annotations


class TestSamplePortfolio:
    def test_sample_portfolio(self, client):
        data = client.get("/api/sample-portfolio").json()
        assert data["id"] == "sample"
        assert len(data["holdings"]) == 6
        total = sum(h["weight_pct"] for h in data["holdings"])
        assert abs(total - 100) < 0.01


class TestCheck:
    def _sample(self):
        return [
            {"thscode": "600519.SH", "name": "贵州茅台", "weight_pct": 33},
            {"thscode": "300750.SZ", "name": "宁德时代", "weight_pct": 22},
            {"thscode": "002594.SZ", "name": "比亚迪", "weight_pct": 16},
            {"thscode": "601012.SH", "name": "隆基绿能", "weight_pct": 10},
            {"thscode": "002475.SZ", "name": "立讯精密", "weight_pct": 12},
            {"thscode": "688981.SH", "name": "中芯国际", "weight_pct": 7},
        ]

    def test_check_returns_report(self, client):
        r = client.post("/api/check", json={"name": "测试组合", "holdings": self._sample()})
        assert r.status_code == 200
        data = r.json()
        assert data["id"].startswith("rpt-")
        assert data["portfolio"]["name"] == "测试组合"
        cats = {f["category"] for f in data["findings"]}
        # 静态维度（集中度/题材暴露）无需凭证即可出结论
        assert "concentration" in cats
        assert "exposure" in cats
        # 数据依赖维度（相关性/估值/波动）无凭证时显式"数据不足"
        assert "correlation" in cats
        assert "valuation" in cats
        assert "volatility" in cats

    def test_check_no_fake_numbers_without_key(self, client):
        data = client.post("/api/check", json={"name": "t", "holdings": self._sample()}).json()
        for f in data["findings"]:
            for e in f["evidence"]:
                if e["data_status"] != "ok":
                    assert e["value"] is None, f"{f['id']}/{e['label']} 非 ok 状态却带数值"
                    assert e.get("note") or e.get("caliber"), f"{f['id']}/{e['label']} 失败无原因"

    def test_check_rejects_weight_sum(self, client):
        hs = [{"thscode": "600519.SH", "name": "茅台", "weight_pct": 80}]
        r = client.post("/api/check", json={"name": "t", "holdings": hs})
        assert r.status_code == 400

    def test_check_rejects_empty(self, client):
        r = client.post("/api/check", json={"name": "t", "holdings": []})
        assert r.status_code == 422  # pydantic min_length=1

    def test_report_persisted_and_retrievable(self, client):
        data = client.post("/api/check", json={"name": "持久化", "holdings": self._sample()}).json()
        rid = data["id"]
        got = client.get(f"/api/reports/{rid}").json()
        assert got["id"] == rid
        assert got["portfolio"]["name"] == "持久化"

    def test_report_404(self, client):
        assert client.get("/api/reports/not-exist").status_code == 404
