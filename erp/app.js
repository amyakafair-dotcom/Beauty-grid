/* Beauty-Grid ERP: inventory, production + QC, orders, customers, expenses, reports.
   Data goes to Supabase when config.js is filled, otherwise to localStorage (demo). */
'use strict';
const CFG = window.ERP_CONFIG || {};
const LIVE = !!(CFG.SUPABASE_URL && CFG.SUPABASE_ANON_KEY && window.supabase);
const sb = LIVE ? window.supabase.createClient(CFG.SUPABASE_URL, CFG.SUPABASE_ANON_KEY) : null;
const TABLES = ['producers', 'materials', 'products', 'formulas', 'batches', 'movements', 'customers', 'orders', 'order_items', 'expenses'];

/* ---------- data layer ---------- */
const uid = () => (crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + Math.random().toString(36).slice(2));
const Local = {
  key: t => 'bg-erp-' + t,
  read(t) { try { return JSON.parse(localStorage.getItem(this.key(t)) || '[]'); } catch (e) { return []; } },
  write(t, rows) { try { localStorage.setItem(this.key(t), JSON.stringify(rows)); } catch (e) { /* storage full or blocked */ } },
  async list(t) { return this.read(t); },
  async insert(t, row) { const r = { id: uid(), created_at: new Date().toISOString(), ...row }; this.write(t, [...this.read(t), r]); return r; },
  async update(t, id, patch) { let out; this.write(t, this.read(t).map(r => r.id === id ? (out = { ...r, ...patch }) : r)); return out; },
  async remove(t, id) { this.write(t, this.read(t).filter(r => r.id !== id)); },
};
const Remote = {
  async list(t) { const { data, error } = await sb.from(t).select('*'); if (error) throw error; return data; },
  async insert(t, row) { const { data, error } = await sb.from(t).insert(row).select().single(); if (error) throw error; return data; },
  async update(t, id, patch) { const { data, error } = await sb.from(t).update(patch).eq('id', id).select().single(); if (error) throw error; return data; },
  async remove(t, id) { const { error } = await sb.from(t).delete().eq('id', id); if (error) throw error; },
};
const db = LIVE ? Remote : Local;

const state = { data: {}, user: null, pid: '', page: 'dashboard', q: '' };
async function loadAll() { for (const t of TABLES) state.data[t] = await db.list(t); }
async function ins(t, row) { const r = await db.insert(t, row); state.data[t].push(r); return r; }
async function upd(t, id, patch) { const r = await db.update(t, id, patch); state.data[t] = state.data[t].map(x => x.id === id ? r : x); return r; }
async function del(t, id) { await db.remove(t, id); state.data[t] = state.data[t].filter(x => x.id !== id); }

/* ---------- helpers ---------- */
const $ = s => document.querySelector(s);
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const num = v => Number(v) || 0;
const fmt = (n, d = 2) => num(n).toLocaleString('en-US', { maximumFractionDigits: d });
const today = () => new Date().toISOString().slice(0, 10);
const byId = (t, id) => state.data[t].find(r => r.id === id);
const nameOf = (t, id) => (byId(t, id) || {}).name || '—';
const isAdmin = () => state.user && state.user.role === 'admin';
const rows = t => state.data[t].filter(r => !state.pid || r.producer_id === state.pid);
const cur = pid => ((byId('producers', pid || state.pid) || {}).currency || '');
const money = (n, pid) => fmt(n) + ' ' + esc(cur(pid));
const daysTo = d => d ? Math.round((new Date(d) - new Date(today())) / 864e5) : Infinity;
const unitCost = pidProduct => state.data.formulas.filter(f => f.product_id === pidProduct)
  .reduce((s, f) => s + num(f.qty_per_unit) * num((byId('materials', f.material_id) || {}).cost), 0);
const LABELS = {
  kind: { raw: 'مادة خام', pack: 'عبوة' },
  qc: { pending: 'بانتظار الفحص', pass: 'ناجح', fail: 'مرفوض' },
  status: { new: 'جديد', confirmed: 'مؤكد', delivering: 'قيد التوصيل', delivered: 'تم التسليم', cancelled: 'ملغي' },
  channel: { instagram: 'إنستغرام', whatsapp: 'واتساب', store: 'متجر', other: 'أخرى' },
};
const opts = o => Object.entries(o);
const UNITS = { g: 'غ', kg: 'كغ', ml: 'مل', l: 'لتر', pc: 'قطعة' };
const unitAr = u => UNITS[u] || u || '';
const pill = (txt, cls = '') => `<span class="pill ${cls}">${esc(txt)}</span>`;
function toast(msg) { const t = document.createElement('div'); t.className = 'toast'; t.textContent = msg; document.body.append(t); setTimeout(() => t.remove(), 2400); }
const lowItems = () => [
  ...rows('materials').filter(m => num(m.stock) <= num(m.reorder_level)).map(m => ({ t: 'مادة', name: m.name, stock: m.stock, unit: unitAr(m.unit) })),
  ...rows('products').filter(p => num(p.stock) <= num(p.reorder_level)).map(p => ({ t: 'منتج', name: p.name, stock: p.stock, unit: 'وحدة' })),
];
const expiring = (days = 60) => rows('batches').filter(b => b.qc === 'pass' && num(b.remaining) > 0 && daysTo(b.expires_on) <= days)
  .sort((a, b) => daysTo(a.expires_on) - daysTo(b.expires_on));
const liveOrders = () => rows('orders').filter(o => o.status !== 'cancelled');
const monthKey = d => String(d || '').slice(0, 7);

/* ---------- stock operations ---------- */
async function move(pid, type, itemId, qty, reason, ref = '') {
  const t = type === 'material' ? 'materials' : 'products', item = byId(t, itemId);
  await upd(t, itemId, { stock: num(item.stock) + num(qty) });
  await ins('movements', { producer_id: pid, item_type: type, item_id: itemId, qty: num(qty), reason, ref });
}
function needProducer() {
  if (state.pid) return state.pid;
  throw new Error('اختر المنتج (الشركة) من القائمة أعلى الصفحة أولاً.');
}

