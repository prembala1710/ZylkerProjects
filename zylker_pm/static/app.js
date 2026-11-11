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
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const clear = (el) => el.replaceChildren();

/* ============================== constants & formatting ============================== */
const STATUS_LABEL = { todo: 'To do', in_progress: 'In progress', in_review: 'In review', blocked: 'Blocked', done: 'Done', cancelled: 'Cancelled' };
const PRIORITY_LABEL = { low: 'Low', medium: 'Medium', high: 'High', critical: 'Critical' };
const TYPE_LABEL = { task: 'Task', bug: 'Bug', story: 'Story', epic: 'Epic' };
const HEALTH_LABEL = { on_track: 'On track', at_risk: 'At risk', off_track: 'Off track', complete: 'Complete', not_started: 'Not started', on_hold: 'On hold' };
const PROJECT_STATUS_LABEL = { planning: 'Planning', active: 'Active', on_hold: 'On hold', completed: 'Completed', archived: 'Archived' };
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const WEEKDAY_SHORT = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
const ROLES = ['viewer', 'member', 'manager', 'admin'];
const DEP_TYPE_LABEL = { finish_to_start: 'Finish \u2192 Start', start_to_start: 'Start \u2192 Start', finish_to_finish: 'Finish \u2192 Finish' };
const DEP_TYPE_SHORT = { finish_to_start: 'FS', start_to_start: 'SS', finish_to_finish: 'FF' };
const DEP_TYPE_HELP = {
  finish_to_start: 'The blocked task cannot start until the blocker is finished.',
  start_to_start: 'The blocked task cannot start until the blocker has started.',
  finish_to_finish: 'The blocked task cannot finish until the blocker is finished.',
};
const LINK_LABEL = { relates_to: 'relates to', duplicates: 'duplicates', duplicated_by: 'is duplicated by', cloned_from: 'is cloned from', cloned_to: 'was cloned to' };
const lagText = (n) => (!n ? '' : n > 0 ? `${n}d lag` : `${-n}d lead`);

const state = { token: null, me: null, meta: null, users: [], projects: [], fields: [], today: '', pendingTask: null, projectCache: {} };

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
function fmtDay(s) {
  const d = parseDate(s);
  return d ? `${WEEKDAY_SHORT[(d.getDay() + 6) % 7]} ${fmtDate(s)}` : '\u2014';
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
const lateChip = (info) => h('span', { class: 'chip late', title: `Projected to finish ${fmtDate(info.projected_finish)}, ${plural(info.late_by_days, 'day')} after its due date.\n${info.explanation}`,
  'aria-label': `Projected ${plural(info.late_by_days, 'day')} late` }, `\u23F1 +${info.late_by_days}d`);
const VERDICT = { will_miss: ['overdue', 'Projected late'], on_track: ['on_track', 'On track'], unknown: ['at_risk', 'Cannot be projected'], no_due_date: ['planning', 'No due date'] };
const verdictBadge = (p) => (p.state === 'complete' ? badge('done', 'No open work') : p.state === 'not_projected' ? badge('on_hold', 'Not projected') : badge(...VERDICT[p.verdict]));
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

/* ============================== custom project fields ============================== */
const FIELD_TYPE_LABEL = { text: 'Text', number: 'Number', url: 'URL' };
const fieldLabel = (key) => { const f = state.fields.find((x) => x.key === key); return f ? f.label : key; };
const fmtNumber = (n, decimals) => new Intl.NumberFormat(undefined, { maximumFractionDigits: decimals ?? 3 }).format(n);
const decimalPlaces = (text) => { const m = /^-?\d*\.(\d+)$/.exec(text); return m ? m[1].length : 0; };
// Only http(s) addresses ever become links. Anything else (javascript:, data:, ...) is shown as plain text.
function safeUrl(value) {
  try { const u = new URL(value); return u.protocol === 'http:' || u.protocol === 'https:' ? u.href : null; } catch (e) { return null; }
}
function fieldValue(def, value) {
  if (value === null || value === undefined || value === '') return h('span', { class: 'muted', text: '\u2014' });
  if (def.type === 'number') return h('span', { text: fmtNumber(value, def.decimals) });
  if (def.type === 'url') {
    const href = safeUrl(value);
    return href ? h('a', { href, target: '_blank', rel: 'noopener noreferrer nofollow', class: 'cf-link ellipsis', title: value, text: value }) : h('span', { text: value });
  }
  return h('span', { text: value });
}
function fieldRules(def) {
  if (def.type === 'text') return `Up to ${def.max_length} characters`;
  if (def.type === 'url') return 'http:// or https:// address';
  const bits = [def.decimals === 0 ? 'Whole numbers' : `Up to ${plural(def.decimals, 'decimal place')}`];
  if (def.min_value !== null) bits.push('min ' + fmtNumber(def.min_value, def.decimals));
  if (def.max_value !== null) bits.push('max ' + fmtNumber(def.max_value, def.decimals));
  return bits.join(', ');
}
// The browser checks these constraints first for instant feedback; the server re-checks all of them.
function fieldControl(def, value, required) {
  const shown = value === null || value === undefined ? '' : value;
  const common = { value: shown, required, autocomplete: 'off', 'aria-describedby': null };
  if (def.type === 'number') {
    const el = h('input', { ...common, type: 'number', step: 'any', min: def.min_value, max: def.max_value, inputmode: def.decimals ? 'decimal' : 'numeric' });
    el.addEventListener('input', () => el.setCustomValidity(decimalPlaces(el.value) > def.decimals ? (def.decimals ? `At most ${plural(def.decimals, 'decimal place')}` : 'Whole numbers only') : ''));
    return el;
  }
  if (def.type === 'url') return h('input', { ...common, type: 'url', maxlength: state.meta.project_field_limits.max_url_length, placeholder: 'https://', inputmode: 'url', spellcheck: 'false', pattern: 'https?://.+', title: 'A web address starting with http:// or https://' });
  return h('input', { ...common, type: 'text', maxlength: def.max_length });
}
// One input per defined field. A required field only blocks saving when the project has (or is getting) a value,
// so projects that predate the field can still be edited.
function customFieldsEditor(values, { creating, disabled }) {
  const controls = {};
  const node = h('div', { class: 'cf-grid' });
  for (const def of state.fields) {
    const current = values ? values[def.key] : null;
    const el = fieldControl(def, current, def.required && (creating || (current !== null && current !== undefined)));
    if (disabled) el.disabled = true;
    controls[def.key] = { def, el };
    node.append(field(def.label + (def.required ? ' *' : ''), el, def.description || fieldRules(def)));
  }
  return { controls, node };
}
function readControl(def, el) {
  const raw = el.value.trim();
  if (raw === '') return null;
  return def.type === 'number' ? Number(raw) : raw;
}
// Only what changed (or, when creating, what was filled in); null clears a value.
function collectCustom(controls, current) {
  const out = {};
  for (const [key, { def, el }] of Object.entries(controls)) {
    const value = readControl(def, el);
    const before = current && current[key] !== undefined ? current[key] : null;
    if (value !== before) out[key] = value;
  }
  return out;
}
const controlsValid = (controls) => Object.values(controls).every(({ el }) => el.disabled || el.reportValidity());
function customFieldSummary(project) {
  const values = project.custom_fields || {};
  const shown = state.fields.filter((d) => values[d.key] !== null && values[d.key] !== undefined);
  if (!shown.length) return null;
  return h('dl', { class: 'cf-summary' }, shown.map((d) => h('div', { class: 'cf-pair' }, h('dt', { text: d.label }), h('dd', {}, fieldValue(d, values[d.key])))));
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
  const [me, meta, users, projects, fields] = await Promise.all([api('/me'), api('/meta'), api('/users'), api('/projects'), api('/project-fields')]);
  Object.assign(state, { me, meta, users: users.items, projects: projects.items, fields: fields.items, today: meta.today });
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
    hasRole('admin') ? h('a', { class: 'nav-link', href: '#/users', dataset: { route: 'users' } }, 'Users & access') : null,
    hasRole('admin') ? h('a', { class: 'nav-link', href: '#/fields', dataset: { route: 'fields' } }, 'Project fields') : null);

  const topbar = h('header', { class: 'topbar' }, h('div', { class: 'search' }, searchBox, results), h('span', { class: 'spacer' }),
    avatar(state.me), h('div', { class: 'user-chip' }, h('div', { text: state.me.display_name }), h('div', { class: 'small muted', text: state.me.role })),
    h('button', { class: 'btn small', onclick: () => signOut('You have been signed out.') }, 'Sign out'));
  root.append(h('div', { class: 'layout' }, sidebar, h('div', { class: 'main' }, topbar, h('main', { class: 'content', id: 'view', tabindex: '-1' }))));
  if (hasRole('admin')) sidebar.append(h('a', { class: 'nav-link', href: '#/webhooks', dataset: { route: 'webhooks' } }, 'Webhooks'));
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
    else if (a === 'webhooks' && hasRole('admin')) await webhooksView(view, alive);
    else if (a === 'mine') await myTasksView(view, alive);
    else if (a === 'team') await teamView(view, alive);
    else if (a === 'users' && hasRole('admin')) await usersView(view, alive);
    else if (a === 'fields' && hasRole('admin')) await fieldsView(view, alive);
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
  let forecastError = null;
  const [portfolio, digest, workload, forecast] = await Promise.all([api('/reports/portfolio'), api('/reports/digest', { query: { days: 7 } }), api('/reports/workload'),
    api('/reports/projection').catch((e) => { forecastError = e.message; return null; })]); // the dashboard still works if the projection cannot be computed
  if (!alive()) return;
  const projected = Object.fromEntries(((forecast && forecast.projects) || []).map((p) => [p.project, p]));
  clear(view);
  const t = portfolio.totals;
  const risky = (t.by_health.at_risk || 0) + (t.by_health.off_track || 0);
  view.append(
    h('div', { class: 'page-head' }, h('div', { class: 'titles' }, h('h1', { text: 'Dashboard' }), h('p', { class: 'muted', text: `Portfolio overview as of ${fmtDate(portfolio.as_of)}` })),
      hasRole('manager') ? h('button', { class: 'btn primary', onclick: openCreateProject }, '+ New project') : null),
    h('div', { class: 'grid kpis' }, kpi('Projects', t.projects), kpi('Open tasks', t.open_tasks), kpi('Overdue tasks', t.overdue_tasks, t.overdue_tasks ? 'bad' : ''), kpi('At risk / off track', risky, risky ? 'warn' : '')),
    h('div', { class: 'card flush', style: { marginTop: '16px' } }, portfolioTable(portfolio.projects, projected),
      forecastError ? h('p', { class: 'small muted', style: { margin: '10px 14px' }, id: 'projection-unavailable', text: `Projected finish is not available: ${forecastError}` }) : null),
    h('div', { class: 'grid two', style: { marginTop: '16px' } }, attentionCard(digest), workloadCard(workload)));
}
function projectedCell(p) {
  if (!p || p.state !== 'projected') return '\u2014';
  if (!p.finish) return h('span', { class: 'small muted', text: 'Cannot be projected' });
  const note = p.late_by_days ? h('div', { class: 'small overdue-text', text: `${plural(p.late_by_days, 'day')} after the due date` })
    : h('div', { class: 'small muted', text: p.due_date ? 'in time' : 'no due date' });
  return [h('span', { text: fmtDate(p.finish) }), note, p.late_tasks ? h('div', { class: 'small overdue-text', text: `${plural(p.late_tasks, 'task')} will be late` }) : null];
}
function portfolioTable(projects, projected = {}) {
  if (!projects.length) return h('div', { class: 'empty', text: 'No projects yet. Create one to get started.' });
  return h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ['Project', 'Health', 'Progress', 'Open', 'Overdue', 'Next milestone', 'Due', 'Projected finish'].map((x) => h('th', { text: x })))),
    h('tbody', {}, projects.map((p) => h('tr', { class: 'click', tabindex: 0, onclick: () => { location.hash = '#/p/' + p.key; }, onkeydown: (e) => { if (e.key === 'Enter') location.hash = '#/p/' + p.key; } },
      h('td', {}, h('strong', { text: p.name }), h('div', { class: 'small muted', text: `${p.key} \u00b7 ${PROJECT_STATUS_LABEL[p.status]} \u00b7 ${p.owner ? p.owner.display_name : 'no owner'}` })),
      h('td', {}, healthBadge(p.health)),
      h('td', { style: { minWidth: '150px' } }, h('div', { class: 'row' }, progressBar(p.progress.tasks_pct), h('span', { class: 'small nowrap', text: `${Math.round(p.progress.tasks_pct)}%` }))),
      h('td', { text: p.open_tasks }),
      h('td', { class: p.overdue ? 'overdue-text' : '', text: p.overdue }),
      h('td', {}, p.next_milestone ? h('span', {}, p.next_milestone.name, ' ', badge(p.next_milestone.status === 'overdue' ? 'overdue' : 'open', fmtDate(p.next_milestone.due_date))) : '\u2014'),
      h('td', { class: 'nowrap' }, p.due_date ? [fmtDate(p.due_date), p.days_remaining !== null ? h('div', { class: 'small ' + (p.days_remaining < 0 ? 'overdue-text' : 'muted'), text: p.days_remaining < 0 ? `${-p.days_remaining}d late` : `${p.days_remaining}d left` }) : null] : '\u2014'),
      h('td', { class: 'nowrap' }, projectedCell(projected[p.key]))))));
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

