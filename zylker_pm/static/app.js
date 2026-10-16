/* Zylker Projects - single-page UI. No frameworks, no innerHTML: every piece of user text is
   inserted with textContent/createTextNode, so stored content can never become markup. */
(() => {
'use strict';

/* ============================== DOM helpers ============================== */
const SVG_NS = 'http://www.w3.org/2000/svg';

function addChildren(el, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false) continue;
    el.append(k instanceof Node ? k : document.createTextNode(String(k)));
  }
}
function make(create, tag, attrs, kids) {
  const el = create(tag);
  let value;
  for (const [name, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (name === 'class') el.setAttribute('class', v);
    else if (name === 'text') el.textContent = v;
    else if (name === 'style') Object.assign(el.style, v); // CSSOM: allowed by the CSP, unlike style="" attributes
    else if (name === 'dataset') Object.assign(el.dataset, v);
    else if (name === 'value') value = v; // applied last so <select> options already exist
    else if (name.startsWith('on') && typeof v === 'function') el.addEventListener(name.slice(2), v);
    else el.setAttribute(name, v === true ? '' : String(v));
  }
  addChildren(el, kids);
  if (value !== undefined) el.value = value;
  return el;
}
const h = (tag, attrs, ...kids) => make((t) => document.createElement(t), tag, attrs, kids);
// Element.append(null) renders the text "null"; put() skips null/false like h() does.
const put = (el, ...kids) => { addChildren(el, kids); return el; };
const sv = (tag, attrs, ...kids) => make((t) => document.createElementNS(SVG_NS, t), tag, attrs, kids);
const $ = (sel, root = document) => root.querySelector(sel);
const clear = (el) => el.replaceChildren();

/* ============================== constants & formatting ============================== */
const STATUS_LABEL = { todo: 'To do', in_progress: 'In progress', in_review: 'In review', blocked: 'Blocked', done: 'Done', cancelled: 'Cancelled' };
const PRIORITY_LABEL = { low: 'Low', medium: 'Medium', high: 'High', critical: 'Critical' };
const TYPE_LABEL = { task: 'Task', bug: 'Bug', story: 'Story', epic: 'Epic' };
const HEALTH_LABEL = { on_track: 'On track', at_risk: 'At risk', off_track: 'Off track', complete: 'Complete', not_started: 'Not started', on_hold: 'On hold' };
const PROJECT_STATUS_LABEL = { planning: 'Planning', active: 'Active', on_hold: 'On hold', completed: 'Completed', archived: 'Archived' };
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const ROLES = ['viewer', 'member', 'manager', 'admin'];

const state = { token: null, me: null, meta: null, users: [], projects: [], today: '', pendingTask: null, projectCache: {} };

const parseDate = (s) => { if (!s) return null; const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };
const dayDiff = (a, b) => Math.round((parseDate(b) - parseDate(a)) / 86400000);
const isoDate = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
function addDays(s, n) { const d = parseDate(s); d.setDate(d.getDate() + n); return isoDate(d); }
function fmtDate(s) {
  const d = parseDate(s);
  if (!d) return '\u2014';
  const same = state.today && d.getFullYear() === parseDate(state.today).getFullYear();
  return `${d.getDate()} ${MONTHS[d.getMonth()]}${same ? '' : ' ' + d.getFullYear()}`;
}
const fmtHours = (n) => (n === null || n === undefined ? '\u2014' : `${Math.round(n * 10) / 10}h`);
function fmtAgo(iso) {
  const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return 'just now';
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  if (secs < 86400 * 14) return `${Math.floor(secs / 86400)}d ago`;
  return fmtDate(iso.slice(0, 10));
}
function dueText(task) {
  if (!task.due_date) return null;
  return h('span', { class: task.is_overdue ? 'overdue-text' : '', title: task.is_overdue ? 'Overdue' : 'Due date' }, fmtDate(task.due_date));
}
const strHash = (s) => { let x = 0; for (const c of s) x = (x * 31 + c.charCodeAt(0)) >>> 0; return x; };
function avatar(user) {
  if (!user) return h('span', { class: 'avatar none', title: 'Unassigned' }, '?');
  const initials = user.display_name.split(/\s+/).map((w) => w[0]).slice(0, 2).join('').toUpperCase();
  return h('span', { class: 'avatar c' + (strHash(user.username) % 8), title: user.display_name }, initials);
}
const badge = (kind, text, title) => h('span', { class: 'badge ' + kind, title }, text);
const statusBadge = (s) => badge(s, STATUS_LABEL[s] || s);
const priorityBadge = (p) => badge(p, PRIORITY_LABEL[p] || p);
const healthBadge = (hl) => badge(hl.status, HEALTH_LABEL[hl.status], hl.reasons.join('\n'));
function progressBar(pct, kind) {
  return h('div', { class: 'bar ' + (kind || ''), role: 'progressbar', 'aria-valuenow': Math.round(pct), 'aria-valuemin': 0, 'aria-valuemax': 100 },
    h('span', { style: { width: Math.max(0, Math.min(100, pct)) + '%' } }));
}
const hasRole = (role) => ROLES.indexOf(state.me.role) >= ROLES.indexOf(role);
const isMember = (project) => project.members.some((m) => m.username === state.me.username);
const canWrite = (project) => project.status !== 'archived' && (hasRole('manager') || (state.me.role === 'member' && isMember(project)));
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

/* ============================== API ============================== */
class ApiError extends Error {
  constructor(status, error) {
    let msg = (error && error.message) || (status === 0 ? 'Cannot reach the server' : `Request failed (${status})`);
    if (error && error.details && typeof error.details === 'object') {
      const parts = Object.entries(error.details).filter(([, v]) => typeof v === 'string').map(([k, v]) => `${k}: ${v}`);
      if (parts.length) msg += ' \u2014 ' + parts.join('; ');
    }
    super(msg);
    this.status = status;
    this.code = error && error.code;
  }
}
async function api(path, opts = {}) {
  const url = new URL('/api' + path, location.origin);
  for (const [k, v] of Object.entries(opts.query || {})) if (v !== '' && v !== null && v !== undefined) url.searchParams.set(k, v);
  const headers = { Authorization: 'Bearer ' + state.token };
  let body;
  if (opts.body !== undefined) {
    headers['Content-Type'] = opts.contentType || 'application/json';
    body = opts.contentType ? opts.body : JSON.stringify(opts.body);
  }
  let res;
  try { res = await fetch(url, { method: opts.method || 'GET', headers, body }); } catch (e) { throw new ApiError(0, null); }
  if (res.status === 204) return null;
  if (opts.blob && res.ok) return res.blob();
  let data = null;
  try { data = await res.json(); } catch (e) { /* not JSON */ }
  if (res.status === 401 && state.me) signOut('Your session has ended. Please sign in again.'); // not while signing in
  if (!res.ok) throw new ApiError(res.status, data && data.error);
  return data;
}

/* ============================== widgets: toast, modal ============================== */
function toast(message, kind) {
  let box = $('.toasts');
  if (!box) { box = h('div', { class: 'toasts', role: 'status', 'aria-live': 'polite' }); document.body.append(box); }
  const el = h('div', { class: 'toast ' + (kind || '') }, message);
  box.append(el);
  setTimeout(() => el.remove(), kind === 'error' ? 7000 : 3500);
}
const modalStack = [];
function openModal({ title, content, footer, narrow, onClose }) {
  const returnTo = document.activeElement;
  const backdrop = h('div', { class: 'backdrop' });
  const dialog = h('div', { class: 'modal' + (narrow ? ' narrow' : ''), role: 'dialog', 'aria-modal': 'true', 'aria-label': title || 'Dialog' });
  const handle = { el: dialog, close() {
    document.removeEventListener('keydown', onKey);
    modalStack.splice(modalStack.indexOf(handle), 1);
    backdrop.remove();
    if (returnTo && returnTo.focus && document.contains(returnTo)) returnTo.focus();
    if (onClose) onClose();
  } };
  const onKey = (e) => { if (e.key === 'Escape' && modalStack[modalStack.length - 1] === handle) handle.close(); };
  if (title) dialog.append(h('div', { class: 'modal-head' }, h('h2', { text: title }), h('span', { class: 'spacer' }),
    h('button', { class: 'btn ghost', 'aria-label': 'Close', onclick: () => handle.close() }, '\u2715')));
  dialog.append(h('div', { class: 'modal-body' }, content));
  if (footer) dialog.append(h('div', { class: 'modal-foot' }, footer));
  backdrop.append(dialog);
  backdrop.addEventListener('mousedown', (e) => { if (e.target === backdrop) handle.close(); });
  document.addEventListener('keydown', onKey);
  modalStack.push(handle);
  document.body.append(backdrop);
  dialog.setAttribute('tabindex', '-1'); // lets focus land on the dialog itself when content renders later
  (dialog.querySelector('input:not([type=hidden]), textarea, select') || dialog.querySelector('button') || dialog).focus();
  return handle;
}
function confirmBox(message, confirmLabel, danger) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (v) => { if (!settled) { settled = true; resolve(v); } };
    const m = openModal({ title: 'Please confirm', narrow: true, onClose: () => done(false), content: h('p', { text: message }),
      footer: [h('button', { class: 'btn', onclick: () => m.close() }, 'Cancel'),
        h('button', { class: 'btn ' + (danger ? 'danger' : 'primary'), onclick: () => { done(true); m.close(); } }, confirmLabel || 'Confirm')] });
  });
}
const field = (label, control, hint) => h('label', { class: 'field' }, label, control, hint ? h('span', { class: 'small muted', text: hint }) : null);
const optionList = (pairs) => pairs.map(([value, label]) => h('option', { value }, label));
const userPairs = (list, blank) => [...(blank ? [['', blank]] : []), ...list.map((u) => [u.username, u.display_name])];

async function guarded(button, fn) {
  button.disabled = true;
  try { return await fn(); } catch (e) { toast(e.message, 'error'); } finally { button.disabled = false; }
}

/* ============================== session & shell ============================== */
function saveToken(token, remember) {
  localStorage.removeItem('zpm_token'); sessionStorage.removeItem('zpm_token');
  (remember ? localStorage : sessionStorage).setItem('zpm_token', token);
}
function signOut(message) {
  localStorage.removeItem('zpm_token'); sessionStorage.removeItem('zpm_token');
  state.token = null; state.me = null; state.projectCache = {};
  showLogin(message);
}
function showLogin(message) {
  const root = $('#root');
  clear(root);
  const input = h('input', { type: 'password', autocomplete: 'off', placeholder: 'zpm_\u2026', required: true, id: 'token-input' });
  const remember = h('input', { type: 'checkbox', id: 'remember' });
  const error = h('p', { class: 'error-text', role: 'alert', text: message || '' });
  const form = h('form', { class: 'stack', onsubmit: async (e) => {
    e.preventDefault();
    error.textContent = '';
    state.token = input.value.trim();
    try {
      await loadSession();
      saveToken(state.token, remember.checked);
      renderShell();
      location.hash = '#/dashboard';
      route();
    } catch (err) {
      state.token = null;
      error.textContent = err.status === 401 ? 'That token was not recognised.' : err.message;
    }
  } },
    field('API token', input, 'Ask an admin for a token, or run `zylker-pm create-user`.'),
    h('label', { class: 'row small' }, remember, 'Remember me on this device'),
    error, h('button', { class: 'btn primary', type: 'submit' }, 'Sign in'));
  root.append(h('div', { class: 'login' }, h('div', { class: 'card stack' },
    h('div', { class: 'brand' }, logo(), 'Zylker Projects'), h('p', { class: 'muted', text: 'Sign in to plan, track and deliver your organisation\u2019s projects.' }), form)));
  input.focus();
}
const logo = () => sv('svg', { width: 28, height: 28, viewBox: '0 0 32 32', 'aria-hidden': 'true' },
  sv('rect', { width: 32, height: 32, rx: 8, fill: '#3d5afe' }),
  sv('path', { d: 'M9 10h14l-9 12h9', fill: 'none', stroke: '#fff', 'stroke-width': 3, 'stroke-linecap': 'round', 'stroke-linejoin': 'round' }));

