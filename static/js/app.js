"use strict";

const CAT_LABEL = {
  concentration: "集中度", correlation: "相关性", valuation: "估值",
  volatility: "波动", exposure: "题材暴露",
};
const SEV_LABEL = { high: "高风险", medium: "中风险", low: "低风险" };
const STATUS_LABEL = { fact: "事实", inference: "推断", uncertain: "不确定" };
const DATA_STATUS_LABEL = { ok: "可用", stale: "过期", missing: "缺失", error: "错误" };

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function badge(cls, text) {
  const b = el("span", "badge " + cls, text);
  return b;
}

// ---------------- 持仓输入 ----------------
function addHoldingRow(thscode = "", name = "", weight = "") {
  const row = el("div", "holding-row");
  const codeIn = el("input");
  codeIn.type = "text"; codeIn.placeholder = "thscode（如 600519.SH）"; codeIn.value = thscode; codeIn.dataset.field = "thscode";
  const nameIn = el("input");
  nameIn.type = "text"; nameIn.placeholder = "名称（可选）"; nameIn.value = name; nameIn.dataset.field = "name";
  const wIn = el("input");
  wIn.type = "number"; wIn.placeholder = "权重 %"; wIn.min = "0"; wIn.max = "100"; wIn.step = "0.1"; wIn.value = weight; wIn.dataset.field = "weight";
  const rm = el("button", "rm", "×"); rm.type = "button";
  rm.addEventListener("click", () => { row.remove(); updateWeightBar(); });
  row.append(codeIn, nameIn, wIn, rm);
  document.getElementById("holdings").appendChild(row);
  [codeIn, nameIn, wIn].forEach((i) => i.addEventListener("input", updateWeightBar));
  updateWeightBar();
}

function collectHoldings() {
  const out = [];
  document.querySelectorAll("#holdings .holding-row").forEach((row) => {
    const thscode = row.querySelector('[data-field="thscode"]').value.trim();
    const name = row.querySelector('[data-field="name"]').value.trim();
    const weight = parseFloat(row.querySelector('[data-field="weight"]').value);
    if (thscode && !isNaN(weight) && weight > 0) out.push({ thscode, name, weight_pct: weight });
  });
  return out;
}

function updateWeightBar() {
  const holdings = collectHoldings();
  const total = holdings.reduce((s, h) => s + h.weight_pct, 0);
  const bar = document.getElementById("weight-bar");
  bar.innerHTML = "";
  if (total > 0) bar.appendChild(Object.assign(el("span"), { style: `width:${Math.min(total, 100)}%` }));
  bar.title = `合计 ${total.toFixed(1)}%`;
  document.getElementById("btn-check").disabled = holdings.length === 0;
}

async function loadSample() {
  const r = await fetch("/api/sample-portfolio");
  const data = await r.json();
  document.getElementById("holdings").innerHTML = "";
  data.holdings.forEach((h) => addHoldingRow(h.thscode, h.name, h.weight_pct));
}

// ---------------- 数据源状态 ----------------
async function refreshHealth() {
  try {
    const r = await fetch("/api/health");
    const d = await r.json();
    const s = d.sources || {};
    const fuyao = s.fuyao ? (s.fuyao.status === "ok" ? "扶摇已连接" : "扶摇未配置") : "扶摇未配置";
    const ifind = s.ifind ? (s.ifind.status === "ok" ? "iFinD 已连接" : "iFinD 未配置") : "iFinD 未配置";
    document.getElementById("source-status").textContent = `${fuyao} · ${ifind}`;
  } catch (e) {
    document.getElementById("source-status").textContent = "数据源状态未知";
  }
}