/* ---------- modal form ---------- */
function form({ title, fields, values = {}, submit = 'حفظ', onSave, extra = '' }) {
  const m = document.createElement('div'); m.className = 'modal';
  const field = f => {
    const v = values[f.k] ?? f.def ?? '';
    let input;
    if (f.type === 'select') input = `<select name="${f.k}" ${f.req ? 'required' : ''}>${f.blank ? '<option value="">—</option>' : ''}${f.options.map(([o, l]) => `<option value="${esc(o)}" ${String(o) === String(v) ? 'selected' : ''}>${esc(l)}</option>`).join('')}</select>`;
    else if (f.type === 'textarea') input = `<textarea name="${f.k}" rows="2">${esc(v)}</textarea>`;
    else if (f.type === 'check') input = `<input type="checkbox" name="${f.k}" ${v ? 'checked' : ''} style="width:auto">`;
    else input = `<input name="${f.k}" type="${f.type || 'text'}" value="${esc(v)}" ${f.type === 'number' ? 'step="any"' : ''} ${f.req ? 'required' : ''}>`;
    return `<label class="f" ${f.half ? '' : ''}>${esc(f.label)}${input}</label>`;
  };
  m.innerHTML = `<form class="sheet"><h3>${esc(title)}</h3><div class="fields">${fields.map(field).join('')}${extra}</div>
    <div class="err"></div><div class="foot"><button class="btn" type="submit">${esc(submit)}</button><button class="btn soft" type="button" data-x>إلغاء</button></div></form>`;
  document.body.append(m);
  m.querySelector('[data-x]').onclick = () => m.remove();
  m.onclick = e => { if (e.target === m) m.remove(); };
  m.querySelector('form').onsubmit = async e => {
    e.preventDefault();
    const fd = new FormData(e.target), out = {};
    fields.forEach(f => { out[f.k] = f.type === 'check' ? fd.has(f.k) : f.type === 'number' ? num(fd.get(f.k)) : (fd.get(f.k) || null); });
    const btn = e.target.querySelector('[type=submit]'); btn.disabled = true;
    try { await onSave(out, m); m.remove(); render(); }
    catch (err) { m.querySelector('.err').textContent = err.message || String(err); btn.disabled = false; }
  };
  return m;
}
const confirmBox = (msg, onYes) => form({ title: 'تأكيد', fields: [], extra: `<p>${esc(msg)}</p>`, submit: 'نعم، تابع', onSave: onYes });

/* ---------- table ---------- */
function table(cols, list, actions) {
  const q = state.q.trim();
  if (q) list = list.filter(r => cols.some(c => String(c.v(r)).replace(/<[^>]+>/g, '').includes(q)));
  if (!list.length) return `<div class="empty">لا توجد بيانات بعد.</div>`;
  return `<div class="scroll"><table class="tbl"><thead><tr>${cols.map(c => `<th>${c.h}</th>`).join('')}${actions ? '<th></th>' : ''}</tr></thead>
    <tbody>${list.map(r => `<tr>${cols.map(c => `<td class="${c.num ? 'num' : ''}">${c.v(r)}</td>`).join('')}${actions ? `<td class="act">${actions(r)}</td>` : ''}</tr>`).join('')}</tbody></table></div>`;
}
const act = (label, fn, id, cls = 'soft') => `<button class="btn sm ${cls}" data-act="${fn}" data-id="${id}">${label}</button>`;
const toolbar = (addLabel, addFn) => `<div class="toolbar"><input placeholder="بحث…" value="${esc(state.q)}" data-search>${addLabel && isAdmin() ? `<button class="btn" data-act="${addFn}">${addLabel}</button>` : ''}</div>`;

/* ---------- pages ---------- */
const NAV = [
  ['dashboard', '◈', 'لوحة التحكم'], ['producers', '◎', 'المنتجون', 'admin'], ['materials', '⬡', 'المواد والعبوات'],
  ['products', '◇', 'المنتجات والتركيبات'], ['batches', '⚗', 'الإنتاج والجودة'], ['stock', '⇅', 'حركة المخزون'],
  ['orders', '▤', 'الطلبات'], ['customers', '☺', 'الزبائن'], ['expenses', '◔', 'المصروفات'], ['reports', '▥', 'التقارير'],
];
const PAGES = {};

PAGES.dashboard = () => {
  if (!state.pid) {
    const ps = state.data.producers;
    return `<div class="card"><h2>ملخّص المنتجين</h2>${table([
      { h: 'المنتج', v: p => `<b>${esc(p.name)}</b>` },
      { h: 'مبيعات الشهر', num: 1, v: p => money(state.data.orders.filter(o => o.producer_id === p.id && o.status !== 'cancelled' && monthKey(o.ordered_on) === monthKey(today())).reduce((s, o) => s + num(o.total), 0), p.id) },
      { h: 'طلبات مفتوحة', num: 1, v: p => state.data.orders.filter(o => o.producer_id === p.id && ['new', 'confirmed', 'delivering'].includes(o.status)).length },
      { h: 'تحت حد الطلب', num: 1, v: p => [...state.data.materials, ...state.data.products].filter(x => x.producer_id === p.id && num(x.stock) <= num(x.reorder_level)).length },
      { h: 'بانتظار الجودة', num: 1, v: p => state.data.batches.filter(b => b.producer_id === p.id && b.qc === 'pending').length },
    ], ps, p => act('فتح', 'openProducer', p.id))}</div>`;
  }
  const month = liveOrders().filter(o => monthKey(o.ordered_on) === monthKey(today()));
  const open = rows('orders').filter(o => ['new', 'confirmed', 'delivering'].includes(o.status));
  const low = lowItems(), exp = expiring(), pend = rows('batches').filter(b => b.qc === 'pending');
  return `<div class="kpis">
      <div class="kpi"><span>مبيعات هذا الشهر</span><b>${money(month.reduce((s, o) => s + num(o.total), 0))}</b></div>
      <div class="kpi"><span>طلبات مفتوحة</span><b>${open.length}</b></div>
      <div class="kpi ${low.length ? 'alert' : ''}"><span>أصناف تحت حد الطلب</span><b>${low.length}</b></div>
      <div class="kpi ${exp.length ? 'alert' : ''}"><span>تشغيلات تنتهي خلال 60 يوماً</span><b>${exp.length}</b></div></div>
    <div class="cols">
      <div class="card"><h2>تنبيهات المخزون</h2>${table([{ h: 'النوع', v: r => r.t }, { h: 'الصنف', v: r => esc(r.name) }, { h: 'المتوفر', num: 1, v: r => fmt(r.stock) + ' ' + esc(r.unit) }], low)}</div>
      <div class="card"><h2>الصلاحية والجودة</h2>${table([
        { h: 'التشغيلة', v: b => esc(b.code) }, { h: 'المنتج', v: b => esc(nameOf('products', b.product_id)) },
        { h: 'الحالة', v: b => b.qc === 'pending' ? pill('بانتظار الفحص', 'warn') : pill(daysTo(b.expires_on) < 0 ? 'منتهية' : `تنتهي بعد ${daysTo(b.expires_on)} يوماً`, daysTo(b.expires_on) < 0 ? 'bad' : 'warn') }], [...pend, ...exp])}</div>
    </div>
    <div class="card"><h2>آخر الطلبات</h2>${orderTable(rows('orders').slice().sort((a, b) => String(b.created_at).localeCompare(String(a.created_at))).slice(0, 6))}</div>`;
};