async function loadSession() {
  const [me, meta, users, projects] = await Promise.all([api('/me'), api('/meta'), api('/users'), api('/projects')]);
  Object.assign(state, { me, meta, users: users.items, projects: projects.items, today: meta.today });
}
async function refreshProjects() {
  state.projects = (await api('/projects')).items;
  state.projectCache = {};
  renderProjectNav();
}
async function getProject(key) {
  if (!state.projectCache[key]) state.projectCache[key] = await api('/projects/' + key);
  return state.projectCache[key];
}

function renderShell() {
  const root = $('#root');
  clear(root);
  const searchBox = h('input', { type: 'search', placeholder: 'Search tasks by title or key\u2026', 'aria-label': 'Search tasks', autocomplete: 'off' });
  const results = h('div', { class: 'search-results hidden' });
  let timer;
  searchBox.addEventListener('input', () => {
    clearTimeout(timer);
    const q = searchBox.value.trim();
    if (!q) { results.classList.add('hidden'); return; }
    timer = setTimeout(async () => {
      try {
        const data = await api('/tasks', { query: { q, limit: 8, sort: '-updated_at' } });
        clear(results);
        if (!data.items.length) results.append(h('div', { class: 'empty small', text: 'No matching tasks' }));
        for (const t of data.items) results.append(h('button', { type: 'button', onclick: () => { results.classList.add('hidden'); searchBox.value = ''; openTaskFromAnywhere(t.key); } },
          h('strong', { class: 'nowrap', text: t.key }), h('span', { class: 'grow', text: t.title }), statusBadge(t.status)));
        results.classList.remove('hidden');
      } catch (e) { /* ignore transient search errors */ }
    }, 250);
  });
  searchBox.addEventListener('keydown', (e) => { if (e.key === 'Escape') { results.classList.add('hidden'); searchBox.blur(); } });
  document.addEventListener('click', (e) => { if (!e.target.closest('.search')) results.classList.add('hidden'); });

  const sidebar = h('aside', { class: 'sidebar', 'aria-label': 'Primary' },
    h('div', { class: 'brand' }, logo(), 'Zylker Projects'),
    h('a', { class: 'nav-link', href: '#/dashboard', dataset: { route: 'dashboard' } }, 'Dashboard'),
    h('a', { class: 'nav-link', href: '#/mine', dataset: { route: 'mine' } }, 'My tasks'),
    h('a', { class: 'nav-link', href: '#/team', dataset: { route: 'team' } }, 'Team workload'),
    h('div', { class: 'nav-heading', text: 'Projects' }),
    h('div', { id: 'project-nav' }),
    hasRole('manager') ? h('button', { class: 'btn small', style: { margin: '8px 10px' }, onclick: openCreateProject }, '+ New project') : null,
    hasRole('admin') ? h('div', { class: 'nav-heading', text: 'Admin' }) : null,
    hasRole('admin') ? h('a', { class: 'nav-link', href: '#/users', dataset: { route: 'users' } }, 'Users & access') : null);

  const topbar = h('header', { class: 'topbar' }, h('div', { class: 'search' }, searchBox, results), h('span', { class: 'spacer' }),
    avatar(state.me), h('div', { class: 'user-chip' }, h('div', { text: state.me.display_name }), h('div', { class: 'small muted', text: state.me.role })),
    h('button', { class: 'btn small', onclick: () => signOut('You have been signed out.') }, 'Sign out'));
  root.append(h('div', { class: 'layout' }, sidebar, h('div', { class: 'main' }, topbar, h('main', { class: 'content', id: 'view', tabindex: '-1' }))));
  renderProjectNav();
}
function renderProjectNav() {
  const box = $('#project-nav');
  if (!box) return;
  clear(box);
  for (const p of state.projects) box.append(h('a', { class: 'nav-link', href: '#/p/' + p.key, dataset: { route: 'p/' + p.key } },
    h('span', { class: 'nav-key', text: p.key }), h('span', { class: 'ellipsis', text: p.name })));
  highlightNav();
}
function highlightNav() {
  const here = location.hash.replace(/^#\//, '');
  for (const a of document.querySelectorAll('.nav-link[data-route]')) a.classList.toggle('active', here === a.dataset.route || here.startsWith(a.dataset.route + '/'));
}

/* ============================== router ============================== */
let renderCount = 0;
async function route() {
  if (!state.token) return;
  const mine = ++renderCount;
  const alive = () => mine === renderCount;
  const view = $('#view');
  if (!view) return;
  highlightNav();
  const [a, b, c] = (location.hash.replace(/^#\/?/, '') || 'dashboard').split('/');
  try {
    if (a === 'p' && b) await projectView(view, b.toUpperCase(), c || 'board', alive);
    else if (a === 't' && b) { state.pendingTask = b.toUpperCase(); const t = await api('/tasks/' + state.pendingTask); location.replace('#/p/' + t.project + '/board'); }
    else if (a === 'mine') await myTasksView(view, alive);
    else if (a === 'team') await teamView(view, alive);
    else if (a === 'users' && hasRole('admin')) await usersView(view, alive);
    else await dashboardView(view, alive);
  } catch (e) {
    if (!alive() || !state.token) return;
    clear(view);
    view.append(h('div', { class: 'card empty' }, h('h2', { text: e.status === 404 ? 'Not found' : 'Something went wrong' }), h('p', { text: e.message }),
      h('button', { class: 'btn', onclick: route }, 'Try again')));
  }
}
window.addEventListener('hashchange', route);

/* ============================== dashboard ============================== */
function kpi(label, value, kind) { return h('div', { class: 'card kpi ' + (kind || '') }, h('div', { class: 'label', text: label }), h('div', { class: 'value', text: value })); }

async function dashboardView(view, alive) {
  const [portfolio, digest, workload] = await Promise.all([api('/reports/portfolio'), api('/reports/digest', { query: { days: 7 } }), api('/reports/workload')]);
  if (!alive()) return;
  clear(view);
  const t = portfolio.totals;
  const risky = (t.by_health.at_risk || 0) + (t.by_health.off_track || 0);
  view.append(
    h('div', { class: 'page-head' }, h('div', { class: 'titles' }, h('h1', { text: 'Dashboard' }), h('p', { class: 'muted', text: `Portfolio overview as of ${fmtDate(portfolio.as_of)}` })),
      hasRole('manager') ? h('button', { class: 'btn primary', onclick: openCreateProject }, '+ New project') : null),
    h('div', { class: 'grid kpis' }, kpi('Projects', t.projects), kpi('Open tasks', t.open_tasks), kpi('Overdue tasks', t.overdue_tasks, t.overdue_tasks ? 'bad' : ''), kpi('At risk / off track', risky, risky ? 'warn' : '')),
    h('div', { class: 'card flush', style: { marginTop: '16px' } }, portfolioTable(portfolio.projects)),
    h('div', { class: 'grid two', style: { marginTop: '16px' } }, attentionCard(digest), workloadCard(workload)));
}
function portfolioTable(projects) {
  if (!projects.length) return h('div', { class: 'empty', text: 'No projects yet. Create one to get started.' });
  return h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ['Project', 'Health', 'Progress', 'Open', 'Overdue', 'Next milestone', 'Due'].map((x) => h('th', { text: x })))),
    h('tbody', {}, projects.map((p) => h('tr', { class: 'click', tabindex: 0, onclick: () => { location.hash = '#/p/' + p.key; }, onkeydown: (e) => { if (e.key === 'Enter') location.hash = '#/p/' + p.key; } },
      h('td', {}, h('strong', { text: p.name }), h('div', { class: 'small muted', text: `${p.key} \u00b7 ${PROJECT_STATUS_LABEL[p.status]} \u00b7 ${p.owner ? p.owner.display_name : 'no owner'}` })),
      h('td', {}, healthBadge(p.health)),
      h('td', { style: { minWidth: '150px' } }, h('div', { class: 'row' }, progressBar(p.progress.tasks_pct), h('span', { class: 'small nowrap', text: `${Math.round(p.progress.tasks_pct)}%` }))),
      h('td', { text: p.open_tasks }),
      h('td', { class: p.overdue ? 'overdue-text' : '', text: p.overdue }),
      h('td', {}, p.next_milestone ? h('span', {}, p.next_milestone.name, ' ', badge(p.next_milestone.status === 'overdue' ? 'overdue' : 'open', fmtDate(p.next_milestone.due_date))) : '\u2014'),
      h('td', { class: 'nowrap' }, p.due_date ? [fmtDate(p.due_date), p.days_remaining !== null ? h('div', { class: 'small ' + (p.days_remaining < 0 ? 'overdue-text' : 'muted'), text: p.days_remaining < 0 ? `${-p.days_remaining}d late` : `${p.days_remaining}d left` }) : null] : '\u2014')))));
}
function attentionCard(digest) {
  const items = digest.groups.flatMap((g) => [
    ...g.overdue.map((t) => ({ ...t, who: g.assignee, late: true })),
    ...g.due_soon.map((t) => ({ ...t, who: g.assignee, late: false }))]);
  items.sort((a, b) => (b.late - a.late) || a.due_date.localeCompare(b.due_date));
  const card = h('div', { class: 'card' }, h('h2', { text: 'Needs attention' }), h('p', { class: 'small muted', text: `${digest.overdue_count} overdue \u00b7 ${digest.due_soon_count} due within ${digest.days} days` }));
  if (!items.length) card.append(h('div', { class: 'empty', text: 'Nothing overdue or due soon \u2014 nice.' }));
  for (const t of items.slice(0, 10)) card.append(h('div', { class: 'item' }, avatar(t.who), h('a', { class: 'grow', href: '#/t/' + t.key }, h('strong', { text: t.key }), ' ', t.title),
    t.late ? h('span', { class: 'overdue-text small nowrap', text: `${t.days_overdue}d overdue` }) : h('span', { class: 'small muted nowrap', text: t.days_until_due === 0 ? 'due today' : `in ${t.days_until_due}d` })));
  if (items.length > 10) card.append(h('p', { class: 'small muted', text: `\u2026and ${items.length - 10} more` }));
  return card;
}
function utilCell(item) {
  const pct = item.utilization_pct;
  const kind = item.overallocated ? 'bad' : pct !== null && pct >= 85 ? 'warn' : 'ok';
  return h('div', { class: 'row util-cell' }, progressBar(Math.min(pct || 0, 100), kind),
    h('span', { class: 'small nowrap ' + (item.overallocated ? 'overdue-text' : ''), text: `${fmtHours(item.scheduled_hours)} / ${fmtHours(item.capacity_hours)}` }));
}
function workloadCard(workload) {
  const card = h('div', { class: 'card' }, h('h2', { text: 'Team workload' }), h('p', { class: 'small muted', text: `Estimated work due within ${workload.weeks} weeks vs capacity` }));
  const rows = workload.items.filter((i) => i.open_tasks || i.scheduled_hours);
  if (!rows.length) card.append(h('div', { class: 'empty', text: 'No assigned work in the window.' }));
  for (const i of rows) card.append(h('div', { class: 'item' }, avatar(i.user), h('span', { class: 'grow', text: i.user.display_name }), utilCell(i)));
  return card;
}