/* ============================== webhooks (admin) ============================== */
const WEBHOOK_GROUP_LABEL = { project: 'Projects', milestone: 'Milestones', task: 'Tasks', checklist: 'Checklists', watcher: 'Watchers', project_field: 'Project fields', comment: 'Comments', attachment: 'Attachments', time: 'Time tracking', import: 'Imports' };
const DELIVERY_BADGE = { succeeded: ['done', 'Delivered'], failed: ['blocked', 'Failed'], pending: ['todo', 'Queued'], sending: ['in_progress', 'Sending'] };
const DELIVERY_ERROR = { timeout: 'timed out', connection_failed: 'could not connect', dns_error: 'host name did not resolve', tls_error: 'TLS certificate problem',
  blocked_address: 'address is private (blocked)', protocol_error: 'invalid HTTP response', invalid_request: 'invalid request', internal_error: 'internal error',
  cancelled: 'cancelled (webhook paused)', lease_expired: 'worker stopped mid-send' };
const describeDelivery = (d) => (d.response_status ? `HTTP ${d.response_status}` : (d.error && (DELIVERY_ERROR[d.error] || d.error)) || '\u2014');
function hostPath(url) { try { const u = new URL(url); return u.host + (u.pathname === '/' ? '' : u.pathname); } catch (e) { return 'invalid URL'; } } // never the query string: it often carries a key
function webhookHealth(w) {
  const hl = w.health;
  if (!w.active) return badge('on_hold', 'Paused');
  if (hl.consecutive_failures >= 3) return badge('off_track', `Failing \u00d7${hl.consecutive_failures}`, 'Several delivery attempts in a row have failed');
  if (hl.consecutive_failures > 0) return badge('at_risk', 'Retrying', 'The last delivery attempt failed and will be retried');
  return hl.last_status === 'succeeded' ? badge('on_track', 'Healthy', 'The last delivery succeeded') : badge('not_started', 'No deliveries yet');
}

async function webhooksView(view, alive) {
  const [data, catalog] = await Promise.all([api('/webhooks'), api('/webhooks/events')]);
  if (!alive()) return;
  clear(view);
  const rows = data.items.map((w) => h('tr', {},
    h('td', {}, h('strong', { text: w.name }), h('div', { class: 'small muted ellipsis', title: hostPath(w.url), text: hostPath(w.url) })),
    h('td', { title: w.events.join('\n'), text: w.events.includes('*') ? 'All events' : plural(w.events.length, 'subscription') }),
    h('td', { text: w.projects.length ? w.projects.join(', ') : 'All projects' }),
    h('td', {}, webhookHealth(w), w.pending_deliveries ? h('div', { class: 'small muted', text: `${w.pending_deliveries} queued` }) : null),
    h('td', { class: 'small', text: w.health.last_delivery_at ? fmtAgo(w.health.last_delivery_at) : '\u2014' }),
    h('td', { class: 'right nowrap' },
      h('button', { class: 'btn small', onclick: () => openDeliveries(w) }, 'Deliveries'), ' ',
      h('button', { class: 'btn small', onclick: (e) => guarded(e.target, () => sendTestEvent(w)) }, 'Send test'), ' ',
      h('button', { class: 'btn small', onclick: (e) => guarded(e.target, async () => { await api('/webhooks/' + w.id, { method: 'PATCH', body: { active: !w.active } }); toast(w.active ? 'Webhook paused' : 'Webhook resumed', 'ok'); route(); }) }, w.active ? 'Pause' : 'Resume'), ' ',
      h('button', { class: 'btn small', onclick: () => openWebhookForm(catalog, w) }, 'Edit'))));
  put(view, // put(), not append(): optional blocks may be null and Element.append(null) prints the word "null"
    h('div', { class: 'page-head' }, h('div', { class: 'titles' }, h('h1', { text: 'Webhooks' }),
      h('p', { class: 'muted', text: 'Call other systems when work changes here. Every request is signed, failures are retried with back-off, and each attempt is logged.' })),
      h('button', { class: 'btn primary', onclick: () => openWebhookForm(catalog, null) }, '+ New webhook')),
    data.worker.running ? null : h('div', { class: 'card notice', role: 'alert', style: { marginBottom: '16px' } },
      h('strong', { text: 'The delivery worker is not running. ' }), 'Events are queued but nothing is sending them. ', h('code', { text: 'zylker-pm serve' }),
      ' starts a worker; next to any other server, run ', h('code', { text: 'zylker-pm webhooks-worker' }), '.'),
    h('div', { class: 'card flush' }, data.items.length
      ? h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ['Webhook', 'Events', 'Projects', 'Status', 'Last delivery', ''].map((x) => h('th', { text: x })))), h('tbody', {}, rows))
      : h('div', { class: 'empty', text: 'No webhooks yet. Add one to start sending events to another system.' })));
}
async function sendTestEvent(w) {
  const sent = await api(`/webhooks/${w.id}/test`, { method: 'POST' });
  toast('Test event queued\u2026');
  for (let i = 0; i < 24; i++) {
    await new Promise((resolve) => setTimeout(resolve, 500));
    const d = await api(`/webhooks/${w.id}/deliveries/${sent.id}`);
    if (d.status === 'succeeded') return toast(`Test delivered: ${describeDelivery(d)}`, 'ok');
    if (d.status === 'failed') return toast(`Test failed: ${describeDelivery(d)}`, 'error');
  }
  return toast('The test is still queued. Is the delivery worker running?', 'error');
}
function showWebhookSecret(name, secret) {
  const m = openModal({ title: `Signing secret for ${name}`, narrow: true, content: h('div', { class: 'stack' },
    h('p', { text: 'Copy this now \u2014 it will not be shown again. Receivers use it to check the X-Zylker-Signature header.' }), h('div', { class: 'token-box', text: secret }),
    h('button', { class: 'btn', onclick: async (e) => { try { await navigator.clipboard.writeText(secret); e.target.textContent = 'Copied \u2713'; } catch (err) { toast('Select the secret and copy it manually', 'error'); } } }, 'Copy to clipboard')),
  footer: [h('button', { class: 'btn primary', onclick: () => m.close() }, 'Done')] });
}
function openWebhookForm(catalog, existing) {
  const have = existing ? existing.events : ['*'];
  const f = {
    name: h('input', { type: 'text', maxlength: 80, required: true, value: existing ? existing.name : '', placeholder: 'e.g. Build server' }),
    url: h('input', { type: 'text', maxlength: 2000, required: true, autocomplete: 'off', value: existing ? existing.url : '', placeholder: 'https://example.com/hooks/zylker' }),
    active: h('input', { type: 'checkbox', checked: existing ? existing.active : true }),
  };
  const everything = h('input', { type: 'checkbox', checked: have.includes('*') });
  const groups = Object.entries(catalog.groups).map(([group, names]) => ({ group, whole: h('input', { type: 'checkbox', checked: have.includes(group + '.*') }),
    each: names.map((name) => ({ name, box: h('input', { type: 'checkbox', checked: have.includes(name) }) })) }));
  const syncEvents = () => groups.forEach((g) => { g.whole.disabled = everything.checked; g.each.forEach((e) => { e.box.disabled = everything.checked || g.whole.checked; }); });
  [everything, ...groups.map((g) => g.whole)].forEach((box) => box.addEventListener('change', syncEvents));
  const everyProject = h('input', { type: 'checkbox', checked: !existing || !existing.projects.length });
  const projectBoxes = state.projects.map((p) => ({ key: p.key, name: p.name, box: h('input', { type: 'checkbox', checked: !!existing && existing.projects.includes(p.key) }) }));
  const syncProjects = () => projectBoxes.forEach((p) => { p.box.disabled = everyProject.checked; });
  everyProject.addEventListener('change', syncProjects);
  syncEvents(); syncProjects();
  const row = (box, ...label) => h('label', { class: 'check-row' }, box, ...label);
  const save = h('button', { class: 'btn primary', onclick: () => guarded(save, async () => {
    const events = everything.checked ? ['*'] : groups.flatMap((g) => (g.whole.checked ? [g.group + '.*'] : g.each.filter((e) => e.box.checked).map((e) => e.name)));
    if (!events.length) { toast('Choose at least one event', 'error'); return; }
    const projects = everyProject.checked ? [] : projectBoxes.filter((p) => p.box.checked).map((p) => p.key);
    if (!everyProject.checked && !projects.length) { toast('Choose at least one project, or tick \u201cAll projects\u201d', 'error'); return; }
    const body = { name: f.name.value, url: f.url.value, events, projects, active: f.active.checked };
    if (existing) { await api('/webhooks/' + existing.id, { method: 'PATCH', body }); m.close(); toast('Webhook saved', 'ok'); route(); return; }
    const w = await api('/webhooks', { method: 'POST', body });
    m.close(); route(); showWebhookSecret(w.name, w.secret);
  }) }, existing ? 'Save changes' : 'Create webhook');
  const remove = existing ? h('button', { class: 'btn danger', onclick: (e) => guarded(e.target, async () => {
    if (!(await confirmBox(`Delete ${existing.name}? Its delivery log is deleted with it.`, 'Delete webhook', true))) return;
    await api('/webhooks/' + existing.id, { method: 'DELETE' }); m.close(); toast('Webhook deleted', 'ok'); route();
  }) }, 'Delete\u2026') : null;
  const rotate = existing ? h('div', { class: 'row small' }, h('span', { class: 'muted', text: `Signing secret ${existing.secret_hint}` }),
    h('button', { class: 'btn small', onclick: (e) => guarded(e.target, async () => {
      if (!(await confirmBox('Issue a new signing secret? Receivers still using the old one will reject deliveries until they are updated.', 'Rotate secret', true))) return;
      const r = await api(`/webhooks/${existing.id}/rotate-secret`, { method: 'POST' }); m.close(); route(); showWebhookSecret(existing.name, r.secret);
    }) }, 'Rotate\u2026')) : null;
  const m = openModal({ title: existing ? `Edit ${existing.name}` : 'New webhook', content: h('div', { class: 'stack' },
    field('Name', f.name), field('Payload URL', f.url, 'Events are POSTed here as JSON. Use https; the address is never written to logs.'),
    h('div', {}, h('div', { class: 'fieldset-title', text: 'Events' }), h('div', { class: 'check-list' }, row(everything, h('strong', { text: 'All events' })),
      groups.map((g) => h('div', { class: 'check-group' }, row(g.whole, `${WEBHOOK_GROUP_LABEL[g.group] || g.group} \u2014 everything`),
        h('div', { class: 'children' }, g.each.map((e) => row(e.box, e.name.slice(e.name.indexOf('.') + 1).replace(/_/g, ' ')))))))),
    h('div', {}, h('div', { class: 'fieldset-title', text: 'Projects' }), h('div', { class: 'check-list' }, row(everyProject, h('strong', { text: 'All projects' })),
      projectBoxes.map((p) => row(p.box, h('span', { class: 'nav-key', text: p.key }), p.name)))),
    row(f.active, 'Active \u2014 send events to this URL'), rotate),
  footer: [remove, h('span', { class: 'spacer' }), h('button', { class: 'btn', onclick: () => m.close() }, 'Cancel'), save] });
}
function openDeliveries(w) {
  const list = h('div'), more = h('div', { style: { marginTop: '10px' } });
  let items = [];
  const render = () => {
    clear(list);
    put(list, items.length ? h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ['When', 'Event', 'Status', 'Tries', 'Result', ''].map((x) => h('th', { text: x })))),
      h('tbody', {}, items.map((d) => {
        const [kind, label] = DELIVERY_BADGE[d.status];
        return h('tr', {}, h('td', { class: 'small nowrap', title: d.created_at, text: fmtAgo(d.created_at) }),
          h('td', {}, h('code', { text: d.event }), d.redelivery_of ? h('div', { class: 'small muted', text: `redelivery of #${d.redelivery_of}` }) : null),
          h('td', {}, badge(kind, label, d.next_attempt_at ? 'Next attempt ' + d.next_attempt_at : undefined)), h('td', { text: `${d.attempts}/${d.max_attempts}` }),
          h('td', { class: 'small', title: d.response_excerpt || undefined, text: describeDelivery(d) }),
          h('td', { class: 'right nowrap' }, h('button', { class: 'btn small', onclick: (e) => guarded(e.target, () => openDeliveryDetail(w, d)) }, 'Details'), ' ',
            d.status === 'succeeded' || d.status === 'failed' ? h('button', { class: 'btn small', onclick: (e) => guarded(e.target, async () => { await api(`/webhooks/${w.id}/deliveries/${d.id}/redeliver`, { method: 'POST' }); toast('Queued again', 'ok'); load(); }) }, 'Redeliver') : null));
      }))) : h('div', { class: 'empty', text: 'Nothing has been sent yet.' }));
  };
  const load = async (older) => {
    const page = await api(`/webhooks/${w.id}/deliveries`, { query: { limit: 25, before: older || '' } });
    items = older ? items.concat(page.items) : page.items;
    render(); clear(more);
    if (page.next_before) more.append(h('button', { class: 'btn', onclick: (e) => guarded(e.target, () => load(page.next_before)) }, 'Load older'));
  };
  const m = openModal({ title: `Deliveries \u2014 ${w.name}`, onClose: () => route(), content: h('div', {}, list, more),
    footer: [h('button', { class: 'btn', onclick: (e) => guarded(e.target, () => load()) }, 'Refresh'), h('button', { class: 'btn primary', onclick: () => m.close() }, 'Close')] });
  load().catch((e) => toast(e.message, 'error'));
}
async function openDeliveryDetail(w, d) {
  const full = await api(`/webhooks/${w.id}/deliveries/${d.id}`);
  const line = (label, value) => h('div', { class: 'item' }, h('span', { class: 'muted', style: { minWidth: '120px' }, text: label }), h('span', { class: 'grow', text: value }));
  const m = openModal({ title: `Delivery #${full.id}`, content: h('div', { class: 'stack' },
    line('Event', full.event), line('Event id', full.event_id), line('Status', `${DELIVERY_BADGE[full.status][1]} \u00b7 attempt ${full.attempts} of ${full.max_attempts}`),
    line('Last attempt', full.last_attempt_at || '\u2014'), full.next_attempt_at ? line('Next attempt', full.next_attempt_at) : null,
    line('Result', describeDelivery(full) + (full.duration_ms !== null ? ` in ${full.duration_ms} ms` : '')), full.response_excerpt ? line('Response', full.response_excerpt) : null,
    h('div', { class: 'fieldset-title', text: 'Payload' }), h('pre', { class: 'code', text: JSON.stringify(full.payload, null, 2) })),
  footer: [h('button', { class: 'btn primary', onclick: () => m.close() }, 'Close')] });
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
  const sel = opts.selection; // { selected: Set of task keys, onChange() } turns on the bulk-action checkboxes
  const th = (label) => {
    const sortable = opts.sort && opts.sortable && opts.sortable[label];
    if (!sortable) return h('th', { text: label });
    const active = opts.sort.replace('-', '') === sortable;
    return h('th', { class: 'sortable', tabindex: 0, 'aria-sort': active ? (opts.sort.startsWith('-') ? 'descending' : 'ascending') : 'none',
      onclick: () => opts.onSort(sortable), onkeydown: (e) => { if (e.key === 'Enter') opts.onSort(sortable); } }, label, active ? (opts.sort.startsWith('-') ? ' \u25BE' : ' \u25B4') : '');
  };
  if (!tasks.length) return h('div', { class: 'empty', text: 'No tasks match.' });
  let table;
  let selectAll = null;
  const syncSelectAll = () => {
    if (!selectAll) return;
    const chosen = tasks.filter((t) => sel.selected.has(t.key)).length;
    selectAll.checked = chosen === tasks.length;
    selectAll.indeterminate = chosen > 0 && chosen < tasks.length;
  };
  if (sel) {
    selectAll = h('input', { type: 'checkbox', class: 'select-all', 'aria-label': 'Select all tasks on this page', onchange: (e) => {
      for (const t of tasks) { if (e.target.checked) sel.selected.add(t.key); else sel.selected.delete(t.key); }
      for (const box of table.querySelectorAll('input.row-check')) box.checked = e.target.checked;
      sel.onChange();
    } });
  }
  const pick = (t) => h('td', { class: 'pick', onclick: (e) => e.stopPropagation() },
    h('input', { type: 'checkbox', class: 'row-check', 'aria-label': 'Select ' + t.key, checked: sel.selected.has(t.key), onchange: (e) => {
      if (e.target.checked) sel.selected.add(t.key); else sel.selected.delete(t.key);
      syncSelectAll(); sel.onChange();
    } }));
  table = h('table', { class: 'table' }, h('thead', {}, h('tr', {}, sel ? h('th', { class: 'pick' }, selectAll) : null, heads.map(th))), h('tbody', {}, tasks.map((t) => h('tr', { class: 'click', tabindex: 0,
    onclick: () => openTask(t.key), onkeydown: (e) => { if (e.key === 'Enter' && e.target === e.currentTarget) openTask(t.key); } },
    sel ? pick(t) : null,
    h('td', { class: 'nowrap' }, h('strong', { text: t.key })),
    h('td', {}, t.title, t.parent ? h('span', { class: 'chip', style: { marginLeft: '6px' }, text: 'sub of ' + t.parent }) : null,
      t.blocked_by_open ? h('span', { class: 'chip', style: { marginLeft: '6px' }, title: 'Waiting on open tasks', text: '\u{1F512} ' + t.blocked_by_open }) : null,
      t.checklist.total ? h('span', { class: 'chip', style: { marginLeft: '6px' }, title: 'Checklist items done', text: `\u2713 ${t.checklist.done}/${t.checklist.total}` }) : null,
      opts.late && opts.late[t.key] ? h('span', { style: { marginLeft: '6px' } }, lateChip(opts.late[t.key])) : null,
      t.labels.map((l) => h('span', { class: 'chip', style: { marginLeft: '6px' }, text: l }))),
    h('td', {}, statusBadge(t.status)), h('td', {}, priorityBadge(t.priority)),
    h('td', {}, h('div', { class: 'row' }, avatar(t.assignee), t.assignee ? h('span', { class: 'small', text: t.assignee.display_name }) : null)),
    h('td', { class: 'nowrap' }, dueText(t) || '\u2014'),
    h('td', { class: 'nowrap small', text: `${fmtHours(t.estimate_hours)} / ${fmtHours(t.logged_hours || null)}` })))));
  syncSelectAll();
  return table;
}