PAGES.producers = () => `<div class="card">${toolbar('+ منتج جديد', 'addProducer')}${table([
  { h: 'الاسم', v: p => `<b>${esc(p.name)}</b>` }, { h: 'الهاتف', v: p => esc(p.phone) }, { h: 'المدينة', v: p => esc(p.city) }, { h: 'العملة', v: p => esc(p.currency) },
  { h: 'المنتجات', num: 1, v: p => state.data.products.filter(x => x.producer_id === p.id).length }],
  state.data.producers, p => act('فتح', 'openProducer', p.id) + ' ' + act('تعديل', 'editProducer', p.id) + ' ' + act('حذف', 'delProducer', p.id, 'danger'))}</div>`;

PAGES.materials = () => `<div class="card">${toolbar('+ مادة أو عبوة', 'addMaterial')}${table([
  { h: 'الاسم', v: m => `<b>${esc(m.name)}</b>` }, { h: 'النوع', v: m => LABELS.kind[m.kind] }, ...(state.pid ? [] : [{ h: 'المنتج', v: m => esc(nameOf('producers', m.producer_id)) }]),
  { h: 'المتوفر', num: 1, v: m => `${fmt(m.stock)} ${esc(unitAr(m.unit))} ${num(m.stock) <= num(m.reorder_level) ? pill('منخفض', 'bad') : ''}` },
  { h: 'حد الطلب', num: 1, v: m => fmt(m.reorder_level) }, { h: 'تكلفة الوحدة', num: 1, v: m => money(m.cost, m.producer_id) }, { h: 'المورّد', v: m => esc(m.supplier) }],
  rows('materials'), isAdmin() && (m => act('استلام', 'receive', m.id) + ' ' + act('تسوية', 'adjustMat', m.id) + ' ' + act('تعديل', 'editMaterial', m.id) + ' ' + act('حذف', 'delMaterial', m.id, 'danger')))}</div>`;

PAGES.products = () => `<div class="card">${toolbar('+ منتج', 'addProduct')}${table([
  { h: 'المنتج', v: p => `<b>${esc(p.name)}</b> <span class="who">${esc(p.size || '')}</span>` }, ...(state.pid ? [] : [{ h: 'الشركة', v: p => esc(nameOf('producers', p.producer_id)) }]),
  { h: 'المتوفر', num: 1, v: p => `${fmt(p.stock, 0)} ${num(p.stock) <= num(p.reorder_level) ? pill('منخفض', 'bad') : ''}` },
  { h: 'سعر البيع', num: 1, v: p => money(p.price, p.producer_id) }, { h: 'تكلفة الوحدة', num: 1, v: p => money(unitCost(p.id), p.producer_id) },
  { h: 'الهامش', num: 1, v: p => { const c = unitCost(p.id), mg = num(p.price) ? (num(p.price) - c) / num(p.price) * 100 : 0; return `${fmt(mg, 0)}%`; } },
  { h: 'التركيبة', v: p => { const n = state.data.formulas.filter(f => f.product_id === p.id).length; return n ? pill(n + ' مكوّن', 'ok') : pill('بدون تركيبة', 'warn'); } }],
  rows('products'), isAdmin() && (p => act('التركيبة', 'formula', p.id) + ' ' + act('تعديل', 'editProduct', p.id) + ' ' + act('حذف', 'delProduct', p.id, 'danger')))}</div>`;

PAGES.batches = () => `<div class="card">${toolbar('+ تشغيلة إنتاج', 'addBatch')}${table([
  { h: 'رقم التشغيلة', v: b => `<b>${esc(b.code)}</b>` }, { h: 'المنتج', v: b => esc(nameOf('products', b.product_id)) },
  { h: 'الكمية', num: 1, v: b => fmt(b.qty, 0) }, { h: 'المتبقي', num: 1, v: b => fmt(b.remaining, 0) },
  { h: 'الإنتاج', num: 1, v: b => esc(b.made_on) }, { h: 'الانتهاء', num: 1, v: b => esc(b.expires_on || '—') },
  { h: 'الجودة', v: b => pill(LABELS.qc[b.qc], { pending: 'warn', pass: 'ok', fail: 'bad' }[b.qc]) + (b.qc_notes ? ` <span class="who">${esc(b.qc_notes)}</span>` : '') }],
  rows('batches').slice().sort((a, b) => String(b.made_on).localeCompare(String(a.made_on))),
  isAdmin() && (b => b.qc === 'pending' ? act('نتيجة الفحص', 'qc', b.id) : ''))}
  <p class="who">المواد تُخصم عند تسجيل التشغيلة. المنتج يدخل المخزون المتاح للبيع فقط بعد نجاح فحص الجودة.</p></div>`;

PAGES.stock = () => `<div class="card">${toolbar()}${table([
  { h: 'التاريخ', num: 1, v: m => esc(String(m.created_at).slice(0, 16).replace('T', ' ')) },
  { h: 'الصنف', v: m => esc(nameOf(m.item_type === 'material' ? 'materials' : 'products', m.item_id)) },
  { h: 'النوع', v: m => m.item_type === 'material' ? 'مادة' : 'منتج' },
  { h: 'الكمية', num: 1, v: m => `<span style="color:${num(m.qty) < 0 ? 'var(--bad)' : 'var(--ok)'}">${num(m.qty) > 0 ? '+' : ''}${fmt(m.qty)}</span>` },
  { h: 'السبب', v: m => esc(m.reason) }, { h: 'مرجع', v: m => esc(m.ref || '') }],
  rows('movements').slice().sort((a, b) => String(b.created_at).localeCompare(String(a.created_at))))}</div>`;

function orderTable(list) {
  return table([
    { h: 'التاريخ', num: 1, v: o => esc(o.ordered_on) }, { h: 'الزبون', v: o => esc(nameOf('customers', o.customer_id)) },
    { h: 'القناة', v: o => LABELS.channel[o.channel] || esc(o.channel) },
    { h: 'المنتجات', v: o => state.data.order_items.filter(i => i.order_id === o.id).map(i => `${esc(nameOf('products', i.product_id))} ×${fmt(i.qty, 0)}`).join('، ') },
    { h: 'الإجمالي', num: 1, v: o => money(o.total, o.producer_id) },
    { h: 'الحالة', v: o => pill(LABELS.status[o.status], { delivered: 'ok', cancelled: 'bad', new: 'warn' }[o.status] || '') + ' ' + (o.paid ? pill('مدفوع', 'ok') : pill('غير مدفوع')) }],
    list, isAdmin() && (o => o.status === 'cancelled' ? '' : act('الحالة', 'orderStatus', o.id)));
}
PAGES.orders = () => `<div class="card">${toolbar('+ طلب جديد', 'addOrder')}${orderTable(rows('orders').slice().sort((a, b) => String(b.ordered_on).localeCompare(String(a.ordered_on))))}</div>`;