/* ============================== my tasks & team ============================== */
async function myTasksView(view, alive) {
  const data = await api('/tasks', { query: { assignee: 'me', open: 'true', sort: 'due_date,-priority', limit: 200 } });
  if (!alive()) return;
  clear(view);
  const today = state.today, week = addDays(today, 7);
  const groups = [['Overdue', (t) => t.is_overdue], ['Due this week', (t) => !t.is_overdue && t.due_date && t.due_date <= week],
    ['Later', (t) => t.due_date && t.due_date > week], ['No due date', (t) => !t.due_date]];
  view.append(h('div', { class: 'page-head' }, h('div', { class: 'titles' }, h('h1', { text: 'My tasks' }), h('p', { class: 'muted', text: `${plural(data.total, 'open task')} assigned to you` }))));
  if (!data.items.length) view.append(h('div', { class: 'card empty', text: 'You have no open tasks. Enjoy the quiet.' }));
  for (const [title, test] of groups) {
    const rows = data.items.filter(test);
    if (!rows.length) continue;
    view.append(h('div', { class: 'card flush', style: { marginBottom: '16px' } }, h('div', { style: { padding: '12px 16px 0' } }, h('h2', { text: `${title} (${rows.length})` })), taskTable(rows, { showProject: true })));
  }
}
async function teamView(view, alive) {
  const [workload, ts] = await Promise.all([api('/reports/workload', { query: { weeks: 4 } }), api('/reports/timesheet', { query: { from: addDays(state.today, -13), to: state.today } })]);
  if (!alive()) return;
  clear(view);
  view.append(h('div', { class: 'page-head' }, h('div', { class: 'titles' }, h('h1', { text: 'Team workload' }), h('p', { class: 'muted', text: 'Next four weeks, based on estimates and due dates of open tasks.' }))),
    h('div', { class: 'card flush' }, h('table', { class: 'table' },
      h('thead', {}, h('tr', {}, ['Person', 'Open', 'Overdue', 'Scheduled vs capacity', 'Unscheduled', 'Unestimated'].map((x) => h('th', { text: x })))),
      h('tbody', {}, workload.items.map((i) => h('tr', {}, h('td', {}, h('div', { class: 'row' }, avatar(i.user), i.user.display_name, i.overallocated ? badge('overdue', 'Overallocated') : null)),
        h('td', { text: i.open_tasks }), h('td', { class: i.overdue ? 'overdue-text' : '', text: i.overdue }), h('td', {}, utilCell(i)),
        h('td', { text: fmtHours(i.unscheduled_hours) }), h('td', { text: i.unestimated_tasks })))))),
    h('div', { class: 'card', style: { marginTop: '16px' } }, h('h2', { text: 'Time logged, last 14 days' }),
      h('p', { class: 'muted', text: hasRole('manager') ? `${fmtHours(ts.total_hours)} across the team` : `${fmtHours(ts.total_hours)} by you` }),
      ts.by_user.length ? ts.by_user.map((u) => h('div', { class: 'item' }, h('span', { class: 'grow', text: u.display_name }), h('strong', { text: fmtHours(u.hours) }))) : h('div', { class: 'empty', text: 'No time logged in this period.' })));
}

/* ============================== task table (shared) ============================== */
function taskTable(tasks, opts = {}) {
  const heads = [opts.showProject ? 'Task' : 'Key', 'Title', 'Status', 'Priority', 'Assignee', 'Due', 'Est / logged'];
  const th = (label) => {
    const sortable = opts.sort && opts.sortable && opts.sortable[label];
    if (!sortable) return h('th', { text: label });
    const active = opts.sort.replace('-', '') === sortable;
    return h('th', { class: 'sortable', tabindex: 0, 'aria-sort': active ? (opts.sort.startsWith('-') ? 'descending' : 'ascending') : 'none',
      onclick: () => opts.onSort(sortable), onkeydown: (e) => { if (e.key === 'Enter') opts.onSort(sortable); } }, label, active ? (opts.sort.startsWith('-') ? ' \u25BE' : ' \u25B4') : '');
  };
  if (!tasks.length) return h('div', { class: 'empty', text: 'No tasks match.' });
  return h('table', { class: 'table' }, h('thead', {}, h('tr', {}, heads.map(th))), h('tbody', {}, tasks.map((t) => h('tr', { class: 'click', tabindex: 0,
    onclick: () => openTask(t.key), onkeydown: (e) => { if (e.key === 'Enter') openTask(t.key); } },
    h('td', { class: 'nowrap' }, h('strong', { text: t.key })),
    h('td', {}, t.title, t.parent ? h('span', { class: 'chip', style: { marginLeft: '6px' }, text: 'sub of ' + t.parent }) : null,
      t.blocked_by_open ? h('span', { class: 'chip', style: { marginLeft: '6px' }, title: 'Waiting on open tasks', text: '\u{1F512} ' + t.blocked_by_open }) : null,
      t.labels.map((l) => h('span', { class: 'chip', style: { marginLeft: '6px' }, text: l }))),
    h('td', {}, statusBadge(t.status)), h('td', {}, priorityBadge(t.priority)),
    h('td', {}, h('div', { class: 'row' }, avatar(t.assignee), t.assignee ? h('span', { class: 'small', text: t.assignee.display_name }) : null)),
    h('td', { class: 'nowrap' }, dueText(t) || '\u2014'),
    h('td', { class: 'nowrap small', text: `${fmtHours(t.estimate_hours)} / ${fmtHours(t.logged_hours || null)}` })))));
}

/* ============================== project view ============================== */
const TABS = [['board', 'Board'], ['list', 'List'], ['timeline', 'Timeline'], ['milestones', 'Milestones'], ['reports', 'Reports'], ['activity', 'Activity'], ['settings', 'Settings']];

async function projectView(view, key, tab, alive) {
  const [project, summary] = await Promise.all([api('/projects/' + key), api(`/projects/${key}/summary`)]);
  state.projectCache[key] = project;
  if (!alive()) return;
  if (!TABS.some(([id]) => id === tab)) tab = 'board';
  clear(view);
  const headerBox = h('div');
  const renderHeader = (s) => {
    clear(headerBox);
    const m = s.metrics;
    headerBox.append(h('div', { class: 'page-head' },
      h('div', { class: 'titles' }, h('div', { class: 'row wrap' }, h('h1', { text: project.name }), badge(project.status, PROJECT_STATUS_LABEL[project.status]), healthBadge(s.health)),
        h('p', { class: 'muted' }, `${project.key} \u00b7 owner ${project.owner ? project.owner.display_name : '\u2014'} \u00b7 ${fmtDate(project.start_date)} \u2192 ${fmtDate(project.due_date)}`),
        project.description ? h('p', { class: 'small', text: project.description }) : null,
        s.health.reasons.length ? h('ul', { class: 'reasons' }, s.health.reasons.map((r) => h('li', { text: r }))) : null),
      h('div', { style: { minWidth: '220px' } }, h('div', { class: 'row' }, progressBar(s.progress.tasks_pct, 'ok'), h('strong', { text: `${Math.round(s.progress.tasks_pct)}%` })),
        h('div', { class: 'small muted', text: `${m.done}/${m.leaf_tasks} done \u00b7 ${m.overdue} overdue \u00b7 ${m.blocked} waiting/blocked` })),
      canWrite(project) ? h('button', { class: 'btn primary', onclick: () => openCreateTask(project) }, '+ New task') : null));
  };
  renderHeader(summary);
  const refreshHeader = async () => renderHeader(await api(`/projects/${key}/summary`));
  const body = h('div');
  view.append(headerBox, h('nav', { class: 'tabs', 'aria-label': 'Project sections' }, TABS.map(([id, label]) => h('a', { href: `#/p/${key}/${id}`, class: id === tab ? 'active' : '', 'aria-current': id === tab ? 'page' : null }, label))), body);
  const tabs = { board: boardTab, list: listTab, timeline: timelineTab, milestones: milestonesTab, reports: reportsTab, activity: activityTab, settings: settingsTab };
  await tabs[tab](body, { project, summary, key, refreshHeader, alive });
  if (alive() && state.pendingTask) { const k = state.pendingTask; state.pendingTask = null; openTask(k); }
}

