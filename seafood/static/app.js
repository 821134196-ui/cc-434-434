/* 海旺水产 · 活鲜暂养平台前端（无框架，原生 JS） */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.message || "请求失败");
  return data;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const kg = (n) => Number(n).toFixed(2);
const yuan = (n) => "¥" + Number(n).toFixed(2);

let META = { species: [], tanks: [], suppliers: [], customers: [], employees: [] };
let OVERVIEW = { tanks: [], alerts: [] };
let BATCHES = [];

/* ---------------- toast ---------------- */
function toast(msg, isErr = false) {
  const box = $("#toast");
  const t = document.createElement("div");
  t.className = "t" + (isErr ? " err" : "");
  t.textContent = msg;
  box.appendChild(t);
  setTimeout(() => t.remove(), 4200);
}

/* ---------------- tabs ---------------- */
$("#tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b === btn));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === "view-" + btn.dataset.view));
  refreshView(btn.dataset.view);
});
let currentView = "board";
function refreshView(view = currentView) {
  currentView = view;
  if (view === "board") renderBoard();
  if (view === "intake") renderBatches();
  if (view === "holding") loadHolding();
  if (view === "orders") loadOrders();
  if (view === "losses") loadLosses();
  if (view === "settle") loadSettle();
}

/* ---------------- meta / selects ---------------- */
function opt(list, val, label) {
  return list.map((x) => `<option value="${esc(x[val])}">${esc(x[label])}</option>`).join("");
}
async function loadMeta() {
  META = await api("/api/meta");
  $("#inSpecies").innerHTML = opt(META.species, "id", "name");
  $("#inSupplier").innerHTML = opt(META.suppliers, "id", "name");
  $("#inTank").innerHTML = META.tanks.map(
    (t) => `<option value="${t.id}">${t.code}（${t.zone} · ${t.capacity_kg}kg · ${t.salinity}）</option>`).join("");
  $("#scTank").innerHTML = opt(META.tanks, "id", "code");
  $("#oCustomer").innerHTML = opt(META.customers, "id", "name");
  $("#oEmployee").innerHTML = opt(META.employees, "id", "name");
}
const speciesName = (id) => META.species.find((s) => s.id === id)?.name || "#" + id;

/* ---------------- 全局告警 / 固定警示位 ---------------- */
const ALERT_LABEL = {
  water_temp: "水温异常", low_oxygen: "缺氧", mix_conflict: "混养冲突",
  weight_diff: "称重差异", order_cancel: "订单取消", mortality: "死亡率超阈值",
  loss_reminder: "损耗提醒",
};
function renderAlerts() {
  const open = OVERVIEW.alerts.filter((a) => a.status === "open");
  $("#alertCount").textContent = open.length;
  const dock = $("#alarmDock");
  dock.classList.toggle("hidden", open.length === 0);
  $("#alarmChips").innerHTML = open.slice(0, 8).map((a) =>
    `<span class="chip">${ALERT_LABEL[a.type] || a.type}｜${esc(a.tank_code || "")} ${esc(a.message)}</span>`).join("")
    + (open.length > 8 ? `<span class="chip">…其余 ${open.length - 8} 条</span>` : "");
}

/* ---------------- 1. 水槽看板 ---------------- */
function renderBoard() {
  const zones = {};
  OVERVIEW.tanks.forEach((t) => (zones[t.zone] ||= []).push(t));
  $("#zones").innerHTML = Object.keys(zones).sort().map((zone) => `
    <div class="zone-title">${zone} 暂养区</div>
    <div class="tank-grid">${zones[zone].map(tankCard).join("")}</div>`).join("");
}
function tankCard(t) {
  const capPct = Math.min(t.occupancy, 100);
  const tempBad = t.abnormal?.type === "water_temp";
  const oxBad = t.abnormal?.type === "low_oxygen";
  return `<div class="tank-card${t.abnormal ? " abnormal" : ""}">
    ${t.abnormal ? `<span class="warn-flag">${t.abnormal.type === "water_temp" ? "水温异常" : "缺氧"}</span>` : ""}
    <h3>水槽 ${esc(t.code)} <span class="sal">${esc(t.salinity)}</span></h3>
    <div class="metrics">
      <div class="metric${tempBad ? " bad" : ""}"><div class="v">${t.probe_temp ?? "--"}℃</div><div class="l">水温</div></div>
      <div class="metric${oxBad ? " bad" : ""}"><div class="v">${t.probe_oxygen ?? "--"}</div><div class="l">溶氧 mg/L</div></div>
      <div class="metric"><div class="v">${kg(t.load_kg)}<small>/${t.capacity_kg}kg</small></div><div class="l">存量 / 容量</div></div>
    </div>
    <div class="capbar"><i class="${t.occupancy > 95 ? "full" : ""}" style="width:${capPct}%"></i></div>
    <div class="muted">容量占用 ${t.occupancy}%</div>
    ${t.batches.length ? t.batches.map((b) => `
      <div class="batch-line">
        <span class="bname">${esc(b.species_name)}<span class="bno">${esc(b.batch_no)} · ${esc(b.spec)}</span></span>
        <span class="big-weight">${kg(b.weight_in_kg - b.sorted_kg - b.loss_kg)}<small>kg</small></span>
      </div>`).join("") : '<div class="tank-empty">空槽</div>'}
    ${t.abnormal ? `<div class="muted" style="color:var(--tomato);margin-top:6px">⚠ ${esc(t.abnormal.message)}</div>` : ""}
  </div>`;
}