PAGES.customers = () => `<div class="card">${toolbar('+ زبون', 'addCustomer')}${table([
  { h: 'الاسم', v: c => `<b>${esc(c.name)}</b>` }, { h: 'الهاتف', num: 1, v: c => esc(c.phone) }, { h: 'المدينة', v: c => esc(c.city) },
  { h: 'القناة', v: c => LABELS.channel[c.channel] || '' },
  { h: 'الطلبات', num: 1, v: c => state.data.orders.filter(o => o.customer_id === c.id && o.status !== 'cancelled').length },
  { h: 'إجمالي الشراء', num: 1, v: c => money(state.data.orders.filter(o => o.customer_id === c.id && o.status !== 'cancelled').reduce((s, o) => s + num(o.total), 0), c.producer_id) }],
  rows('customers'), isAdmin() && (c => act('تعديل', 'editCustomer', c.id) + ' ' + act('حذف', 'delCustomer', c.id, 'danger')))}</div>`;

PAGES.expenses = () => `<div class="card">${toolbar('+ مصروف', 'addExpense')}${table([
  { h: 'التاريخ', num: 1, v: e => esc(e.spent_on) }, { h: 'البند', v: e => esc(e.category) }, { h: 'المبلغ', num: 1, v: e => money(e.amount, e.producer_id) }, { h: 'ملاحظات', v: e => esc(e.notes || '') }],
  rows('expenses').slice().sort((a, b) => String(b.spent_on).localeCompare(String(a.spent_on))), isAdmin() && (e => act('حذف', 'delExpense', e.id, 'danger')))}</div>`;

PAGES.reports = () => {
  if (!state.pid) return `<div class="card empty">اختر المنتج (الشركة) من القائمة أعلى الصفحة لعرض تقاريره.</div>`;
  const orders = liveOrders(), items = state.data.order_items.filter(i => orders.some(o => o.id === i.order_id));
  const months = [...Array(6)].map((_, i) => { const d = new Date(); d.setDate(1); d.setMonth(d.getMonth() - 5 + i); return d.toISOString().slice(0, 7); });
  const sales = months.map(m => orders.filter(o => monthKey(o.ordered_on) === m).reduce((s, o) => s + num(o.total), 0));
  const maxS = Math.max(1, ...sales);
  const prod = rows('products').map(p => { const its = items.filter(i => i.product_id === p.id);
    return { p, qty: its.reduce((s, i) => s + num(i.qty), 0), rev: its.reduce((s, i) => s + num(i.qty) * num(i.price), 0), cost: unitCost(p.id) }; }).sort((a, b) => b.rev - a.rev);
  const maxR = Math.max(1, ...prod.map(x => x.rev));
  const m = monthKey(today()), mOrders = orders.filter(o => monthKey(o.ordered_on) === m);
  const mSales = mOrders.reduce((s, o) => s + num(o.total) - num(o.delivery_fee), 0);
  const mCogs = state.data.order_items.filter(i => mOrders.some(o => o.id === i.order_id)).reduce((s, i) => s + num(i.qty) * unitCost(i.product_id), 0);
  const mExp = rows('expenses').filter(e => monthKey(e.spent_on) === m).reduce((s, e) => s + num(e.amount), 0);
  const stockVal = rows('materials').reduce((s, x) => s + num(x.stock) * num(x.cost), 0) + rows('products').reduce((s, p) => s + num(p.stock) * unitCost(p.id), 0);
  const cats = {}; rows('expenses').forEach(e => cats[e.category] = (cats[e.category] || 0) + num(e.amount));
  const maxC = Math.max(1, ...Object.values(cats));
  return `<div class="kpis">
      <div class="kpi"><span>مبيعات الشهر (بدون التوصيل)</span><b>${money(mSales)}</b></div>
      <div class="kpi"><span>تكلفة البضاعة المباعة</span><b>${money(mCogs)}</b></div>
      <div class="kpi"><span>مصروفات الشهر</span><b>${money(mExp)}</b></div>
      <div class="kpi ${mSales - mCogs - mExp < 0 ? 'alert' : ''}"><span>صافي الربح التقديري</span><b>${money(mSales - mCogs - mExp)}</b></div></div>
    <div class="cols">
      <div class="card"><h2>المبيعات آخر 6 أشهر</h2><div class="bars">${months.map((mm, i) => `<div class="r"><span>${mm}</span><div class="t"><i style="width:${sales[i] / maxS * 100}%"></i></div><span class="num">${fmt(sales[i], 0)}</span></div>`).join('')}</div></div>
      <div class="card"><h2>الأكثر مبيعاً</h2><div class="bars">${prod.slice(0, 6).map(x => `<div class="r"><span>${esc(x.p.name)}</span><div class="t"><i style="width:${x.rev / maxR * 100}%"></i></div><span class="num">${fmt(x.rev, 0)}</span></div>`).join('') || '<div class="empty">لا مبيعات بعد.</div>'}</div></div>
    </div>
    <div class="card"><h2>ربحية المنتجات<span class="sp"></span><span class="who">قيمة المخزون: ${money(stockVal)}</span></h2>${table([
      { h: 'المنتج', v: x => esc(x.p.name) }, { h: 'المباع', num: 1, v: x => fmt(x.qty, 0) }, { h: 'الإيراد', num: 1, v: x => money(x.rev) },
      { h: 'تكلفة الوحدة', num: 1, v: x => money(x.cost) }, { h: 'ربح الوحدة', num: 1, v: x => money(num(x.p.price) - x.cost) },
      { h: 'إجمالي الربح', num: 1, v: x => money(x.rev - x.qty * x.cost) }], prod)}</div>
    <div class="card"><h2>المصروفات حسب البند</h2><div class="bars">${Object.entries(cats).map(([c, v]) => `<div class="r"><span>${esc(c)}</span><div class="t"><i style="width:${v / maxC * 100}%"></i></div><span class="num">${fmt(v, 0)}</span></div>`).join('') || '<div class="empty">لا مصروفات بعد.</div>'}</div></div>`;
};

