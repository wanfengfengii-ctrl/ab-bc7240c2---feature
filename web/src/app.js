/* Beamline exposure scheduler — frontend (no build step). */
"use strict";

const state = {
  horizon: 1000,
  exposures: [
    { id: "A", duration: 4, earliest_start: 0, latest_start: 50, equipment: "X", cooling: 2, mode: "fast" },
    { id: "B", duration: 3, earliest_start: 0, latest_start: 50, equipment: "X", cooling: 1, mode: "fast" },
    { id: "C", duration: 5, earliest_start: 0, latest_start: 50, equipment: "Y", cooling: 0, mode: "fast" },
    { id: "D", duration: 2, earliest_start: 0, latest_start: 50, equipment: "Y", cooling: 3, mode: "fine" },
    { id: "E", duration: 6, earliest_start: 2, latest_start: 40, equipment: "Z", cooling: 0, mode: "fast" },
  ],
  links: [{ from_id: "A", to_id: "B", min_gap: 0, max_gap: "" }],
  modesEnabled: false,
  // Keyed by equipment name: { initial: string, transitions: [{from,to,dur}] }
  modeConfigs: {
    X: { initial: "fast", transitions: [{ from: "fast", to: "fine", dur: 3 }] },
    Y: { initial: "fast", transitions: [
      { from: "fast", to: "fine", dur: 2 }, { from: "fine", to: "fast", dur: 4 },
    ] },
    Z: { initial: "fast", transitions: [] },
  },
  result: null,
  stale: false,
};

const $ = (sel) => document.querySelector(sel);

/* ---------------- table rendering ---------------- */

function renderExpRows() {
  const tbody = $("#exp-table tbody");
  tbody.innerHTML = "";
  state.exposures.forEach((row, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="muted">${i + 1}</td>
      <td class="id-cell"><input data-i="${i}" data-k="id" value="${row.id}"></td>
      <td><input type="number" min="1" step="1" data-i="${i}" data-k="duration" value="${row.duration}"></td>
      <td><input type="number" min="0" step="1" data-i="${i}" data-k="earliest_start" value="${row.earliest_start}"></td>
      <td><input type="number" min="0" step="1" data-i="${i}" data-k="latest_start" value="${row.latest_start}"></td>
      <td class="cell-eq"><input data-i="${i}" data-k="equipment" value="${row.equipment}"></td>
      <td><input type="number" min="0" step="1" data-i="${i}" data-k="cooling" value="${row.cooling}"></td>
      <td class="mode-cell"><input data-i="${i}" data-k="mode" class="mode-input${state.modesEnabled ? "" : " disabled"}"
        placeholder="${state.modesEnabled ? "必填" : "未启用"}" value="${row.mode || ""}"></td>
      <td><button type="button" class="btn secondary" data-del-i="${i}">删除</button></td>`;
    tbody.appendChild(tr);
  });
  tbody.querySelectorAll("input").forEach((inp) => {
    inp.addEventListener("input", onExpInput);
  });
  tbody.querySelectorAll("button[data-del-i]").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (state.exposures.length <= 5) {
        flashForm("至少需要 5 项曝光", true);
        return;
      }
      state.exposures.splice(Number(btn.dataset.delI), 1);
      reconcileLinks();
      reconcileModeConfigs();
      markDirty();
      renderAll();
    });
  });
  syncLinkIdOptions();
}

function onExpInput(ev) {
  const i = Number(ev.target.dataset.i);
  const k = ev.target.dataset.k;
  let v = ev.target.value;
  if (k !== "id" && k !== "equipment" && k !== "mode") v = v === "" ? "" : Number(v);
  if (k === "equipment") {
    renameModeConfig(state.exposures[i].equipment, String(v).trim());
  }
  state.exposures[i][k] = v;
  if (k === "id") syncLinkIdOptions();
  if (k === "equipment") {
    reconcileModeConfigs();
    renderModeConfigs();
  }
  markDirty();
}

function renderLinkRows() {
  const tbody = $("#link-table tbody");
  tbody.innerHTML = "";
  state.links.forEach((ln, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><select data-i="${i}" data-k="from_id"></select></td>
      <td><select data-i="${i}" data-k="to_id"></select></td>
      <td><input type="number" min="0" step="1" data-i="${i}" data-k="min_gap" value="${ln.min_gap}"></td>
      <td><input type="number" min="0" step="1" data-max-i="${i}" placeholder="不限" value="${ln.max_gap}"></td>
      <td><button type="button" class="btn secondary" data-del-link="${i}">删除</button></td>`;
    tbody.appendChild(tr);
  });
  syncLinkIdOptions();
  tbody.querySelectorAll("select, input").forEach((el) => {
    el.addEventListener("input", onLinkInput);
    el.addEventListener("change", onLinkInput);
  });
  tbody.querySelectorAll("button[data-del-link]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.links.splice(Number(btn.dataset.delLink), 1);
      markDirty();
      renderAll();
    });
  });
}