/* ---------- board ---------- */
async function boardTab(root, ctx) {
  const { project, key } = ctx;
  const data = await api(`/projects/${key}/board`);
  if (!ctx.alive()) return;
  const filter = { assignee: '', q: '', showCancelled: false };
  const editable = canWrite(project);
  const board = h('div', { class: 'board' });
  const match = (t) => (!filter.assignee || (filter.assignee === 'me' ? t.assignee && t.assignee.username === state.me.username : filter.assignee === 'none' ? !t.assignee : t.assignee && t.assignee.username === filter.assignee))
    && (!filter.q || (t.key + ' ' + t.title + ' ' + t.labels.join(' ')).toLowerCase().includes(filter.q.toLowerCase()));
  let dragging = null;

  function card(t) {
    const el = h('article', { class: 'task-card ' + (t.priority === 'critical' ? 'crit' : t.priority === 'high' ? 'high' : ''), tabindex: 0, role: 'button', draggable: editable ? 'true' : null,
      'aria-label': `${t.key} ${t.title}`, onclick: () => openTask(t.key), onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openTask(t.key); } } },
      h('div', { class: 'row' }, h('span', { class: 'key', text: t.key }), h('span', { class: 'chip', text: TYPE_LABEL[t.type] }), h('span', { class: 'spacer' }), t.priority !== 'medium' ? priorityBadge(t.priority) : null),
      h('div', { class: 'title', text: t.title }),
      h('div', { class: 'meta' }, avatar(t.assignee), dueText(t),
        t.subtasks.total ? h('span', { title: 'Subtasks done', text: `\u2611 ${t.subtasks.done}/${t.subtasks.total}` }) : null,
        t.blocked_by_open ? h('span', { title: 'Waiting on open tasks', text: `\u{1F512} ${t.blocked_by_open}` }) : null,
        t.labels.slice(0, 2).map((l) => h('span', { class: 'chip', text: l }))));
    el.addEventListener('dragstart', (e) => {
      dragging = t; el.classList.add('dragging'); e.dataTransfer.setData('text/plain', t.key); e.dataTransfer.effectAllowed = 'move';
      const allowed = state.meta.transitions[t.status] || [];
      for (const c of board.children) c.classList.toggle('allowed', allowed.includes(c.dataset.status));
    });
    el.addEventListener('dragend', () => { dragging = null; el.classList.remove('dragging'); for (const c of board.children) c.classList.remove('allowed', 'over'); });
    return el;
  }

  async function moveTo(task, status) {
    if (task.status === status) return;
    try {
      const updated = await api('/tasks/' + task.key, { method: 'PATCH', body: { status } });
      for (const col of data.columns) col.tasks = col.tasks.filter((x) => x.key !== task.key);
      data.columns.find((c) => c.status === updated.status).tasks.push(updated);
      toast(`${task.key} moved to ${STATUS_LABEL[status]}`, 'ok');
      ctx.refreshHeader();
    } catch (e) { toast(e.message, 'error'); }
    render();
  }

  function column(col) {
    const tasks = col.tasks.filter(match);
    const list = h('div', { class: 'cards' }, tasks.map(card));
    const el = h('section', { class: 'column', dataset: { status: col.status }, 'aria-label': STATUS_LABEL[col.status] },
      h('header', {}, h('span', { class: 'dot ' + col.status }), STATUS_LABEL[col.status], h('span', { class: 'count', text: tasks.length }), h('span', { class: 'spacer' }),
        editable && ['todo', 'in_progress'].includes(col.status) ? h('button', { class: 'btn ghost small', 'aria-label': `Add task to ${STATUS_LABEL[col.status]}`, onclick: () => quickAdd(col, el, list) }, '+') : null),
      list, !tasks.length ? h('div', { class: 'small muted', text: 'No tasks' }) : null);
    el.addEventListener('dragover', (e) => { if (dragging) { e.preventDefault(); el.classList.add('over'); } });
    el.addEventListener('dragleave', (e) => { if (!el.contains(e.relatedTarget)) el.classList.remove('over'); });
    el.addEventListener('drop', (e) => { e.preventDefault(); el.classList.remove('over'); if (dragging) moveTo(dragging, col.status); });
    return el;
  }
  function quickAdd(col, colEl, list) {
    if ($('.quick-add', colEl)) return;
    const input = h('input', { type: 'text', placeholder: 'Task title, then Enter', 'aria-label': 'New task title', maxlength: 200 });
    const form = h('form', { class: 'quick-add', onsubmit: async (e) => {
      e.preventDefault();
      if (!input.value.trim()) return;
      try { await api(`/projects/${key}/tasks`, { method: 'POST', body: { title: input.value.trim(), status: col.status } }); route(); } catch (err) { toast(err.message, 'error'); }
    } }, input);
    input.addEventListener('keydown', (e) => { if (e.key === 'Escape') form.remove(); });
    list.before(form);
    input.focus();
  }
  function render() {
    clear(board);
    for (const col of data.columns) if (col.status !== 'cancelled' || col.tasks.length || filter.showCancelled) board.append(column(col));
  }

  const members = [...project.members];
  const toolbar = h('div', { class: 'toolbar' },
    h('select', { 'aria-label': 'Filter by assignee', value: filter.assignee, onchange: (e) => { filter.assignee = e.target.value; render(); } },
      optionList([['', 'Everyone'], ['me', 'Assigned to me'], ['none', 'Unassigned'], ...userPairs(members)])),
    h('input', { type: 'search', placeholder: 'Filter cards\u2026', 'aria-label': 'Filter cards', oninput: (e) => { filter.q = e.target.value; render(); } }),
    h('label', { class: 'row small' }, h('input', { type: 'checkbox', onchange: (e) => { filter.showCancelled = e.target.checked; render(); } }), 'Show cancelled'),
    h('span', { class: 'spacer' }),
    data.hidden_closed ? h('span', { class: 'small muted', text: `${plural(data.hidden_closed, 'older closed task')} hidden \u2014 see the List tab` }) : null,
    data.truncated ? h('span', { class: 'small overdue-text', text: 'Board truncated at 500 tasks \u2014 use the List tab' }) : null);
  root.append(toolbar, board);
  render();
}

/* ---------- list ---------- */
async function listTab(root, ctx) {
  const { project, key } = ctx;
  const f = { status: '', assignee: '', priority: '', q: '', open: false, sort: 'key', offset: 0 };
  const LIMIT = 25;
  const results = h('div', { class: 'card flush' });
  const pager = h('div', { class: 'row small muted', style: { marginTop: '10px' } });
  async function load() {
    const data = await api('/tasks', { query: { project: key, status: f.status, assignee: f.assignee, priority: f.priority, q: f.q, open: f.open ? 'true' : '', sort: f.sort, limit: LIMIT, offset: f.offset } });
    if (!ctx.alive()) return;
    clear(results);
    results.append(taskTable(data.items, { sort: f.sort, onSort: (field) => { f.sort = f.sort === field ? '-' + field : field; f.offset = 0; load(); },
      sortable: { Key: 'key', Title: 'title', Status: 'status', Priority: 'priority', Assignee: 'assignee', Due: 'due_date' } }));
    clear(pager);
    const end = Math.min(f.offset + LIMIT, data.total);
    pager.append(h('span', { text: data.total ? `${f.offset + 1}\u2013${end} of ${data.total}` : '0 tasks' }), h('span', { class: 'spacer' }),
      h('button', { class: 'btn small', disabled: f.offset === 0, onclick: () => { f.offset = Math.max(0, f.offset - LIMIT); load(); } }, '\u2039 Prev'),
      h('button', { class: 'btn small', disabled: end >= data.total, onclick: () => { f.offset += LIMIT; load(); } }, 'Next \u203A'));
  }
  const set = (name, transform) => (e) => { f[name] = transform ? transform(e) : e.target.value; f.offset = 0; load(); };
  let timer;
  root.append(h('div', { class: 'toolbar' },
    h('input', { type: 'search', placeholder: 'Search title, description or key\u2026', 'aria-label': 'Search', oninput: (e) => { clearTimeout(timer); const v = e.target.value; timer = setTimeout(() => { f.q = v; f.offset = 0; load(); }, 250); } }),
    h('select', { 'aria-label': 'Status', onchange: set('status') }, optionList([['', 'Any status'], ...state.meta.statuses.map((s) => [s, STATUS_LABEL[s]])])),
    h('select', { 'aria-label': 'Priority', onchange: set('priority') }, optionList([['', 'Any priority'], ...state.meta.priorities.map((p) => [p, PRIORITY_LABEL[p]])])),
    h('select', { 'aria-label': 'Assignee', onchange: set('assignee') }, optionList([['', 'Anyone'], ['me', 'Me'], ['none', 'Unassigned'], ...userPairs(project.members)])),
    h('label', { class: 'row small' }, h('input', { type: 'checkbox', onchange: set('open', (e) => e.target.checked) }), 'Open only'),
    h('span', { class: 'spacer' }),
    h('button', { class: 'btn', onclick: () => downloadCsv(key) }, 'Export CSV')), results, pager);
  await load();
}
async function downloadCsv(key) {
  try {
    const blob = await api(`/projects/${key}/export.csv`, { blob: true });
    const url = URL.createObjectURL(blob);
    const a = h('a', { href: url, download: `${key}-tasks.csv` });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  } catch (e) { toast(e.message, 'error'); }
}

