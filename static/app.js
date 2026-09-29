/* 活鲜暂养与损耗结算平台前端 */
"use strict";

const state = {
  uid: Number(localStorage.getItem("aqua_uid")) || 1,
  meta: null,
  dashboardTimer: null,
};

/* ---------------- 基础 ---------------- */
function headers() {
  return { "Content-Type": "application/json", "X-User-Id": String(state.uid) };
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers: headers(),
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `请求失败 ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function toast(msg, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "toast" + (isError ? " error" : "");
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), 3600);
}

const fmt = (n, d = 1) => Number(n).toFixed(d);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function bigNum(n, unit = "kg", tomato = false) {
  return `<span class="big-num${tomato ? " tomato" : ""}">${fmt(n, 2)}<small> ${unit}</small></span>`;
}

function statusTag(s) {
  const map = { "在养": "blue", "已清空": "gray", "已结算": "green",
    "待分拣": "gray", "分拣中": "blue", "已完成": "green", "已取消": "red",
    "待供应商确认": "red", "已确认": "blue", "已结算": "green",
    "待确认": "red", "已驳回": "gray", "已锁定": "blue", "已释放": "gray" };
  return `<span class="tag ${map[s] || "gray"}">${esc(s)}</span>`;
}

/* ---------------- 用户 ---------------- */
async function initUsers() {
  const sel = document.getElementById("userSelect");
  sel.innerHTML = state.meta.users.map((u) =>
    `<option value="${u.id}" ${u.id === state.uid ? "selected" : ""}>${esc(u.name)}</option>`
  ).join("");
  updateRoleBadge();
  sel.onchange = () => {
    state.uid = Number(sel.value);
    localStorage.setItem("aqua_uid", String(state.uid));
    updateRoleBadge();
    refreshAll();
    toast(`已切换为 ${currentUser().name}`);
  };
}

function currentUser() {
  return state.meta.users.find((u) => u.id === state.uid) || { role: "staff", name: "访客" };
}

function updateRoleBadge() {
  const u = currentUser();
  const roleName = { admin: "管理员", staff: "档口员工", supplier: "供应商" }[u.role] || u.role;
  document.getElementById("userRole").textContent = roleName;
  const scope = document.getElementById("settleScope");
  if (scope) {
    scope.textContent = u.role === "supplier"
      ? "供应商视角:仅显示本单位结算单与差异" : "档口视角:显示全部供应商结算";
  }
}

/* ---------------- 页签 ---------------- */
function initTabs() {
  document.getElementById("tabs").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-tab]");
    if (!btn) return;
    document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b === btn));
    document.querySelectorAll("main .tab").forEach((t) =>
      t.classList.toggle("active", t.id === "tab-" + btn.dataset.tab));
    loadTab(btn.dataset.tab);
  });
}

function loadTab(tab) {
  clearInterval(state.dashboardTimer);
  state.dashboardTimer = null;
  if (tab === "dashboard") {
    loadDashboard();
    state.dashboardTimer = setInterval(loadDashboard, 5000);
  }
  if (tab === "entry") loadBatches();
  if (tab === "holding") loadHolding();
  if (tab === "orders") loadOrders();
  if (tab === "loss") loadLoss();
  if (tab === "settlement") loadSettlement();
}

function refreshAll() {
  const active = document.querySelector("#tabs button.active").dataset.tab;
  loadTab(active);
}

/* ---------------- 水槽看板 ---------------- */
async function loadDashboard() {
  let data;
  try { data = await api("/api/dashboard"); } catch (e) { return; }

  // 固定警示位
  const abnormal = [];
  for (const z of data.zones)
    for (const t of z.tanks)
      if (t.abnormal) abnormal.push(t);
  const strip = document.getElementById("warnStrip");
  if (abnormal.length) {
    strip.classList.remove("hidden");
    strip.innerHTML = `<span class="warn-title">⚠ 异常水槽警示位(${abnormal.length})</span>` +
      abnormal.map((t) => {
        const kinds = [...new Set(t.alerts.map((a) => a.kind))].join("/") || "状态异常";
        return `<span class="warn-chip">${esc(t.code)} · ${esc(kinds)}` +
          (t.water_temp != null ? ` · ${fmt(t.water_temp)}°C` : "") + `</span>`;
      }).join("");
  } else {
    strip.classList.add("hidden");
  }

  document.getElementById("zones").innerHTML = data.zones.map((z) => `
    <div class="zone-block">
      <div class="zone-title">${esc(z.zone)}</div>
      <div class="tank-grid">
        ${z.tanks.map(tankCard).join("")}
      </div>
    </div>`).join("");
}

function tankCard(t) {
  const pct = Math.min(100, Math.round(t.usage * 100));
  const tempHot = t.abnormal && t.water_temp != null;
  const batches = t.batches.length
    ? t.batches.map((b) => `
        <div class="tank-batch">
          <span class="sp">${esc(b.species_name)} <span class="bn">${esc(b.batch_no)}</span></span>
          ${bigNum(b.current_weight)}
        </div>`).join("")
    : `<div class="empty-tip">空槽</div>`;
  return `
    <div class="tank-card ${t.abnormal ? "abnormal" : ""}">
      <div class="tank-head">
        <span class="tank-code">${esc(t.code)}</span>
        <span class="tank-status">${t.abnormal ? "异常" : esc(t.status)}</span>
      </div>
      <div class="meter-row">
        <div class="meter">
          <div class="label">水温</div>
          <div class="value temp ${tempHot ? "hot" : ""}">${t.water_temp != null ? fmt(t.water_temp) : "--"}<span class="unit"> °C</span></div>
        </div>
        <div class="meter">
          <div class="label">盐度 / 溶氧</div>
          <div class="value">${fmt(t.salinity, 0)}<span class="unit">‰ · </span>${t.oxygen != null ? fmt(t.oxygen) : "--"}<span class="unit">mg/L</span></div>
        </div>
      </div>
      <div class="cap-bar"><div class="cap-fill ${pct >= 95 ? "full" : ""}" style="width:${pct}%"></div></div>
      <div class="cap-text">容量 ${fmt(t.load)} / ${fmt(t.capacity_kg, 0)} kg(${pct}%)</div>
      <div class="tank-batches">${batches}</div>
    </div>`;
}

/* ---------------- 批次入池 ---------------- */
function fillEntryForm() {
  const f = document.getElementById("entryForm");
  const m = state.meta;
  f.species_id.innerHTML = m.species.map((s) =>
    `<option value="${s.id}">${esc(s.name)}(盐度 ${s.salinity_min}~${s.salinity_max}‰)</option>`).join("");
  f.supplier_id.innerHTML = m.suppliers.map((s) =>
    `<option value="${s.id}">${esc(s.name)}</option>`).join("");
  f.tank_id.innerHTML = m.tanks.map((t) =>
    `<option value="${t.id}">${esc(t.code)}(容量 ${t.capacity_kg}kg · 盐度 ${t.salinity}‰)</option>`).join("");
  f.source_batch_no.value = "SRC-" + new Date().toISOString().slice(0, 10).replace(/-/g, "") + "-01";
  f.idem_key.value = "";
}

async function readScale() {
  const f = document.getElementById("entryForm");
  const guess = parseFloat(f.weight.value) || 50;
  const r = await api("/api/devices/scale/read", { method: "POST", body: { true_weight: guess } });
  f.weight.value = r.weight;
  f.device_id.value = r.device_id;
  f.idem_key.value = r.idem_key;
  toast(`电子秤 ${r.device_id} 读数 ${r.weight}kg(幂等键 ${r.idem_key})`);
}

async function submitEntry(e) {
  e.preventDefault();
  const f = e.target;
  const body = {
    species_id: f.species_id.value, spec: f.spec.value,
    supplier_id: f.supplier_id.value, source_batch_no: f.source_batch_no.value,
    tank_id: f.tank_id.value, weight: f.weight.value,
    device_id: f.device_id.value || "SCALE-01", idem_key: f.idem_key.value || null,
  };
  try {
    const r = await api("/api/entry", { method: "POST", body });
    toast(r.deduped ? "重复秤单已去重" : `入池成功:${r.batch.batch_no}`);
    f.idem_key.value = "";
    loadBatches();
  } catch (err) { toast(err.message, true); }
}

async function loadBatches() {
  const rows = await api("/api/batches");
  const tb = document.querySelector("#batchTable tbody");
  tb.innerHTML = rows.map((b) => `
    <tr>
      <td>${esc(b.batch_no)}</td>
      <td>${esc(b.species_name)}</td>
      <td>${esc(b.spec)}</td>
      <td>${esc(b.tank_code)}</td>
      <td>${esc(b.supplier_name)}</td>
      <td>${bigNum(b.entry_weight)}</td>
      <td>${bigNum(b.current_weight)}</td>
      <td>${bigNum(b.loss_weight, "kg", b.loss_weight > 0)}</td>
      <td>${statusTag(b.status)}</td>
      <td>
        ${b.status === "在养" ? `<button class="btn small ghost" onclick="reweigh(${b.id})">补录称重</button>` : ""}
        ${!b.settled ? `<button class="btn small primary" onclick="genSettle(${b.id})">生成结算</button>` : ""}
      </td>
    </tr>`).join("") || `<tr><td colspan="10" class="empty-tip">暂无批次</td></tr>`;
}

async function reweigh(batchId) {
  const w = prompt("补录称重(kg):");
  if (!w) return;
  try {
    const r = await api(`/api/batches/${batchId}/reweigh`,
      { method: "POST", body: { weight: parseFloat(w), device_id: "SCALE-01" } });
    toast(r.message || "补录完成");
    loadBatches();
  } catch (err) { toast(err.message, true); }
}

async function genSettle(batchId) {
  try {
    const r = await api("/api/settlements/generate", { method: "POST", body: { batch_id: batchId } });
    toast(r.existed ? "结算单已存在" : `结算单已生成:¥${r.settlement.amount}`);
    loadBatches();
  } catch (err) { toast(err.message, true); }
}

/* ---------------- 暂养记录 ---------------- */
async function loadHolding() {
  const filter = document.getElementById("holdingTankFilter");
  if (!filter.options.length || filter.options.length === 1) {
    filter.innerHTML = `<option value="">全部水槽</option>` +
      state.meta.tanks.map((t) => `<option value="${t.id}">${esc(t.code)}</option>`).join("");
    filter.onchange = loadHolding;
  }
  const q = filter.value ? `?tank_id=${filter.value}` : "";
  const rows = await api("/api/holding" + q);
  document.querySelector("#holdingTable tbody").innerHTML = rows.map((r) => `
    <tr>
      <td>${esc(r.created_at)}</td><td>${esc(r.tank_code)}</td>
      <td class="num">${fmt(r.water_temp)}</td>
      <td class="num">${fmt(r.salinity, 0)}</td>
      <td class="num" ${r.oxygen < 4 ? 'style="color:var(--tomato);font-weight:800"' : ""}>${fmt(r.oxygen)}</td>
      <td>${esc(r.note)}</td>
    </tr>`).join("") || `<tr><td colspan="6" class="empty-tip">暂无记录,等待巡检任务采样</td></tr>`;
}

/* ---------------- 订单分拣 ---------------- */
function fillOrderForm() {
  const f = document.getElementById("orderForm");
  f.customer_id.innerHTML = state.meta.customers.map((c) =>
    `<option value="${c.id}">${esc(c.name)}</option>`).join("");
  f.species_id.innerHTML = state.meta.species.map((s) =>
    `<option value="${s.id}">${esc(s.name)}</option>`).join("");
}

async function submitOrder(e) {
  e.preventDefault();
  const f = e.target;
  try {
    const r = await api("/api/orders", {
      method: "POST",
      body: {
        customer_id: f.customer_id.value,
        items: [{ species_id: Number(f.species_id.value), spec: f.spec.value, qty: Number(f.qty.value) }],
      },
    });
    toast(`订单 ${r.order_no} 已创建`);
    loadOrders();
  } catch (err) { toast(err.message, true); }
}

async function loadOrders() {
  const [orders, batches] = await Promise.all([api("/api/orders"), api("/api/batches?status=在养")]);
  const box = document.getElementById("orderList");
  box.innerHTML = orders.map((o) => `
    <div class="card order-card ${o.status === "已取消" ? "cancelled" : ""}">
      <div class="order-head">
        <span><span class="order-no">${esc(o.order_no)}</span> · ${esc(o.customer_name)} · ${esc(o.created_at)}</span>
        <span>${statusTag(o.status)}
          ${o.status !== "已取消" && o.status !== "已完成"
            ? `<button class="btn small danger" onclick="cancelOrder(${o.id})">取消订单</button>` : ""}
        </span>
      </div>
      <div class="order-items">
        ${o.items.map((it) => orderItemRow(it, batches, o.status)).join("")}
      </div>
    </div>`).join("") || `<div class="card empty-tip">暂无订单</div>`;
}

function orderItemRow(it, batches, orderStatus) {
  const pct = Math.min(100, Math.round((it.sorted_qty / it.qty) * 100));
  const candidates = batches.filter((b) => b.species_id === it.species_id);
  const locks = (it.sortings || []).map((s) =>
    `<span class="tag ${s.status === "已锁定" ? "blue" : "gray"}">${esc(s.batch_no)} ${fmt(s.qty)}kg ${esc(s.employee)} ${s.status === "已释放" ? "(已释放)" : ""}</span>`
  ).join(" ");
  const canSort = orderStatus !== "已取消" && it.sorted_qty < it.qty && candidates.length;
  return `
    <div class="order-item">
      <strong>${esc(it.species_name)}</strong> ${esc(it.spec)}
      <span class="grow"></span>
      <span class="num">${fmt(it.sorted_qty)} / ${fmt(it.qty)} kg</span>
      <div class="progress"><div style="width:${pct}%"></div></div>
      ${canSort ? `
        <span class="sort-ctrl">
          <select id="sb-${it.id}">${candidates.map((b) =>
            `<option value="${b.id}">${esc(b.batch_no)} · ${esc(b.tank_code)} · 余 ${fmt(b.current_weight)}kg</option>`).join("")}</select>
          <input id="sq-${it.id}" type="number" step="0.5" min="0.5" placeholder="kg">
          <button class="btn small primary" onclick="doSort(${it.id})">分拣出池</button>
        </span>` : ""}
      <div style="width:100%">${locks}</div>
    </div>`;
}

async function doSort(itemId) {
  const batchId = document.getElementById("sb-" + itemId).value;
  const qty = parseFloat(document.getElementById("sq-" + itemId).value);
  if (!qty) { toast("请输入分拣数量", true); return; }
  try {
    const r = await api("/api/sorting", { method: "POST", body: { order_item_id: itemId, batch_id: batchId, qty } });
    toast(`分拣成功:${r.batch_no} 出池 ${fmt(qty)}kg(${r.employee})`);
    loadOrders();
  } catch (err) { toast(err.message, true); loadOrders(); }
}

async function cancelOrder(id) {
  if (!confirm("确认取消该订单?已锁定数量将释放回批次。")) return;
  try {
    const r = await api("/api/orders/" + id + "/cancel", { method: "POST" });
    toast(`订单已取消,释放 ${r.released} 笔锁定`);
    loadOrders();
  } catch (err) { toast(err.message, true); }
}

/* ---------------- 损耗处置 ---------------- */
function fillLossForm(batches) {
  const sel = document.querySelector("#lossForm select[name=batch_id]");
  sel.innerHTML = batches.filter((b) => b.status === "在养").map((b) =>
    `<option value="${b.id}">${esc(b.batch_no)} · ${esc(b.species_name)} · 余 ${fmt(b.current_weight)}kg</option>`).join("");
}

async function submitLoss(e) {
  e.preventDefault();
  const f = e.target;
  try {
    const r = await api("/api/loss", {
      method: "POST",
      body: { batch_id: f.batch_id.value, qty: f.qty.value, reason: f.reason.value, note: f.note.value },
    });
    toast(`报损已登记,批次累计损耗率 ${(r.death_rate * 100).toFixed(1)}%` +
      (r.alerts.length ? " ⚠ 已超阈值告警" : ""), r.alerts.length > 0);
    loadLoss();
  } catch (err) { toast(err.message, true); }
}

async function loadLoss() {
  const [losses, batches] = await Promise.all([api("/api/loss"), api("/api/batches")]);
  fillLossForm(batches);

  const lossCol = document.getElementById("lossCol");
  const batchCol = document.getElementById("batchCol");
  const batchIds = [...new Set(losses.map((l) => l.batch_id))];
  const related = batchIds.map((id) => batches.find((b) => b.id === id)).filter(Boolean);

  lossCol.innerHTML = `<h3 style="color:var(--tomato-dark)">损耗记录</h3>` + (losses.map((l) => `
    <div class="loss-node loss" id="loss-${l.id}" data-batch="${l.batch_id}">
      <div class="title">${esc(l.reason)} · ${bigNum(l.qty, "kg", true)}</div>
      <div class="meta">${esc(l.created_at)} · ${esc(l.note || "—")}</div>
    </div>`).join("") || `<div class="empty-tip">暂无损耗记录</div>`);

  batchCol.innerHTML = `<h3>原批次</h3>` + related.map((b) => {
    const rate = b.entry_weight ? (b.loss_weight / b.entry_weight) : 0;
    const over = rate > 0.08;
    return `
    <div class="loss-node batch" id="batch-${b.id}">
      <div class="title">${esc(b.batch_no)} · ${esc(b.species_name)}</div>
      <div class="meta">来源 ${esc(b.source_batch_no)} · ${esc(b.supplier_name)} · ${esc(b.tank_code)}</div>
      <div class="meta">入池 ${fmt(b.entry_weight)}kg · 损耗 ${fmt(b.loss_weight)}kg · 损耗率 ${(rate * 100).toFixed(1)}%</div>
      <div class="rate-bar ${over ? "over" : ""}"><div style="width:${Math.min(100, rate * 100 * 3)}%"></div></div>
    </div>`;
  }).join("");

  requestAnimationFrame(() => drawLossLines(losses));
}

function drawLossLines(losses) {
  const svg = document.getElementById("lossLines");
  const map = document.getElementById("lossMap");
  const box = map.getBoundingClientRect();
  svg.setAttribute("viewBox", `0 0 ${box.width} ${box.height}`);
  svg.innerHTML = "";
  for (const l of losses) {
    const a = document.getElementById("loss-" + l.id);
    const b = document.getElementById("batch-" + l.batch_id);
    if (!a || !b) continue;
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    const x1 = ra.right - box.left, y1 = ra.top + ra.height / 2 - box.top;
    const x2 = rb.left - box.left, y2 = rb.top + rb.height / 2 - box.top;
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", `M ${x1} ${y1} C ${x1 + 40} ${y1}, ${x2 - 40} ${y2}, ${x2} ${y2}`);
    path.setAttribute("stroke", "var(--tomato)");
    path.setAttribute("stroke-width", "2");
    path.setAttribute("fill", "none");
    path.setAttribute("stroke-dasharray", "5 4");
    path.setAttribute("opacity", "0.75");
    svg.appendChild(path);
  }
}

/* ---------------- 供应商对账 ---------------- */
async function loadSettlement() {
  const [diffs, settles] = await Promise.all([api("/api/diffs"), api("/api/settlements")]);
  document.getElementById("diffCount").textContent = diffs.length;
  document.querySelector("#diffTable tbody").innerHTML = diffs.map((d) => `
    <tr>
      <td>${esc(d.batch_no)}</td><td>${esc(d.kind)}</td>
      <td class="num">${fmt(d.expect_weight, 2)}</td>
      <td class="num">${fmt(d.actual_weight, 2)}</td>
      <td class="num" style="color:var(--tomato)">${d.diff > 0 ? "+" : ""}${fmt(d.diff, 2)}</td>
      <td>
        <button class="btn small primary" onclick="resolveDiff(${d.id}, true)">确认</button>
        <button class="btn small ghost" onclick="resolveDiff(${d.id}, false)">驳回</button>
      </td>
    </tr>`).join("") || `<tr><td colspan="6" class="empty-tip">无待确认差异</td></tr>`;

  document.querySelector("#settleTable tbody").innerHTML = settles.map((s) => `
    <tr>
      <td>JS-${String(s.id).padStart(4, "0")}</td>
      <td>${esc(s.batch_no)}<br><span class="hint">${esc(s.species_name)} ${esc(s.spec)}</span></td>
      <td>${esc(s.supplier_name)}</td>
      <td class="num">${fmt(s.entry_weight, 2)}</td>
      <td class="num" style="color:var(--tomato)">${fmt(s.loss_weight, 2)}</td>
      <td>${bigNum(s.settle_weight)}</td>
      <td class="num">¥${fmt(s.unit_price, 2)}</td>
      <td>${bigNum(s.amount, "元")}</td>
      <td>${statusTag(s.status)}</td>
      <td>${s.status === "待供应商确认"
        ? `<button class="btn small primary" onclick="confirmSettle(${s.id})">确认结算</button>` : ""}</td>
    </tr>`).join("") || `<tr><td colspan="10" class="empty-tip">暂无结算单</td></tr>`;
}

async function resolveDiff(id, accept) {
  try {
    await api(`/api/diffs/${id}/resolve`, { method: "POST", body: { accept } });
    toast(accept ? "差异已确认并计入结算" : "差异已驳回");
    loadSettlement();
  } catch (err) { toast(err.message, true); }
}

async function confirmSettle(id) {
  try {
    await api(`/api/settlements/${id}/confirm`, { method: "POST" });
    toast("结算单已确认,批次锁定为已结算");
    loadSettlement();
  } catch (err) { toast(err.message, true); }
}

/* ---------------- 看板操作 ---------------- */
async function patrol() {
  await api("/api/patrol", { method: "POST" });
  toast("已执行一次巡检");
  loadDashboard();
}

async function injectAnomaly() {
  const tankId = state.meta.tanks[0].id;
  await api("/api/devices/anomaly", { method: "POST", body: { tank_id: tankId, temp_delta: 8 } });
  await api("/api/patrol", { method: "POST" });
  toast(`已向 ${state.meta.tanks[0].code} 注入水温异常`);
  loadDashboard();
}

/* ---------------- 启动 ---------------- */
async function main() {
  state.meta = await api("/api/meta");
  initUsers();
  initTabs();
  fillEntryForm();
  fillOrderForm();
  document.getElementById("entryForm").addEventListener("submit", submitEntry);
  document.getElementById("orderForm").addEventListener("submit", submitOrder);
  document.getElementById("lossForm").addEventListener("submit", submitLoss);
  document.getElementById("btnScale").addEventListener("click", readScale);
  document.getElementById("btnPatrol").addEventListener("click", patrol);
  document.getElementById("btnInject").addEventListener("click", injectAnomaly);
  window.addEventListener("resize", () => {
    if (document.querySelector("#tabs button.active").dataset.tab === "loss") loadLoss();
  });
  loadTab("dashboard");
}

main().catch((e) => toast("初始化失败:" + e.message, true));