/* ---------- actions ---------- */
const prodOpts = () => rows('products').map(p => [p.id, p.name]);
const ACTIONS = {
  openProducer: id => { state.pid = id; go('dashboard'); },
  addProducer: () => form({ title: 'منتج جديد (شركة)', fields: producerFields(), onSave: v => ins('producers', v) }),
  editProducer: id => form({ title: 'تعديل المنتج', fields: producerFields(), values: byId('producers', id), onSave: v => upd('producers', id, v) }),
  delProducer: id => confirmBox('حذف المنتج وكل بياناته (المخزون والطلبات والزبائن)؟', async () => {
    for (const t of TABLES.slice(1)) for (const r of state.data[t].filter(x => x.producer_id === id)) if (!LIVE) await del(t, r.id);
    await del('producers', id); if (LIVE) await loadAll(); if (state.pid === id) state.pid = ''; }),

  addMaterial: () => form({ title: 'مادة خام أو عبوة', fields: materialFields(true), onSave: async v => {
    const pid = needProducer(), stock = v.stock; delete v.stock;
    const m = await ins('materials', { ...v, producer_id: pid, stock: 0 });
    if (stock) await move(pid, 'material', m.id, stock, 'رصيد افتتاحي'); } }),
  editMaterial: id => form({ title: 'تعديل المادة', fields: materialFields(false), values: byId('materials', id), onSave: v => upd('materials', id, v) }),
  delMaterial: id => confirmBox('حذف هذه المادة من كل التركيبات؟', async () => {
    for (const f of state.data.formulas.filter(f => f.material_id === id)) await del('formulas', f.id); await del('materials', id); }),
  receive: id => { const m = byId('materials', id); form({ title: 'استلام: ' + m.name, fields: [
      { k: 'qty', label: `الكمية (${unitAr(m.unit)})`, type: 'number', req: 1 }, { k: 'cost', label: 'تكلفة الوحدة', type: 'number', def: m.cost },
      { k: 'ref', label: 'رقم الفاتورة أو المورّد' }, { k: 'expense', label: 'سجّل قيمتها في المصروفات', type: 'check', def: true }],
    onSave: async v => {
      if (v.qty <= 0) throw new Error('أدخل كمية أكبر من صفر.');
      const old = num(m.stock), avgCost = old + v.qty > 0 ? (old * num(m.cost) + v.qty * v.cost) / (old + v.qty) : v.cost;
      await move(m.producer_id, 'material', id, v.qty, 'استلام', v.ref || '');
      await upd('materials', id, { cost: Math.round(avgCost * 10000) / 10000 });
      if (v.expense) await ins('expenses', { producer_id: m.producer_id, category: 'شراء مواد', amount: v.qty * v.cost, spent_on: today(), notes: m.name });
    } }); },
  adjustMat: id => adjust('material', id),

  addProduct: () => form({ title: 'منتج جديد', fields: productFields(), onSave: v => ins('products', { ...v, producer_id: needProducer(), stock: 0 }) }),
  editProduct: id => form({ title: 'تعديل المنتج', fields: productFields(), values: byId('products', id), onSave: v => upd('products', id, v) }),
  delProduct: id => confirmBox('حذف المنتج وتركيبته وتشغيلاته؟', async () => {
    for (const t of ['formulas', 'batches']) for (const r of state.data[t].filter(x => x.product_id === id)) await del(t, r.id); await del('products', id); }),
  formula: id => formulaEditor(id),

  addBatch: () => { const pid = needProducerSafe(); if (!pid) return;
    form({ title: 'تشغيلة إنتاج', fields: [
      { k: 'product_id', label: 'المنتج', type: 'select', options: prodOpts(), req: 1 },
      { k: 'code', label: 'رقم التشغيلة', def: 'B' + today().replace(/-/g, '').slice(2) + '-' + (rows('batches').length + 1), req: 1 },
      { k: 'qty', label: 'عدد الوحدات', type: 'number', req: 1 }, { k: 'made_on', label: 'تاريخ الإنتاج', type: 'date', def: today() },
      { k: 'expires_on', label: 'تاريخ الانتهاء', type: 'date' }],
    onSave: async v => {
      if (v.qty <= 0) throw new Error('أدخل عدد وحدات أكبر من صفر.');
      const fs = state.data.formulas.filter(f => f.product_id === v.product_id);
      if (!fs.length) throw new Error('هذا المنتج بلا تركيبة. أضف التركيبة من صفحة المنتجات أولاً.');
      const short = fs.map(f => { const m = byId('materials', f.material_id), need = num(f.qty_per_unit) * v.qty;
        return num(m.stock) < need ? `${m.name}: المطلوب ${fmt(need)} ${unitAr(m.unit)}، المتوفر ${fmt(m.stock)}` : null; }).filter(Boolean);
      if (short.length) throw new Error('المواد غير كافية:\n' + short.join('\n'));
      for (const f of fs) await move(pid, 'material', f.material_id, -num(f.qty_per_unit) * v.qty, 'إنتاج', v.code);
      await ins('batches', { ...v, producer_id: pid, remaining: 0, qc: 'pending' });
    } }); },
  qc: id => { const b = byId('batches', id); form({ title: 'فحص الجودة: ' + b.code, fields: [
      { k: 'qc', label: 'النتيجة', type: 'select', options: [['pass', 'ناجح: يدخل المخزون'], ['fail', 'مرفوض: لا يُباع']], req: 1 },
      { k: 'qc_notes', label: 'ملاحظات الفحص (pH، اللزوجة، المظهر…)', type: 'textarea' }],
    onSave: async v => {
      await upd('batches', id, { qc: v.qc, qc_notes: v.qc_notes, remaining: v.qc === 'pass' ? num(b.qty) : 0 });
      if (v.qc === 'pass') await move(b.producer_id, 'product', b.product_id, num(b.qty), 'اجتياز الجودة', b.code);
    } }); },

  addCustomer: () => form({ title: 'زبون جديد', fields: customerFields(), onSave: v => ins('customers', { ...v, producer_id: needProducer() }) }),
  editCustomer: id => form({ title: 'تعديل الزبون', fields: customerFields(), values: byId('customers', id), onSave: v => upd('customers', id, v) }),
  delCustomer: id => confirmBox('حذف الزبون؟ طلباته تبقى بدون اسم.', () => del('customers', id)),

  addOrder: () => { if (needProducerSafe()) orderEditor(); },
  orderStatus: id => { const o = byId('orders', id); form({ title: 'حالة الطلب', values: o, fields: [
      { k: 'status', label: 'الحالة', type: 'select', options: opts(LABELS.status) }, { k: 'paid', label: 'تم الدفع', type: 'check' }],
    onSave: async v => {
      if (v.status === 'cancelled') for (const i of state.data.order_items.filter(i => i.order_id === id)) {
        await move(o.producer_id, 'product', i.product_id, num(i.qty), 'إلغاء طلب', id.slice(0, 8));
        const b = state.data.batches.filter(b => b.product_id === i.product_id && b.qc === 'pass').sort((a, c) => String(c.expires_on).localeCompare(String(a.expires_on)))[0];
        if (b) await upd('batches', b.id, { remaining: num(b.remaining) + num(i.qty) });
      }
      await upd('orders', id, v);
    } }); },

  addExpense: () => form({ title: 'مصروف', fields: [
      { k: 'category', label: 'البند', type: 'select', options: ['شراء مواد', 'عبوات', 'توصيل', 'إعلانات', 'رواتب', 'إيجار', 'كهرباء', 'أخرى'].map(x => [x, x]) },
      { k: 'amount', label: 'المبلغ', type: 'number', req: 1 }, { k: 'spent_on', label: 'التاريخ', type: 'date', def: today() }, { k: 'notes', label: 'ملاحظات' }],
    onSave: v => ins('expenses', { ...v, producer_id: needProducer() }) }),
  delExpense: id => confirmBox('حذف المصروف؟', () => del('expenses', id)),
};
function needProducerSafe() { try { return needProducer(); } catch (e) { toast(e.message); return null; } }
const producerFields = () => [{ k: 'name', label: 'اسم الشركة', req: 1 }, { k: 'phone', label: 'الهاتف' }, { k: 'city', label: 'المدينة / الدولة' },
  { k: 'currency', label: 'العملة', type: 'select', options: ['SDG', 'USD', 'SAR', 'AED', 'EGP', 'QAR', 'GBP', 'EUR'].map(x => [x, x]) }, { k: 'notes', label: 'ملاحظات', type: 'textarea' }];