/* ---------------- 2. 批次入池 ---------------- */
$("#intakeForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api("/api/intake", { method: "POST", body: Object.fromEntries(f) });
    toast("入池成功：已记录品种称重并完成容量/盐度校验");
    e.target.reset();
    await reloadAll();
  } catch (err) { toast(err.message, true); }
});
function statusTag(b) {
  if (b.status === "已出清") return '<span class="tag gray">已出清</span>';
  if (b.source_settled) return '<span class="tag red">已结算·重量锁定</span>';
  return '<span class="tag sea">在池</span>';
}
async function renderBatches() {
  BATCHES = (await api("/api/batches")).batches;
  $("#batchTable tbody").innerHTML = BATCHES.map((b) => {
    const rate = b.weight_in_kg ? (b.loss_kg / b.weight_in_kg) : 0;
    return `<tr>
      <td><b>${esc(b.batch_no)}</b></td><td>${esc(b.source_no)}</td>
      <td>${esc(b.species_name)}<div class="muted">${esc(b.spec)}</div></td>
      <td>${esc(b.supplier_name)}</td><td>${esc(b.zone)} ${esc(b.tank_code)}</td>
      <td class="big-weight" style="font-size:18px">${kg(b.weight_in_kg)}<small>kg</small></td>
      <td>${kg(b.locked_kg)}</td><td>${kg(b.sorted_kg)}</td>
      <td style="color:${b.loss_kg > 0 ? "var(--tomato)" : "inherit"}">${kg(b.loss_kg)}</td>
      <td><span class="tag ${rate > b.mortality_threshold ? "red" : "ok"}">${(rate * 100).toFixed(1)}%</span></td>
      <td>${statusTag(b)}</td>
      <td><button class="btn ghost sm" onclick="reweighBatch(${b.id},${b.source_settled})">补录称重</button></td>
    </tr>`;
  }).join("");
}
window.reweighBatch = async (id, settled) => {
  if (settled) return toast("该来源批次已结算，重量不可再修改", true);
  const v = prompt("输入补录称重重量 kg（原始入池值与设备将保留，差异待供应商确认）：");
  if (!v) return;
  try {
    const r = await api("/api/reweigh", { method: "POST", body: { batch_id: id, corrected_kg: Number(v) } });
    toast(`补录成功：差异 ${r.result.delta_kg >= 0 ? "+" : ""}${r.result.delta_kg}kg，状态：${r.result.status}`);
    await reloadAll();
  } catch (err) { toast(err.message, true); }
};

/* ---------------- 3. 暂养记录 / 设备注入 ---------------- */
$("#scApply").addEventListener("click", async () => {
  try {
    await api("/api/devices/scenario", {
      method: "POST",
      body: { tank_id: Number($("#scTank").value), scenario: $("#scMode").value },
    });
    const r = await api("/api/monitor/scan", { method: "POST" });
    toast(`已立即采样，新增 ${r.new_alerts} 条告警；后台持续监控中`);
    await reloadAll();
  } catch (err) { toast(err.message, true); }
});
async function loadHolding() {
  const { logs } = await api("/api/holding");
  $("#holdingTable tbody").innerHTML = logs.slice(0, 60).map((h) => `
    <tr><td>${esc(h.recorded_at)}</td><td>${esc(h.tank_code)}</td>
    <td>${h.temp ?? "--"}</td><td>${h.oxygen ?? "--"}</td></tr>`).join("");
}