// ---------------- 报告渲染 ----------------
function renderReport(rep) {
  document.getElementById("report-empty").hidden = true;
  const box = document.getElementById("report");
  box.hidden = false;
  box.innerHTML = "";

  // 整体摘要
  const ov = rep.overall || {};
  const ovBox = el("div", "overall");
  ovBox.appendChild(el("h3", null, "整体摘要 " + (ov.engine === "llm" ? "（AI 生成）" : "（规则引擎）")));
  ovBox.appendChild(el("p", null, ov.text || ""));
  ovBox.appendChild(renderPending(ov));
  box.appendChild(ovBox);

  // 持仓快照
  const snap = el("div", "meta-line", "持仓快照：");
  (rep.holdings_snapshot || []).forEach((h) => {
    const price = h.last_price != null ? h.last_price : "—";
    const chg = h.price_change_ratio_pct != null ? `（${h.price_change_ratio_pct > 0 ? "+" : ""}${h.price_change_ratio_pct}%）` : "";
    snap.appendChild(badge(h.data_status === "ok" ? "ok" : h.data_status, DATA_STATUS_LABEL[h.data_status] || h.data_status));
    snap.appendChild(document.createTextNode(` ${h.name || h.thscode} ${h.weight_pct}% · ${price}${chg}   `));
  });
  box.appendChild(snap);

  // 风险发现
  const findings = rep.findings || [];
  box.appendChild(el("h3", null, `风险发现（${findings.length} 项）`));
  findings.forEach((f) => box.appendChild(renderFinding(f)));

  box.appendChild(el("div", "meta-line",
    `报告编号 ${rep.id} · 生成于 ${rep.generated_at} · 结论可回溯，AI 产出待复核`));
}

function renderPending(n) {
  const line = el("div", "n-head");
  line.appendChild(badge("pending", "待复核"));
  line.appendChild(el("b", null, `引擎：${n.engine === "llm" ? "LLM" : "规则引擎"}`));
  return line;
}

function renderFinding(f) {
  const box = el("div", "finding " + f.severity);
  const head = el("div", "finding-head");
  head.appendChild(badge(f.severity, SEV_LABEL[f.severity] || f.severity));
  head.appendChild(badge("status-" + f.status, STATUS_LABEL[f.status] || f.status));
  head.appendChild(el("span", "category", CAT_LABEL[f.category] || f.category));
  head.appendChild(el("h3", null, f.title));
  box.appendChild(head);

  const evList = el("ul", "evidence");
  (f.evidence || []).forEach((e) => {
    const li = el("li");
    li.appendChild(el("span", "ev-label", e.label + "："));
    const val = e.value != null ? `${e.value}${e.unit || ""}` : "—";
    li.appendChild(el("span", "ev-value", val));
    li.appendChild(el("span", "ev-meta",
      `${e.source || ""}${e.as_of ? " · " + e.as_of : ""}${e.caliber ? " · " + e.caliber : ""}`));
    li.appendChild(badge(e.data_status === "ok" ? "ok" : e.data_status, DATA_STATUS_LABEL[e.data_status] || e.data_status));
    evList.appendChild(li);
  });
  box.appendChild(evList);

  if (f.rule) {
    box.appendChild(el("div", "rule", `规则：${f.rule.rule_id} · 阈值 ${f.rule.threshold} · ${f.rule.reason}`));
  }

  if (f.narrative) {
    const n = el("div", "narrative");
    const nHead = el("div", "n-head");
    nHead.appendChild(el("b", null, "解读"));
    nHead.appendChild(renderPending(f.narrative));
    n.appendChild(nHead);
    n.appendChild(el("p", null, f.narrative.text || ""));
    box.appendChild(n);
  }
  return box;
}

// ---------------- 主流程 ----------------
async function runCheck() {
  const holdings = collectHoldings();
  const total = holdings.reduce((s, h) => s + h.weight_pct, 0);
  if (Math.abs(total - 100) > 1) {
    alert(`权重之和应为 100%（当前 ${total.toFixed(1)}%），请调整后再体检。`);
    return;
  }
  const btn = document.getElementById("btn-check");
  btn.disabled = true; btn.textContent = "体检中…";
  try {
    const r = await fetch("/api/check", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: "我的组合", holdings }),
    });
    if (!r.ok) {
      const err = await r.json().catch(() => ({}));
      alert("体检失败：" + (err.detail || r.status));
      return;
    }
    renderReport(await r.json());
    refreshHealth();
  } catch (e) {
    alert("请求失败：" + e.message);
  } finally {
    btn.disabled = false; btn.textContent = "开始体检";
  }
}

document.getElementById("btn-sample").addEventListener("click", loadSample);
document.getElementById("btn-add").addEventListener("click", () => addHoldingRow());
document.getElementById("btn-check").addEventListener("click", runCheck);
refreshHealth();
addHoldingRow();