const materialFields = withStock => [{ k: 'name', label: 'الاسم', req: 1 }, { k: 'kind', label: 'النوع', type: 'select', options: opts(LABELS.kind) },
  { k: 'unit', label: 'الوحدة', type: 'select', options: [['g', 'غرام'], ['kg', 'كيلو'], ['ml', 'مل'], ['l', 'لتر'], ['pc', 'قطعة']] },
  { k: 'cost', label: 'تكلفة الوحدة', type: 'number' }, ...(withStock ? [{ k: 'stock', label: 'الرصيد الحالي', type: 'number' }] : []),
  { k: 'reorder_level', label: 'حد إعادة الطلب', type: 'number' }, { k: 'supplier', label: 'المورّد' }];
const productFields = () => [{ k: 'name', label: 'اسم المنتج', req: 1 }, { k: 'size', label: 'الحجم (مثل 50 مل)' }, { k: 'sku', label: 'الرمز SKU' },
  { k: 'price', label: 'سعر البيع', type: 'number' }, { k: 'reorder_level', label: 'حد إعادة الطلب', type: 'number' }];
const customerFields = () => [{ k: 'name', label: 'الاسم', req: 1 }, { k: 'phone', label: 'الهاتف / واتساب' }, { k: 'city', label: 'المدينة' },
  { k: 'channel', label: 'وصلت عن طريق', type: 'select', options: opts(LABELS.channel) }, { k: 'notes', label: 'ملاحظات (نوع البشرة، تفضيلات)', type: 'textarea' }];

function adjust(type, id) {
  const t = type === 'material' ? 'materials' : 'products', it = byId(t, id);
  form({ title: 'تسوية مخزون: ' + it.name, fields: [{ k: 'qty', label: 'الكمية (+ إضافة، − خصم)', type: 'number', req: 1 },
    { k: 'reason', label: 'السبب', type: 'select', options: ['جرد', 'تلف', 'عينات', 'مرتجع', 'أخرى'].map(x => [x, x]) }],
  onSave: async v => { if (num(it.stock) + v.qty < 0) throw new Error('الرصيد لا يكفي لهذا الخصم.'); await move(it.producer_id, type, id, v.qty, v.reason); } });
}

function formulaEditor(pid) {
  const p = byId('products', pid), mats = state.data.materials.filter(m => m.producer_id === p.producer_id);
  if (!mats.length) return toast('أضف المواد الخام والعبوات لهذا المنتج أولاً.');
  const cur = state.data.formulas.filter(f => f.product_id === pid);
  const line = (f = {}) => `<div class="line"><select data-m>${mats.map(m => `<option value="${m.id}" ${m.id === f.material_id ? 'selected' : ''}>${esc(m.name)} (${esc(unitAr(m.unit))})</option>`).join('')}</select>
    <input data-q type="number" step="any" placeholder="الكمية" value="${f.qty_per_unit ?? ''}"><span class="who">لكل وحدة</span><button type="button" class="btn sm soft" data-rm>✕</button></div>`;
  const m = form({ title: 'تركيبة ' + p.name, fields: [], submit: 'حفظ التركيبة',
    extra: `<div class="lines">${(cur.length ? cur : [{}]).map(line).join('')}</div><button type="button" class="btn soft" data-add>+ مكوّن</button>
      <p class="who">أدخل كمية كل مادة أو عبوة اللازمة لإنتاج وحدة واحدة من المنتج.</p>`,
    onSave: async () => {
      const ls = [...m.querySelectorAll('.line')].map(l => ({ material_id: l.querySelector('[data-m]').value, qty_per_unit: num(l.querySelector('[data-q]').value) })).filter(l => l.qty_per_unit > 0);
      for (const f of cur) await del('formulas', f.id);
      for (const l of ls) await ins('formulas', { ...l, product_id: pid, producer_id: p.producer_id });
    } });
  m.querySelector('[data-add]').onclick = () => m.querySelector('.lines').insertAdjacentHTML('beforeend', line());
  m.addEventListener('click', e => { if (e.target.matches('[data-rm]')) e.target.closest('.line').remove(); });
}