/* ---------- timeline ---------- */
async function timelineTab(root, ctx) {
  const { project, key } = ctx;
  const [tasksResp, ms, cp] = await Promise.all([api('/tasks', { query: { project: key, limit: 200, sort: 'due_date,key', leaf: 'true' } }),
    api(`/projects/${key}/milestones`), api(`/projects/${key}/critical-path`)]);
  if (!ctx.alive()) return;
  const critical = new Set(cp.tasks.filter((t) => t.critical).map((t) => t.key));
  const scheduled = tasksResp.items.filter((t) => (t.start_date || t.due_date) && t.status !== 'cancelled');
  const unscheduled = tasksResp.items.filter((t) => !t.start_date && !t.due_date && t.status !== 'cancelled');
  const dates = [state.today, project.start_date, project.due_date, ...ms.items.map((m) => m.due_date), ...scheduled.flatMap((t) => [t.start_date, t.due_date])].filter(Boolean).sort();
  if (!scheduled.length && !ms.items.length) { root.append(h('div', { class: 'card empty', text: 'Give tasks start and due dates to see them on the timeline.' })); return; }
  const first = addDays(dates[0], -3), last = addDays(dates[dates.length - 1], 3);
  const days = dayDiff(first, last) + 1;
  const px = Math.max(9, Math.min(26, Math.floor(980 / days)));
  const labelW = 270, rowH = 30, headH = 52, msH = 26;
  const x = (d) => labelW + dayDiff(first, d) * px;
  const width = labelW + days * px + 130, height = headH + msH + scheduled.length * rowH + 16;
  const svg = sv('svg', { class: 'chart', width, height, viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': 'Project timeline' });
  // month ticks & week grid
  for (let i = 0; i < days; i++) {
    const d = addDays(first, i), date = parseDate(d);
    if (date.getDate() === 1 || i === 0) svg.append(sv('text', { x: x(d) + 3, y: 18 }, `${MONTHS[date.getMonth()]} ${date.getFullYear()}`), sv('line', { x1: x(d), x2: x(d), y1: 4, y2: height, class: 'grid-line' }));
    if (date.getDay() === 1) { svg.append(sv('line', { x1: x(d), x2: x(d), y1: headH - 14, y2: height, class: 'grid-line', opacity: 0.5 }), sv('text', { x: x(d) + 2, y: headH - 3 }, String(date.getDate()))); }
  }
  svg.append(sv('line', { x1: x(state.today), x2: x(state.today), y1: 2, y2: height, class: 'chart today' }), sv('text', { x: x(state.today) + 4, y: 32, fill: 'currentColor' }, 'Today'));
  ms.items.forEach((m) => {
    const cx = x(m.due_date);
    svg.append(sv('path', { d: `M${cx} ${headH + 4} l8 8 l-8 8 l-8 -8 z`, class: 'tl-ms' }, sv('title', {}, `${m.name} \u2014 ${fmtDate(m.due_date)} (${m.status}, ${Math.round(m.progress)}%)`)),
      sv('text', { x: cx + 12, y: headH + 17 }, m.name));
  });
  scheduled.forEach((t, i) => {
    const y = headH + msH + i * rowH;
    const s = t.start_date || t.due_date, e = t.due_date || t.start_date;
    const bx = x(s), bw = Math.max(px * (dayDiff(s, e) + 1), 8);
    const label = `${t.key}  ${t.title}`;
    const row = sv('g', { class: 'tl-row', tabindex: 0, role: 'button', 'aria-label': `${t.key} ${t.title}, ${STATUS_LABEL[t.status]}`, onclick: () => openTask(t.key), onkeydown: (ev) => { if (ev.key === 'Enter') openTask(t.key); } },
      sv('rect', { class: 'hit', x: 0, y, width, height: rowH }),
      sv('text', { x: 6, y: y + 19, class: 'tl-label' }, label.length > 40 ? label.slice(0, 39) + '\u2026' : label),
      sv('rect', { class: `tl-bar ${t.status} ${critical.has(t.key) ? 'critical' : ''}`, x: bx, y: y + 6, width: bw, height: rowH - 12, rx: 5 },
        sv('title', {}, `${t.key} ${t.title}\n${STATUS_LABEL[t.status]} \u00b7 ${t.assignee ? t.assignee.display_name : 'unassigned'}\n${fmtDate(t.start_date)} \u2192 ${fmtDate(t.due_date)}${critical.has(t.key) ? '\nOn the critical path' : ''}`)));
    svg.append(row);
  });
  root.append(h('div', { class: 'card' }, h('div', { class: 'row wrap', style: { marginBottom: '10px' } }, h('h2', { text: 'Timeline' }), h('span', { class: 'spacer' }),
    h('span', { class: 'legend' }, h('span', {}, h('span', { class: 'sw s-in_progress' }), 'In progress'), h('span', {}, h('span', { class: 'sw s-done' }), 'Done'), h('span', {}, h('span', { class: 'sw s-todo' }), 'To do'),
      h('span', {}, h('span', { class: 'sw s-blocked' }), 'Blocked'), h('span', {}, '\u25C6 Milestone'), h('span', {}, 'Red outline = critical path'))),
    h('div', { class: 'timeline-wrap' }, svg),
    unscheduled.length ? h('p', { class: 'small muted', style: { marginTop: '10px' }, text: `${plural(unscheduled.length, 'task')} without dates are not shown: ${unscheduled.slice(0, 8).map((t) => t.key).join(', ')}${unscheduled.length > 8 ? '\u2026' : ''}` }) : null));
}

/* ---------- milestones ---------- */
async function milestonesTab(root, ctx) {
  const { project, key } = ctx;
  const data = await api(`/projects/${key}/milestones`);
  if (!ctx.alive()) return;
  const manage = hasRole('manager') && project.status !== 'archived';
  const card = h('div', { class: 'card' }, h('div', { class: 'row' }, h('h2', { text: 'Milestones' }), h('span', { class: 'spacer' }),
    manage ? h('button', { class: 'btn primary small', onclick: () => openMilestoneForm(key) }, '+ Add milestone') : null));
  if (!data.items.length) card.append(h('div', { class: 'empty', text: 'No milestones yet. Milestones group tasks around dated goals.' }));
  for (const m of data.items) card.append(h('div', { class: 'item' }, h('div', { class: 'grow' }, h('strong', { text: m.name }), m.description ? h('div', { class: 'small muted', text: m.description }) : null),
    h('div', { style: { width: '170px' } }, h('div', { class: 'row' }, progressBar(m.progress, m.status === 'overdue' ? 'bad' : 'ok'), h('span', { class: 'small', text: `${m.tasks_done}/${m.tasks_total}` }))),
    badge(m.status, m.status === 'achieved' ? 'Achieved' : m.status === 'overdue' ? 'Overdue' : 'Open'), h('span', { class: 'small nowrap muted', text: fmtDate(m.due_date) }),
    manage ? h('button', { class: 'btn small', onclick: () => openMilestoneForm(key, m) }, 'Edit') : null));
  root.append(card);
}
function openMilestoneForm(key, existing) {
  const name = h('input', { type: 'text', value: existing ? existing.name : '', maxlength: 100, required: true });
  const due = h('input', { type: 'date', value: existing ? existing.due_date : '', required: true });
  const desc = h('textarea', { maxlength: 2000, value: existing ? existing.description : '' });
  const save = h('button', { class: 'btn primary', onclick: () => guarded(save, async () => {
    const body = { name: name.value, due_date: due.value, description: desc.value };
    if (existing) await api('/milestones/' + existing.id, { method: 'PATCH', body }); else await api(`/projects/${key}/milestones`, { method: 'POST', body });
    m.close(); route();
  }) }, 'Save');
  const remove = existing ? h('button', { class: 'btn danger', onclick: async () => { if (await confirmBox(`Delete milestone "${existing.name}"? Tasks stay, but lose the link.`, 'Delete', true)) { await guarded(remove, () => api('/milestones/' + existing.id, { method: 'DELETE' })); m.close(); route(); } } }, 'Delete') : null;
  const m = openModal({ title: existing ? 'Edit milestone' : 'New milestone', narrow: true, content: h('div', { class: 'stack' }, field('Name', name), field('Due date', due), field('Description', desc)), footer: [remove, h('span', { class: 'spacer' }), h('button', { class: 'btn', onclick: () => m.close() }, 'Cancel'), save] });
}

/* ---------- reports ---------- */
async function reportsTab(root, ctx) {
  const { key, summary } = ctx;
  const [work, cp] = await Promise.all([api('/reports/workload', { query: { project: key, weeks: 2 } }), api(`/projects/${key}/critical-path`)]);
  if (!ctx.alive()) return;
  const m = summary.metrics, c = summary.counts;
  const total = c.total || 1;
  const burnBox = h('div');
  const metricSel = h('select', { class: 'inline', 'aria-label': 'Burndown metric', onchange: () => loadBurndown() }, optionList([['tasks', 'Tasks'], ['hours', 'Estimated hours']]));
  async function loadBurndown() {
    const data = await api(`/projects/${key}/burndown`, { query: { metric: metricSel.value } });
    if (!ctx.alive()) return;
    clear(burnBox);
    put(burnBox, burndownChart(data), data.unestimated_tasks && metricSel.value === 'hours' ? h('p', { class: 'small muted', text: `${plural(data.unestimated_tasks, 'task')} without an estimate count as 0 hours.` }) : null);
  }
  root.append(
    h('div', { class: 'grid kpis' }, kpi('Complete', `${Math.round(summary.progress.tasks_pct)}%`), kpi('Open tasks', m.open), kpi('Overdue', m.overdue, m.overdue ? 'bad' : ''), kpi('Due within 7 days', m.due_soon),
      kpi('Waiting / blocked', m.blocked, m.flagged_blocked ? 'warn' : ''), kpi('Unassigned', m.unassigned, m.unassigned ? 'warn' : ''),
      kpi('Hours logged', fmtHours(summary.hours.logged)), kpi('Est. remaining', fmtHours(summary.hours.remaining_estimate))),
    h('div', { class: 'card', style: { marginTop: '16px' } }, h('h2', { text: 'Tasks by status' }),
      h('div', { class: 'stacked', role: 'img', 'aria-label': 'Tasks by status' }, state.meta.statuses.map((s) => h('span', { class: 's-' + s, title: `${STATUS_LABEL[s]}: ${c.by_status[s]}`, style: { width: (100 * c.by_status[s] / total) + '%' } }))),
      h('div', { class: 'legend', style: { marginTop: '8px' } }, state.meta.statuses.map((s) => h('span', {}, h('span', { class: 'sw s-' + s }), `${STATUS_LABEL[s]} ${c.by_status[s]}`)))),
    h('div', { class: 'card', style: { marginTop: '16px' } }, h('div', { class: 'row' }, h('h2', { text: 'Burndown' }), h('span', { class: 'spacer' }), metricSel), burnBox),
    h('div', { class: 'grid two', style: { marginTop: '16px' } }, criticalPathCard(cp),
      h('div', { class: 'card' }, h('h2', { text: 'Workload for this project' }), h('p', { class: 'small muted', text: `Open work due in the next ${work.weeks} weeks vs capacity (across all projects)` }),
        work.items.filter((i) => i.open_tasks || i.scheduled_hours).map((i) => h('div', { class: 'item' }, avatar(i.user), h('span', { class: 'grow', text: i.user.display_name }), utilCell(i))))));
  await loadBurndown();
}
function burndownChart(data) {
  const W = 760, H = 270, L = 44, R = 16, T = 14, B = 34;
  const pts = data.days;
  const max = Math.max(1, ...pts.flatMap((d) => [d.remaining || 0, d.scope || 0, d.ideal || 0]));
  const x = (i) => L + (pts.length < 2 ? 0 : (i * (W - L - R)) / (pts.length - 1));
  const y = (v) => T + (H - T - B) * (1 - v / max);
  const svg = sv('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}`, role: 'img', 'aria-label': `Burndown chart (${data.metric}) from ${data.from} to ${data.to}` });
  const ticks = 4;
  for (let i = 0; i <= ticks; i++) { const v = (max * i) / ticks; svg.append(sv('line', { x1: L, x2: W - R, y1: y(v), y2: y(v), class: 'grid-line' }), sv('text', { x: L - 6, y: y(v) + 4, 'text-anchor': 'end' }, String(Math.round(v * 10) / 10))); }
  const every = Math.max(1, Math.ceil(pts.length / 8));
  pts.forEach((d, i) => { const last = pts.length - 1; if ((i % every === 0 && last - i >= every * 0.6) || i === last) svg.append(sv('text', { x: x(i), y: H - 12, 'text-anchor': 'middle' }, fmtDate(d.date))); });
  const path = (key, step) => {
    let d = '', started = false, prev = null;
    pts.forEach((p, i) => { if (p[key] === null || p[key] === undefined) return; d += `${started ? (step ? `L${x(i)} ${y(prev)} ` : 'L') : 'M'}${x(i)} ${y(p[key])} `; started = true; prev = p[key]; });
    return d;
  };
  svg.append(sv('path', { d: path('ideal'), class: 'chart line ideal' }), sv('path', { d: path('scope', true), class: 'chart line scope' }), sv('path', { d: path('remaining'), class: 'chart line remaining' }));
  const todayIdx = pts.findIndex((p) => p.date === state.today);
  if (todayIdx >= 0) svg.append(sv('line', { x1: x(todayIdx), x2: x(todayIdx), y1: T, y2: H - B, class: 'chart today' }));
  pts.forEach((p, i) => { if (p.remaining !== null) svg.append(sv('circle', { cx: x(i), cy: y(p.remaining), r: 3.2, class: 'pt' }, sv('title', {}, `${fmtDate(p.date)}: ${p.remaining} remaining of ${p.scope} (${p.completed} completed)`))); });
  return h('div', {}, svg, h('div', { class: 'legend' }, h('span', {}, h('span', { class: 'sw s-in_progress' }), 'Remaining'), h('span', {}, h('span', { class: 'sw', style: { background: 'var(--muted)' } }), 'Ideal'),
    h('span', {}, h('span', { class: 'sw', style: { background: 'var(--warn)' } }), 'Scope (rises when tasks are added)'), h('span', { text: 'Dashed red = today' })));
}
function criticalPathCard(cp) {
  const card = h('div', { class: 'card' }, h('h2', { text: 'Critical path' }));
  if (!cp.path.length) { card.append(h('div', { class: 'empty', text: 'No open dependencies yet. Link tasks with "blocked by" to see the critical path.' })); return card; }
  card.append(h('p', { class: 'muted small', text: `Longest chain of dependent open tasks: ${fmtHours(cp.total_hours)} of effort (${cp.assumptions.unit}).` }),
    h('div', { class: 'row wrap', style: { marginBottom: '10px' } }, cp.path.flatMap((k, i) => [i ? h('span', { class: 'muted', text: '\u2192' }) : null, h('a', { href: '#/t/' + k, class: 'badge overdue', text: k })])));
  card.append(h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ['Task', 'Effort', 'Slack'].map((x) => h('th', { text: x })))),
    h('tbody', {}, cp.tasks.map((t) => h('tr', {}, h('td', {}, h('a', { href: '#/t/' + t.key, text: t.key }), ' ', t.title, t.container ? h('span', { class: 'chip', style: { marginLeft: '6px' }, text: 'parent' }) : null),
      h('td', { class: 'nowrap', text: fmtHours(t.duration_hours) }), h('td', { class: 'nowrap' }, t.critical ? badge('overdue', 'critical') : fmtHours(t.slack_hours)))))),
    h('p', { class: 'small muted', text: cp.assumptions.notes }));
  return card;
}