function syncLinkIdOptions() {
  const ids = state.exposures.map((e) => e.id);
  document.querySelectorAll("#link-table select").forEach((sel) => {
    const i = Number(sel.dataset.i);
    const k = sel.dataset.k;
    const current = state.links[i] ? state.links[i][k] : "";
    sel.innerHTML = ids
      .map((id) => `<option value="${id}"${id === current ? " selected" : ""}>${id}</option>`)
      .join("");
  });
}

function onLinkInput(ev) {
  const el = ev.target;
  if (el.dataset.maxI !== undefined) {
    const i = Number(el.dataset.maxI);
    state.links[i].max_gap = el.value === "" ? "" : Number(el.value);
  } else {
    const i = Number(el.dataset.i);
    const k = el.dataset.k;
    let v = el.value;
    if (k === "min_gap") v = v === "" ? "" : Number(v);
    state.links[i][k] = v;
  }
  markDirty();
}

function reconcileLinks() {
  const ids = new Set(state.exposures.map((e) => e.id));
  state.links = state.links.filter((l) => ids.has(l.from_id) && ids.has(l.to_id));
}

/* ---------------- readout-mode config ---------------- */

function equipmentNames() {
  return [...new Set(state.exposures.map((e) => String(e.equipment || "").trim()).filter(Boolean))].sort();
}

function reconcileModeConfigs() {
  const keep = new Set(equipmentNames());
  for (const eq of Object.keys(state.modeConfigs)) {
    if (!keep.has(eq)) delete state.modeConfigs[eq];
  }
  for (const eq of keep) {
    if (!state.modeConfigs[eq]) {
      state.modeConfigs[eq] = { initial: "", transitions: [] };
    }
  }
}

function renameModeConfig(oldName, newName) {
  if (!oldName || !newName || oldName === newName) return;
  if (state.modeConfigs[oldName] && !state.modeConfigs[newName]) {
    state.modeConfigs[newName] = state.modeConfigs[oldName];
    delete state.modeConfigs[oldName];
  }
}

function renderModeConfigs() {
  reconcileModeConfigs();
  const host = $("#mode-configs");
  host.innerHTML = "";
  equipmentNames().forEach((eq) => {
    const cfg = state.modeConfigs[eq];
    const box = document.createElement("div");
    box.className = "mode-cfg";
    const rows = cfg.transitions.map((tr, ti) => `
      <tr>
        <td><input data-eq="${eq}" data-ti="${ti}" data-tk="from" value="${tr.from || ""}" placeholder="源模式"></td>
        <td class="arrow">→</td>
        <td><input data-eq="${eq}" data-ti="${ti}" data-tk="to" value="${tr.to || ""}" placeholder="目标模式"></td>
        <td><input type="number" min="0" step="1" data-eq="${eq}" data-ti="${ti}" data-tk="dur" value="${tr.dur === "" || tr.dur === undefined ? "" : tr.dur}"></td>
        <td><button type="button" class="btn secondary" data-del-tr="${eq}" data-ti="${ti}">删除</button></td>
      </tr>`).join("");
    box.innerHTML = `
      <div class="mode-cfg-head">
        <span class="eq-name">设备 ${escapeHtml(eq)}</span>
        <label>初始模式：<input data-eq-init="${eq}" class="init-input" value="${cfg.initial || ""}" placeholder="如 fast"></label>
      </div>
      <table class="tr-table">
        <thead><tr><th>源模式</th><th></th><th>目标模式</th><th>有向切换耗时</th><th></th></tr></thead>
        <tbody>${rows || `<tr><td colspan="5" class="muted">尚未登记任何有向转换；不同模式相邻将不可达。</td></tr>`}</tbody>
      </table>
      <button type="button" class="btn secondary" data-add-tr="${eq}">＋ 添加转换（${escapeHtml(eq)}）</button>`;
    host.appendChild(box);
  });

  host.querySelectorAll("input[data-eq-init]").forEach((inp) => {
    inp.addEventListener("input", (ev) => {
      state.modeConfigs[ev.target.dataset.eqInit].initial = ev.target.value.trim();
      markDirty();
    });
  });
  host.querySelectorAll("input[data-eq]").forEach((inp) => {
    inp.addEventListener("input", (ev) => {
      const cfg = state.modeConfigs[ev.target.dataset.eq];
      const tr = cfg.transitions[Number(ev.target.dataset.ti)];
      const tk = ev.target.dataset.tk;
      tr[tk] = tk === "dur" ? (ev.target.value === "" ? "" : Number(ev.target.value)) : ev.target.value.trim();
      markDirty();
    });
  });
  host.querySelectorAll("button[data-add-tr]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.modeConfigs[btn.dataset.addTr].transitions.push({ from: "", to: "", dur: "" });
      markDirty();
      renderModeConfigs();
    });
  });
  host.querySelectorAll("button[data-del-tr]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const cfg = state.modeConfigs[btn.dataset.delTr];
      cfg.transitions.splice(Number(btn.dataset.ti), 1);
      markDirty();
      renderModeConfigs();
    });
  });
}