/* ---------------- 4. 订单分拣 ---------------- */
async function renderOrderPicker() {
  BATCHES = (await api("/api/batches?status=在池")).batches;
  // 后端 /api/batches 暂无过滤参数，手动过滤
  const live = BATCHES.filter((b) => b.status === "在池");
  $("#itemPicker tbody").innerHTML = live.map((b) => {
    const avail = Math.max(0, b.weight_in_kg - b.sorted_kg - b.loss_kg - b.locked_kg);
    return `<tr data-bid="${b.id}">
      <td>${esc(b.batch_no)} · ${esc(b.species_name)} <span class="muted">(${esc(b.tank_code)})</span></td>
      <td>${kg(avail)}</td>
      <td><input type="number" min="0" step="0.5" class="pick-qty" style="width:110px;padding:5px;border:1px solid var(--sea-mid);border-radius:6px"></td>
    </tr>`;
  }).join("") || '<tr><td colspan="3" class="muted">暂无在池批次</td></tr>';
}
$("#orderForm").addEventListener("submit", async (e) => {
  if (e.submitter && e.submitter.id === "simConcurrent") return;
  e.preventDefault();
  const items = $$("#itemPicker tbody tr[data-bid]").map((tr) => ({
    batch_id: Number(tr.dataset.bid), qty_kg: Number($(".pick-qty", tr).value),
  })).filter((x) => x.qty_kg > 0);
  if (!items.length) return toast("请填写至少一行分拣数量", true);
  try {
    const r = await api("/api/orders", {
      method: "POST",
      body: { customer_id: Number($("#oCustomer").value),
              employee_id: Number($("#oEmployee").value), items },
    });
    toast(`订单 ${r.order.order_no} 已锁定批次，等待确认出池`);
    await reloadAll();
  } catch (err) { toast(err.message, true); }
});
$("#simConcurrent").addEventListener("click", async () => {
  const live = BATCHES.filter((b) => b.status === "在池")
    .map((b) => ({ ...b, avail: b.weight_in_kg - b.sorted_kg - b.loss_kg - b.locked_kg }))
    .filter((b) => b.avail > 1);
  if (!live.length) return toast("没有可争抢的在池批次", true);
  const target = live[0];
  const qty = Math.min(target.avail, 10);
  const emp = META.employees;
  const mk = (ei) => api("/api/orders", {
    method: "POST",
    body: { customer_id: META.customers[0].id, employee_id: emp[ei].id,
            items: [{ batch_id: target.id, qty_kg: qty }] },
  });
  toast(`两员工同时对 ${target.batch_no} 各下单 ${qty}kg …`);
  const [a, b] = await Promise.allSettled([mk(0), mk(1)]);
  if (a.status === "fulfilled") toast(`✅ 员工A（${emp[0].name}）锁定成功：${a.value.order.order_no}`);
  else toast(`❌ 员工A（${emp[0].name}）被拒：${a.reason.message}`, true);
  if (b.status === "fulfilled") toast(`✅ 员工B（${emp[1].name}）锁定成功：${b.value.order.order_no}`);
  else toast(`❌ 员工B（${emp[1].name}）被拒：${b.reason.message}`, true);
  await reloadAll();
});
async function loadOrders() {
  await renderOrderPicker();
  const { orders } = await api("/api/orders");
  const tag = { 待分拣: "gray", 分拣中: "warn", 已完成: "ok", 已取消: "red" };
  $("#orderList").innerHTML = orders.map((o) => `
    <div class="settle-card ${o.status === "已完成" ? "confirmed" : o.status === "分拣中" ? "pending" : ""}">
      <div class="kv">
        <div>订单号 <b>${esc(o.order_no)}</b></div>
        <div>客户 <b>${esc(o.customer_name)}</b></div>
        <div>分拣员工 <b>${esc(o.employee_name || "-")}</b></div>
        <div>状态 <span class="tag ${tag[o.status]}">${o.status}</span></div>
        <div>创建 ${esc(o.created_at)}</div>
      </div>
      <table><thead><tr><th>批次</th><th>品种</th><th>规格</th><th>数量 kg</th><th>行状态</th></tr></thead>
      <tbody>${o.items.map((i) => `<tr><td>${esc(i.batch_no)}</td><td>${esc(i.species_name)}</td>
        <td>${esc(i.spec)}</td><td>${kg(i.qty_kg)}</td><td>${esc(i.status)}</td></tr>`).join("")}</tbody></table>
      ${o.status === "分拣中" ? `<div class="row-actions" style="margin-top:8px">
        <button class="btn sm" onclick="orderAction(${o.id},'complete')">确认出池</button>
        <button class="btn tomato sm" onclick="orderAction(${o.id},'cancel')">取消订单（释放锁定）</button></div>` : ""}
      ${o.status === "已取消" ? `<div class="muted">取消原因：${esc(o.cancel_reason || "")}</div>` : ""}
    </div>`).join("") || '<p class="muted">暂无订单</p>';
}
window.orderAction = async (id, act) => {
  try {
    await api(`/api/orders/${id}/${act}`, { method: "POST", body: {} });
    toast(act === "complete" ? "已确认出池，分拣数量入账" : "订单已取消，锁定数量已释放（已记取消告警）");
    await reloadAll();
  } catch (err) { toast(err.message, true); }
};

