"""合规边界测试：不荐股、不预测、不编造数字、AI 待复核、不泄露凭证。"""
from __future__ import annotations

import os
from pathlib import Path

FORBIDDEN = ["买入", "卖出", "增持", "减持", "目标价", "看涨", "看跌", "必涨",
             "抄底", "加仓", "减仓", "收益承诺", "保本", "稳赚", "预测涨幅"]


def _report(client):
    hs = [
        {"thscode": "600519.SH", "name": "贵州茅台", "weight_pct": 33},
        {"thscode": "300750.SZ", "name": "宁德时代", "weight_pct": 22},
        {"thscode": "002594.SZ", "name": "比亚迪", "weight_pct": 16},
        {"thscode": "601012.SH", "name": "隆基绿能", "weight_pct": 10},
        {"thscode": "002475.SZ", "name": "立讯精密", "weight_pct": 12},
        {"thscode": "688981.SH", "name": "中芯国际", "weight_pct": 7},
    ]
    return client.post("/api/check", json={"name": "合规测试", "holdings": hs}).json()


class TestNoAdvice:
    def test_no_buy_sell_advice(self, client):
        data = _report(client)
        texts = [data["overall"].get("text", "")]
        for f in data["findings"]:
            if f.get("narrative"):
                texts.append(f["narrative"]["text"])
        blob = "".join(texts)
        for word in FORBIDDEN:
            assert word not in blob, f"出现违禁词「{word}」"


class TestNumberProvenance:
    def test_every_evidence_has_provenance(self, client):
        data = _report(client)
        for f in data["findings"]:
            for e in f["evidence"]:
                assert e["source"], f"{f['id']}/{e['label']} 缺 source"
                assert e["unit"] != "" or e["value"] is not None, f"{f['id']}/{e['label']} 数值缺单位"
                if e["data_status"] == "ok":
                    assert e["value"] is not None
                    assert e["caliber"]

    def test_failure_status_never_fake(self, client):
        data = _report(client)
        checked = 0
        for f in data["findings"]:
            for e in f["evidence"]:
                if e["data_status"] != "ok":
                    assert e["value"] is None
                    checked += 1
        assert checked >= 3, "无凭证环境应至少产生 3 个显式失败的数据点（估值/相关性/波动）"


class TestAITraceability:
    def test_narratives_pending(self, client):
        data = _report(client)
        assert data["overall"]["review_status"] == "pending"
        for f in data["findings"]:
            assert f["narrative"]["review_status"] == "pending"
            assert f["narrative"]["ai_output_id"]

    def test_ai_outputs_stored(self, client):
        data = _report(client)
        ids = {data["overall"]["ai_output_id"]}
        ids |= {f["narrative"]["ai_output_id"] for f in data["findings"]}
        outputs = client.get("/api/ai-outputs").json()["outputs"]
        stored_ids = {o["id"] for o in outputs}
        assert ids <= stored_ids

    def test_rules_engine_used_without_key(self, client):
        data = _report(client)
        assert data["overall"]["engine"] == "rules"
        assert all(f["narrative"]["engine"] == "rules" for f in data["findings"])


class TestNoCredentialLeak:
    def test_health_does_not_leak_keys(self, client):
        d = client.get("/api/health").json()
        for src in d["sources"].values():
            # 无凭证环境：必须报未配置，且只返回 name/configured/status/message，绝不回传密钥值
            assert src["status"] == "disabled"
            assert src["configured"] is False
            assert set(src.keys()) <= {"name", "configured", "status", "message"}

    def test_gitignore_excludes_env(self):
        root = Path(__file__).resolve().parent.parent
        gi = (root / ".gitignore").read_text(encoding="utf-8")
        assert ".env" in gi