/* ============================== project view ============================== */
const TABS = [['board', 'Board'], ['list', 'List'], ['timeline', 'Timeline'], ['milestones', 'Milestones'], ['files', 'Files'], ['reports', 'Reports'], ['activity', 'Activity'], ['settings', 'Settings']];

async function projectView(view, key, tab, alive) {
    const [project, summary, fields, forecast] = await Promise.all([api('/projects/' + key), api(`/projects/${key}/summary`), api('/project-fields'),
      api(`/projects/${key}/projection`).catch(() => null)]); // the project still opens if the projection cannot be computed
  const late = {}; // task key -> the projection of a task that will miss its due date
  const setLate = (f) => { for (const k of Object.keys(late)) delete late[k]; for (const t of (f && f.tasks) || []) if (t.late_by_days) late[t.key] = t; };
  setLate(forecast);
  state.projectCache[key] = project;
  state.fields = fields.items; // another admin may have changed the definitions since sign-in
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
        customFieldSummary(project),
        s.health.reasons.length ? h('ul', { class: 'reasons' }, s.health.reasons.map((r) => h('li', { text: r }))) : null),
      h('div', { style: { minWidth: '220px' } }, h('div', { class: 'row' }, progressBar(s.progress.tasks_pct, 'ok'), h('strong', { text: `${Math.round(s.progress.tasks_pct)}%` })),
        h('div', { class: 'small muted', text: `${m.done}/${m.leaf_tasks} done \u00b7 ${m.overdue} overdue \u00b7 ${m.blocked} waiting/blocked` })),
      canWrite(project) ? h('button', { class: 'btn primary', onclick: () => openCreateTask(project) }, '+ New task') : null));
  };
  renderHeader(summary);
  const refreshHeader = async () => {
    const [s, f] = await Promise.all([api(`/projects/${key}/summary`), api(`/projects/${key}/projection`).catch(() => null)]);
    setLate(f);
    renderHeader(s);
  };
  const body = h('div');
  view.append(headerBox, h('nav', { class: 'tabs', 'aria-label': 'Project sections' }, TABS.map(([id, label]) => h('a', { href: `#/p/${key}/${id}`, class: id === tab ? 'active' : '', 'aria-current': id === tab ? 'page' : null }, label))), body);
  const tabs = { board: boardTab, list: listTab, timeline: timelineTab, milestones: milestonesTab, files: filesTab, reports: reportsTab, activity: activityTab, settings: settingsTab };
  await tabs[tab](body, { project, summary, key, refreshHeader, alive, late });
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
        t.checklist.total ? h('span', { title: 'Checklist items done', text: `\u2713 ${t.checklist.done}/${t.checklist.total}` }) : null,
        t.blocked_by_open ? h('span', { title: 'Waiting on open tasks', text: `\u{1F512} ${t.blocked_by_open}` }) : null,
        ctx.late[t.key] && !['done', 'cancelled'].includes(t.status) ? lateChip(ctx.late[t.key]) : null,
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
      await ctx.refreshHeader(); // the projection of everything else may have changed too
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
  const editable = canWrite(project);
  const selected = new Set(); // task keys ticked for a bulk action; survives paging and sorting, cleared by any filter change
  const milestones = editable ? (await api(`/projects/${key}/milestones`)).items : [];
  const results = h('div', { class: 'card flush' });
  const pager = h('div', { class: 'row small muted', style: { marginTop: '10px' } });
  const bulk = editable ? bulkBar(selected, { project, milestones, onDone: () => load() }) : null;
  async function load() {
    const data = await api('/tasks', { query: { project: key, status: f.status, assignee: f.assignee, priority: f.priority, q: f.q, open: f.open ? 'true' : '', sort: f.sort, limit: LIMIT, offset: f.offset } });
    if (!ctx.alive()) return;
    clear(results);
    results.append(taskTable(data.items, { late: ctx.late, sort: f.sort, onSort: (field) => { f.sort = f.sort === field ? '-' + field : field; f.offset = 0; load(); },
      sortable: { Key: 'key', Title: 'title', Status: 'status', Priority: 'priority', Assignee: 'assignee', Due: 'due_date' },
      selection: bulk ? { selected, onChange: bulk.update } : null }));
    if (bulk) bulk.update();
    clear(pager);
    const end = Math.min(f.offset + LIMIT, data.total);
    pager.append(h('span', { text: data.total ? `${f.offset + 1}\u2013${end} of ${data.total}` : '0 tasks' }), h('span', { class: 'spacer' }),
      h('button', { class: 'btn small', disabled: f.offset === 0, onclick: () => { f.offset = Math.max(0, f.offset - LIMIT); load(); } }, '\u2039 Prev'),
      h('button', { class: 'btn small', disabled: end >= data.total, onclick: () => { f.offset += LIMIT; load(); } }, 'Next \u203A'));
  }
  const refilter = () => { f.offset = 0; selected.clear(); load(); };
  const set = (name, transform) => (e) => { f[name] = transform ? transform(e) : e.target.value; refilter(); };
  let timer;
  root.append(h('div', { class: 'toolbar' },
    h('input', { type: 'search', placeholder: 'Search title, description or key\u2026', 'aria-label': 'Search', oninput: (e) => { clearTimeout(timer); const v = e.target.value; timer = setTimeout(() => { f.q = v; refilter(); }, 250); } }),
    h('select', { 'aria-label': 'Status', onchange: set('status') }, optionList([['', 'Any status'], ...state.meta.statuses.map((s) => [s, STATUS_LABEL[s]])])),
    h('select', { 'aria-label': 'Priority', onchange: set('priority') }, optionList([['', 'Any priority'], ...state.meta.priorities.map((p) => [p, PRIORITY_LABEL[p]])])),
    h('select', { 'aria-label': 'Assignee', onchange: set('assignee') }, optionList([['', 'Anyone'], ['me', 'Me'], ['none', 'Unassigned'], ...userPairs(project.members)])),
    h('label', { class: 'row small' }, h('input', { type: 'checkbox', onchange: set('open', (e) => e.target.checked) }), 'Open only'),
    h('span', { class: 'spacer' }),
    h('button', { class: 'btn', onclick: () => downloadCsv(key) }, 'Export CSV')), bulk ? bulk.el : null, results, pager);
  await load();
}

/* ---------- bulk actions (List tab) ---------- */
const BULK_FIELDS = [['status', 'Status'], ['priority', 'Priority'], ['assignee', 'Assignee'], ['milestone', 'Milestone'], ['due_date', 'Due date'], ['add_labels', 'Add label'], ['remove_labels', 'Remove label']];
function bulkBar(selected, { project, milestones, onDone }) {
  const count = h('strong', { 'aria-live': 'polite' });
  const fieldSel = h('select', { 'aria-label': 'Field to change', onchange: () => buildValue() }, optionList(BULK_FIELDS));
  const valueBox = h('span', { class: 'row' });
  let value;
  function buildValue() {
    clear(valueBox);
    const kind = fieldSel.value;
    if (kind === 'status') value = h('select', { 'aria-label': 'New status' }, optionList(state.meta.statuses.map((x) => [x, STATUS_LABEL[x]])));
    else if (kind === 'priority') value = h('select', { 'aria-label': 'New priority' }, optionList(state.meta.priorities.map((x) => [x, PRIORITY_LABEL[x]])));
    else if (kind === 'assignee') value = h('select', { 'aria-label': 'New assignee' }, optionList(userPairs(project.members, 'Unassigned')));
    else if (kind === 'milestone') value = h('select', { 'aria-label': 'New milestone' }, optionList([['', 'No milestone'], ...milestones.map((m) => [String(m.id), m.name])]));
    else if (kind === 'due_date') value = h('input', { type: 'date', 'aria-label': 'New due date', title: 'Leave empty to clear the due date' });
    else value = h('input', { type: 'text', placeholder: 'label, label\u2026', 'aria-label': kind === 'add_labels' ? 'Labels to add' : 'Labels to remove', maxlength: 200 });
    put(valueBox, value, kind === 'due_date' ? h('span', { class: 'small muted', text: 'empty clears it' }) : null);
  }
  function changes() {
    const kind = fieldSel.value, v = value.value.trim();
    if (kind === 'assignee') return { assignee: v === '' ? null : v };
    if (kind === 'milestone') return { milestone: v === '' ? null : Number(v) };
    if (kind === 'due_date') return { due_date: v === '' ? null : v };
    if (kind === 'add_labels' || kind === 'remove_labels') {
      const labels = v.split(/[,\s]+/).filter(Boolean);
      if (!labels.length) throw new Error('Type at least one label');
      return { [kind]: labels };
    }
    return { [kind]: v };
  }
  const apply = h('button', { class: 'btn primary small', onclick: () => guarded(apply, async () => {
    const body = { tasks: [...selected], changes: changes() };
    const res = await api('/tasks/bulk', { method: 'POST', body });
    selected.clear();
    toast(`${plural(res.updated, 'task')} updated${res.unchanged ? `, ${res.unchanged} already up to date` : ''}${res.failed ? `, ${res.failed} refused` : ''}`, res.failed ? 'error' : 'ok');
    if (res.failed) bulkReport(res);
    await onDone();
  }) }, 'Apply');
  const el = h('div', { class: 'bulk-bar hidden', role: 'region', 'aria-label': 'Bulk actions' }, count, h('span', { class: 'small muted', text: 'Change' }), fieldSel, valueBox, apply,
    h('button', { class: 'btn ghost small', onclick: () => { selected.clear(); for (const box of $$('input.row-check, input.select-all', el.parentNode)) { box.checked = false; box.indeterminate = false; } update(); } }, 'Clear selection'));
  function update() {
    el.classList.toggle('hidden', selected.size === 0);
    count.textContent = `${selected.size} selected`;
  }
  buildValue();
  return { el, update };
}
// Tasks the server refused are listed with the reason, so nothing fails silently.
function bulkReport(res) {
  const rows = res.results.filter((r) => r.result === 'failed');
  const m = openModal({ title: `${plural(rows.length, 'task')} could not be changed`, narrow: true,
    content: h('div', { class: 'stack' }, h('p', { class: 'small muted', text: 'The other tasks were saved.' }),
      rows.map((r) => h('div', { class: 'item' }, h('strong', { class: 'nowrap', text: r.key }), h('span', { class: 'grow small', text: new ApiError(409, r.error).message })))),
    footer: [h('button', { class: 'btn primary', onclick: () => m.close() }, 'OK')] });
}
async function downloadFile(path, filename) {
  try {
    const blob = await api(path, { blob: true });
    const url = URL.createObjectURL(blob);
    const a = h('a', { href: url, download: filename });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  } catch (e) { toast(e.message, 'error'); }
}
function downloadCsv(key) { return downloadFile(`/projects/${key}/export.csv`, `${key}-tasks.csv`); }
function downloadMsProject(key) { return downloadFile(`/projects/${key}/export/msproject`, `${key}-msproject.xml`); }

