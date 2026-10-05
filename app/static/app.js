/* 乱序轨迹复核器前端：编辑指令/事件，调用真实 API，渲染逐步状态与违规证据。 */
"use strict";

const SAMPLES = {
  correct: {
    rob_depth: 16,
    instructions: [
      { seq: 1, op: "add", dst: 1, src1: 2, src2: 3 },
      { seq: 2, op: "add", dst: 2, src1: 1, src2: 4 },
      { seq: 3, op: "beq", src1: 1, src2: 2, predicted: false },
      { seq: 4, op: "add", dst: 3, src1: 1 },
      { seq: 5, op: "lw", dst: 4, src1: 3 },
      { seq: 6, op: "add", dst: 5, src1: 4 }
    ],
    events: [
      { type: "dispatch", seq: 1 },
      { type: "dispatch", seq: 2 },
      { type: "dispatch", seq: 3 },
      { type: "dispatch", seq: 4 },
      { type: "dispatch", seq: 5 },
      { type: "dispatch", seq: 6 },
      { type: "writeback", seq: 1, value: 10 },
      { type: "writeback", seq: 2, value: 20 },
      { type: "writeback", seq: 4, value: 40 },
      { type: "resolve", seq: 3, target: "taken" },
      { type: "writeback", seq: 6, value: 60 },
      { type: "writeback", seq: 3 },
      { type: "commit", seq: 1 },
      { type: "commit", seq: 2 },
      { type: "commit", seq: 3 }
    ]
  },
  violation: {
    rob_depth: 16,
    instructions: [
      { seq: 1, op: "add", dst: 1, src1: 2 },
      { seq: 2, op: "add", dst: 2, src1: 1 }
    ],
    events: [
      { type: "dispatch", seq: 1 },
      { type: "dispatch", seq: 2 },
      { type: "writeback", seq: 2, value: 2 },
      // 队首 seq=1 尚未写回，却试图先提交 seq=2 —— 乱序提交违规
      { type: "commit", seq: 2 }
    ]
  },
  exception: {
    rob_depth: 16,
    instructions: [
      { seq: 1, op: "lw", dst: 1 },
      { seq: 2, op: "add", dst: 2, src1: 1 },
      { seq: 3, op: "add", dst: 3 }
    ],
    events: [
      { type: "dispatch", seq: 1 },
      { type: "dispatch", seq: 2 },
      { type: "dispatch", seq: 3 },
      { type: "writeback", seq: 2, value: 22 },
      { type: "writeback", seq: 3, value: 33 },
      { type: "exception", seq: 1, cause: "page fault" }
    ]
  }
};

const $ = (sel) => document.querySelector(sel);
const instrEl = $("#instrInput");
const eventEl = $("#eventInput");

function loadSample(name) {
  const s = SAMPLES[name];
  instrEl.value = JSON.stringify(s.instructions, null, 2);
  eventEl.value = JSON.stringify(s.events, null, 2);
  $("#robDepth").value = s.rob_depth;
  $("#physRegs").value = "";
  hideError();
}

document.querySelectorAll("[data-sample]").forEach((btn) =>
  btn.addEventListener("click", () => loadSample(btn.dataset.sample))
);

function showError(msg) {
  const el = $("#inputError");
  el.textContent = msg;
  el.classList.remove("hidden");
}
function hideError() { $("#inputError").classList.add("hidden"); }

async function checkHealth() {
  const el = $("#health");
  try {
    const res = await fetch("/healthz");
    if (!res.ok) throw new Error();
    el.textContent = "● API 在线";
    el.className = "health ok";
  } catch {
    el.textContent = "● API 不可达";
    el.className = "health bad";
  }
}

async function submit() {
  hideError();
  let instructions, events;
  try {
    instructions = JSON.parse(instrEl.value || "[]");
  } catch (e) { showError("instructions JSON 解析失败：" + e.message); return; }
  try {
    events = JSON.parse(eventEl.value || "[]");
  } catch (e) { showError("events JSON 解析失败：" + e.message); return; }

  const trace = { instructions, events, rob_depth: Number($("#robDepth").value) || 16 };
  const phys = $("#physRegs").value.trim();
  if (phys) trace.physical_registers = Number(phys);

  const btn = $("#analyzeBtn");
  btn.disabled = true;
  btn.textContent = "复核中…";
  try {
    const res = await fetch("/api/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(trace)
    });
    const data = await res.json();
    if (!res.ok) { showError(data.error || "分析失败"); return; }
    render(data);
  } catch (e) {
    showError("请求失败：" + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "提交复核";
  }
}
$("#analyzeBtn").addEventListener("click", submit);

/* ---------------- 渲染 ---------------- */

function mapRows(before, after) {
  const regs = [...Array(8).keys()];
  return regs.map((r) => {
    const b = before.map_table[r], a = after.map_table[r];
    return `<tr>
      <th>r${r}</th>
      <td>p${b}</td>
      <td class="${b !== a ? "changed" : ""}">p${a}${b !== a ? " ←" : ""}</td>
      <td class="committed">p${after.committed_map[r]}</td>
    </tr>`;
  }).join("");
}

function robHtml(snap) {
  if (!snap.rob.length) return '<div class="cp-list">（ROB 已清空）</div>';
  return '<div class="rob-list">' + snap.rob.map((e) => {
    const cls = e.at_head ? "rob-item head" : "rob-item";
    const mark = e.completed ? "✓" : "…";
    return `<span class="${cls}" title="ROB#${e.rob_seq} ${e.op}${e.is_branch ? " [分支检查点]" : ""}">
      ${e.at_head ? "队首 " : ""}#${e.seq} ${e.op}${e.dst !== undefined && e.dst !== null ? " r" + e.dst : ""}
      <span class="tag">${e.tag >= 0 ? "p" + e.tag : ""} ${mark}${e.is_branch ? " ⚑" : ""}</span>
    </span>`;
  }).join("") + "</div>";
}