/* ---------------- 5. 损耗处置 ---------------- */
$("#lossForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  try {
    await api("/api/losses", { method: "POST", body: Object.fromEntries(f) });
    toast("报损成功，已关联原批次并累计损耗");
    await reloadAll();
  } catch (err) { toast(err.message, true); }
});
let LOSSES = [];
async function loadLosses() {
  const [{ batches }, { losses }] = await Promise.all([api("/api/batches"), api("/api/losses")]);
  BATCHES = batches;
  LOSSES = losses;
  const live = batches.filter((b) => b.status === "在池");
  $("#lBatch").innerHTML = live.map((b) =>
    `<option value="${b.id}">${b.batch_no} · ${b.species_name}（${b.tank_code}）</option>`).join("");
  // 关联图：左原批次，右损耗，曲线连接
  const withLoss = batches.filter((b) => losses.some((l) => l.batch_id === b.id));
  $("#relBatches").innerHTML = withLoss.map((b) => `
    <div class="rnode batch" id="rb-${b.id}">
      <div class="rn-title">${esc(b.batch_no)} · ${esc(b.species_name)}</div>
      <div class="rn-sub">入池 ${kg(b.weight_in_kg)}kg ｜ 累计损耗 ${kg(b.loss_kg)}kg ｜ ${esc(b.tank_code)}</div>
    </div>`).join("") || '<p class="muted">暂无报损记录</p>';
  $("#relLosses").innerHTML = losses.map((l) => `
    <div class="rnode loss" id="rl-${l.id}" data-bid="${l.batch_id}">
      <div class="rn-title">报损 #${l.id} · ${esc(l.reason)} ${kg(l.qty_kg)}kg</div>
      <div class="rn-sub">原批次 ${esc(l.batch_no)} ｜ 来源 ${esc(l.source_no)} ｜ ${esc(l.recorded_at)} ｜ ${esc(l.operator)}</div>
    </div>`).join("");
  $("#lossTable tbody").innerHTML = losses.map((l) => `
    <tr><td>${esc(l.recorded_at)}</td><td>#${l.id}</td><td><b>${esc(l.batch_no)}</b></td>
    <td>${esc(l.source_no)}</td><td>${esc(l.species_name)}</td><td>${esc(l.tank_code)}</td>
    <td style="color:var(--tomato);font-weight:700">-${kg(l.qty_kg)}</td>
    <td><span class="tag red">${esc(l.reason)}</span></td><td>${esc(l.operator)}</td></tr>`).join("");
  requestAnimationFrame(drawRelations);
}
function drawRelations() {
  const svg = $("#relationSvg");
  const wrap = $("#relationWrap");
  const wRect = wrap.getBoundingClientRect();
  svg.setAttribute("width", wrap.scrollWidth);
  svg.setAttribute("height", wrap.scrollHeight);
  svg.innerHTML = "";
  const NS = "http://www.w3.org/2000/svg";
  LOSSES.forEach((l) => {
    const a = document.getElementById(`rb-${l.batch_id}`);
    const b = document.getElementById(`rl-${l.id}`);
    if (!a || !b) return;
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    const x1 = ra.right - wRect.left, y1 = ra.top - wRect.top + ra.height / 2;
    const x2 = rb.left - wRect.left, y2 = rb.top - wRect.top + rb.height / 2;
    const mx = (x1 + x2) / 2;
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", `M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`);
    svg.appendChild(path);
  });
}