/* ---------------- render all ---------------- */

function renderAll() {
  renderExpRows();
  renderLinkRows();
  renderModeConfigs();
}

/* ---------------- draft invalidation ---------------- */

function markDirty() {
  state.stale = true;
  state.result = null;
  $("#stale-banner").classList.remove("hidden");
  $("#result-panel").classList.add("hidden");
  $("#input-error-panel").classList.add("hidden");
  $("#infeasible-panel").classList.add("hidden");
  flashForm("", false);
}

/* ---------------- client-side validation ---------------- */

function validateDraft() {
  const errors = [];
  const ids = [];
  state.exposures.forEach((e, idx) => {
    const tag = `第 ${idx + 1} 项（${e.id || "未命名"}）`;
    if (!String(e.id).trim()) errors.push(`${tag}：编号不能为空`);
    ids.push(e.id);
    if (!String(e.equipment).trim()) errors.push(`${tag}：设备不能为空`);
    for (const [k, label] of [
      ["duration", "持续时间"],
      ["earliest_start", "最早开始"],
      ["latest_start", "最晚开始"],
      ["cooling", "冷却时间"],
    ]) {
      const v = e[k];
      if (!Number.isInteger(v)) errors.push(`${tag}：${label}必须是整数`);
    }
    if (Number.isInteger(e.duration) && e.duration < 1)
      errors.push(`${tag}：持续时间必须 ≥ 1`);
    for (const k of ["earliest_start", "latest_start", "cooling"]) {
      if (Number.isInteger(e[k]) && e[k] < 0)
        errors.push(`${tag}：${k} 不能为负`);
    }
    if (Number.isInteger(e.earliest_start) && Number.isInteger(e.latest_start)
        && e.earliest_start > e.latest_start)
      errors.push(`${tag}：最早开始晚于最晚开始`);
    if (Number.isInteger(e.latest_start) && Number.isInteger(e.duration)
        && e.latest_start + e.duration > state.horizon)
      errors.push(`${tag}：最晚结束超过时间范围 H=${state.horizon}`);
  });
  const dupes = ids.filter((id, i) => id && ids.indexOf(id) !== i);
  [...new Set(dupes)].forEach((d) => errors.push(`曝光编号重复：${d}`));

  state.links.forEach((ln, i) => {
    const tag = `约束 ${i + 1}（${ln.from_id} → ${ln.to_id}）`;
    if (ln.from_id === ln.to_id) errors.push(`${tag}：不能衔接自身`);
    if (!Number.isInteger(ln.min_gap) || ln.min_gap < 0)
      errors.push(`${tag}：最小间隔必须为非负整数`);
    if (ln.max_gap !== "" && ln.max_gap !== null) {
      if (!Number.isInteger(ln.max_gap) || ln.max_gap < 0)
        errors.push(`${tag}：最大间隔必须为非负整数或留空`);
      else if (Number.isInteger(ln.min_gap) && ln.max_gap < ln.min_gap)
        errors.push(`${tag}：最大间隔小于最小间隔`);
    }
  });

  if (state.modesEnabled) errors.push(...validateModeDraft());
  return errors;
}