function orderEditor() {
  const pid = state.pid, ps = rows('products');
  if (!ps.length) return toast('أضف منتجات أولاً.');
  const line = () => `<div class="line"><select data-p>${ps.map(p => `<option value="${p.id}" data-price="${num(p.price)}">${esc(p.name)} (متوفر ${fmt(p.stock, 0)})</option>`).join('')}</select>
    <input data-q type="number" step="1" min="1" value="1"><input data-pr type="number" step="any" value="${num(ps[0].price)}"><button type="button" class="btn sm soft" data-rm>✕</button></div>`;
  const m = form({ title: 'طلب جديد', submit: 'تسجيل الطلب', fields: [
      { k: 'customer_id', label: 'الزبون', type: 'select', blank: 1, options: rows('customers').map(c => [c.id, c.name + (c.phone ? ' · ' + c.phone : '')]) },
      { k: 'new_customer', label: 'أو زبون جديد (الاسم)' }, { k: 'phone', label: 'هاتف الزبون الجديد' },
      { k: 'channel', label: 'القناة', type: 'select', options: opts(LABELS.channel) }, { k: 'ordered_on', label: 'التاريخ', type: 'date', def: today() },
      { k: 'delivery_fee', label: 'رسوم التوصيل', type: 'number', def: 0 }, { k: 'paid', label: 'تم الدفع', type: 'check' }, { k: 'notes', label: 'العنوان / ملاحظات' }],
    extra: `<div class="who">المنتج · الكمية · السعر</div><div class="lines">${line()}</div><button type="button" class="btn soft" data-add>+ منتج</button>`,
    onSave: async v => {
      const ls = [...m.querySelectorAll('.line')].map(l => ({ product_id: l.querySelector('[data-p]').value, qty: num(l.querySelector('[data-q]').value), price: num(l.querySelector('[data-pr]').value) })).filter(l => l.qty > 0);
      if (!ls.length) throw new Error('أضف منتجاً واحداً على الأقل.');
      const need = {}; ls.forEach(l => need[l.product_id] = (need[l.product_id] || 0) + l.qty);
      const short = Object.entries(need).filter(([id, q]) => num(byId('products', id).stock) < q).map(([id, q]) => `${nameOf('products', id)}: المطلوب ${q}، المتوفر ${fmt(byId('products', id).stock, 0)}`);
      if (short.length) throw new Error('المخزون غير كافٍ:\n' + short.join('\n'));
      let cid = v.customer_id;
      if (!cid && v.new_customer) cid = (await ins('customers', { producer_id: pid, name: v.new_customer, phone: v.phone, channel: v.channel })).id;
      const total = ls.reduce((s, l) => s + l.qty * l.price, 0) + num(v.delivery_fee);
      const o = await ins('orders', { producer_id: pid, customer_id: cid || null, channel: v.channel, status: 'new', paid: v.paid, delivery_fee: num(v.delivery_fee), total, ordered_on: v.ordered_on || today(), notes: v.notes });
      for (const l of ls) {
        await ins('order_items', { ...l, order_id: o.id, producer_id: pid });
        await move(pid, 'product', l.product_id, -l.qty, 'بيع', o.id.slice(0, 8));
        // first-expiring batches first
        let left = l.qty;
        for (const b of state.data.batches.filter(b => b.product_id === l.product_id && b.qc === 'pass' && num(b.remaining) > 0).sort((a, c) => String(a.expires_on || '9999').localeCompare(String(c.expires_on || '9999')))) {
          if (left <= 0) break; const take = Math.min(left, num(b.remaining)); left -= take; await upd('batches', b.id, { remaining: num(b.remaining) - take });
        }
      }
    } });
  m.querySelector('[data-add]').onclick = () => m.querySelector('.lines').insertAdjacentHTML('beforeend', line());
  m.addEventListener('click', e => { if (e.target.matches('[data-rm]')) e.target.closest('.line').remove(); });
  m.addEventListener('change', e => { if (e.target.matches('[data-p]')) e.target.closest('.line').querySelector('[data-pr]').value = e.target.selectedOptions[0].dataset.price; });
}