/* ---------------- 6. 供应商对账 ---------------- */
async function loadSettle() {
  const [{ sources }, { settlements, diffs }] = await Promise.all([
    api("/api/sources"), api("/api/settlements")]);
  $("#sourceList").innerHTML = sources.map((s) => `
    <div class="settle-card ${s.settled ? "confirmed" : "pending"}">
      <div class="kv">
        <div>来源批次 <b>${esc(s.batch_no)}</b></div>
        <div>供应商 <b>${esc(s.supplier_name)}</b></div>
        <div>到货 ${esc(s.arrived_at)}</div>
        <div>池内批次数 <b>${s.batch_cnt}</b></div>
        <div>入池总量 <b>${kg(s.gross_kg)}kg</b></div>
        <div>累计损耗 <b style="color:var(--tomato)">${kg(s.loss_kg)}kg</b></div>
        <div>结算状态 ${s.settled ? '<span class="tag ok">已结算（重量锁定）</span>' : '<span class="tag warn">未结算</span>'}</div>
      </div>
      ${s.settled ? "" : `<button class="btn sm" onclick="makeSettlement(${s.id})">生成待结算对账单</button>`}
    </div>`).join("") || '<p class="muted">暂无来源批次</p>';

  $("#diffList").innerHTML = diffs.map((d) => {
    const tagCls = d.status === "已确认" ? "ok" : d.status === "已驳回" ? "gray" : "warn";
    return `<div class="settle-card ${d.status === "待确认" ? "pending" : "confirmed"}">
      <div class="kv">
        <div>池内批次 <b>${esc(d.batch_no)}</b></div>
        <div>入池原值 <b>${kg(d.original_kg)}kg</b></div>
        <div>补录值 <b>${kg(d.corrected_kg)}kg</b></div>
        <div>差异 <b style="color:var(--tomato)">${d.delta_kg >= 0 ? "+" : ""}${kg(d.delta_kg)}kg</b></div>
        <div>差异金额 <span class="amount" style="font-size:16px">${yuan(d.amount)}</span></div>
        <div>供应商 <b>${esc(d.supplier_name)}</b></div>
        <div>状态 <span class="tag ${tagCls}">${d.status}</span></div>
      </div>
      ${d.status === "待确认" ? `<button class="btn sm" onclick="confirmDiff(${d.id})">用所选供应商凭证确认</button>
        <span class="muted">仅本批次归属供应商可确认；确认后才计入对账单</span>` : ""}
    </div>`;
  }).join("") || '<p class="muted">暂无称重差异</p>';

  $("#settleList").innerHTML = settlements.map((s) => `
    <div class="settle-card ${s.status === "已确认" ? "confirmed" : "pending"}">
      <div class="kv">
        <div>结算单 #${s.id}</div><div>来源批次 <b>${esc(s.source_no)}</b></div>
        <div>供应商 <b>${esc(s.supplier_name)}</b></div>
        <div>入池 ${kg(s.gross_kg)}kg</div><div>损耗 ${kg(s.loss_kg)}kg</div>
        <div>已分拣 ${kg(s.sorted_kg)}kg</div>
        <div>已确认差异 <b>${yuan(s.diff_amount)}</b></div>
        <div>应付 <span class="amount">${yuan(s.payable)}</span></div>
        <div>状态 <span class="tag ${s.status === "已确认" ? "ok" : "warn"}">${s.status}</span></div>
      </div>
      ${s.status === "待确认" ? `<button class="btn sm tomato" onclick="confirmSettlement(${s.id})">用所选供应商凭证确认结算（锁定重量）</button>` : ""}
    </div>`).join("") || '<p class="muted">暂无结算单</p>';
}
window.makeSettlement = async (id) => {
  try { const r = await api("/api/settlements", { method: "POST", body: { source_batch_id: id } });
    toast(`对账单 #${r.settlement.id} 已生成，应付 ${yuan(r.settlement.payable)}，待供应商确认`);
    await reloadAll();
  } catch (err) { toast(err.message, true); }
};
window.confirmDiff = async (id) => {
  try { await api(`/api/diffs/${id}/confirm`, { method: "POST", body: { token: $("#diffToken").value } });
    toast("差异已确认，将计入下一张对账单"); await reloadAll();
  } catch (err) { toast(err.message, true); }
};
window.confirmSettlement = async (id) => {
  try { await api(`/api/settlements/${id}/confirm`, { method: "POST", body: { token: $("#settleToken").value } });
    toast("结算已确认，关联批次重量已锁定"); await reloadAll();
  } catch (err) { toast(err.message, true); }
};

/* ---------------- 轮询 ---------------- */
async function reloadAll() {
  OVERVIEW = await api("/api/overview");
  renderAlerts();
  refreshView();
}
window.addEventListener("resize", () => { if (currentView === "losses") drawRelations(); });
(async function init() {
  await loadMeta();
  await reloadAll();
  setInterval(async () => { OVERVIEW = await api("/api/overview"); renderAlerts(); renderBoard(); }, 5000);
})();