function cpsHtml(snap) {
  if (!snap.checkpoints.length) return '<div class="cp-list">无活动检查点</div>';
  return '<div class="cp-list">' + snap.checkpoints.map((c) =>
    `⚑ ROB#${c.rob_seq} (seq=${c.instr_seq}) 映射 ${JSON.stringify(c.map_table)}`
  ).join("<br>") + "</div>";
}

function freeHtml(before, after) {
  const bf = new Set(before.free), af = new Set(after.free);
  const added = [...af].filter((t) => !bf.has(t));
  const removed = [...bf].filter((t) => !af.has(t));
  const dead = (after.dead_tags || []).map((d) => d.tag);
  const parts = [];
  if (after.free.length) parts.push(`空闲: ${after.free.map((t) => "p" + t).join(" ")}`);
  if (added.length) parts.push(`<span class="recycled">新回收: ${added.map((t) => "p" + t).join(" ")}</span>`);
  if (removed.length) parts.push(`已分配: ${removed.map((t) => "p" + t).join(" ")}`);
  if (dead.length) parts.push(`<span class="dead">回滚隔离: ${dead.map((t) => "p" + t).join(" ")}</span>`);
  return `<div class="free-list">${parts.join("<br>") || "无空闲标签"}</div>`;
}

function renderState(title, before, after) {
  return `<div class="state-block">
    <h4>${title}</h4>
    <table class="map">
      <tr><th>寄存器</th><th>事件前推测映射</th><th>事件后推测映射</th><th>已提交映射</th></tr>
      ${mapRows(before, after)}
    </table>
    <h4 style="margin-top:10px">ROB</h4>${robHtml(after)}
    <h4 style="margin-top:10px">分支检查点</h4>${cpsHtml(after)}
    <h4 style="margin-top:10px">物理标签</h4>${freeHtml(before, after)}
  </div>`;
}

function evTitle(ev) {
  const t = ev.event.type;
  const parts = [`#${ev.index}`, t.toUpperCase()];
  if (ev.event.seq !== undefined) parts.push(`seq=${ev.event.seq}`);
  if (ev.event.tag !== undefined) parts.push(`tag=p${ev.event.tag}`);
  return parts.join(" · ");
}

function renderEvent(ev, violationIndex) {
  const isViolation = !ev.ok;
  const card = document.createElement("div");
  card.className = "ev" + (isViolation ? " first-violation open" : "");
  card.dataset.index = ev.index;
  const body = [];
  body.push(`<div class="ev-head">
    <span class="ev-idx">${String(ev.index).padStart(2, "0")}</span>
    <span class="ev-type t-${ev.event.type}">${ev.event.type}</span>
    <span class="ev-summary">${ev.message ? escapeHtml(ev.message) : escapeHtml(JSON.stringify(ev.event))}</span>
    <span class="ev-outcome o-${ev.outcome}">${ev.outcome}</span>
  </div>`);
  body.push('<div class="ev-body">');
  body.push(`<div class="split">
    ${renderState("事件前 → 事件后状态", ev.before, ev.after)}
    <div class="state-block">
      <h4>事件输入</h4>
      <pre style="margin:0;font-family:var(--mono);font-size:12px">${escapeHtml(JSON.stringify(ev.event, null, 2))}</pre>
      ${ev.detail ? `<details class="detail-json" open><summary>处理详情</summary><pre>${escapeHtml(JSON.stringify(ev.detail, null, 2))}</pre></details>` : ""}
    </div>
  </div>`);
  if (isViolation) {
    body.push(`<div class="evidence">
      <h4>⛔ 首个违规证据 · ${escapeHtml(ev.code)}</h4>
      <pre>${escapeHtml(JSON.stringify(ev.evidence, null, 2))}</pre>
    </div>`);
  }
  body.push("</div>");
  card.innerHTML = body.join("");
  card.querySelector(".ev-head").addEventListener("click", () => card.classList.toggle("open"));
  return card;
}

function render(data) {
  const verdict = $("#verdict");
  verdict.classList.remove("hidden", "consistent", "violation");
  if (data.verdict === "violation") {
    verdict.classList.add("violation");
    verdict.innerHTML = "⛔ 轨迹违规：错误路径结果可能进入已提交状态 —— 已定位首个违规事件";
  } else {
    verdict.classList.add("consistent");
    verdict.innerHTML = "✅ 轨迹一致：全部事件满足按序提交、检查点回滚与精确异常约束";
  }

  const vCard = $("#violationCard");
  if (data.first_violation) {
    const v = data.first_violation;
    vCard.classList.remove("hidden");
    vCard.innerHTML = `
      <span class="tag">${escapeHtml(v.code)}</span>
      <h3>事件 #${v.index} · ${escapeHtml(v.event.type)}${v.event.seq !== undefined ? " seq=" + v.event.seq : ""}</h3>
      <p>${escapeHtml(v.message)}</p>
      <div class="jump"><a id="jumpLink">定位到时间线中的违规事件 ↓</a></div>`;
    $("#jumpLink").addEventListener("click", () => {
      const el = document.querySelector(`.ev[data-index="${v.index}"]`);
      if (el) { el.scrollIntoView({ behavior: "smooth", block: "center" }); el.classList.add("open"); }
    });
  } else {
    vCard.classList.add("hidden");
  }

  const tl = $("#timeline");
  tl.innerHTML = "";
  data.events.forEach((ev) => tl.appendChild(renderEvent(ev, data.first_violation?.index)));
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

checkHealth();
loadSample("correct");