/* ---------- activity ---------- */
function describeActivity(a) {
  const d = a.details || {};
  const t = a.task || '';
  const change = (c) => Object.entries(c || {}).map(([f, [o, n]]) => `${f.replace('_', ' ')}: ${Array.isArray(o) ? o.join(', ') || 'none' : o ?? 'none'} \u2192 ${Array.isArray(n) ? n.join(', ') || 'none' : n ?? 'none'}`).join('; ');
  switch (a.action) {
    case 'task.created': return `created ${t} \u201c${d.title}\u201d`;
    case 'task.updated': return `updated ${t} (${change(d.changes)})`;
    case 'task.status_changed': return `moved ${t}: ${change(d.changes)}`;
    case 'task.deleted': return `deleted ${t} \u201c${d.title}\u201d`;
    case 'task.dependency_added': return `made ${d.blocker} block ${d.blocked}`;
    case 'task.dependency_removed': return `removed the dependency between ${t} and ${d.other}`;
    case 'comment.added': return `commented on ${t}${d.mentions && d.mentions.length ? ' (mentioned ' + d.mentions.join(', ') + ')' : ''}`;
    case 'comment.edited': return `edited a comment on ${t}`;
    case 'comment.deleted': return `deleted a comment on ${t}`;
    case 'time.logged': return `logged ${d.hours}h on ${t} for ${d.work_date}`;
    case 'time.deleted': return `removed a ${d.hours}h time entry from ${t}`;
    case 'milestone.created': return `added milestone \u201c${d.name}\u201d due ${d.due_date}`;
    case 'milestone.updated': return `updated a milestone (${change(d.changes)})`;
    case 'milestone.deleted': return `deleted milestone \u201c${d.name}\u201d`;
    case 'project.created': return `created the project`;
    case 'project.updated': return `updated the project (${change(d.changes)})`;
    case 'project.member_added': return `added ${d.user} to the project`;
    case 'project.member_removed': return `removed ${d.user} from the project`;
    case 'import.completed': return `imported tasks from CSV: ${d.created} created, ${d.skipped_duplicates} skipped, ${d.errors} errors`;
    default: return a.action;
  }
}
function activityItem(a) {
  return h('div', { class: 'item' }, avatar(a.actor), h('div', { class: 'grow' }, h('strong', { text: a.actor ? a.actor.display_name : 'System' }), ' ', describeActivity(a)), h('span', { class: 'small muted nowrap', text: fmtAgo(a.created_at) }));
}
async function activityTab(root, ctx) {
  const first = await api(`/projects/${ctx.key}/activity`, { query: { limit: 40 } });
  if (!ctx.alive()) return;
  const list = h('div'), more = h('div', { style: { marginTop: '10px' } });
  const show = (page) => {
    page.items.forEach((a) => list.append(activityItem(a)));
    clear(more);
    if (page.next_before) more.append(h('button', { class: 'btn', onclick: async (e) => guarded(e.target, async () => show(await api(`/projects/${ctx.key}/activity`, { query: { limit: 40, before: page.next_before } }))) }, 'Load older activity'));
  };
  root.append(h('div', { class: 'card' }, h('h2', { text: 'Activity' }), first.items.length ? list : h('div', { class: 'empty', text: 'Nothing has happened yet.' }), more));
  show(first);
}

/* ---------- settings ---------- */
async function settingsTab(root, ctx) {
  const { project, key } = ctx;
  const manage = hasRole('manager');
  const inputs = {
    name: h('input', { type: 'text', value: project.name, maxlength: 100, disabled: !manage }),
    status: h('select', { value: project.status, disabled: !manage }, optionList(state.meta.project_statuses.map((s) => [s, PROJECT_STATUS_LABEL[s]]))),
    start_date: h('input', { type: 'date', value: project.start_date || '', disabled: !manage }),
    due_date: h('input', { type: 'date', value: project.due_date || '', disabled: !manage }),
    owner: h('select', { value: project.owner ? project.owner.username : '', disabled: !manage }, optionList(userPairs(project.members, '\u2014'))),
    description: h('textarea', { maxlength: 5000, value: project.description, disabled: !manage }),
  };
  const save = h('button', { class: 'btn primary', onclick: () => guarded(save, async () => {
    await api('/projects/' + key, { method: 'PATCH', body: { name: inputs.name.value, status: inputs.status.value, start_date: inputs.start_date.value || null, due_date: inputs.due_date.value || null, owner: inputs.owner.value || null, description: inputs.description.value } });
    toast('Project saved', 'ok'); await refreshProjects(); route();
  }) }, 'Save changes');
  const memberList = h('div');
  const renderMembers = () => {
    clear(memberList);
    for (const mem of project.members) memberList.append(h('div', { class: 'item' }, avatar(mem), h('span', { class: 'grow', text: `${mem.display_name} (${mem.username})` }), badge(mem.role, mem.role),
      project.owner && project.owner.username === mem.username ? badge('active', 'Owner') : null,
      manage ? h('button', { class: 'btn small danger', onclick: (e) => guarded(e.target, async () => { Object.assign(project, await api(`/projects/${key}/members/${mem.username}`, { method: 'DELETE' })); state.projectCache[key] = project; renderMembers(); }) }, 'Remove') : null));
  };
  const addSel = h('select', { 'aria-label': 'Add a member' }, optionList(userPairs(state.users.filter((u) => !project.members.some((m) => m.username === u.username)), 'Add a member\u2026')));
  renderMembers();
  put(root, h('div', { class: 'grid two' },
    h('div', { class: 'card stack' }, h('h2', { text: 'Project details' }), h('div', { class: 'form-grid' }, field('Name', inputs.name), field('Status', inputs.status), field('Start date', inputs.start_date), field('Due date', inputs.due_date), field('Owner', inputs.owner)),
      field('Description', inputs.description), manage ? h('div', {}, save) : h('p', { class: 'small muted', text: 'Only managers and admins can edit project details.' })),
    h('div', { class: 'card' }, h('h2', { text: 'Members' }), h('p', { class: 'small muted', text: 'Members can be assigned work and can edit tasks in this project.' }), memberList,
      manage ? h('div', { class: 'row', style: { marginTop: '10px' } }, addSel, h('button', { class: 'btn', onclick: (e) => guarded(e.target, async () => { if (!addSel.value) return; Object.assign(project, await api(`/projects/${key}/members`, { method: 'POST', body: { username: addSel.value } })); route(); }) }, 'Add')) : null)),
    manage ? importCard(key) : null,
    hasRole('admin') ? h('div', { class: 'card', style: { marginTop: '16px' } }, h('h2', { text: 'Danger zone' }), h('p', { class: 'small muted', text: 'Deleting a project permanently removes its tasks, comments, time entries and history.' }),
      h('button', { class: 'btn danger', onclick: () => deleteProjectDialog(project) }, 'Delete project\u2026')) : null);
}
function importCard(key) {
  const file = h('input', { type: 'file', accept: '.csv,text/csv', 'aria-label': 'CSV file' });
  const dry = h('input', { type: 'checkbox', checked: true });
  const addMem = h('input', { type: 'checkbox' });
  const out = h('div', { class: 'stack small', style: { marginTop: '10px' } });
  const run = h('button', { class: 'btn primary', onclick: () => guarded(run, async () => {
    if (!file.files[0]) { toast('Choose a CSV file first', 'error'); return; }
    const text = await file.files[0].text();
    const r = await api(`/projects/${key}/import`, { method: 'POST', body: text, contentType: 'text/csv', query: { dry_run: dry.checked ? 'true' : '', add_missing_members: addMem.checked ? 'true' : '' } });
    clear(out);
    out.append(h('div', {}, h('strong', { text: r.dry_run ? 'Dry run \u2014 nothing was saved. ' : 'Import finished. ' }), `${r.rows_read} rows read: ${r.created} ${r.dry_run ? 'would be created' : 'created'}, ${r.skipped_duplicates} duplicates skipped, ${r.errors.length} errors, ${r.warnings.length} warnings.`),
      r.errors.length ? h('div', {}, h('strong', { class: 'error-text', text: 'Errors' }), r.errors.slice(0, 20).map((e) => h('div', { text: `Row ${e.row}: ${e.message}` }))) : null,
      r.warnings.length ? h('div', {}, h('strong', { text: 'Warnings' }), r.warnings.slice(0, 20).map((w) => h('div', { class: 'muted', text: `Row ${w.row}: ${w.message}` }))) : null);
    if (!r.dry_run && r.created) { toast(`${plural(r.created, 'task')} imported`, 'ok'); }
  }) }, 'Run import');
  return h('div', { class: 'card', style: { marginTop: '16px' } }, h('h2', { text: 'Import tasks from CSV' }),
    h('p', { class: 'small muted', text: 'Columns recognised: title/summary, description, type, status, priority, assignee, reporter, start date, due date, estimate (hours), labels, key. Re-importing a file never duplicates tasks that have the same key.' }),
    h('div', { class: 'row wrap' }, file, h('label', { class: 'row small' }, dry, 'Dry run first'), h('label', { class: 'row small' }, addMem, 'Add missing assignees as members'), run), out);
}
function deleteProjectDialog(project) {
  const input = h('input', { type: 'text', placeholder: project.key, 'aria-label': 'Type the project key to confirm' });
  const go = h('button', { class: 'btn danger', disabled: true, onclick: () => guarded(go, async () => { await api(`/projects/${project.key}`, { method: 'DELETE', query: { confirm: project.key } }); m.close(); await refreshProjects(); location.hash = '#/dashboard'; toast('Project deleted', 'ok'); }) }, 'Delete forever');
  input.addEventListener('input', () => { go.disabled = input.value !== project.key; });
  const m = openModal({ title: `Delete ${project.name}?`, narrow: true, content: h('div', { class: 'stack' }, h('p', { text: `This cannot be undone. Type ${project.key} to confirm.` }), input), footer: [h('button', { class: 'btn', onclick: () => m.close() }, 'Cancel'), go] });
}