function validateModeDraft() {
  const errors = [];
  reconcileModeConfigs();
  equipmentNames().forEach((eq) => {
    const cfg = state.modeConfigs[eq];
    if (!String(cfg.initial || "").trim())
      errors.push(`设备 ${eq}：未登记初始读出模式`);
    const seen = new Set();
    cfg.transitions.forEach((tr, ti) => {
      const tag = `设备 ${eq} 第 ${ti + 1} 条转换`;
      const a = String(tr.from || "").trim();
      const b = String(tr.to || "").trim();
      if (!a || !b) errors.push(`${tag}：源模式与目标模式均不能为空`);
      if (a && b && a === b) errors.push(`${tag}：同模式相邻无需校准，不能登记自转换 ${a}→${b}`);
      if (!Number.isInteger(tr.dur) || tr.dur < 0)
        errors.push(`${tag}：切换耗时必须为非负整数`);
      const key = `${a}→${b}`;
      if (a && b && seen.has(key))
        errors.push(`设备 ${eq}：有向转换 ${key} 重复登记且耗时冲突`);
      seen.add(key);
    });
  });
  state.exposures.forEach((e, idx) => {
    const tag = `第 ${idx + 1} 项（${e.id || "未命名"}）`;
    const m = String(e.mode || "").trim();
    if (!m) {
      errors.push(`${tag}：启用模式切换后必须为每项曝光选择读出模式`);
      return;
    }
    const eq = String(e.equipment || "").trim();
    const cfg = state.modeConfigs[eq];
    if (cfg) {
      const known = new Set([String(cfg.initial || "").trim()]);
      cfg.transitions.forEach((tr) => {
        known.add(String(tr.from || "").trim());
        known.add(String(tr.to || "").trim());
      });
      known.delete("");
      if (!known.has(m))
        errors.push(`${tag}：模式 '${m}' 未在设备 '${eq}' 的初始模式或转换表中登记`);
    }
  });
  return errors;
}

/* ---------------- solve ---------------- */

function buildPayload() {
  const payload = {
    horizon: state.horizon,
    exposures: state.exposures.map((e) => {
      const out = {
        id: String(e.id).trim(),
        duration: e.duration,
        earliest_start: e.earliest_start,
        latest_start: e.latest_start,
        equipment: String(e.equipment).trim(),
        cooling: e.cooling,
      };
      if (state.modesEnabled) out.mode = String(e.mode || "").trim();
      return out;
    }),
    links: state.links.map((l) => ({
      from_id: l.from_id,
      to_id: l.to_id,
      min_gap: l.min_gap,
      max_gap: l.max_gap === "" ? null : l.max_gap,
    })),
  };
  if (state.modesEnabled) {
    payload.readout_modes = equipmentNames().map((eq) => {
      const cfg = state.modeConfigs[eq];
      return {
        equipment: eq,
        initial_mode: String(cfg.initial || "").trim(),
        transitions: cfg.transitions.map((tr) => ({
          from_mode: String(tr.from || "").trim(),
          to_mode: String(tr.to || "").trim(),
          duration: tr.dur,
        })),
      };
    });
  }
  return payload;
}

