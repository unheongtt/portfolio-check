"""AI 分析引擎：把风险引擎的确定性发现合成为自然语言解读。

角色边界（写入 README）：
- 引擎只做「读与讲」：把 evidence 里的结构化数字转译成"为什么这是风险"；
- 绝不生成任何数字——所有数值只能来自 risk/engine.py 的确定性计算与结构化数据源；
- 每个 AI 产出落库 ai_output，携带模型/时间/输入引用/复核状态，未复核前 UI 显示「待复核」。

LLM 未配置时全部走规则模板（确定性、可复现），并显式标注 engine=rules。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import httpx

from ..config import get_settings
from ..db import get_db
from ..models import AIOutput, RISK_CATEGORY_LABELS, Severity, now_iso
from .rules import narrative_for_finding, summarize_rule

NARRATIVE_SYSTEM_PROMPT = """你是投资组合风险解读助手。给定一个风险发现（含确定性证据数字），用中文写 2–4 句解读：该风险是什么、为什么重要、需要注意什么。
硬约束：
- 只能引用 evidence 里已有的数字与事实，禁止编造任何数字、日期或比例；
- 事实与推断要区分：事实用「数据显示」，推断用「可能/意味着」；
- 不输出任何买入/卖出/目标价/收益承诺/涨跌预测；
- 不给确定性投资建议，最多说「可关注/复核」。
输出严格 JSON：{"narrative": "解读文本", "status": "fact|inference|uncertain"}"""

SUMMARY_SYSTEM_PROMPT = """你是投资组合体检报告摘要助手。给定一个组合的若干风险发现，用 3–5 句概括整体风险状况与最需关注的方面。
硬约束同上：只引用给定内容中的数字；不编造；不输出买卖/涨跌预测/收益承诺；事实与推断区分。
输出严格 JSON：{"narrative": "摘要文本"}"""


class AIEngine:
    def __init__(self) -> None:
        s = get_settings()
        self.llm_key = s.llm_api_key
        self.llm_base = s.llm_base_url.rstrip("/") if s.llm_base_url else ""
        self.llm_model = s.llm_model
        self.db = get_db()

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_key and self.llm_base)

    def _prompt_hash(self, prompt: str) -> str:
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]

    def _store(self, kind: str, model: str, prompt_hash: str,
               input_refs: list[str], output: dict[str, Any]) -> AIOutput:
        out = AIOutput(
            id=f"ai-{uuid.uuid4().hex[:12]}", kind=kind, model=model,
            prompt_hash=prompt_hash, input_refs=input_refs, output=output,
            review_status="pending",
        )
        self.db.upsert(
            "ai_output",
            {"id": out.id, "kind": out.kind, "model": out.model,
             "prompt_hash": out.prompt_hash, "input_refs": json.dumps(out.input_refs, ensure_ascii=False),
             "output": json.dumps(out.output, ensure_ascii=False),
             "review_status": out.review_status, "created_at": out.created_at},
            ["id"],
        )
        return out

    async def _llm_json(self, system: str, user: str) -> dict[str, Any]:
        headers = {
            "x-api-key": self.llm_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        body = {
            "model": self.llm_model or "claude-sonnet-4-5",
            "max_tokens": 768,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{self.llm_base}/v1/messages", json=body, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
        text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")
        return json.loads(text.strip().lstrip("`json").strip("`").strip())

    def _finding_prompt(self, finding: dict[str, Any]) -> str:
        label = RISK_CATEGORY_LABELS.get(finding["category"], finding["category"])
        evidence = json.dumps(finding.get("evidence", []), ensure_ascii=False)
        rule = (finding.get("rule") or {}).get("reason", "")
        return (f"风险类别：{label}\n标题：{finding['title']}\n严重度：{finding['severity']}\n"
                f"证据（含数字与来源）：{evidence}\n规则依据：{rule}")

    # ------------------------------------------------------------------ 单项解读
    async def narrate_finding(self, finding: dict[str, Any]) -> dict[str, Any]:
        """为单个风险发现生成解读。LLM 优先，失败/未配置降级规则模板。"""
        prompt = self._finding_prompt(finding)
        if self.llm_enabled:
            try:
                r = await self._llm_json(NARRATIVE_SYSTEM_PROMPT, prompt)
                text = str(r.get("narrative", "")).strip()
                if not text:
                    raise ValueError("empty narrative")
                out = {"engine": "llm", "model": self.llm_model or "llm",
                       "narrative": text, "status": r.get("status", "inference")}
                stored = self._store("risk_narrative", self.llm_model or "llm",
                                     self._prompt_hash(NARRATIVE_SYSTEM_PROMPT + prompt), [finding["id"]], out)
                return {"text": out["narrative"], "engine": "llm", "model": out["model"],
                        "review_status": "pending", "ai_output_id": stored.id,
                        "status": out["status"], "llm_fallback": False}
            except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError) as e:
                fallback = narrative_for_finding(finding)
                stored = self._store("risk_narrative", "rules(fallback)",
                                     self._prompt_hash(prompt), [finding["id"]],
                                     {"engine": "rules", "narrative": fallback,
                                      "llm_fallback_reason": f"LLM 调用失败降级规则模板: {type(e).__name__}"})
                return {"text": fallback, "engine": "rules", "model": "",
                        "review_status": "pending", "ai_output_id": stored.id,
                        "status": finding.get("status", "inference"), "llm_fallback": True}
        fallback = narrative_for_finding(finding)
        stored = self._store("risk_narrative", "rules",
                             self._prompt_hash(prompt), [finding["id"]],
                             {"engine": "rules", "narrative": fallback})
        return {"text": fallback, "engine": "rules", "model": "",
                "review_status": "pending", "ai_output_id": stored.id,
                "status": finding.get("status", "inference"), "llm_fallback": False}

    # ------------------------------------------------------------------ 整体摘要
    async def summarize(self, findings: list[dict[str, Any]], portfolio_name: str) -> dict[str, Any]:
        """生成整体摘要。LLM 优先，失败/未配置降级规则模板。"""
        if not self.llm_enabled:
            text = summarize_rule(findings, portfolio_name)
            stored = self._store("overall_summary", "rules",
                                 self._prompt_hash(portfolio_name), [],
                                 {"engine": "rules", "narrative": text})
            return {"text": text, "engine": "rules", "model": "",
                    "review_status": "pending", "ai_output_id": stored.id}
        compact = [{"title": f["title"], "severity": f["severity"],
                    "category": RISK_CATEGORY_LABELS.get(f["category"], f["category"])}
                   for f in findings]
        prompt = f"组合：{portfolio_name}\n风险发现：{json.dumps(compact, ensure_ascii=False)}"
        try:
            r = await self._llm_json(SUMMARY_SYSTEM_PROMPT, prompt)
            text = str(r.get("narrative", "")).strip()
            if not text:
                raise ValueError("empty summary")
            out = {"engine": "llm", "model": self.llm_model or "llm", "narrative": text}
            stored = self._store("overall_summary", self.llm_model or "llm",
                                 self._prompt_hash(SUMMARY_SYSTEM_PROMPT + prompt), [], out)
            return {"text": text, "engine": "llm", "model": out["model"],
                    "review_status": "pending", "ai_output_id": stored.id}
        except (httpx.HTTPError, KeyError, ValueError, json.JSONDecodeError) as e:
            text = summarize_rule(findings, portfolio_name)
            stored = self._store("overall_summary", "rules(fallback)",
                                 self._prompt_hash(prompt), [],
                                 {"engine": "rules", "narrative": text,
                                  "llm_fallback_reason": f"LLM 调用失败降级规则模板: {type(e).__name__}"})
            return {"text": text, "engine": "rules", "model": "",
                    "review_status": "pending", "ai_output_id": stored.id}


_engine: AIEngine | None = None


def get_engine() -> AIEngine:
    global _engine
    if _engine is None:
        _engine = AIEngine()
    return _engine