/* ============================== create dialogs ============================== */
function openCreateTask(project, defaults = {}) {
  const f = {
    title: h('input', { type: 'text', maxlength: 200, required: true, placeholder: 'What needs doing?' }),
    type: h('select', {}, optionList(state.meta.task_types.map((t) => [t, TYPE_LABEL[t]]))),
    priority: h('select', { value: 'medium' }, optionList(state.meta.priorities.map((p) => [p, PRIORITY_LABEL[p]]))),
    assignee: h('select', {}, optionList(userPairs(project.members, 'Unassigned'))),
    due_date: h('input', { type: 'date' }), start_date: h('input', { type: 'date' }), estimate_hours: h('input', { type: 'number', min: 0, step: '0.25', placeholder: 'hours' }),
    labels: h('input', { type: 'text', placeholder: 'comma separated, e.g. frontend, bug' }), description: h('textarea', {}),
  };
  const create = h('button', { class: 'btn primary', onclick: () => guarded(create, async () => {
    const body = { title: f.title.value, type: f.type.value, priority: f.priority.value, description: f.description.value, ...defaults };
    if (f.assignee.value) body.assignee = f.assignee.value;
    for (const k of ['due_date', 'start_date']) if (f[k].value) body[k] = f[k].value;
    if (f.estimate_hours.value !== '') body.estimate_hours = Number(f.estimate_hours.value);
    const labels = f.labels.value.split(/[,\s]+/).filter(Boolean);
    if (labels.length) body.labels = labels;
    const t = await api(`/projects/${project.key}/tasks`, { method: 'POST', body });
    m.close(); toast(`${t.key} created`, 'ok'); route();
  }) }, 'Create task');
  const m = openModal({ title: `New task in ${project.key}`, content: h('div', { class: 'stack' }, field('Title', f.title),
    h('div', { class: 'form-grid' }, field('Type', f.type), field('Priority', f.priority), field('Assignee', f.assignee), field('Start date', f.start_date), field('Due date', f.due_date), field('Estimate (hours)', f.estimate_hours)),
    field('Labels', f.labels), field('Description', f.description)), footer: [h('button', { class: 'btn', onclick: () => m.close() }, 'Cancel'), create] });
}
function openCreateProject() {
  const f = {
    key: h('input', { type: 'text', maxlength: 10, placeholder: 'e.g. WEB', required: true, oninput: (e) => { e.target.value = e.target.value.toUpperCase().replace(/[^A-Z0-9]/g, ''); } }),
    name: h('input', { type: 'text', maxlength: 100, required: true }),
    status: h('select', { value: 'active' }, optionList(state.meta.project_statuses.filter((s) => s !== 'archived').map((s) => [s, PROJECT_STATUS_LABEL[s]]))),
    start_date: h('input', { type: 'date' }), due_date: h('input', { type: 'date' }), description: h('textarea', {}),
  };
  const checks = state.users.filter((u) => u.username !== state.me.username).map((u) => [u, h('input', { type: 'checkbox' })]);
  const create = h('button', { class: 'btn primary', onclick: () => guarded(create, async () => {
    const body = { key: f.key.value, name: f.name.value, status: f.status.value, description: f.description.value, members: checks.filter(([, c]) => c.checked).map(([u]) => u.username) };
    for (const k of ['start_date', 'due_date']) if (f[k].value) body[k] = f[k].value;
    const p = await api('/projects', { method: 'POST', body });
    m.close(); await refreshProjects(); location.hash = '#/p/' + p.key; toast(`Project ${p.key} created`, 'ok');
  }) }, 'Create project');
  const m = openModal({ title: 'New project', content: h('div', { class: 'stack' }, h('div', { class: 'form-grid' }, field('Key', f.key, '2\u201310 letters or digits; prefixes task keys'), field('Name', f.name), field('Status', f.status), field('Start date', f.start_date), field('Due date', f.due_date)),
    field('Description', f.description), h('div', {}, h('div', { class: 'small muted', text: 'Members (you are added automatically)' }), h('div', { class: 'row wrap' }, checks.map(([u, c]) => h('label', { class: 'row small' }, c, u.display_name))))),
    footer: [h('button', { class: 'btn', onclick: () => m.close() }, 'Cancel'), create] });
}

/* ============================== task dialog ============================== */
async function openTaskFromAnywhere(key) { location.hash = '#/t/' + key; }

async function openTask(key) {
  let task;
  try { task = await api('/tasks/' + key); } catch (e) { toast(e.message, 'error'); return; }
  const project = await getProject(task.project);
  const milestones = (await api(`/projects/${task.project}/milestones`)).items;
  const editable = canWrite(project);
  let dirty = false, comments = [], time = [], acts = [];
  // Typed-but-unsent text survives the re-render that follows every saved field change.
  const drafts = { description: null, comment: '', subtask: '', depKey: '', depDir: 'blocked_by', hours: '', day: state.today, note: '' };
  const track = (name) => (e) => { drafts[name] = e.target.value; };
  const body = h('div');
  const handle = openModal({ title: null, content: body, onClose: () => { if (dirty) route(); } });
  handle.el.setAttribute('aria-label', `${task.key} ${task.title}`);

  let loadSeq = 0;
  async function loadAll() {
    const mine = ++loadSeq;
    const fresh = await Promise.all([api('/tasks/' + key), api(`/tasks/${key}/comments`).then((r) => r.items), api(`/tasks/${key}/time`).then((r) => r.items), api(`/tasks/${key}/activity`, { query: { limit: 15 } }).then((r) => r.items)]);
    if (mine !== loadSeq) return; // a newer load is in flight: an older response must never overwrite newer state
    [task, comments, time, acts] = fresh;
    render();
  }
  async function patch(change) {
    try { task = await api('/tasks/' + key, { method: 'PATCH', body: change }); dirty = true; await loadAll(); } catch (e) { toast(e.message, 'error'); render(); }
  }
  const act = (fn) => async (e) => { const btn = e && e.currentTarget; if (btn) btn.disabled = true; try { await fn(); } catch (err) { toast(err.message, 'error'); } finally { if (btn) btn.disabled = false; } };
  const reload = async () => { dirty = true; await loadAll(); };

  function render() {
    clear(body);
    const title = h('input', { class: 'title-input', type: 'text', value: task.title, maxlength: 200, disabled: !editable, 'aria-label': 'Title', onchange: (e) => patch({ title: e.target.value }) });
    const descChanged = () => drafts.description !== null && drafts.description !== task.description;
    const desc = h('textarea', { 'aria-label': 'Description', placeholder: editable ? 'Add a description\u2026' : 'No description', disabled: !editable, value: drafts.description ?? task.description,
      oninput: (e) => { drafts.description = e.target.value; saveDesc.classList.toggle('hidden', !descChanged()); } });
    const saveDesc = h('button', { class: 'btn small primary' + (descChanged() ? '' : ' hidden'), onclick: act(async () => { await patch({ description: desc.value }); drafts.description = null; }) }, 'Save description');

    const statusOpts = [task.status, ...task.allowed_transitions];
    const sel = (label, value, pairs, name, nullable) => field(label, h('select', { value: value ?? '', disabled: !editable, onchange: (e) => patch({ [name]: e.target.value === '' && nullable ? null : e.target.value }) }, optionList(pairs)));
    const date = (label, name) => field(label, h('input', { type: 'date', value: task[name] || '', disabled: !editable, onchange: (e) => patch({ [name]: e.target.value || null }) }));
    const side = h('div', { class: 'sidebox' },
      sel('Status', task.status, statusOpts.map((s) => [s, STATUS_LABEL[s]]), 'status'),
      sel('Priority', task.priority, state.meta.priorities.map((p) => [p, PRIORITY_LABEL[p]]), 'priority'),
      sel('Type', task.type, state.meta.task_types.map((t) => [t, TYPE_LABEL[t]]), 'type'),
      sel('Assignee', task.assignee ? task.assignee.username : '', userPairs(project.members, 'Unassigned'), 'assignee', true),
      sel('Milestone', task.milestone ? String(task.milestone.id) : '', [['', 'None'], ...milestones.map((m) => [String(m.id), m.name])], 'milestone', true),
      date('Start date', 'start_date'), date('Due date', 'due_date'),
      field('Estimate (hours)', h('input', { type: 'number', min: 0, step: '0.25', value: task.estimate_hours ?? '', disabled: !editable, onchange: (e) => patch({ estimate_hours: e.target.value === '' ? null : Number(e.target.value) }) })),
      field('Labels', h('input', { type: 'text', value: task.labels.join(', '), disabled: !editable, placeholder: 'comma separated', onchange: (e) => patch({ labels: e.target.value.split(/[,\s]+/).filter(Boolean) }) })),
      h('div', { class: 'small muted' }, h('div', { text: `Reporter: ${task.reporter ? task.reporter.display_name : '\u2014'}` }), h('div', { text: `Created ${fmtAgo(task.created_at)}` }), h('div', { text: `Updated ${fmtAgo(task.updated_at)}` }), task.completed_at ? h('div', { text: `Closed ${fmtAgo(task.completed_at)}` }) : null,
        task.external_ref ? h('div', { text: `Imported from ${task.external_ref}` }) : null, task.parent ? h('div', {}, 'Subtask of ', h('a', { href: '#', onclick: (e) => { e.preventDefault(); switchTo(task.parent); } }, task.parent)) : null),
      hasRole('manager') && project.status !== 'archived' ? h('button', { class: 'btn danger small', onclick: act(deleteTask) }, 'Delete task') : null);

    put(body,
      h('div', { class: 'row', style: { marginBottom: '6px' } }, h('strong', { class: 'muted', text: task.key }), badge(task.status, STATUS_LABEL[task.status]), task.is_overdue ? badge('overdue', 'Overdue') : null,
        task.blocked_by_open ? badge('blocked', `Waiting on ${task.blocked_by_open}`) : null, h('span', { class: 'spacer' }), h('button', { class: 'btn ghost', 'aria-label': 'Close', onclick: () => handle.close() }, '\u2715')),
      project.status === 'archived' ? h('p', { class: 'small muted', text: 'This project is archived, so its tasks are read-only.' }) : !editable ? h('p', { class: 'small muted', text: 'You have read-only access to this task.' }) : null,
      h('div', { class: 'task-layout' }, h('div', {}, title, h('div', { class: 'stack', style: { marginTop: '8px' } }, desc, saveDesc),
        subtasksSection(), dependenciesSection(), timeSection(), commentsSection(), activitySection()), side));
  }

  async function switchTo(otherKey) { handle.close(); openTask(otherKey); }
  async function deleteTask() {
    if (!(await confirmBox(`Delete ${task.key} permanently? Its comments, time and history go with it.`, 'Delete task', true))) return;
    try { await api('/tasks/' + key, { method: 'DELETE' }); }
    catch (e) {
      if (e.code === 'has_subtasks' && (await confirmBox(`${e.message} Delete the subtasks too?`, 'Delete all', true))) await api('/tasks/' + key, { method: 'DELETE', query: { cascade: 'true' } });
      else throw e;
    }
    dirty = true; handle.close(); toast(`${task.key} deleted`, 'ok');
  }

  function subtasksSection() {
    const input = h('input', { type: 'text', placeholder: 'Add a subtask\u2026', maxlength: 200, 'aria-label': 'New subtask title', value: drafts.subtask, oninput: track('subtask') });
    const add = h('button', { class: 'btn small', onclick: act(async () => { if (!input.value.trim()) return; await api(`/projects/${task.project}/tasks`, { method: 'POST', body: { title: input.value.trim(), parent: key } }); drafts.subtask = ''; await reload(); }) }, 'Add');
    return h('div', { class: 'section' }, h('h3', { text: `Subtasks ${task.subtasks.total ? `(${task.subtasks.done}/${task.subtasks.total} done)` : ''}` }),
      task.subtask_list.map((s) => h('div', { class: 'item' }, statusBadge(s.status), h('a', { href: '#', class: 'grow', onclick: (e) => { e.preventDefault(); switchTo(s.key); } }, h('strong', { text: s.key }), ' ', s.title), h('span', { class: 'small muted', text: s.assignee || '' }))),
      editable ? h('div', { class: 'row', style: { marginTop: '6px' } }, input, add) : null);
  }
  function dependenciesSection() {
    const input = h('input', { type: 'text', placeholder: 'Task key, e.g. ' + task.project + '-1', 'aria-label': 'Related task key', value: drafts.depKey, oninput: track('depKey') });
    const dir = h('select', { 'aria-label': 'Relationship', value: drafts.depDir, onchange: track('depDir') }, optionList([['blocked_by', 'is blocked by'], ['blocks', 'blocks']]));
    const add = h('button', { class: 'btn small', onclick: act(async () => { if (!input.value.trim()) return; await api(`/tasks/${key}/dependencies`, { method: 'POST', body: { [dir.value]: input.value.trim().toUpperCase() } }); drafts.depKey = ''; await reload(); }) }, 'Link');
    const rows = (list, label) => list.map((d) => h('div', { class: 'item' }, h('span', { class: 'small muted nowrap', text: label }), statusBadge(d.status), h('a', { href: '#', class: 'grow', onclick: (e) => { e.preventDefault(); switchTo(d.key); } }, h('strong', { text: d.key }), ' ', d.title),
      editable ? h('button', { class: 'btn ghost small', 'aria-label': `Remove link to ${d.key}`, onclick: act(async () => { await api(`/tasks/${key}/dependencies/${d.key}`, { method: 'DELETE' }); await reload(); }) }, '\u2715') : null));
    return h('div', { class: 'section' }, h('h3', { text: 'Dependencies' }), rows(task.blocked_by, 'blocked by'), rows(task.blocks, 'blocks'),
      !task.blocked_by.length && !task.blocks.length ? h('div', { class: 'small muted', text: 'No dependencies.' }) : null,
      editable ? h('div', { class: 'row', style: { marginTop: '6px' } }, dir, input, add) : null);
  }
  function timeSection() {
    const hours = h('input', { type: 'number', min: 0.25, max: 24, step: '0.25', placeholder: 'hours', 'aria-label': 'Hours', style: { width: '100px' }, value: drafts.hours, oninput: track('hours') });
    const day = h('input', { type: 'date', value: drafts.day, 'aria-label': 'Date worked', style: { width: '150px' }, oninput: track('day') });
    const note = h('input', { type: 'text', placeholder: 'What did you do? (optional)', maxlength: 500, 'aria-label': 'Note', value: drafts.note, oninput: track('note') });
    const log = h('button', { class: 'btn small', onclick: act(async () => { await api(`/tasks/${key}/time`, { method: 'POST', body: { hours: Number(hours.value), work_date: day.value, note: note.value } }); drafts.hours = ''; drafts.note = ''; await reload(); }) }, 'Log time');
    const pct = task.estimate_hours ? (100 * task.logged_hours) / task.estimate_hours : 0;
    return h('div', { class: 'section' }, h('h3', { text: 'Time' }),
      h('div', { class: 'row' }, task.estimate_hours ? progressBar(pct, pct > 100 ? 'bad' : 'ok') : null, h('span', { class: 'small nowrap', text: `${fmtHours(task.logged_hours)} logged${task.estimate_hours ? ` of ${fmtHours(task.estimate_hours)} estimated` : ''}` })),
      time.map((e) => h('div', { class: 'item' }, avatar(e.user), h('span', { class: 'grow' }, h('strong', { text: fmtHours(e.hours) }), ` on ${fmtDate(e.work_date)} \u2014 ${e.user.display_name}`, e.note ? h('span', { class: 'muted', text: ` \u00b7 ${e.note}` }) : null),
        (e.user.username === state.me.username || hasRole('manager')) && editable ? h('button', { class: 'btn ghost small', 'aria-label': 'Delete time entry', onclick: act(async () => { await api('/time/' + e.id, { method: 'DELETE' }); await reload(); }) }, '\u2715') : null)),
      editable ? h('div', { class: 'row wrap', style: { marginTop: '6px' } }, hours, day, note, log) : null);
  }
  function commentsSection() {
    const text = h('textarea', { placeholder: 'Write a comment\u2026 use @username to mention someone', 'aria-label': 'New comment', maxlength: 10000, value: drafts.comment, oninput: track('comment') });
    const post = h('button', { class: 'btn small primary', onclick: act(async () => { if (!text.value.trim()) return; await api(`/tasks/${key}/comments`, { method: 'POST', body: { body: text.value } }); drafts.comment = ''; await reload(); }) }, 'Comment');
    return h('div', { class: 'section' }, h('h3', { text: `Comments (${comments.length})` }),
      comments.map((c) => h('div', { class: 'comment' }, h('div', { class: 'row' }, avatar(c.author), h('strong', { text: c.author.display_name }), h('span', { class: 'small muted', text: fmtAgo(c.created_at) + (c.edited_at ? ' (edited)' : '') }), h('span', { class: 'spacer' }),
        c.author.username === state.me.username && editable ? h('button', { class: 'btn ghost small', onclick: () => editComment(c) }, 'Edit') : null,
        (c.author.username === state.me.username || hasRole('manager')) && editable ? h('button', { class: 'btn ghost small', onclick: act(async () => { if (await confirmBox('Delete this comment?', 'Delete', true)) { await api('/comments/' + c.id, { method: 'DELETE' }); await reload(); } }) }, 'Delete') : null),
        h('div', { class: 'body', text: c.body }))),
      editable ? h('div', { class: 'stack', style: { marginTop: '8px' } }, text, h('div', {}, post)) : null);
  }
  function editComment(c) {
    const area = h('textarea', { value: c.body, maxlength: 10000, 'aria-label': 'Edit comment' });
    const save = h('button', { class: 'btn primary', onclick: () => guarded(save, async () => { await api('/comments/' + c.id, { method: 'PATCH', body: { body: area.value } }); m.close(); await reload(); }) }, 'Save');
    const m = openModal({ title: 'Edit comment', narrow: true, content: area, footer: [h('button', { class: 'btn', onclick: () => m.close() }, 'Cancel'), save] });
  }
  function activitySection() {
    return h('div', { class: 'section' }, h('h3', { text: 'History' }), acts.length ? acts.map(activityItem) : h('div', { class: 'small muted', text: 'No history yet.' }));
  }

  await loadAll();
}