async function solve() {
  if (state.stale === false && state.result) return;
  $("#input-error-panel").classList.add("hidden");
  $("#infeasible-panel").classList.add("hidden");
  $("#result-panel").classList.add("hidden");

  const localErrors = validateDraft();
  if (localErrors.length) {
    showInputErrors(localErrors);
    return;
  }

  const payload = buildPayload();

  const btn = $("#solve-btn");
  btn.disabled = true;
  btn.textContent = "求解中…";
  try {
    const resp = await fetch("/api/schedule", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    let data = null;
    try { data = await resp.json(); } catch (_) { /* non-JSON */ }

    if (resp.status === 400 || resp.status === 422) {
      const msgs = data && data.field_errors && data.field_errors.length
        ? data.field_errors
        : ["服务端拒绝了请求（字段校验未通过）。"];
      showInputErrors(msgs);
      return;
    }
    if (resp.status === 503) {
      flashForm("求解器在时限内未能判定，请增大时间范围或放宽约束后重试。", true);
      return;
    }
    if (!resp.ok || !data) {
      flashForm(`服务异常（HTTP ${resp.status}），请检查 API 健康状态。`, true);
      return;
    }
    if (!data.feasible) {
      // Valid input, but no executable timing exists.
      $("#infeasible-panel").classList.remove("hidden");
      $("#stale-banner").classList.add("hidden");
      state.stale = false;
      state.result = null;
      return;
    }
    state.result = data;
    state.stale = false;
    $("#stale-banner").classList.add("hidden");
    renderResult(data);
  } catch (err) {
    flashForm(`无法连接 API：${err.message}`, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "求解排程";
  }
}

function showInputErrors(messages) {
  $("#field-error-list").innerHTML = messages
    .map((m) => `<li>${escapeHtml(m)}</li>`)
    .join("");
  $("#input-error-panel").classList.remove("hidden");
}

/* ---------------- result rendering ---------------- */

function renderResult(r) {
  $("#m-makespan").textContent = r.makespan;
  $("#m-sum").textContent = r.sum_starts;
  $("#m-vector").textContent = `[${r.starts.join(", ")}]`;
  $("#m-time").textContent = `${r.solver_time_ms} ms`;

  drawGantt(r);
  drawOrders(r.equipment_orders);
  drawSlacks(r.slacks);
  drawCalibrations(r.calibrations);

  $("#result-panel").classList.remove("hidden");
}

function drawOrders(orders) {
  const box = $("#equipment-orders");
  box.innerHTML = "";
  orders.forEach((o) => {
    const row = document.createElement("div");
    row.className = "order-row";
    const initTag = o.initial_mode !== undefined && o.initial_mode !== null
      ? `<span class="init-tag">初始 ${escapeHtml(o.initial_mode)}</span>` : "";
    row.innerHTML =
      `<span class="eq-name">${escapeHtml(o.equipment)}</span>${initTag}` +
      o.sequence
        .map((id, i) => {
          const modeTag = o.modes
            ? `<span class="mode-tag">${escapeHtml(o.modes[i])}</span>` : "";
          return (i ? '<span class="arrow">→</span>' : "") +
            `<span class="chip">${escapeHtml(id)}${modeTag}</span>`;
        })
        .join("");
    box.appendChild(row);
  });
}

function drawSlacks(slacks) {
  const tbody = $("#slack-table tbody");
  if (!slacks.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="muted">未设置衔接约束。</td></tr>`;
    return;
  }
  tbody.innerHTML = slacks
    .map((s) => {
      const req = `[${s.min_gap}, ${s.max_gap === null ? "∞" : s.max_gap}]`;
      const slack = s.slack === null ? "—" : s.slack;
      return `<tr>
        <td>${escapeHtml(s.from_id)} → ${escapeHtml(s.to_id)}</td>
        <td>${req}</td>
        <td>${s.actual_gap}</td>
        <td>${slack}</td>
      </tr>`;
    })
    .join("");
}

function drawCalibrations(calibrations) {
  const title = $("#cal-title");
  const wrap = $("#cal-wrap");
  if (!Array.isArray(calibrations)) {
    title.classList.add("hidden");
    wrap.classList.add("hidden");
    return;
  }
  title.classList.remove("hidden");
  wrap.classList.remove("hidden");
  const tbody = $("#cal-table tbody");
  if (!calibrations.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="muted">设备顺序中相邻曝光模式均相同，本次排程无需切换校准。</td></tr>`;
    return;
  }
  tbody.innerHTML = calibrations
    .map((c) => `<tr>
      <td>${escapeHtml(c.equipment)}</td>
      <td>${c.prev_exposure_id === null ? '<span class="muted">初始模式</span>' : escapeHtml(c.prev_exposure_id)}</td>
      <td>${escapeHtml(c.exposure_id)}</td>
      <td>${escapeHtml(c.from_mode)} <span class="arrow">→</span> ${escapeHtml(c.to_mode)}</td>
      <td>${c.switch_duration}</td>
      <td>${c.cal_start}</td>
      <td>${c.cal_end}</td>
      <td>${c.wait_margin}</td>
    </tr>`)
    .join("");
}

/* ---------------- SVG gantt ---------------- */

function drawGantt(r) {
  const host = $("#gantt");
  host.innerHTML = "";
  const exps = state.exposures;
  const eqNames = [...new Set(exps.map((e) => e.equipment))].sort();
  const byEq = new Map(eqNames.map((q) => [q, []]));
  exps.forEach((e, i) => {
    byEq.get(e.equipment).push({ e, i, start: r.starts[i], finish: r.finishes[i] });
  });
  const cals = Array.isArray(r.calibrations) ? r.calibrations : [];
  const calsByEq = new Map(eqNames.map((q) => [q, []]));
  cals.forEach((c) => { if (calsByEq.has(c.equipment)) calsByEq.get(c.equipment).push(c); });

  const maxT = Math.max(
    ...exps.map((e, i) => r.finishes[i] + e.cooling),
    ...cals.map((c) => c.cal_end),
    ...exps.map((e) => e.latest_start),
    1
  );

  const labelW = 80;
  const padR = 30;
  const axisH = 26;
  const rowH = 38;
  const availW = Math.max(host.clientWidth - 24, 640);
  const unit = Math.min(40, Math.max(3, (availW - labelW - padR) / (maxT + 1)));
  const chartW = Math.ceil(maxT * unit) + padR;
  const W = labelW + chartW;
  const Hpx = axisH + eqNames.length * rowH + 10;

  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("width", W);
  svg.setAttribute("height", Hpx);
  svg.setAttribute("viewBox", `0 0 ${W} ${Hpx}`);

  const x = (t) => labelW + t * unit;

  // axis + ticks
  const step = niceStep(maxT);
  for (let t = 0; t <= maxT; t += step) {
    const g = document.createElementNS(svgNS, "g");
    const line = document.createElementNS(svgNS, "line");
    line.setAttribute("x1", x(t)); line.setAttribute("x2", x(t));
    line.setAttribute("y1", axisH - 8); line.setAttribute("y2", Hpx - 4);
    line.setAttribute("stroke", "#2b3a55"); line.setAttribute("stroke-width", "1");
    const txt = document.createElementNS(svgNS, "text");
    txt.setAttribute("x", x(t)); txt.setAttribute("y", 14);
    txt.setAttribute("fill", "#93a4c0"); txt.setAttribute("font-size", "10");
    txt.setAttribute("text-anchor", "middle");
    txt.textContent = String(t);
    g.appendChild(line); g.appendChild(txt);
    svg.appendChild(g);
  }

  eqNames.forEach((eq, ri) => {
    const top = axisH + ri * rowH;
    const lbl = document.createElementNS(svgNS, "text");
    lbl.setAttribute("x", 8); lbl.setAttribute("y", top + rowH / 2 + 4);
    lbl.setAttribute("fill", "#7ee0c8"); lbl.setAttribute("font-size", "12");
    lbl.setAttribute("font-weight", "600");
    lbl.textContent = eq;
    svg.appendChild(lbl);

    // calibration segments (drawn first, beneath exposure blocks)
    calsByEq.get(eq).forEach((c) => {
      const rect = document.createElementNS(svgNS, "rect");
      rect.setAttribute("x", x(c.cal_start));
      rect.setAttribute("y", top + 14);
      rect.setAttribute("width", Math.max((c.cal_end - c.cal_start) * unit - 1, 2));
      rect.setAttribute("height", 12);
      rect.setAttribute("fill", "#ffc857");
      rect.setAttribute("opacity", "0.9");
      rect.setAttribute("rx", "2");
      const prev = c.prev_exposure_id === null ? "初始模式" : c.prev_exposure_id;
      const title = document.createElementNS(svgNS, "title");
      title.textContent =
        `校准 ${prev} → ${c.exposure_id}：${c.from_mode}→${c.to_mode}，` +
        `耗时 ${c.switch_duration}（${c.cal_start} → ${c.cal_end}），等待余量 ${c.wait_margin}`;
      rect.appendChild(title);
      svg.appendChild(rect);
    });

    byEq.get(eq).forEach(({ e, i, start, finish }) => {
      // start-window band
      const win = document.createElementNS(svgNS, "rect");
      win.setAttribute("x", x(e.earliest_start));
      win.setAttribute("y", top + 14);
      win.setAttribute("width", Math.max((e.latest_start - e.earliest_start) * unit, 1));
      win.setAttribute("height", 12);
      win.setAttribute("fill", "#2b3a55");
      win.setAttribute("opacity", "0.45");
      win.setAttribute("rx", "2");
      svg.appendChild(win);

      // cooling block
      if (e.cooling > 0) {
        const c = document.createElementNS(svgNS, "rect");
        c.setAttribute("x", x(finish));
        c.setAttribute("y", top + 6);
        c.setAttribute("width", Math.max(e.cooling * unit - 1, 2));
        c.setAttribute("height", 26);
        c.setAttribute("fill", "#8a7dff");
        c.setAttribute("opacity", "0.85");
        c.setAttribute("rx", "3");
        const title = document.createElementNS(svgNS, "title");
        title.textContent = `${e.id} 冷却 ${e.cooling}（${finish} → ${finish + e.cooling}）`;
        c.appendChild(title);
        svg.appendChild(c);
      }

      // exposure block
      const b = document.createElementNS(svgNS, "rect");
      b.setAttribute("x", x(start));
      b.setAttribute("y", top + 6);
      b.setAttribute("width", Math.max(e.duration * unit - 1, 2));
      b.setAttribute("height", 26);
      b.setAttribute("fill", "#4da3ff");
      b.setAttribute("rx", "3");
      const t = document.createElementNS(svgNS, "title");
      t.textContent = `${e.id}：开始 ${start}，结束 ${finish}，设备 ${e.equipment}` +
        (e.mode ? `，模式 ${e.mode}` : "");
      b.appendChild(t);
      svg.appendChild(b);

      const lab = document.createElementNS(svgNS, "text");
      lab.setAttribute("x", x(start) + 4);
      lab.setAttribute("y", top + 23);
      lab.setAttribute("fill", "#0b1626");
      lab.setAttribute("font-size", "11");
      lab.setAttribute("font-weight", "700");
      lab.textContent = e.mode ? `${e.id}·${e.mode}` : e.id;
      svg.appendChild(lab);
    });
  });

  host.appendChild(svg);
}

function niceStep(maxT) {
  const target = Math.max(1, Math.round(maxT / 8));
  const pow = Math.pow(10, Math.floor(Math.log10(target)));
  for (const m of [1, 2, 5, 10]) {
    if (m * pow >= target) return m * pow;
  }
  return 10 * pow;
}

/* ---------------- misc ---------------- */

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

let flashTimer = null;
function flashForm(msg, isError) {
  const el = $("#form-msg");
  el.textContent = msg;
  el.style.color = isError ? "var(--danger)" : "var(--ok)";
  clearTimeout(flashTimer);
  if (msg) flashTimer = setTimeout(() => { el.textContent = ""; }, 4000);
}

async function checkHealth() {
  const pill = $("#api-health");
  try {
    const resp = await fetch("/health", { cache: "no-store" });
    if (resp.ok) {
      pill.textContent = "● API 正常";
      pill.className = "health-pill ok";
    } else {
      throw new Error("bad status");
    }
  } catch (_) {
    pill.textContent = "● API 不可达";
    pill.className = "health-pill bad";
  }
}

/* ---------------- wiring ---------------- */

$("#modes-enabled").addEventListener("change", (ev) => {
  state.modesEnabled = ev.target.checked;
  $("#modes-body").classList.toggle("hidden", !state.modesEnabled);
  $("#modes-off-hint").classList.toggle("hidden", state.modesEnabled);
  reconcileModeConfigs();
  renderExpRows();
  renderModeConfigs();
  markDirty();
});

$("#add-exp").addEventListener("click", () => {
  if (state.exposures.length >= 10) {
    flashForm("最多 10 项曝光", true);
    return;
  }
  const n = state.exposures.length;
  state.exposures.push({
    id: String.fromCharCode(65 + n),
    duration: 1, earliest_start: 0, latest_start: 100,
    equipment: "X", cooling: 0, mode: "",
  });
  reconcileModeConfigs();
  markDirty();
  renderAll();
});
$("#del-exp").addEventListener("click", () => {
  if (state.exposures.length <= 5) {
    flashForm("至少需要 5 项曝光", true);
    return;
  }
  state.exposures.pop();
  reconcileLinks();
  reconcileModeConfigs();
  markDirty();
  renderAll();
});
$("#add-link").addEventListener("click", () => {
  const ids = state.exposures.map((e) => e.id);
  state.links.push({
    from_id: ids[0], to_id: ids[Math.min(1, ids.length - 1)],
    min_gap: 0, max_gap: "",
  });
  markDirty();
  renderAll();
});
$("#horizon").addEventListener("input", (ev) => {
  state.horizon = ev.target.value === "" ? 0 : Number(ev.target.value);
  markDirty();
});
$("#solve-btn").addEventListener("click", solve);

renderAll();
checkHealth();
setInterval(checkHealth, 10000);