/* ---------- timeline ---------- */
const DEP_STATES = ['ok', 'holding', 'conflict'];
const depSummary = (d) => `${DEP_TYPE_SHORT[d.type]}${d.lag_days ? ' \u00b7 ' + lagText(d.lag_days) : ''}`;
// The route of one dependency arrow, from the blocker's bar (a) to the blocked task's bar (b); both are {l, r, cy}.
function depPath(type, a, b, rowH) {
  const stub = 7;
  if (type === 'start_to_start') return `M${a.l} ${a.cy} H${Math.min(a.l, b.l) - stub} V${b.cy} H${b.l}`;
  if (type === 'finish_to_finish') return `M${a.r} ${a.cy} H${Math.max(a.r, b.r) + stub} V${b.cy} H${b.r}`;
  if (b.l >= a.r + 2 * stub) return `M${a.r} ${a.cy} H${a.r + stub} V${b.cy} H${b.l}`;
  // The blocked bar starts before the blocker's ends (or too close to turn in): go round, through the gap between rows.
  const gapY = a.cy + (b.cy > a.cy ? 1 : -1) * rowH / 2;
  return `M${a.r} ${a.cy} H${a.r + stub} V${gapY} H${b.l - stub} V${b.cy} H${b.l}`;
}
async function timelineTab(root, ctx) {
  const { project, key } = ctx;
  const [tasksResp, ms, cp, depResp] = await Promise.all([api('/tasks', { query: { project: key, limit: 200, sort: 'due_date,key', leaf: 'true' } }),
    api(`/projects/${key}/milestones`), api(`/projects/${key}/critical-path`), api(`/projects/${key}/dependencies`).catch(() => null)]);
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
  const svg = sv('svg', { class: 'chart', width, height, viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': 'Project timeline' },
    sv('defs', {}, DEP_STATES.map((st) => sv('marker', { id: `tl-head-${st}`, viewBox: '0 0 8 8', refX: 7, refY: 4, markerWidth: 8, markerHeight: 8, markerUnits: 'userSpaceOnUse', orient: 'auto' },
      sv('path', { d: 'M0 0 L8 4 L0 8 z', class: `tl-head ${st}` })))));
  // Dependencies: what each task waits for / holds up (for the tooltips), and which arrows touch which task (for the highlight on hover).
  const deps = depResp ? depResp.items : [];
  const open = (status) => status !== 'done' && status !== 'cancelled';
  const depState = (d) => (d.schedule && d.schedule.conflict && open(d.blocked.status) ? 'conflict' : d.holding ? 'holding' : 'ok');
  const push = (map, k, v) => { if (!map.has(k)) map.set(k, []); map.get(k).push(v); };
  const waitsFor = new Map(), holdsUp = new Map(), arrowsOf = new Map(), geo = new Map();
  for (const d of deps) { push(waitsFor, d.blocked.key, `${d.blocker.key} (${depSummary(d)})`); push(holdsUp, d.blocker.key, `${d.blocked.key} (${depSummary(d)})`); }
  const emphasise = (taskKey, on) => {
    const own = arrowsOf.get(taskKey) || [];
    svg.classList.toggle('dep-focus', on && own.length > 0);
    for (const arrow of own) arrow.classList.toggle('hl', on);
  };
  const depLines = (k) => [waitsFor.has(k) ? `\nWaits for: ${waitsFor.get(k).join(', ')}` : '', holdsUp.has(k) ? `\nHolds up: ${holdsUp.get(k).join(', ')}` : ''].join('');
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
    geo.set(t.key, { l: bx, r: bx + bw, cy: y + rowH / 2 });
    const label = `${t.key}  ${t.title}`;
    const row = sv('g', { class: 'tl-row', tabindex: 0, role: 'button', 'aria-label': `${t.key} ${t.title}, ${STATUS_LABEL[t.status]}`, onclick: () => openTask(t.key), onkeydown: (ev) => { if (ev.key === 'Enter') openTask(t.key); },
      onmouseenter: () => emphasise(t.key, true), onmouseleave: () => emphasise(t.key, false), onfocus: () => emphasise(t.key, true), onblur: () => emphasise(t.key, false) },
      sv('rect', { class: 'hit', x: 0, y, width, height: rowH }),
      sv('text', { x: 6, y: y + 19, class: 'tl-label' }, label.length > 40 ? label.slice(0, 39) + '\u2026' : label),
      sv('rect', { class: `tl-bar ${t.status} ${critical.has(t.key) ? 'critical' : ''}`, x: bx, y: y + 6, width: bw, height: rowH - 12, rx: 5 },
        sv('title', {}, `${t.key} ${t.title}\n${STATUS_LABEL[t.status]} \u00b7 ${t.assignee ? t.assignee.display_name : 'unassigned'}\n${fmtDate(t.start_date)} \u2192 ${fmtDate(t.due_date)}${critical.has(t.key) ? '\nOn the critical path' : ''}${depLines(t.key)}`)));
    svg.append(row);
  });
  // The arrows go on top of the rows but never take a click or a hover away from them.
  const arrows = sv('g', { class: 'tl-deps', 'aria-hidden': 'true' });
  for (const d of deps) {
    const a = geo.get(d.blocker.key), b = geo.get(d.blocked.key);
    if (!a || !b) continue;
    const st = depState(d);
    const arrow = sv('path', { class: `tl-dep ${st}`, d: depPath(d.type, a, b, rowH), 'marker-end': `url(#tl-head-${st})`,
      'data-from': d.blocker.key, 'data-to': d.blocked.key, 'data-type': d.type, 'data-state': st });
    arrows.append(arrow);
    push(arrowsOf, d.blocker.key, arrow); push(arrowsOf, d.blocked.key, arrow);
  }
  svg.append(arrows);
  const drawn = arrows.childNodes.length, conflicts = deps.filter((d) => depState(d) === 'conflict');
  const count = (n) => (n === 1 ? '1 dependency' : `${n} dependencies`);
  const plannedVerb = (d) => (d.schedule.applies_to === 'start' ? 'start' : 'finish');
  const conflictList = !conflicts.length ? null : h('div', { class: 'section', id: 'timeline-conflicts' }, h('h3', { text: 'Dates that break a dependency' }),
    conflicts.map((d) => h('div', { class: 'item' }, h('div', { class: 'grow' },
      h('a', { href: '#', onclick: (e) => { e.preventDefault(); openTask(d.blocked.key); } }, h('strong', { text: d.blocked.key }), ' ', d.blocked.title),
      ` is planned to ${plannedVerb(d)} on ${fmtDate(plannedVerb(d) === 'start' ? d.blocked.start_date : d.blocked.due_date)}, but `,
      h('a', { href: '#', onclick: (e) => { e.preventDefault(); openTask(d.blocker.key); } }, h('strong', { text: d.blocker.key }), ' ', d.blocker.title),
      ` (${DEP_TYPE_LABEL[d.type]}${d.lag_days ? ', ' + lagText(d.lag_days) : ''}) allows it no earlier than ${fmtDate(d.schedule.not_before)}.`))));
  const depNote = depResp === null ? 'The dependencies could not be loaded, so no arrows are drawn.'
    : !deps.length ? null
    : `${count(drawn)} drawn${deps.length > drawn ? `; ${count(deps.length - drawn)} not drawn because a task is not on the timeline` : ''}${depResp.truncated ? '; the project has more dependencies than one answer carries' : ''}.`;
  root.append(h('div', { class: 'card' }, h('div', { class: 'row wrap', style: { marginBottom: '10px' } }, h('h2', { text: 'Timeline' }), h('span', { class: 'spacer' }),
    drawn ? h('label', { class: 'row small' }, h('input', { type: 'checkbox', checked: true, onchange: (e) => svg.classList.toggle('no-deps', !e.target.checked) }), 'Dependency arrows') : null,
    h('span', { class: 'legend' }, h('span', {}, h('span', { class: 'sw s-in_progress' }), 'In progress'), h('span', {}, h('span', { class: 'sw s-done' }), 'Done'), h('span', {}, h('span', { class: 'sw s-todo' }), 'To do'),
      h('span', {}, h('span', { class: 'sw s-blocked' }), 'Blocked'), h('span', {}, '\u25C6 Milestone'), h('span', {}, 'Red outline = critical path'),
      drawn ? [h('span', {}, h('span', { class: 'sw-line ok' }), 'Dependency'), h('span', {}, h('span', { class: 'sw-line holding' }), 'Holding a task back'), h('span', {}, h('span', { class: 'sw-line conflict' }), 'Dates break it')] : null)),
    h('div', { class: 'timeline-wrap' }, svg),
    depNote ? h('p', { class: 'small muted', id: 'timeline-dep-note', style: { marginTop: '10px' }, text: depNote }) : null,
    unscheduled.length ? h('p', { class: 'small muted', style: { marginTop: '10px' }, text: `${plural(unscheduled.length, 'task')} without dates are not shown: ${unscheduled.slice(0, 8).map((t) => t.key).join(', ')}${unscheduled.length > 8 ? '\u2026' : ''}` }) : null,
    conflictList));
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
    projectionCard(ctx),
    h('div', { class: 'grid kpis', style: { marginTop: '16px' } }, kpi('Complete', `${Math.round(summary.progress.tasks_pct)}%`), kpi('Open tasks', m.open), kpi('Overdue', m.overdue, m.overdue ? 'bad' : ''), kpi('Due within 7 days', m.due_soon),
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
function projectionCard(ctx) {
  const { key } = ctx;
  const card = h('div', { class: 'card', id: 'projection' }, h('h2', { text: 'Delivery projection' }), h('p', { class: 'muted', text: 'Calculating\u2026' }));
  const assume = { person: '', capacity: '100', overrun: '0', reassign: '' }; // reassign: "KEY-1:username", set by previewing a suggestion
  let seq = 0, last = null;
  async function load() {
    const mine = ++seq;
    const query = {};
    if (assume.person && Number(assume.capacity) !== 100) query.capacity = `${assume.person}:${Number(assume.capacity)}`; // Number() turns "1e2" or ".5" into what the server expects
    if (Number(assume.overrun) > 0) query.overrun = String(Number(assume.overrun));
    if (assume.reassign) query.reassign = assume.reassign;
    try {
      const data = await api(`/projects/${key}/projection`, { query });
      if (mine === seq && ctx.alive()) { last = data; draw(data); }
    } catch (e) {
      if (mine !== seq) return;
      if (last) draw(last, e.message); // keep the last good answer and the form, and say what was wrong with the question
      else { clear(card); put(card, h('h2', { text: 'Delivery projection' }), h('p', { class: 'error-text', role: 'alert', text: e.message })); }
    }
  }
  async function loadAdvice(box, forLoad) {
    let advice;
    try { advice = await api(`/projects/${key}/projection/advice`); } catch (e) { advice = { error: e.message }; }
    if (forLoad !== seq || !ctx.alive()) return; // the question has changed since: this answer is about an older picture
    clear(box);
    if (advice.error) { put(box, h('span', { class: 'muted', text: `Suggestions are not available: ${advice.error}` })); return; }
    if (!advice.suggestions.length) { put(box, h('span', { class: 'muted', text: advice.reason || 'No suggestions.' })); return; }
    put(box, h('strong', { text: 'What would help' }), advice.suggestions.map((s) => h('div', { class: 'item suggestion' },
      h('div', { class: 'grow' }, h('div', { text: s.summary }),
        h('div', { class: 'muted', text: `${s.to.display_name} has ${fmtHours(s.to.open_hours)} of other work${s.to.busy_until ? `, the last of it ending ${fmtDate(s.to.busy_until)}` : ''}.` })),
      h('a', { href: '#/t/' + s.task, class: 'btn small', 'aria-label': `Open ${s.task}` }, 'Open'),
      h('button', { class: 'btn small', type: 'button', 'aria-label': `Preview giving ${s.task} to ${s.to.display_name}`, onclick: () => { assume.reassign = s.reassign; load(); } }, 'Preview'))),
      h('div', { class: 'muted', text: `Suggestions look only at capacity and queues, not at skills. ${advice.limited ? 'Only some reassignments were tried. ' : ''}Nothing is changed until you reassign the task yourself.` }));
  }
  function draw(data, problem) {
    clear(card);
    const people = data.people;
    const personSel = h('select', { 'aria-label': 'Person whose availability changes', value: assume.person, onchange: (e) => { assume.person = e.target.value; capacity.required = !!assume.person; } },
      optionList([['', 'Nobody in particular'], ...people.map((p) => [p.username, p.display_name])]));
    const capacity = h('input', { type: 'number', min: 0, max: 200, step: 1, required: !!assume.person, 'aria-label': 'Availability in percent', value: assume.capacity, style: { width: '84px' }, oninput: (e) => { assume.capacity = e.target.value; } });
    const overrun = h('input', { type: 'number', min: 0, max: 300, step: 1, 'aria-label': 'Estimates overrun in percent', value: assume.overrun, style: { width: '84px' }, oninput: (e) => { assume.overrun = e.target.value; } });
    const active = !!data.scenario;
    const late = data.tasks.filter((t) => t.late_by_days || t.state !== 'scheduled');
    const adviceBox = h('div', { id: 'projection-advice', class: 'small', style: { marginTop: '12px' } }, h('span', { class: 'muted', text: 'Looking for a way to fix it\u2026' }));
    const needsFix = !active && (data.counts.late_tasks > 0 || ['will_miss', 'unknown'].includes(data.verdict));
    if (needsFix) loadAdvice(adviceBox, seq);
    put(card, h('div', { class: 'row wrap' }, h('h2', { text: 'Delivery projection' }), verdictBadge(data)),
      h('p', { id: 'projection-headline', text: data.headline }),
      active ? h('p', { class: 'small', id: 'projection-scenario' }, h('strong', { text: 'With these assumptions: ' }),
        data.scenario.reassign.length ? `${data.scenario.reassign.map((m) => `${m.task} goes to ${m.to_name}`).join(', ')}; ` : '',
        data.scenario.finish_moves_by_days === null ? 'the finish cannot be compared.' : data.scenario.finish_moves_by_days === 0 ? 'the finish does not move' : `the finish moves ${data.scenario.finish_moves_by_days > 0 ? 'later' : 'earlier'} by ${plural(Math.abs(data.scenario.finish_moves_by_days), 'day')}`,
        data.scenario.extra_late_tasks ? `, and ${plural(Math.abs(data.scenario.extra_late_tasks), 'task')} ${data.scenario.extra_late_tasks > 0 ? 'more' : 'fewer'} will be late.` : '.') : null,
      late.length ? h('div', { class: 'scroll-x' }, h('table', { class: 'table', 'aria-label': 'Tasks projected to be late' }, h('thead', {}, h('tr', {}, ['Task', 'Who', 'Due', 'Projected', 'Late by', 'Why'].map((x) => h('th', { text: x })))),
        h('tbody', {}, late.slice(0, 10).map((t) => h('tr', {}, h('td', {}, h('a', { href: '#/t/' + t.key }, h('strong', { text: t.key })), ' ', t.title),
          h('td', { text: t.assignee || '\u2014' }), h('td', { class: 'nowrap', text: fmtDate(t.due_date) }), h('td', { class: 'nowrap', text: t.projected_finish ? fmtDate(t.projected_finish) : '\u2014' }),
          h('td', { class: 'nowrap overdue-text', text: t.late_by_days ? `+${t.late_by_days}d` : 'n/a' }), h('td', { class: 'small why', text: t.explanation })))))) : null,
      late.length > 10 ? h('p', { class: 'small muted', text: `\u2026and ${late.length - 10} more` }) : null,
      data.milestones.some((m) => m.late_by_days) ? h('div', { class: 'small', style: { marginTop: '8px' } }, h('strong', { text: 'Milestones at risk: ' }),
        data.milestones.filter((m) => m.late_by_days).map((m) => `${m.name} (due ${fmtDate(m.due_date)}, projected ${fmtDate(m.projected_finish)})`).join('; ')) : null,
      needsFix ? adviceBox : null,
      people.length ? h('div', { style: { marginTop: '10px' } }, h('div', { class: 'small muted', text: 'Who is carrying the work (busy until counts every project):' }),
        people.map((p) => h('div', { class: 'item' }, h('span', { class: 'grow', text: p.display_name + (p.capacity_pct !== 100 ? ` (${p.capacity_pct}% available)` : '') }),
          h('span', { class: 'small muted nowrap', text: `${fmtHours(p.open_hours)} open \u00b7 ${p.hours_per_day}h a day` }),
          h('span', { class: 'small nowrap ' + (p.late_tasks ? 'overdue-text' : ''), text: p.busy_until ? `busy until ${fmtDate(p.busy_until)}` : '' })))) : null,
      problem ? h('p', { class: 'error-text', role: 'alert', id: 'projection-error', text: problem }) : null,
      h('form', { class: 'row wrap whatif', style: { marginTop: '12px' }, onsubmit: (e) => { e.preventDefault(); load(); } }, h('strong', { class: 'small', text: 'What if\u2026' }),
        personSel, h('span', { class: 'small', text: 'is available' }), capacity, h('span', { class: 'small', text: '% and estimates overrun by' }), overrun, h('span', { class: 'small', text: '%' }),
        h('button', { class: 'btn small primary', type: 'submit' }, 'Recalculate'),
        h('button', { class: 'btn small', type: 'button', onclick: () => { Object.assign(assume, { person: '', capacity: '100', overrun: '0', reassign: '' }); load(); } }, 'Reset')),
      h('details', { class: 'small', style: { marginTop: '10px' } }, h('summary', { text: 'How this is worked out' }), h('ul', { class: 'reasons' }, data.assumptions.notes.map((n) => h('li', { text: n })),
        h('li', { text: `Work days: ${data.assumptions.work_days}.` }))));
  }
  load();
  return card;
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
  const name = (f) => (f.startsWith('custom_fields.') ? fieldLabel(f.slice('custom_fields.'.length)) : f.replace('_', ' '));
  const change = (c) => Object.entries(c || {}).map(([f, [o, n]]) => `${name(f)}: ${Array.isArray(o) ? o.join(', ') || 'none' : o ?? 'none'} \u2192 ${Array.isArray(n) ? n.join(', ') || 'none' : n ?? 'none'}`).join('; ');
  switch (a.action) {
    case 'task.created': return `created ${t} \u201c${d.title}\u201d`;
    case 'task.updated': return `updated ${t} (${change(d.changes)})`;
    case 'task.status_changed': return `moved ${t}: ${change(d.changes)}`;
    case 'task.deleted': return `deleted ${t} \u201c${d.title}\u201d`;
    case 'task.dependency_added': return `made ${d.blocker} block ${d.blocked}${d.type && d.type !== 'finish_to_start' ? ` (${DEP_TYPE_LABEL[d.type]})` : ''}${d.lag_days ? ` with ${lagText(d.lag_days)}` : ''}`;
    case 'task.dependency_updated': return `changed the dependency of ${d.blocker} on ${d.blocked}: ${Object.entries(d.changes || {}).map(([f, [o, n]]) => (f === 'type' ? `${DEP_TYPE_LABEL[o]} \u2192 ${DEP_TYPE_LABEL[n]}` : `lag ${o} \u2192 ${n} days`)).join('; ')}`;
    case 'task.link_added': return `linked ${t} ${LINK_LABEL[d.type] || d.type} ${d.other}`;
    case 'task.link_removed': return `removed the link between ${t} and ${d.other}`;
    case 'checklist.added': return `added \u201c${d.text}\u201d to the checklist of ${t}`;
    case 'checklist.checked': return `ticked \u201c${d.text}\u201d on ${t}`;
    case 'checklist.unchecked': return `unticked \u201c${d.text}\u201d on ${t}`;
    case 'checklist.edited': return `changed the checklist item \u201c${d.text}\u201d on ${t} to \u201c${d.new_text}\u201d`;
    case 'checklist.moved': return `moved \u201c${d.text}\u201d to position ${d.position} on the checklist of ${t}`;
    case 'checklist.removed': return `removed \u201c${d.text}\u201d from the checklist of ${t}`;
    case 'watcher.added': return d.self ? `started watching ${t}` : `added ${d.user} as a watcher of ${t}`;
    case 'watcher.removed': return d.self ? `stopped watching ${t}` : `removed ${d.user} from the watchers of ${t}`;
    case 'task.dependency_removed': return `removed the dependency between ${t} and ${d.other}`;
    case 'comment.added': return `commented on ${t}${d.mentions && d.mentions.length ? ' (mentioned ' + d.mentions.join(', ') + ')' : ''}`;
    case 'comment.edited': return `edited a comment on ${t}`;
    case 'comment.deleted': return `deleted a comment on ${t}`;
    case 'time.logged': return `logged ${d.hours}h on ${t} for ${d.work_date}`;
    case 'time.deleted': return `removed a ${d.hours}h time entry from ${t}`;
    case 'attachment.added': return `attached \u201c${d.filename}\u201d (${fmtBytes(d.size)}) to ${t}`;
    case 'attachment.deleted': return `removed the attachment \u201c${d.filename}\u201d from ${t}`;
    case 'milestone.created': return `added milestone \u201c${d.name}\u201d due ${d.due_date}`;
    case 'milestone.updated': return `updated a milestone (${change(d.changes)})`;
    case 'milestone.deleted': return `deleted milestone \u201c${d.name}\u201d`;
    case 'project.created': return `created the project`;
    case 'project.updated': return `updated the project (${change(d.changes)})`;
    case 'project_field.created': return `defined the ${FIELD_TYPE_LABEL[d.type] || ''} project field \u201c${d.label}\u201d`;
    case 'project_field.updated': return `changed the project field \u201c${fieldLabel(d.key)}\u201d (${change(d.changes)})`;
    case 'project_field.deleted': return `deleted the project field \u201c${d.label}\u201d (removed ${plural(d.values_removed, 'value')})`;
    case 'project.member_added': return `added ${d.user} to the project`;
    case 'project.member_removed': return `removed ${d.user} from the project`;
    case 'import.completed': return `imported tasks from ${d.source === 'msproject' ? 'Microsoft Project' : 'CSV'}: ${d.created} created, ${d.skipped_duplicates} skipped, ${d.errors} errors`;
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
  const cf = state.fields.length ? customFieldsEditor(project.custom_fields, { creating: false, disabled: !manage }) : null;
  const save = h('button', { class: 'btn primary', onclick: () => guarded(save, async () => {
    if (cf && !controlsValid(cf.controls)) return;
    const body = { name: inputs.name.value, status: inputs.status.value, start_date: inputs.start_date.value || null, due_date: inputs.due_date.value || null, owner: inputs.owner.value || null, description: inputs.description.value };
    const changed = cf ? collectCustom(cf.controls, project.custom_fields) : {};
    if (Object.keys(changed).length) body.custom_fields = changed;
    await api('/projects/' + key, { method: 'PATCH', body });
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
      field('Description', inputs.description),
      cf ? h('div', { class: 'stack' }, h('div', { class: 'cf-heading', text: 'Custom fields' }), cf.node)
        : hasRole('admin') ? h('p', { class: 'small muted' }, 'No custom fields yet. ', h('a', { href: '#/fields', text: 'Define some' }), ' to track extra details such as a client, a budget or a wiki link.') : null,
      manage ? h('div', {}, save) : h('p', { class: 'small muted', text: 'Only managers and admins can edit project details.' })),
    h('div', { class: 'card' }, h('h2', { text: 'Members' }), h('p', { class: 'small muted', text: 'Members can be assigned work and can edit tasks in this project.' }), memberList,
      manage ? h('div', { class: 'row', style: { marginTop: '10px' } }, addSel, h('button', { class: 'btn', onclick: (e) => guarded(e.target, async () => { if (!addSel.value) return; Object.assign(project, await api(`/projects/${key}/members`, { method: 'POST', body: { username: addSel.value } })); route(); }) }, 'Add')) : null)),
    manage ? importCard(key) : null,
    msprojectCard(key, manage),
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
function msprojectCard(key, manage) {
  const file = h('input', { type: 'file', accept: '.xml,application/xml,text/xml', 'aria-label': 'Microsoft Project XML file' });
  const dry = h('input', { type: 'checkbox', checked: true });
  const addMem = h('input', { type: 'checkbox' });
  const out = h('div', { class: 'stack small', style: { marginTop: '10px' } });
  const notes = (items, title, cls) => items.length ? h('div', {}, h('strong', { class: cls, text: title }),
    items.slice(0, 20).map((e) => h('div', { class: cls === 'error-text' ? '' : 'muted', text: e.uid === null || e.uid === undefined ? e.message : `UID ${e.uid}: ${e.message}` })),
    items.length > 20 ? h('div', { class: 'muted', text: `\u2026and ${items.length - 20} more` }) : null) : null;
  const run = h('button', { class: 'btn primary', onclick: () => guarded(run, async () => {
    if (!file.files[0]) { toast('Choose a Microsoft Project XML file first', 'error'); return; }
    const data = await file.files[0].arrayBuffer(); // the bytes as they are: the file says which encoding it uses
    const r = await api(`/projects/${key}/import/msproject`, { method: 'POST', body: data, contentType: 'application/xml', query: { dry_run: dry.checked ? 'true' : '', add_missing_members: addMem.checked ? 'true' : '' } });
    clear(out);
    out.append(h('div', {}, h('strong', { text: r.dry_run ? 'Dry run \u2014 nothing was saved. ' : 'Import finished. ' }),
      `${r.tasks_read} tasks read: ${r.created} ${r.dry_run ? 'would be created' : 'created'}, ${r.skipped_duplicates} already here, ${plural(r.milestones, 'milestone')}, ${r.links} ${r.links === 1 ? 'dependency' : 'dependencies'}, ${plural(r.errors.length, 'error')}, ${plural(r.warnings.length, 'warning')}.`),
      notes(r.errors, 'Errors', 'error-text'), notes(r.warnings, 'Warnings', ''));
    if (!r.dry_run && r.created) { toast(`${plural(r.created, 'task')} imported`, 'ok'); }
  }) }, 'Run Microsoft Project import');
  return h('div', { class: 'card', style: { marginTop: '16px' } }, h('h2', { text: 'Microsoft Project' }),
    h('p', { class: 'small muted', text: 'Exports the tasks, subtasks, milestones, dependencies, estimates, dates and assignees as a Microsoft Project XML file (the MSPDI format, not .mpp). Import adds what a file of that kind contains and never changes tasks that are already here.' }),
    h('div', { class: 'row wrap' }, h('button', { class: 'btn', onclick: () => downloadMsProject(key) }, 'Export for Microsoft Project')),
    manage ? h('div', { class: 'stack', style: { marginTop: '14px' } }, h('div', { class: 'cf-heading', text: 'Import from Microsoft Project' }),
      h('div', { class: 'row wrap' }, file, h('label', { class: 'row small' }, dry, 'Dry run (check only, save nothing)'), h('label', { class: 'row small' }, addMem, 'Add missing people as members'), run), out) : null);
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
  const cf = state.fields.length ? customFieldsEditor(null, { creating: true }) : null;
  const checks = state.users.filter((u) => u.username !== state.me.username).map((u) => [u, h('input', { type: 'checkbox' })]);
  const create = h('button', { class: 'btn primary', onclick: () => guarded(create, async () => {
    if (cf && !controlsValid(cf.controls)) return;
    const body = { key: f.key.value, name: f.name.value, status: f.status.value, description: f.description.value, members: checks.filter(([, c]) => c.checked).map(([u]) => u.username) };
    for (const k of ['start_date', 'due_date']) if (f[k].value) body[k] = f[k].value;
    const custom = cf ? collectCustom(cf.controls, null) : {};
    if (Object.keys(custom).length) body.custom_fields = custom;
    const p = await api('/projects', { method: 'POST', body });
    m.close(); await refreshProjects(); location.hash = '#/p/' + p.key; toast(`Project ${p.key} created`, 'ok');
  }) }, 'Create project');
  const m = openModal({ title: 'New project', content: h('div', { class: 'stack' }, h('div', { class: 'form-grid' }, field('Key', f.key, '2\u201310 letters or digits; prefixes task keys'), field('Name', f.name), field('Status', f.status), field('Start date', f.start_date), field('Due date', f.due_date)),
    field('Description', f.description),
    cf ? h('div', { class: 'stack' }, h('div', { class: 'cf-heading', text: 'Custom fields' }), cf.node) : null,
    h('div', {}, h('div', { class: 'small muted', text: 'Members (you are added automatically)' }), h('div', { class: 'row wrap' }, checks.map(([u, c]) => h('label', { class: 'row small' }, c, u.display_name))))),
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
  let dirty = false, comments = [], time = [], acts = [], files = { items: [], total_bytes: 0, limits: null }, uploading = false, uploadNote = '', forecast = null;
  // Typed-but-unsent text survives the re-render that follows every saved field change.
  const drafts = { description: null, comment: '', subtask: '', depKey: '', depDir: 'blocked_by', depType: 'finish_to_start', depLag: '', linkKey: '', linkType: 'relates_to', checkText: '', watcher: '', hours: '', day: state.today, note: '' };
  const track = (name) => (e) => { drafts[name] = e.target.value; };
  const body = h('div');
  const handle = openModal({ title: null, content: body, onClose: () => { stopAccepting(); if (dirty) route(); } });
  handle.el.setAttribute('aria-label', `${task.key} ${task.title}`);
  const stopAccepting = acceptFiles({ isTop: () => modalStack[modalStack.length - 1] === handle, canAttach: editable,
    onFiles: (list) => (editable ? attach(list) : toast('You have read-only access to this task, so files cannot be attached.', 'error')) });

  let loadSeq = 0;
  async function loadAll() {
    const mine = ++loadSeq;
    const fresh = await Promise.all([api('/tasks/' + key), api(`/tasks/${key}/comments`).then((r) => r.items), api(`/tasks/${key}/time`).then((r) => r.items), api(`/tasks/${key}/activity`, { query: { limit: 15 } }).then((r) => r.items),
        api(`/tasks/${key}/attachments`),
        api(`/projects/${task.project}/projection`).then((r) => r.tasks.find((t) => t.key === key) || null).catch(() => null)]); // the dialog still works without it
      if (mine !== loadSeq) return; // a newer load is in flight: an older response must never overwrite newer state
      [task, comments, time, acts, files, forecast] = fresh;
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
      watchersBox(),
      hasRole('manager') && project.status !== 'archived' ? h('button', { class: 'btn danger small', onclick: act(deleteTask) }, 'Delete task') : null);

    put(body,
      h('div', { class: 'row', style: { marginBottom: '6px' } }, h('strong', { class: 'muted', text: task.key }), badge(task.status, STATUS_LABEL[task.status]), task.is_overdue ? badge('overdue', 'Overdue') : null,
        task.blocked_by_open ? badge('blocked', `Waiting on ${task.blocked_by_open}`) : null, h('span', { class: 'spacer' }), h('button', { class: 'btn ghost', 'aria-label': 'Close', onclick: () => handle.close() }, '\u2715')),
      project.status === 'archived' ? h('p', { class: 'small muted', text: 'This project is archived, so its tasks are read-only.' }) : !editable ? h('p', { class: 'small muted', text: 'You have read-only access to this task.' }) : null,
      h('div', { class: 'task-layout' }, h('div', {}, title, h('div', { class: 'stack', style: { marginTop: '8px' } }, desc, saveDesc),
        checklistSection(), attachmentsSection(), projectionSection(), subtasksSection(), dependenciesSection(), linksSection(), timeSection(), commentsSection(), activitySection()), side));
  }

  async function switchTo(otherKey) { handle.close(); openTask(otherKey); }
  async function deleteTask() {
    if (!(await confirmBox(`Delete ${task.key} permanently? Its comments, time, attachments and history go with it.`, 'Delete task', true))) return;
    try { await api('/tasks/' + key, { method: 'DELETE' }); }
    catch (e) {
      if (e.code === 'has_subtasks' && (await confirmBox(`${e.message} Delete the subtasks too?`, 'Delete all', true))) await api('/tasks/' + key, { method: 'DELETE', query: { cascade: 'true' } });
      else throw e;
    }
    dirty = true; handle.close(); toast(`${task.key} deleted`, 'ok');
  }

  function projectionSection() {
    if (!forecast || task.status === 'done' || task.status === 'cancelled') return null;
    const f = forecast;
    const when = f.projected_finish ? `Projected to finish ${fmtDay(f.projected_finish)}` : 'Cannot be projected';
    const verdict = f.late_by_days ? h('span', { class: 'overdue-text', text: ` \u2014 ${plural(f.late_by_days, 'day')} after its due date` })
      : f.margin_days !== null && f.projected_finish ? h('span', { class: 'muted', text: f.margin_days === 0 ? ' \u2014 on its due date' : ` \u2014 ${plural(f.margin_days, 'day')} before its due date` }) : null;
    return h('div', { class: 'section', id: 'projection-section' }, h('h3', { text: 'Projection' }),
      h('div', {}, h('strong', { text: when }), verdict), h('p', { class: 'small', text: f.explanation }),
      f.notes.length ? h('ul', { class: 'reasons' }, f.notes.map((n) => h('li', { text: n }))) : null);
  }

  function subtasksSection() {
    const input = h('input', { type: 'text', placeholder: 'Add a subtask\u2026', maxlength: 200, 'aria-label': 'New subtask title', value: drafts.subtask, oninput: track('subtask') });
    const add = h('button', { class: 'btn small', onclick: act(async () => { if (!input.value.trim()) return; await api(`/projects/${task.project}/tasks`, { method: 'POST', body: { title: input.value.trim(), parent: key } }); drafts.subtask = ''; await reload(); }) }, 'Add');
    return h('div', { class: 'section' }, h('h3', { text: `Subtasks ${task.subtasks.total ? `(${task.subtasks.done}/${task.subtasks.total} done)` : ''}` }),
      task.subtask_list.map((s) => h('div', { class: 'item' }, statusBadge(s.status), h('a', { href: '#', class: 'grow', onclick: (e) => { e.preventDefault(); switchTo(s.key); } }, h('strong', { text: s.key }), ' ', s.title), h('span', { class: 'small muted', text: s.assignee || '' }))),
      editable ? h('div', { class: 'row', style: { marginTop: '6px' } }, input, add) : null);
  }
  function depLabel(d) {
    const lag = lagText(d.lag_days);
    return `${DEP_TYPE_SHORT[d.type]}${lag ? ' \u00b7 ' + lag : ''}`;
  }
  function dependenciesSection() {
    const input = h('input', { type: 'text', placeholder: 'Task key, e.g. ' + task.project + '-1', 'aria-label': 'Related task key', value: drafts.depKey, oninput: track('depKey') });
    const dir = h('select', { 'aria-label': 'Relationship', value: drafts.depDir, onchange: track('depDir') }, optionList([['blocked_by', 'is blocked by'], ['blocks', 'blocks']]));
    const type = h('select', { 'aria-label': 'Dependency type', title: DEP_TYPE_HELP[drafts.depType], value: drafts.depType, onchange: (e) => { drafts.depType = e.target.value; e.target.title = DEP_TYPE_HELP[drafts.depType]; } },
      optionList(state.meta.dependency_types.map((t) => [t, DEP_TYPE_LABEL[t]])));
    const lag = h('input', { type: 'number', step: 1, min: -state.meta.max_lag_days, max: state.meta.max_lag_days, placeholder: 'lag (days)', 'aria-label': 'Lag in days (negative for lead)', title: 'Days to wait after the blocker (negative: may overlap by that many days)', style: { width: '110px' }, value: drafts.depLag, oninput: track('depLag') });
    const add = h('button', { class: 'btn small', onclick: act(async () => {
      if (!input.value.trim()) return;
      const body = { [dir.value]: input.value.trim().toUpperCase(), type: type.value };
      if (lag.value !== '') body.lag_days = Number(lag.value);
      await api(`/tasks/${key}/dependencies`, { method: 'POST', body });
      drafts.depKey = ''; drafts.depLag = ''; await reload();
    }) }, 'Link');
    const rows = (list, label, mine) => list.map((d) => {
      const conflict = d.schedule && d.schedule.conflict;
      const when = d.schedule ? `${d.schedule.applies_to === 'start' ? 'start' : 'finish'} no earlier than ${fmtDate(d.schedule.not_before)}` : '';
      return h('div', { class: 'item' }, h('span', { class: 'small muted nowrap', text: label }),
        h('span', { class: 'chip nowrap', title: `${DEP_TYPE_LABEL[d.type]}. ${DEP_TYPE_HELP[d.type]}`, text: depLabel(d) }),
        statusBadge(d.status), h('a', { href: '#', class: 'grow', onclick: (e) => { e.preventDefault(); switchTo(d.key); } }, h('strong', { text: d.key }), ' ', d.title),
        conflict ? h('span', { class: 'badge overdue', title: `${mine ? 'This task is' : d.key + ' is'} planned before the dependency allows: should ${when}`, text: '\u26A0 dates' }) : null,
        editable ? h('button', { class: 'btn ghost small', 'aria-label': `Edit dependency on ${d.key}`, onclick: () => editDependency(d) }, 'Edit') : null,
        editable ? h('button', { class: 'btn ghost small', 'aria-label': `Remove link to ${d.key}`, onclick: act(async () => { await api(`/tasks/${key}/dependencies/${d.key}`, { method: 'DELETE' }); await reload(); }) }, '\u2715') : null);
    });
    return h('div', { class: 'section' }, h('h3', { text: 'Dependencies' }), rows(task.blocked_by, 'blocked by', true), rows(task.blocks, 'blocks', false),
      !task.blocked_by.length && !task.blocks.length ? h('div', { class: 'small muted', text: 'No dependencies.' }) : null,
      editable ? h('div', { class: 'form-row', style: { marginTop: '6px' } }, dir, input, type, lag, add) : null);
  }
  function editDependency(d) {
    const type = h('select', { 'aria-label': 'Dependency type', value: d.type }, optionList(state.meta.dependency_types.map((t) => [t, DEP_TYPE_LABEL[t]])));
    const lag = h('input', { type: 'number', step: 1, min: -state.meta.max_lag_days, max: state.meta.max_lag_days, value: d.lag_days, 'aria-label': 'Lag in days' });
    const save = h('button', { class: 'btn primary', onclick: () => guarded(save, async () => {
      await api(`/tasks/${key}/dependencies/${d.key}`, { method: 'PATCH', body: { type: type.value, lag_days: lag.value === '' ? 0 : Number(lag.value) } });
      m.close(); await reload();
    }) }, 'Save');
    const m = openModal({ title: `Dependency on ${d.key}`, narrow: true, content: h('div', { class: 'stack' },
      field('Type', type, 'Finish \u2192 Start is the usual one: the blocked task waits for the blocker to finish.'),
      field('Lag (days)', lag, 'Positive: wait this many days after. Negative: may overlap by that many days. Dates are only checked, never moved.')),
      footer: [h('button', { class: 'btn', onclick: () => m.close() }, 'Cancel'), save] });
  }
  function linksSection() {
    const input = h('input', { type: 'text', placeholder: 'Task key, e.g. ' + task.project + '-2', 'aria-label': 'Task to link', value: drafts.linkKey, oninput: track('linkKey') });
    const type = h('select', { 'aria-label': 'Link type', value: drafts.linkType, onchange: track('linkType') }, optionList(state.meta.link_types.map((t) => [t, LINK_LABEL[t]])));
    const add = h('button', { class: 'btn small', onclick: act(async () => {
      if (!input.value.trim()) return;
      await api(`/tasks/${key}/links`, { method: 'POST', body: { type: type.value, task: input.value.trim().toUpperCase() } });
      drafts.linkKey = ''; await reload();
    }) }, 'Add relation');
    return h('div', { class: 'section' }, h('h3', { text: 'Related tasks' }),
      task.links.map((l) => h('div', { class: 'item' }, h('span', { class: 'small muted nowrap', text: LINK_LABEL[l.type] }), statusBadge(l.task.status),
        h('a', { href: '#', class: 'grow', onclick: (e) => { e.preventDefault(); switchTo(l.task.key); } }, h('strong', { text: l.task.key }), ' ', l.task.title),
        editable ? h('button', { class: 'btn ghost small', 'aria-label': `Remove relation to ${l.task.key}`, onclick: act(async () => { await api(`/tasks/${key}/links/${l.task.key}`, { method: 'DELETE' }); await reload(); }) }, '\u2715') : null)),
      !task.links.length ? h('div', { class: 'small muted', text: 'Nothing related. These links are for reference only; they never block work.' }) : null,
      editable ? h('div', { class: 'form-row', style: { marginTop: '6px' } }, type, input, add) : null);
  }
  function checklistSection() {
    const items = task.checklist_items;
    const done = items.filter((i) => i.done).length;
    const input = h('input', { type: 'text', placeholder: 'Add a checklist item\u2026', maxlength: state.meta.max_checklist_text, 'aria-label': 'New checklist item', value: drafts.checkText, oninput: track('checkText'),
      onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); add.click(); } } });
    const add = h('button', { class: 'btn small', onclick: act(async () => {
      if (!input.value.trim()) return;
      await api(`/tasks/${key}/checklist`, { method: 'POST', body: { text: input.value.trim() } });
      drafts.checkText = ''; await reload();
      const next = $('input[aria-label="New checklist item"]', body); // ready for the next item
      if (next) next.focus();
    }) }, 'Add');
    const row = (i, index) => h('div', { class: 'item check-item' + (i.done ? ' done' : '') },
      h('input', { type: 'checkbox', checked: i.done, disabled: !editable, 'aria-label': i.text, title: i.done && i.done_by ? `Done by ${i.done_by.display_name} ${fmtAgo(i.done_at)}` : null, onchange: async (e) => {
        const box = e.currentTarget;
        box.disabled = true;
        try { await api('/checklist/' + i.id, { method: 'PATCH', body: { done: box.checked } }); await reload(); } catch (err) { toast(err.message, 'error'); box.checked = !box.checked; box.disabled = false; }
      } }),
      h('span', { class: 'grow check-text', text: i.text }),
      editable ? h('span', { class: 'row', style: { gap: '2px' } },
        h('button', { class: 'btn ghost small', 'aria-label': `Move ${i.text} up`, disabled: index === 0, onclick: act(async () => { await api('/checklist/' + i.id, { method: 'PATCH', body: { position: index } }); await reload(); }) }, '\u25B2'),
        h('button', { class: 'btn ghost small', 'aria-label': `Move ${i.text} down`, disabled: index === items.length - 1, onclick: act(async () => { await api('/checklist/' + i.id, { method: 'PATCH', body: { position: index + 2 } }); await reload(); }) }, '\u25BC'),
        h('button', { class: 'btn ghost small', 'aria-label': `Edit ${i.text}`, onclick: () => editChecklistItem(i) }, 'Edit'),
        h('button', { class: 'btn ghost small', 'aria-label': `Delete ${i.text}`, onclick: act(async () => { await api('/checklist/' + i.id, { method: 'DELETE' }); await reload(); }) }, '\u2715')) : null);
    return h('div', { class: 'section' }, h('h3', { text: `Checklist${items.length ? ` (${done}/${items.length})` : ''}` }),
      items.length ? h('div', { class: 'row', style: { marginBottom: '4px' } }, progressBar((100 * done) / items.length, done === items.length ? 'ok' : '')) : null,
      items.map(row),
      !items.length && !editable ? h('div', { class: 'small muted', text: 'No checklist items.' }) : null,
      editable ? h('div', { class: 'row', style: { marginTop: '6px' } }, input, add) : null);
  }
  function editChecklistItem(i) {
    const area = h('input', { type: 'text', value: i.text, maxlength: state.meta.max_checklist_text, 'aria-label': 'Checklist item text' });
    const save = h('button', { class: 'btn primary', onclick: () => guarded(save, async () => { await api('/checklist/' + i.id, { method: 'PATCH', body: { text: area.value } }); m.close(); await reload(); }) }, 'Save');
    const m = openModal({ title: 'Edit checklist item', narrow: true, content: area, footer: [h('button', { class: 'btn', onclick: () => m.close() }, 'Cancel'), save] });
  }
  function watchersBox() {
    const mine = task.watching;
    const others = state.users.filter((u) => !task.watchers.some((w) => w.username === u.username));
    const toggle = h('button', { class: 'btn small' + (mine ? '' : ' primary'), 'aria-pressed': String(mine), disabled: !mine && project.status === 'archived',
      onclick: act(async () => {
        if (mine) await api(`/tasks/${key}/watchers/${state.me.username}`, { method: 'DELETE' });
        else await api(`/tasks/${key}/watchers`, { method: 'POST', body: {} });
        await reload();
      }) }, mine ? 'Stop watching' : 'Watch');
    const picker = h('select', { 'aria-label': 'Add a watcher', value: drafts.watcher, onchange: track('watcher') }, optionList([['', 'Add someone\u2026'], ...userPairs(others)]));
    const addOther = h('button', { class: 'btn small', onclick: act(async () => { if (!picker.value) return; await api(`/tasks/${key}/watchers`, { method: 'POST', body: { username: picker.value } }); drafts.watcher = ''; await reload(); }) }, 'Add');
    return h('div', { class: 'watch-box' }, h('div', { class: 'row' }, h('strong', { class: 'small', text: `Watchers (${task.watchers.length})` }), h('span', { class: 'spacer' }), toggle),
      task.watchers.length ? task.watchers.map((w) => h('div', { class: 'row small' }, avatar(w), h('span', { class: 'grow ellipsis', text: w.display_name }),
        editable || w.username === state.me.username ? h('button', { class: 'btn ghost small', 'aria-label': `Remove ${w.display_name} from watchers`, onclick: act(async () => { await api(`/tasks/${key}/watchers/${w.username}`, { method: 'DELETE' }); await reload(); }) }, '\u2715') : null))
        : h('div', { class: 'small muted', text: 'Nobody is following this task.' }),
      editable ? h('div', { class: 'row' }, picker, addOther) : null);
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
  async function attach(list) {
    if (uploading) { toast('Wait for the current upload to finish.', 'error'); return; }
    uploading = true; uploadNote = 'Starting\u2026'; render();
    try {
      const sent = await uploadFiles(key, list, files.limits, (text) => { uploadNote = text; const el = $('[data-upload-status]', body); if (el) el.textContent = text; });
      if (sent) toast(`${plural(sent, 'file')} attached`, 'ok');
    } finally { uploading = false; uploadNote = ''; await reload(); }
  }
  function attachmentsSection() {
    const picker = h('input', { type: 'file', multiple: true, class: 'hidden', 'aria-label': 'Choose files to attach', onchange: (e) => { const chosen = [...e.target.files]; e.target.value = ''; if (chosen.length) attach(chosen); } });
    const size = files.items.length ? ` (${files.items.length} \u00b7 ${fmtBytes(files.total_bytes)})` : '';
    return h('div', { class: 'section', id: 'attachments' }, h('h3', { text: `Attachments${size}` }),
      files.items.map((a) => attachmentItem(a, project, reload)),
      !files.items.length ? h('div', { class: 'small muted', text: editable ? 'No files yet. Drop them anywhere on this window, paste a screenshot, or choose files.' : 'No attachments.' }) : null,
      editable ? h('div', { class: 'row', style: { marginTop: '6px' } }, h('button', { class: 'btn small', disabled: uploading, onclick: () => picker.click() }, 'Attach files'), picker,
        h('span', { class: 'small muted', role: 'status', 'aria-live': 'polite', dataset: { uploadStatus: '' }, text: uploading ? uploadNote : '' })) : null);
  }
  function activitySection() {
    return h('div', { class: 'section' }, h('h3', { text: 'History' }), acts.length ? acts.map(activityItem) : h('div', { class: 'small muted', text: 'No history yet.' }));
  }

  await loadAll();
}

/* ============================== attachments ============================== */
// Files are only ever handed to the browser to save (always as an octet-stream), or - for the few image types the server
// has verified by their bytes - shown through a data: URL in an <img>. Nothing from a file is ever rendered as a page.
function fmtBytes(n) {
  if (n < 1024) return `${n} B`;
  const units = ['KB', 'MB', 'GB'];
  let v = n / 1024, i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v >= 100 || Number.isInteger(v) ? Math.round(v) : v.toFixed(1)} ${units[i]}`;
}
const fileExt = (name) => { const m = /\.([A-Za-z0-9]{1,5})$/.exec(name); return m ? m[1].toUpperCase() : 'FILE'; };
const canDeleteAttachment = (a, project) => canWrite(project) && (hasRole('manager') || (a.uploaded_by && a.uploaded_by.username === state.me.username));

async function downloadAttachment(a) {
  try {
    const stored = await api(`/attachments/${a.id}/content`, { blob: true });
    const url = URL.createObjectURL(new Blob([stored], { type: 'application/octet-stream' })); // never a type a browser would render
    const link = h('a', { href: url, download: a.filename });
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 2000);
  } catch (e) { toast(e.message, 'error'); }
}
async function previewAttachment(a) {
  const note = h('p', { class: 'muted small', text: 'Loading preview\u2026' });
  const box = h('div', { class: 'preview-box' }, note);
  const m = openModal({ title: a.filename, content: box, footer: [h('button', { class: 'btn', onclick: () => downloadAttachment(a) }, 'Download'), h('button', { class: 'btn primary', onclick: () => m.close() }, 'Close')] });
  try {
    const stored = await api(`/attachments/${a.id}/content`, { blob: true });
    const typed = new Blob([await stored.arrayBuffer()], { type: a.preview }); // the type the server verified, not the one the file claimed
    const url = await new Promise((resolve, reject) => { const r = new FileReader(); r.onload = () => resolve(r.result); r.onerror = reject; r.readAsDataURL(typed); });
    clear(box);
    box.append(h('img', { src: url, alt: a.filename, class: 'preview-img' }));
  } catch (e) { note.textContent = e.message; }
}
async function deleteAttachment(a, button, onChange) {
  if (!(await confirmBox(`Delete ${a.filename} from ${a.task}? This cannot be undone.`, 'Delete', true))) return;
  await guarded(button, async () => { await api('/attachments/' + a.id, { method: 'DELETE' }); toast('Attachment deleted', 'ok'); await onChange(); });
}
function attachmentItem(a, project, onChange) {
  const who = a.uploaded_by ? a.uploaded_by.display_name : 'unknown';
  return h('div', { class: 'item attachment' }, h('span', { class: 'ext', text: fileExt(a.filename) }),
    h('div', { class: 'grow' }, h('div', { class: 'file-name', title: a.filename, text: a.filename }), h('div', { class: 'small muted', text: `${fmtBytes(a.size)} \u00b7 ${who} \u00b7 ${fmtAgo(a.created_at)}` })),
    a.preview ? h('button', { class: 'btn ghost small', 'aria-label': `Preview ${a.filename}`, onclick: () => previewAttachment(a) }, 'Preview') : null,
    h('button', { class: 'btn ghost small', 'aria-label': `Download ${a.filename}`, onclick: () => downloadAttachment(a) }, 'Download'),
    canDeleteAttachment(a, project) ? h('button', { class: 'btn ghost small', 'aria-label': `Delete ${a.filename}`, onclick: (e) => deleteAttachment(a, e.currentTarget, onChange) }, '\u2715') : null);
}
// One at a time: the server holds a whole file in memory while it stores it, and sequential requests keep the order obvious.
async function uploadFiles(key, list, limits, progress) {
  const max = limits ? limits.max_file_bytes : Infinity;
  let sent = 0;
  for (const [i, file] of list.entries()) {
    progress(`Uploading ${i + 1} of ${list.length}: ${file.name}\u2026`);
    if (file.size === 0) { toast(`${file.name} is empty, so it was not attached.`, 'error'); continue; }
    if (file.size > max) { toast(`${file.name} is ${fmtBytes(file.size)}; files can be at most ${fmtBytes(max)}.`, 'error'); continue; }
    try {
      await api(`/tasks/${key}/attachments`, { method: 'POST', body: file, contentType: file.type || 'application/octet-stream', query: { filename: file.name } });
      sent++;
    } catch (e) { toast(`${file.name}: ${e.message}`, 'error'); }
  }
  return sent;
}
// A screenshot pasted from the clipboard arrives as "image.png"; give it a name that tells one from another.
function pastedName(file) {
  if (file.name && !/^(image|blob|untitled)(\.\w+)?$/i.test(file.name)) return file;
  const ext = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/gif': 'gif', 'image/webp': 'webp' }[file.type] || 'bin';
  const d = new Date(), pad = (n) => String(n).padStart(2, '0');
  return new File([file], `pasted-${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}.${ext}`, { type: file.type });
}
// While a task dialog is the top-most window, files dropped or pasted anywhere go to that task. A file dropped on a page the app does not
// handle would make the browser leave the app and open it, so every file drag is claimed - a read-only dialog explains instead of attaching.
function acceptFiles({ isTop, canAttach, onFiles }) {
  let depth = 0, idle = null; // dragenter/dragleave also fire for every element the pointer crosses, and a missed one must not leave the overlay stuck
  const carriesFiles = (e) => e.dataTransfer && [...e.dataTransfer.types].includes('Files');
  const claims = () => isTop() && canAttach;
  const highlight = (on) => { document.body.classList.toggle('dropping', on); clearTimeout(idle); if (!on) depth = 0; };
  const listeners = {
    dragenter: (e) => { if (!carriesFiles(e)) return; e.preventDefault(); if (claims()) depth++; },
    dragover: (e) => {
      if (!carriesFiles(e)) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = claims() ? 'copy' : 'none';
      if (claims()) { highlight(true); idle = setTimeout(() => highlight(false), 1000); } // dragover repeats while the pointer is over the page; silence means the drag is over
    },
    dragleave: (e) => { if (!carriesFiles(e)) return; depth = Math.max(0, depth - 1); if (!depth) highlight(false); },
    drop: (e) => {
      if (!carriesFiles(e)) return;
      e.preventDefault(); highlight(false);
      if (isTop() && e.dataTransfer.files.length) onFiles([...e.dataTransfer.files]);
    },
    paste: (e) => {
      const list = isTop() && e.clipboardData ? [...e.clipboardData.files] : [];
      if (!list.length) return; // ordinary text pastes untouched
      e.preventDefault();
      onFiles(list.map(pastedName));
    },
  };
  for (const [type, fn] of Object.entries(listeners)) document.addEventListener(type, fn);
  return () => { for (const [type, fn] of Object.entries(listeners)) document.removeEventListener(type, fn); highlight(false); };
}

/* ---------- the project's Files tab ---------- */
async function filesTab(root, ctx) {
  const { project, key } = ctx;
  const LIMIT = 25;
  const f = { q: '', offset: 0 };
  const results = h('div', { class: 'card flush' });
  const summary = h('span', { class: 'small muted' });
  const pager = h('div', { class: 'row small muted', style: { marginTop: '10px' } });
  async function load() {
    const data = await api(`/projects/${key}/attachments`, { query: { q: f.q, limit: LIMIT, offset: f.offset } });
    if (!ctx.alive()) return;
    summary.textContent = `${plural(data.total, 'file')} \u00b7 ${fmtBytes(data.total_bytes)}${f.q ? ' matching the search' : ' in this project'}`;
    clear(results);
    if (!data.items.length) results.append(h('div', { class: 'empty', text: f.q ? 'No file names match.' : 'No files yet. Open a task and attach screenshots, specs or logs.' }));
    else results.append(h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ['File', 'Task', 'Size', 'Attached by', 'When', ''].map((x) => h('th', { text: x })))),
      h('tbody', {}, data.items.map((a) => h('tr', {},
        h('td', {}, h('div', { class: 'row attachment' }, h('span', { class: 'ext', text: fileExt(a.filename) }), h('span', { class: 'file-name', title: a.filename, text: a.filename }))),
        h('td', {}, h('a', { href: '#/t/' + a.task, title: a.task_title, onclick: (e) => { e.preventDefault(); openTask(a.task); } }, h('strong', { text: a.task }), ' ', h('span', { class: 'muted small', text: a.task_title }))),
        h('td', { class: 'nowrap', text: fmtBytes(a.size) }),
        h('td', {}, h('div', { class: 'row' }, avatar(a.uploaded_by), a.uploaded_by ? h('span', { class: 'small nowrap', text: a.uploaded_by.display_name }) : null)),
        h('td', { class: 'nowrap small muted', text: fmtAgo(a.created_at) }),
        h('td', { class: 'nowrap right' },
          a.preview ? h('button', { class: 'btn ghost small', 'aria-label': `Preview ${a.filename}`, onclick: () => previewAttachment(a) }, 'Preview') : null,
          h('button', { class: 'btn ghost small', 'aria-label': `Download ${a.filename}`, onclick: () => downloadAttachment(a) }, 'Download'),
          canDeleteAttachment(a, project) ? h('button', { class: 'btn ghost small', 'aria-label': `Delete ${a.filename}`, onclick: (e) => deleteAttachment(a, e.currentTarget, load) }, '\u2715') : null))))));
    clear(pager);
    const end = Math.min(f.offset + LIMIT, data.total);
    if (data.total > LIMIT) pager.append(h('span', { text: `${f.offset + 1}\u2013${end} of ${data.total}` }), h('span', { class: 'spacer' }),
      h('button', { class: 'btn small', disabled: f.offset === 0, onclick: () => { f.offset = Math.max(0, f.offset - LIMIT); load(); } }, '\u2039 Prev'),
      h('button', { class: 'btn small', disabled: end >= data.total, onclick: () => { f.offset += LIMIT; load(); } }, 'Next \u203A'));
  }
  let timer;
  root.append(h('div', { class: 'toolbar' },
    h('input', { type: 'search', placeholder: 'Search file names\u2026', 'aria-label': 'Search file names', oninput: (e) => { clearTimeout(timer); const v = e.target.value; timer = setTimeout(() => { f.q = v.trim(); f.offset = 0; load(); }, 250); } }),
    h('span', { class: 'spacer' }), summary), results, pager);
  await load();
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

/* ============================== admin: project fields ============================== */
async function fieldsView(view, alive) {
  const data = await api('/project-fields');
  if (!alive()) return;
  state.fields = data.items;
  clear(view);
  const full = data.items.length >= state.meta.project_field_limits.max_fields;
  view.append(h('div', { class: 'page-head' }, h('div', { class: 'titles' }, h('h1', { text: 'Project fields' }),
    h('p', { class: 'muted', text: 'Extra details every project can carry, such as a client, a budget or a wiki link. Managers fill them in on the project\u2019s Settings tab.' })),
    h('button', { class: 'btn primary', disabled: full, title: full ? `At most ${state.meta.project_field_limits.max_fields} fields` : null, onclick: () => openFieldDialog(null) }, '+ New field')),
    data.items.length
      ? h('div', { class: 'card flush' }, h('table', { class: 'table' }, h('thead', {}, h('tr', {}, ['Field', 'Key', 'Type', 'Rules', 'Required', ''].map((x) => h('th', { text: x })))),
        h('tbody', {}, data.items.map((f) => h('tr', {},
          h('td', {}, h('strong', { text: f.label }), f.description ? h('div', { class: 'small muted', text: f.description }) : null),
          h('td', {}, h('code', { text: f.key })), h('td', {}, h('span', { class: 'chip', text: FIELD_TYPE_LABEL[f.type] })), h('td', { class: 'small', text: fieldRules(f) }),
          h('td', {}, f.required ? badge('active', 'Required') : h('span', { class: 'muted', text: '\u2014' })),
          h('td', { class: 'right nowrap' }, h('button', { class: 'btn small', onclick: () => openFieldDialog(f) }, 'Edit'), ' ', h('button', { class: 'btn small danger', onclick: () => deleteFieldDialog(f) }, 'Delete')))))))
      : h('div', { class: 'card empty' }, h('h2', { text: 'No project fields yet' }), h('p', { text: 'Add a text, number or URL field and it appears on every project.' })));
}
async function fieldsChanged(message) {
  state.fields = (await api('/project-fields')).items;
  state.projectCache = {};
  toast(message, 'ok');
  route();
}
const slugify = (text) => text.toLowerCase().normalize('NFKD').replace(/[^a-z0-9]+/g, '_').replace(/^[^a-z]+/, '').replace(/_+$/, '').slice(0, 40);

function openFieldDialog(def) {
  const editing = !!def;
  const limits = state.meta.project_field_limits;
  const f = {
    label: h('input', { type: 'text', maxlength: 50, required: true, value: editing ? def.label : '', placeholder: 'e.g. Client' }),
    key: h('input', { type: 'text', maxlength: 40, required: true, disabled: editing, value: editing ? def.key : '', pattern: '[a-z][a-z0-9_]{1,39}', title: 'Lower-case letters, digits and underscores, starting with a letter' }),
    type: h('select', { value: editing ? def.type : 'text', disabled: editing }, optionList(state.meta.project_field_types.map((t) => [t, FIELD_TYPE_LABEL[t]]))),
    description: h('input', { type: 'text', maxlength: 200, value: editing ? def.description : '', placeholder: 'Help text shown under the input' }),
    required: h('input', { type: 'checkbox', checked: editing && def.required }),
    max_length: h('input', { type: 'number', min: 1, max: limits.max_text_length, step: 1, value: editing && def.max_length !== null ? def.max_length : limits.default_max_length }),
    decimals: h('select', { value: String(editing && def.decimals !== null ? def.decimals : limits.default_decimals) }, optionList(Array.from({ length: limits.max_decimals + 1 }, (_, n) => [String(n), String(n)]))),
    min_value: h('input', { type: 'number', step: 'any', value: editing && def.min_value !== null ? def.min_value : '' }),
    max_value: h('input', { type: 'number', step: 'any', value: editing && def.max_value !== null ? def.max_value : '' }),
  };
  let keyEdited = false;
  f.key.addEventListener('input', () => { keyEdited = true; });
  f.label.addEventListener('input', () => { if (!editing && !keyEdited) f.key.value = slugify(f.label.value); });
  const panels = {
    text: h('div', { class: 'form-grid' }, field('Maximum length', f.max_length, `1\u2013${limits.max_text_length} characters`)),
    number: h('div', { class: 'form-grid' }, field('Decimal places', f.decimals), field('Minimum (optional)', f.min_value), field('Maximum (optional)', f.max_value)),
    url: h('p', { class: 'small muted', text: 'Only http:// and https:// addresses are accepted. They open in a new tab.' }),
  };
  const sync = () => { for (const [type, panel] of Object.entries(panels)) panel.classList.toggle('hidden', type !== f.type.value); };
  f.type.addEventListener('change', sync);
  sync();

  const save = h('button', { class: 'btn primary', onclick: () => guarded(save, async () => {
    const type = f.type.value;
    const check = [f.label, f.key, ...(type === 'text' ? [f.max_length] : []), ...(type === 'number' ? [f.min_value, f.max_value] : [])];
    if (!check.every((el) => el.disabled || el.reportValidity())) return;
    const body = { label: f.label.value.trim(), description: f.description.value.trim(), required: f.required.checked };
    if (!editing) Object.assign(body, { key: f.key.value, type });
    if (type === 'text') body.max_length = Number(f.max_length.value);
    if (type === 'number') {
      Object.assign(body, { decimals: Number(f.decimals.value), min_value: f.min_value.value === '' ? null : Number(f.min_value.value), max_value: f.max_value.value === '' ? null : Number(f.max_value.value) });
      if (body.min_value !== null && body.max_value !== null && body.min_value > body.max_value) { toast('The minimum cannot be greater than the maximum', 'error'); return; }
    }
    await api(editing ? '/project-fields/' + def.key : '/project-fields', { method: editing ? 'PATCH' : 'POST', body });
    m.close();
    await fieldsChanged(editing ? 'Field saved' : 'Field created');
  }) }, editing ? 'Save changes' : 'Create field');
  const m = openModal({ title: editing ? `Edit \u201c${def.label}\u201d` : 'New project field', content: h('div', { class: 'stack' },
    h('div', { class: 'form-grid' }, field('Label', f.label), field('Key', f.key, editing ? 'Cannot be changed' : 'Used by the API; letters, digits, underscores'), field('Type', f.type, editing ? 'Cannot be changed' : null)),
    panels.text, panels.number, panels.url, field('Description (optional)', f.description),
    h('label', { class: 'row small' }, f.required, 'Required when creating a project (existing projects are not affected)')), footer: [h('button', { class: 'btn', onclick: () => m.close() }, 'Cancel'), save] });
}
function deleteFieldDialog(def) {
  const input = h('input', { type: 'text', placeholder: def.key, 'aria-label': 'Type the field key to confirm', autocomplete: 'off' });
  const go = h('button', { class: 'btn danger', disabled: true, onclick: () => guarded(go, async () => {
    await api('/project-fields/' + def.key, { method: 'DELETE', query: { confirm: def.key } });
    m.close();
    await fieldsChanged('Field deleted');
  }) }, 'Delete forever');
  input.addEventListener('input', () => { go.disabled = input.value !== def.key; });
  const m = openModal({ title: `Delete \u201c${def.label}\u201d?`, narrow: true, content: h('div', { class: 'stack' },
    h('p', { text: `This removes the field and its value from every project. It cannot be undone. Type ${def.key} to confirm.` }), input), footer: [h('button', { class: 'btn', onclick: () => m.close() }, 'Cancel'), go] });
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