/* ============================== users (admin) ============================== */
async function usersView(view, alive) {
  const data = await api('/users', { query: { include_inactive: 'true' } });
  if (!alive()) return;
  clear(view);
  const roleSel = (u) => h('select', { value: u.role, 'aria-label': `Role of ${u.username}`, onchange: (e) => updateUser(u, { role: e.target.value }) }, optionList(ROLES.map((r) => [r, r])));
  async function updateUser(u, change) { try { await api('/users/' + u.id, { method: 'PATCH', body: change }); toast('User updated', 'ok'); } catch (e) { toast(e.message, 'error'); } route(); }
  view.append(h('div', { class: 'page-head' }, h('div', { class: 'titles' }, h('h1', { text: 'Users & access' }), h('p', { class: 'muted', text: 'Viewers read, members edit tasks in their projects, managers run projects, admins manage people. Tokens are shown once.' })),
    h('button', { class: 'btn primary', onclick: openCreateUser }, '+ New user')),
    h('div', { class: 'card flush' }, h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ['User', 'Role', 'Capacity / week', 'Status', ''].map((x) => h('th', { text: x })))),
      h('tbody', {}, data.items.map((u) => h('tr', {}, h('td', {}, h('div', { class: 'row' }, avatar(u), h('div', {}, h('strong', { text: u.display_name }), h('div', { class: 'small muted', text: `${u.username}${u.email ? ' \u00b7 ' + u.email : ''}` })))),
        h('td', {}, roleSel(u)), h('td', { text: fmtHours(u.weekly_capacity_hours) }), h('td', {}, badge(u.active ? 'active' : 'archived', u.active ? 'Active' : 'Deactivated')),
        h('td', { class: 'right nowrap' }, h('button', { class: 'btn small', onclick: async (e) => guarded(e.target, async () => { if (await confirmBox(`Issue a new token for ${u.display_name}? Their current token stops working immediately.`, 'Issue new token')) showToken(u.username, (await api(`/users/${u.id}/rotate-token`, { method: 'POST' })).token); }) }, 'New token'), ' ',
          h('button', { class: 'btn small ' + (u.active ? 'danger' : ''), onclick: () => updateUser(u, { active: !u.active }) }, u.active ? 'Deactivate' : 'Reactivate'))))))));
}
function showToken(username, token) {
  const m = openModal({ title: `Token for ${username}`, narrow: true, content: h('div', { class: 'stack' }, h('p', { text: 'Copy this now \u2014 it will not be shown again.' }), h('div', { class: 'token-box', text: token }),
    h('button', { class: 'btn', onclick: async (e) => { try { await navigator.clipboard.writeText(token); e.target.textContent = 'Copied \u2713'; } catch (err) { toast('Select the token and copy it manually', 'error'); } } }, 'Copy to clipboard')), footer: [h('button', { class: 'btn primary', onclick: () => m.close() }, 'Done')] });
}
function openCreateUser() {
  const f = { username: h('input', { type: 'text', maxlength: 32, required: true, placeholder: 'lowercase, e.g. dana' }), display_name: h('input', { type: 'text', maxlength: 100, required: true }), email: h('input', { type: 'email' }),
    role: h('select', { value: 'member' }, optionList(ROLES.map((r) => [r, r]))), weekly_capacity_hours: h('input', { type: 'number', min: 0, max: 168, value: 40 }) };
  const create = h('button', { class: 'btn primary', onclick: () => guarded(create, async () => {
    const body = { username: f.username.value, display_name: f.display_name.value, role: f.role.value, weekly_capacity_hours: Number(f.weekly_capacity_hours.value) };
    if (f.email.value) body.email = f.email.value;
    const r = await api('/users', { method: 'POST', body });
    m.close(); state.users = (await api('/users')).items; showToken(r.user.username, r.token); route();
  }) }, 'Create user');
  const m = openModal({ title: 'New user', narrow: true, content: h('div', { class: 'stack' }, field('Username', f.username), field('Display name', f.display_name), field('E-mail (optional)', f.email), h('div', { class: 'form-grid' }, field('Role', f.role), field('Weekly capacity (h)', f.weekly_capacity_hours))), footer: [h('button', { class: 'btn', onclick: () => m.close() }, 'Cancel'), create] });
}

/* ============================== boot ============================== */
async function boot() {
  const token = sessionStorage.getItem('zpm_token') || localStorage.getItem('zpm_token');
  if (!token) return showLogin();
  state.token = token;
  try { await loadSession(); } catch (e) { state.token = null; return showLogin(e.status === 401 ? 'Your saved token is no longer valid.' : e.message); }
  renderShell();
  route();
}
boot();
})();