/* ---------- shell ---------- */
function go(page) { state.page = page; state.q = ''; location.hash = page; render(); }
function render() {
  const root = $('#root');
  if (!state.user) return renderLogin(root);
  const nav = NAV.filter(n => !n[3] || isAdmin());
  if (!nav.some(n => n[0] === state.page)) state.page = 'dashboard';
  const title = NAV.find(n => n[0] === state.page)[2];
  const prodSel = isAdmin() ? `<select class="sel-producer" data-pid><option value="">كل المنتجين</option>${state.data.producers.map(p => `<option value="${p.id}" ${p.id === state.pid ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select>` : `<b>${esc(nameOf('producers', state.pid))}</b>`;
  root.innerHTML = `<div class="app"><aside><div class="brand"><img src="../logo.png" alt=""><div><b>BEAUTY GRID</b><small>نظام إدارة المنتجين</small></div></div>
      <nav>${nav.map(([k, i, l]) => `<a href="#${k}" class="${k === state.page ? 'on' : ''}" data-go="${k}"><i>${i}</i>${l}</a>`).join('')}</nav></aside>
    <main>${LIVE ? '' : `<div class="demo-banner">وضع تجريبي: البيانات محفوظة في هذا المتصفح فقط. اربط Supabase في config.js للعمل الحقيقي. <button class="btn sm soft" data-act="viewAs">${isAdmin() ? 'عرض كمنتج' : 'عرض كمدير'}</button> <button class="btn sm danger" data-act="resetDemo">مسح البيانات التجريبية</button></div>`}
      <div class="top"><h1>${title}</h1>${prodSel}<span class="who">${esc(state.user.label || '')}</span>${LIVE ? '<button class="btn sm soft" data-act="logout">خروج</button>' : ''}</div>
      <div id="page">${PAGES[state.page]()}</div></main></div>`;
}
document.addEventListener('click', e => {
  const g = e.target.closest('[data-go]'); if (g) { e.preventDefault(); return go(g.dataset.go); }
  const a = e.target.closest('[data-act]'); if (!a) return;
  const fn = ACTIONS[a.dataset.act] || SHELL[a.dataset.act];
  if (fn) { try { fn(a.dataset.id); } catch (err) { toast(err.message); } }
});
document.addEventListener('change', e => { if (e.target.matches('[data-pid]')) { state.pid = e.target.value; render(); } });
document.addEventListener('input', e => {
  if (!e.target.matches('[data-search]')) return;
  state.q = e.target.value; const pos = e.target.selectionStart;
  $('#page').innerHTML = PAGES[state.page](); const s = $('[data-search]'); s.focus(); s.setSelectionRange(pos, pos);
});
window.addEventListener('hashchange', () => { const p = location.hash.slice(1); if (p && p !== state.page) { state.page = p; render(); } });

const SHELL = {
  logout: async () => { await sb.auth.signOut(); state.user = null; render(); },
  viewAs: () => {
    if (isAdmin()) { const p = state.data.producers[0]; if (!p) return toast('أضف منتجاً أولاً.'); state.user = { role: 'producer', label: 'عرض المنتج (تجريبي)' }; state.pid = p.id; }
    else { state.user = { role: 'admin', label: 'مدير Beauty-Grid (تجريبي)' }; }
    render();
  },
  resetDemo: () => confirmBox('مسح كل البيانات التجريبية من هذا المتصفح؟', async () => { TABLES.forEach(t => Local.write(t, [])); await seedDemo(); await loadAll(); state.pid = ''; }),
};

function renderLogin(root) {
  root.innerHTML = `<div class="login"><form class="sheet" id="login"><div class="brand"><img src="../logo.png" alt=""><div><b>BEAUTY GRID</b><small>نظام إدارة المنتجين</small></div></div>
    <div class="fields"><label class="f">البريد الإلكتروني<input name="email" type="email" required autocomplete="username"></label>
    <label class="f">كلمة المرور<input name="password" type="password" required autocomplete="current-password"></label></div>
    <div class="err"></div><div class="foot"><button class="btn" type="submit" style="flex:1">دخول</button></div></form></div>`;
  $('#login').onsubmit = async e => {
    e.preventDefault(); const fd = new FormData(e.target);
    const { error } = await sb.auth.signInWithPassword({ email: fd.get('email'), password: fd.get('password') });
    if (error) { $('#login .err').textContent = 'تعذّر الدخول: تأكد من البريد وكلمة المرور.'; return; }
    await startLive();
  };
}

async function startLive() {
  const { data: { session } } = await sb.auth.getSession();
  if (!session) { state.user = null; return render(); }
  const { data: prof } = await sb.from('profiles').select('*').eq('id', session.user.id).single();
  if (!prof || (prof.role !== 'admin' && !prof.producer_id)) {
    $('#root').innerHTML = `<div class="login"><div class="sheet"><h3>الحساب بانتظار التفعيل</h3><p>اطلب من Beauty-Grid ربط حسابك بشركتك.</p><button class="btn soft" id="lo">خروج</button></div></div>`;
    $('#lo').onclick = async () => { await sb.auth.signOut(); location.reload(); }; return;
  }
  state.user = { role: prof.role, label: prof.full_name || session.user.email };
  state.pid = prof.role === 'admin' ? '' : prof.producer_id;
  await loadAll(); render();
}

/* demo data so the system can be explored right away */
async function seedDemo() {
  if (Local.read('producers').length) return;
  const P = await Local.insert('producers', { name: 'Indigo Cosmetics', phone: '', city: 'الخرطوم', currency: 'SDG' });
  const pid = P.id;
  const mat = async (name, kind, unit, cost, stock, reorder) => (await Local.insert('materials', { producer_id: pid, name, kind, unit, cost, stock, reorder_level: reorder, supplier: '' })).id;
  const shea = await mat('زبدة الشيا', 'raw', 'g', 6, 4000, 800), glyc = await mat('جليسرين', 'raw', 'g', 3, 2500, 500),
    nia = await mat('نياسيناميد', 'raw', 'g', 40, 150, 100), aloe = await mat('جل الألوفيرا', 'raw', 'g', 5, 3000, 600),
    surf = await mat('منظّف لطيف (سيرفاكتنت)', 'raw', 'g', 8, 1800, 400), water = await mat('ماء مقطّر', 'raw', 'ml', 0.2, 20000, 3000),
    jar = await mat('علبة 50 مل', 'pack', 'pc', 350, 120, 40), drop = await mat('قطّارة 30 مل', 'pack', 'pc', 420, 30, 40), pump = await mat('عبوة مضخة 150 مل', 'pack', 'pc', 380, 90, 30);
  const prod = async (name, size, price, reorder, f) => { const p = await Local.insert('products', { producer_id: pid, name, size, sku: '', price, stock: 0, reorder_level: reorder });
    for (const [m, q] of f) await Local.insert('formulas', { producer_id: pid, product_id: p.id, material_id: m, qty_per_unit: q }); return p; };
  const aura = await prod('Aura مرطّب', '50 مل', 9000, 10, [[shea, 12], [glyc, 4], [aloe, 10], [water, 25], [jar, 1]]);
  const glaze = await prod('Glaze سيروم التصبغ', '30 مل', 12000, 10, [[nia, 1.5], [glyc, 2], [aloe, 6], [water, 20], [drop, 1]]);
  const moon = await prod('Blue Moon غسول', '150 مل', 7000, 10, [[surf, 18], [glyc, 5], [aloe, 8], [water, 110], [pump, 1]]);
  const d = n => { const x = new Date(); x.setDate(x.getDate() + n); return x.toISOString().slice(0, 10); };
  for (const [p, code, qty, sold, exp] of [[aura, 'B-A01', 40, 22, d(300)], [glaze, 'B-G01', 25, 21, d(45)], [moon, 'B-M01', 30, 12, d(400)]]) {
    await Local.insert('batches', { producer_id: pid, product_id: p.id, code, qty, remaining: qty - sold, made_on: d(-40), expires_on: exp, qc: 'pass', qc_notes: 'pH ضمن المواصفة' });
    Local.write('products', Local.read('products').map(x => x.id === p.id ? { ...x, stock: qty - sold } : x));
  }
  await Local.insert('batches', { producer_id: pid, product_id: aura.id, code: 'B-A02', qty: 30, remaining: 0, made_on: d(-1), expires_on: d(365), qc: 'pending' });
  const c1 = await Local.insert('customers', { producer_id: pid, name: 'سارة أحمد', phone: '0912000000', city: 'الخرطوم', channel: 'instagram', notes: 'بشرة دهنية' });
  const c2 = await Local.insert('customers', { producer_id: pid, name: 'مريم عثمان', phone: '0923000000', city: 'أم درمان', channel: 'whatsapp', notes: '' });
  const order = async (c, ch, st, paid, day, items) => { const total = items.reduce((s, [p, q]) => s + q * p.price, 0) + 1500;
    const o = await Local.insert('orders', { producer_id: pid, customer_id: c.id, channel: ch, status: st, paid, delivery_fee: 1500, total, ordered_on: d(day), notes: '' });
    for (const [p, q] of items) await Local.insert('order_items', { producer_id: pid, order_id: o.id, product_id: p.id, qty: q, price: p.price }); };
  await order(c1, 'instagram', 'delivered', true, -20, [[aura, 2], [moon, 1]]);
  await order(c2, 'whatsapp', 'delivered', true, -6, [[glaze, 3]]);
  await order(c1, 'instagram', 'new', false, 0, [[glaze, 1], [aura, 1]]);
  await Local.insert('expenses', { producer_id: pid, category: 'إعلانات', amount: 25000, spent_on: d(-3), notes: 'إعلان إنستغرام' });
  await Local.insert('expenses', { producer_id: pid, category: 'توصيل', amount: 6000, spent_on: d(-2), notes: '' });
}

(async () => {
  state.page = location.hash.slice(1) || 'dashboard';
  try {
    if (LIVE) { sb.auth.onAuthStateChange((ev) => { if (ev === 'SIGNED_OUT') { state.user = null; render(); } }); await startLive(); }
    else { await seedDemo(); await loadAll(); state.user = { role: 'admin', label: 'مدير Beauty-Grid (تجريبي)' }; render(); }
  } catch (e) { $('#root').innerHTML = `<div class="login"><div class="sheet"><h3>تعذّر تحميل النظام</h3><p class="err">${esc(e.message)}</p></div></div>`; }
})();
