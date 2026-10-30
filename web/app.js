/* Shipment Doc Reconciler — vanilla JS, no build step, no dependencies.
 *
 * Security note: every string that originates from an uploaded document
 * (raw_value, evidence_text, reason, filenames) or from the model is inserted
 * with textContent, never interpolated into innerHTML. A scanned page can hold
 * arbitrary text and this demo is public. Keep it that way.
 *
 * Second note: nothing here stores an API key. The server holds it in memory
 * and only ever returns a mask, so the drawer's inputs are write-only.
 */
'use strict';

const $ = (sel) => document.querySelector(sel);

const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = String(text);
  return n;
};

const DOC_TYPES = [
  ['', 'auto-detect'],
  ['commercial_invoice', 'Commercial invoice'],
  ['packing_list', 'Packing list'],
  ['bill_of_lading', 'Bill of lading'],
  ['certificate_of_origin', 'Certificate of origin'],
  ['other', 'Other'],
];

/**
 * Field labels come from the report. The server owns the field contract
 * (app/fields.py) and sends a label with every comparison; a copy of that list
 * here went stale the day the set grew past six, and the UI would have shown
 * raw snake_case keys for whatever came next.
 */
function fieldLabel(report, key) {
  const comp = (report.comparisons || []).find((c) => c.field === key);
  return comp ? comp.label : key.replace(/_/g, ' ');
}

const ICON = {
  consistent: '✓', suspicious: '⚠', conflict: '✗',
  single_source: '•', absent: '–',
};

// Word for word the server's VERDICT_HEADLINE in app/export.py, which a test
// pins: the web app and the filed HTML must not describe one shipment two ways.
// Descriptive only -- the banner below is the one line that tells the reader
// what to do, because it is the one that has read every stage.
const HEADLINE = {
  clean: 'All cross-checked fields agree',
  review: 'No hard conflicts — a few fields deserve a glance',
  conflicts: 'Conflicts found in the field table',
  running: 'Extracting…',
  empty: 'Nothing to reconcile yet',
};

const STATUS_FILTERS = [
  ['conflict', 'conflicts'],
  ['suspicious', 'suspicious'],
  ['consistent', 'consistent'],
  ['single_source', 'single source'],
];

const PROVIDERS = [['nvidia', 'NVIDIA'], ['nebius', 'Nebius']];
const THEMES = [['system', 'System'], ['light', 'Light'], ['dark', 'Dark']];

const state = {
  files: [],
  declared: [],
  // One state per chosen file, parallel to `files`: queued -> uploading ->
  // uploaded -> reading/verifying/comparing -> done or failed. The upload
  // states come from byte progress, the rest from SSE stage events.
  fileStates: [],
  busy: false,
  readingSince: {},
  healthResolved: false,
  shipmentId: null,
  pollTimer: null,
  stream: null,
  elapsedTimer: null,
  startedAt: null,
  showRaw: false,
  filter: null,
  report: null,
  health: null,
  config: null,
  configToken: '',
  // Per-document stage events from the SSE stream, keyed by document id. The
  // timeline renders these; they are advisory progress, never the source of
  // truth for a verdict (the report event is).
  stages: {},
  // An API key for a protected instance. Memory only, for this tab:
  // localStorage would put the thing that guards other people's shipments
  // where any script on this origin can read it.
  apiKey: '',
  reviewer: '',
  pendingCorrection: null,
  clearedKeys: new Set(),
};

// ---------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------

// ---------------------------------------------------------------------
// first paint
// ---------------------------------------------------------------------

// Long enough that the mark is seen rather than flashed, short enough that it
// never reads as a wait. The cap is the honest part: if /api/health or the
// access check is slow, the intro leaves anyway and the page speaks for itself.
const INTRO_MIN_MS = 480;
const INTRO_MAX_MS = 1300;

const intro = { el: null, shownAt: 0, done: false, health: false, access: false, until: 0 };

function startIntro() {
  const el = $('#intro');
  if (!el) return;
  // Served hidden: the server saw a recent visit and suppressed the curtain
  // before the browser had anything to paint. Nothing left to do.
  if (el.hidden) return;
  const params = new URLSearchParams(location.search);
  const calm = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  // A deep link is somebody asking for a specific report, and a calm setting is
  // an instruction: neither gets a curtain.
  if (calm || params.get('run') || params.get('demo')) {
    el.hidden = true;
    return;
  }
  intro.el = el;
  intro.shownAt = performance.now();
  intro.until = setTimeout(dismissIntro, INTRO_MAX_MS);
}

function introArrived(stage) {
  intro[stage] = true;
  if (intro.health && intro.access) dismissIntro();
}

function dismissIntro() {
  if (!intro.el || intro.done) return;
  intro.done = true;
  clearTimeout(intro.until);
  const wait = Math.max(0, INTRO_MIN_MS - (performance.now() - intro.shownAt));
  setTimeout(() => {
    intro.el.classList.add('is-out');
    setTimeout(() => { intro.el.hidden = true; }, 340);
  }, wait);
}

document.addEventListener('DOMContentLoaded', () => {
  startIntro();
  initTheme();
  wireDropzone();
  wireButtons();
  wireDrawer();

  loadHealth();

  // A protected instance answers 401 for everything data-bearing, so the boot
  // sequence asks before it loads: history, fixtures and the demo shortcut all
  // wait for the unlock that may never come.
  ensureAccess().then((granted) => {
    introArrived('access');
    if (!granted) return;
    loadFixtures();
    loadHistory();

    const params = new URLSearchParams(location.search);
    const wanted = params.get('demo');
    if (wanted) runFixture(wanted);
    else if (params.get('run')) openRun(params.get('run'));
  });
});

// ---------------------------------------------------------------------
// theme
// ---------------------------------------------------------------------

function initTheme() {
  const saved = localStorage.getItem('sdr-theme') || 'system';
  applyTheme(saved);
  buildSegmented($('#theme-seg'), THEMES, saved, (value) => {
    applyTheme(value);
    localStorage.setItem('sdr-theme', value);
  });
}

function applyTheme(pref) {
  const root = document.documentElement;
  if (pref === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', pref);
}

function buildSegmented(host, options, active, onPick) {
  host.innerHTML = '';
  options.forEach(([value, label]) => {
    const b = el('button', null, label);
    b.type = 'button';
    b.setAttribute('aria-pressed', String(value === active));
    b.addEventListener('click', () => {
      host.querySelectorAll('button').forEach((o) =>
        o.setAttribute('aria-pressed', String(o === b)));
      onPick(value);
    });
    host.appendChild(b);
  });
}

// ---------------------------------------------------------------------
// connection state (left rail + header flag)
// ---------------------------------------------------------------------

// The free Render plan spins down after 15 idle minutes and can take a minute to
// answer its first request. Four seconds of silence is the point where "working"
// stops being believable and the reader deserves the reason.
const COLD_START_MS = 4000;

function setServerState(kind, words) {
  const dot = $('#srv-dot');
  const label = $('#server-state-text');
  if (!dot || !label) return;
  dot.className = 'srv-dot ' + kind;
  const text = {
    checking: 'Checking the server…',
    ready: 'Server ready',
    waking: 'Server is waking up…',
    down: 'Server unreachable',
  };
  label.textContent = words || text[kind] || '';
}

function showColdStart(on) {
  const note = $('#cold-start');
  if (note) note.hidden = !on;
  if (on) setServerState('waking');
}

async function loadHealth() {
  const timer = setTimeout(() => {
    if (!state.healthResolved) showColdStart(true);
  }, COLD_START_MS);
  try {
    const h = await getJSON('/api/health');
    state.healthResolved = true;
    clearTimeout(timer);
    showColdStart(false);
    setServerState('ready');
    state.health = h;
    renderModeChip(h);
    renderConnection(h);
    renderKeyWarning(h);
    introArrived('health');
  } catch (err) {
    state.healthResolved = true;
    clearTimeout(timer);
    showColdStart(false);
    setServerState('down');
    const chip = $('#mode-chip');
    chip.hidden = false;
    chip.className = 'flag flag-bad';
    chip.textContent = 'cannot reach the server';
    introArrived('health');
  }
}

function renderModeChip(h) {
  const chip = $('#mode-chip');
  chip.hidden = false;
  // The cold-start banner says the one thing that matters on first load: the
  // demo is real captured model output being replayed, not a live call and not
  // a fabrication. It lives apart from the chip because the chip is a small
  // status flag a returning user skims, while this is a claim about provenance.
  const banner = $('#demo-banner');
  banner.hidden = !h.demo_mode;
  // Retention is a property of the deployment, not of the mode: an instance that
  // deletes runs after N hours has to say so before somebody builds a workflow on
  // storage that will not be there tomorrow.
  const retention = $('#retention-note');
  if (retention) {
    const hours = Number(h.run_ttl_hours || 0);
    if (hours > 0) {
      retention.textContent =
        `Uploaded documents and their reports are deleted after ${hours} h, and `
        + 'every run is lost when this instance redeploys — it has no persistent '
        + 'disk. The three sample shipments are unaffected; they cost nothing and '
        + 'can be run again.';
      retention.hidden = false;
    } else {
      retention.hidden = true;
    }
  }
  if (h.demo_mode) {
    // Read-only: demo mode is an environment decision now, not a UI toggle.
    chip.className = 'flag flag-warn';
    chip.textContent = 'demo mode — serving from cache';
  } else if (!h.vision.key_present) {
    chip.className = 'flag flag-bad';
    chip.textContent = 'live — no vision key';
  } else {
    chip.className = 'flag flag-live';
    chip.textContent = 'live — ' + h.vision.model;
  }
}

function renderKeyWarning(h) {
  const box = $('#key-warning');
  // A keyed instance answers a stranger with status and mode only, so the
  // provider block is absent until this tab holds a session. Nothing to warn
  // about in that state: the warning is about provider keys, not the API key.
  if (!h.vision) { box.hidden = true; return; }
  const show = !h.demo_mode && !h.vision.key_present;
  box.hidden = !show;
  if (!show) return;
  const needsSummary = h.summary.enabled && !h.summary.key_present;
  $('#key-warning-text').textContent = needsSummary
    ? 'Live mode is on but no vision key is set, so documents cannot be read. The ' +
      'executive summary also has no Nebius key, though that one can never fail a run.'
    : 'Live mode is on but no vision key is set, so documents cannot be read yet.';
}

function renderConnection(h) {
  const host = $('#conn-list');
  host.innerHTML = '';
  // Same reason as renderKeyWarning: no provider detail until this tab is
  // unlocked. The list comes back on the next loadHealth() after the session
  // opens, which is exactly when the names mean something.
  if (!h.vision || !h.summary) return;
  const rows = [
    ['Mode', h.demo_mode ? 'demo · cached' : 'live'],
    ['Vision', h.vision.model, h.vision.host, h.vision.key_present],
    ['Summary', h.summary.model, h.summary.host, h.summary.key_present],
  ];
  rows.forEach(([label, value, host_, present]) => {
    const row = el('div');
    row.appendChild(el('dt', null, label));
    const dd = el('dd', null, value);
    dd.title = host_ || value;
    row.appendChild(dd);
    host.appendChild(row);
    if (host_ && present !== undefined) {
      row.appendChild(el('span', 'flag flag-tiny ' + (present ? 'flag-ok' : ''),
        present ? 'key' : 'no key'));
    }
  });
}

// ---------------------------------------------------------------------
// demo shipments + run history, both straight from the API
// ---------------------------------------------------------------------

async function loadFixtures() {
  try {
    const data = await getJSON('/api/fixtures');
    const host = $('#fixture-list');
    host.innerHTML = '';
    Object.entries(data.shipments || {}).forEach(([name, entry]) => {
      const btn = el('button', 'picker');
      btn.type = 'button';
      btn.appendChild(el('strong', null, humanize(name)));
      const docs = (entry.documents || []).length;
      const pages = (entry.documents || []).reduce((n, d) => n + (d.pages || 1), 0);
      btn.appendChild(el('span', null, `${docs} document${docs === 1 ? '' : 's'} · ${pages} page${pages === 1 ? '' : 's'}`));
      btn.addEventListener('click', () => runFixture(name));
      host.appendChild(btn);
    });
    if (data.note) {
      const note = $('#fixtures-note');
      note.hidden = false;
      note.textContent = data.note;
    }
  } catch (err) {
    // The file picker still works, but silence here looked like a server with
    // no fixtures. Say what failed instead of rendering nothing.
    const note = $('#fixtures-note');
    note.hidden = false;
    note.className = 'alert';
    note.textContent = 'Could not load sample shipments: ' + err.message;
  }
}

async function loadHistory() {
  try {
    const data = await getJSON('/api/shipments');
    renderHistory(data.shipments || [], data.skipped || 0);
  } catch (err) {
    const host = $('#history');
    host.innerHTML = '';
    const p = el('p', 'run-empty alert', 'Could not load run history: ' + err.message);
    host.appendChild(p);
  }
}

function renderHistory(rows, skipped = 0) {
  const host = $('#history');
  host.innerHTML = '';
  if (skipped > 0) {
    host.appendChild(el('p', 'run-empty',
      `${skipped} run${skipped === 1 ? '' : 's'} on disk could not be read ` +
      `and ${skipped === 1 ? 'is' : 'are'} not listed.`));
  }
  if (!rows.length) {
    host.appendChild(el('p', 'run-empty',
      skipped ? 'No readable runs in this container.' : 'No runs in this container yet.'));
    return;
  }
  rows.forEach((row) => {
    const verdict = (row.summary && row.summary.verdict) || 'running';
    const btn = el('button', 'run');
    btn.type = 'button';
    btn.setAttribute('aria-current', String(row.shipment_id === state.shipmentId));
    btn.appendChild(el('span', 'dot dot-' + verdict));

    const mid = el('span');
    mid.appendChild(el('span', 'run-id', row.shipment_id));
    btn.appendChild(mid);
    btn.appendChild(el('span', 'run-when', relativeTime(row.updated_at)));
    btn.title = `${verdict} · ${row.documents} document(s)`;
    btn.addEventListener('click', () => openRun(row.shipment_id));
    host.appendChild(btn);
  });
}

function humanize(name) {
  return name.replace(/^shipment_/, '').replace(/_/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
}

function relativeTime(iso) {
  if (!iso) return '';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return mins + 'm ago';
  const hours = Math.round(mins / 60);
  if (hours < 24) return hours + 'h ago';
  return Math.round(hours / 24) + 'd ago';
}

// ---------------------------------------------------------------------
// dropzone
// ---------------------------------------------------------------------

function wireDropzone() {
  const zone = $('#dropzone');
  const input = $('#file-input');

  zone.addEventListener('click', () => input.click());
  zone.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); }
  });
  input.addEventListener('change', () => {
    addFiles(Array.from(input.files));
    input.value = '';
  });

  ['dragenter', 'dragover'].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add('drag'); }));
  ['dragleave', 'drop'].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove('drag'); }));
  zone.addEventListener('drop', (e) => {
    addFiles(Array.from(e.dataTransfer.files || []));
  });
}

function addFiles(files) {
  const room = 5 - state.files.length;
  if (room <= 0) { showError('A shipment holds at most 5 documents.'); return; }
  const accepted = files.filter((f) => /\.(pdf|jpe?g|png|webp|tiff?)$/i.test(f.name));
  const rejected = files.length - accepted.length;
  if (rejected > 0) showError(`${rejected} file(s) skipped — only PDF and images are accepted.`);
  const taken = accepted.slice(0, room);
  state.files.push(...taken);
  state.declared.push(...taken.map(() => ''));
  state.fileStates.push(...taken.map(() => 'queued'));
  renderUploads();
}

const UPLOAD_STATE = {
  queued: 'Queued',
  uploading: 'Uploading',
  uploaded: 'Uploaded',
  reading: 'Reading',
  verifying: 'Verifying',
  comparing: 'Comparing',
  done: 'Done',
  failed: 'Failed',
};

function paintFileStates() {
  document.querySelectorAll('#upload-list .upload').forEach((row, i) => {
    const chip = row.querySelector('.upload-state');
    if (!chip) return;
    const value = state.fileStates[i] || 'queued';
    if (chip.dataset.state === value) return;
    chip.dataset.state = value;
    chip.className = 'upload-state state-' + value;
    chip.textContent = UPLOAD_STATE[value] || value;
  });
}

function syncClear() {
  const btn = $('#reset');
  if (!btn) return;
  btn.hidden = !(state.files.length || state.shipmentId);
}

function renderUploads() {
  const list = $('#upload-list');
  list.innerHTML = '';
  const count = state.files.length;

  if (!count) {
    list.hidden = true;
    $('#analyze').disabled = true;
    $('#intake-count').textContent = '';
    $('#dropzone').hidden = false;
    syncClear();
    return;
  }
  list.hidden = false;
  syncClear();
  $('#intake-count').textContent = `${count} of 5 documents`;

  state.files.forEach((file, i) => {
    const li = el('li', 'upload');
    li.appendChild(el('span', 'name', file.name));
    const chip = el('span', 'upload-state state-queued', UPLOAD_STATE.queued);
    chip.dataset.state = 'queued';
    li.appendChild(chip);

    const sel = el('select');
    sel.setAttribute('aria-label', 'Document type for ' + file.name);
    DOC_TYPES.forEach(([value, label]) => {
      const opt = el('option', null, label);
      opt.value = value;
      if ((state.declared[i] || '') === value) opt.selected = true;
      sel.appendChild(opt);
    });
    sel.addEventListener('change', () => { state.declared[i] = sel.value; });
    li.appendChild(sel);

    li.appendChild(el('span', 'size', (file.size / 1024).toFixed(0) + ' KB'));

    const rm = el('button', 'rm', '×');
    rm.type = 'button';
    rm.setAttribute('aria-label', 'Remove ' + file.name);
    rm.addEventListener('click', () => {
      state.files.splice(i, 1);
      state.declared.splice(i, 1);
      state.fileStates.splice(i, 1);
      renderUploads();
    });
    li.appendChild(rm);
    list.appendChild(li);
  });

  $('#analyze').disabled = state.busy;
  paintFileStates();
}

function wireButtons() {
  $('#analyze').addEventListener('click', uploadAndAnalyse);
  $('#error-retry').addEventListener('click', () => {
    showError(null);
    uploadAndAnalyse();
  });
  $('#reset').addEventListener('click', () => {
    stopWatching();
    stopAnalysis(true);
    state.files = []; state.declared = []; state.fileStates = [];
    state.shipmentId = null; state.report = null;
    state.stages = {}; state.readingSince = {};
    setBusy(false);
    hideUploadProgress();
    renderUploads();
    renderStageLine();
    renderTimeline();
    loadHistory();
    history.replaceState(null, '', location.pathname);
    $('#results').hidden = true;
    $('#empty').hidden = false;
    showError(null);
  });
  $('#show-raw').addEventListener('change', (e) => {
    state.showRaw = e.target.checked;
    if (state.report) renderComparison(state.report);
  });
  $('#open-config').addEventListener('click', openDrawer);
  $('#close-config').addEventListener('click', closeDrawer);
  $('#cancel-config').addEventListener('click', closeDrawer);
  $('#scrim').addEventListener('click', closeDrawer);
  $('#save-config').addEventListener('click', saveConfig);
  $('#copy-link').addEventListener('click', copyRunLink);
  $('#warn-config').addEventListener('click', openDrawer);
  $('#correction-form').addEventListener('submit', submitCorrection);
  $('#correction-cancel').addEventListener('click', () => $('#correction-dialog').close());
  $('#correction-dialog').addEventListener('close', () => {
    state.pendingCorrection = null;
    $('#correction-error').hidden = true;
  });
  document.querySelectorAll('[data-clear]').forEach((btn) =>
    btn.addEventListener('click', () => clearKey(btn.dataset.clear)));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('#drawer').hidden) closeDrawer();
  });
}

// ---------------------------------------------------------------------
// run
// ---------------------------------------------------------------------

class UploadError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status || 0;
  }
}

/**
 * POST the files and report byte progress.
 *
 * `fetch` has no upload progress, so this one call goes through XHR. The
 * response is still JSON and still carries the server's `detail` (the 429 that
 * names the daily cap is the one a reader most needs verbatim).
 */
function uploadWithProgress(form, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/shipments');
    xhr.responseType = 'json';
    xhr.timeout = 120000;
    xhr.upload.addEventListener('progress', (event) => {
      if (event.lengthComputable) onProgress(event.loaded, event.total);
    });
    xhr.addEventListener('load', () => {
      const body = xhr.response || {};
      if (xhr.status >= 200 && xhr.status < 300 && body.shipment_id) {
        resolve(body);
        return;
      }
      reject(new UploadError(body.detail || ('HTTP ' + xhr.status), xhr.status));
    });
    xhr.addEventListener('error', () => reject(new UploadError(
      'the network dropped during the upload', 0)));
    xhr.addEventListener('timeout', () => reject(new UploadError(
      'the upload timed out after two minutes', 0)));
    xhr.send(form);
  });
}

function setBusy(on) {
  state.busy = on;
  const btn = $('#analyze');
  btn.disabled = on;
  btn.classList.toggle('busy', on);
  btn.setAttribute('aria-busy', String(on));
  $('#analyze-label').textContent = on ? 'Uploading…' : 'Analyse shipment';
  $('#upload-list').classList.toggle('busy', on);
}

/**
 * The combined bar, plus which file the bytes have reached.
 *
 * One multipart request means the browser reports bytes for the whole body, not
 * per part. Cumulative file sizes are what turn that into "this file is the one
 * being sent now" without inventing a progress figure the browser never gave.
 */
function showUploadBytes(loaded, total) {
  const box = $('#upload-progress');
  box.hidden = false;
  box.classList.add('uploading');
  const pct = total ? Math.min(100, Math.round((loaded / total) * 100)) : 0;
  const bar = $('#upload-bar');
  bar.setAttribute('aria-valuenow', String(pct));
  bar.firstElementChild.style.width = pct + '%';

  let sent = 0;
  let uploading = -1;
  let uploaded = 0;
  state.files.forEach((file, i) => {
    sent += file.size || 0;
    if (loaded >= sent) uploaded = i + 1;
    else if (uploading === -1) uploading = i;
  });
  if (loaded >= (total || 0) && total > 0) {
    uploaded = state.files.length;
    uploading = -1;
  }
  state.fileStates = state.files.map((_f, i) => (
    i < uploaded ? 'uploaded' : (i === uploading ? 'uploading' : 'queued')
  ));
  paintFileStates();
  $('#upload-line').textContent = `${uploaded} of ${state.files.length} files uploaded` +
    (pct < 100 ? ` · ${pct}%` : '');
}

function finishUploadProgress() {
  const box = $('#upload-progress');
  const bar = $('#upload-bar');
  box.classList.remove('uploading');
  bar.setAttribute('aria-valuenow', '100');
  bar.firstElementChild.style.width = '100%';
  $('#upload-line').textContent =
    `${state.files.length} of ${state.files.length} files uploaded`;
}

function hideUploadProgress() {
  const box = $('#upload-progress');
  box.hidden = true;
  box.classList.remove('uploading');
  $('#upload-bar').setAttribute('aria-valuenow', '0');
  $('#upload-bar').firstElementChild.style.width = '0%';
  $('#upload-line').textContent = '';
}

async function uploadAndAnalyse() {
  if (state.busy || !state.files.length) return;
  showError(null);
  const files = state.files.slice();
  const form = new FormData();
  files.forEach((f) => form.append('files', f, f.name));
  form.append('doc_types', JSON.stringify(state.declared));

  // Everything below runs before the first await, so the button answers a click
  // in the same frame: a request that takes a minute must not look like a click
  // that did nothing.
  setBusy(true);
  state.fileStates = files.map(() => 'queued');
  renderUploads();
  showUploadBytes(0, 0);
  setProgress('Uploading ' + files.length + ' document(s)…');
  startAnalysis();

  try {
    const data = await uploadWithProgress(form, showUploadBytes);
    finishUploadProgress();
    beginRun(data.shipment_id);
  } catch (err) {
    const message = err instanceof UploadError
      ? err.message : (err && err.message) || String(err);
    state.fileStates = files.map(() => 'failed');
    paintFileStates();
    hideUploadProgress();
    setProgress('');
    stopWatching();
    stopAnalysis(true);
    showError('Upload failed: ' + message, { retry: true });
  } finally {
    setBusy(false);
  }
}

async function runFixture(name) {
  showError(null);
  setProgress('Reading the bundled sample shipment…');
  startAnalysis();
  try {
    const res = await fetch('/api/fixture/' + encodeURIComponent(name));
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || ('HTTP ' + res.status));
    const data = await res.json();
    state.files = []; state.declared = [];
    renderUploads();
    $('#dropzone').hidden = true;
    beginRun(data.shipment_id);
  } catch (err) {
    showError('Could not load the sample shipment: ' + err.message);
    setProgress('');
    stopAnalysis(true);
  }
}

async function openRun(shipmentId) {
  showError(null);
  try {
    const report = await getJSON(`/api/shipments/${encodeURIComponent(shipmentId)}/report`);
    state.shipmentId = shipmentId;
    syncClear();
    state.report = report;
    showResults();
    renderReport(report);
    loadHistory();
    if (report.state === 'running') {
      startAnalysis();
      watchRun(shipmentId);
    } else {
      setProgress('');
      stopAnalysis(true);
    }
  } catch (err) {
    showError('Could not open that run: ' + err.message);
  }
}

function beginRun(shipmentId) {
  state.shipmentId = shipmentId;
  syncClear();
  state.report = null;
  state.filter = null;
  state.readingSince = {};
  showResults();
  renderRunning();
  watchRun(shipmentId);
}

function showResults() {
  $('#empty').hidden = true;
  $('#results').hidden = false;
  $('#export').hidden = true;
  if ($('#export-pdf')) $('#export-pdf').hidden = true;
  $('#copy-link').hidden = false;
  history.replaceState(null, '', '?run=' + encodeURIComponent(state.shipmentId));
}

async function poll() {
  if (!state.shipmentId) return;
  try {
    const report = await getJSON(`/api/shipments/${state.shipmentId}/report`);
    applyReport(report);
  } catch (err) {
    setProgress('Still working — the last poll failed: ' + err.message);
  }
}

function applyReport(report) {
  state.report = report;
  if (report.state !== 'running' && analysis.active) {
    const wait = ANALYSIS_MIN_MS - (Date.now() - analysis.shownAt);
    if (wait > 0) {
      clearTimeout(analysis.revealTimer);
      analysis.revealTimer = setTimeout(() => applyReport(state.report || report), wait);
      return;
    }
  }
  renderReport(report);
  if (report.state !== 'running') {
    stopWatching();
    // Nothing may be left spinning once the run has settled: no upload bar, no
    // stage line, and no file row still claiming to be reading. A row the stage
    // stream never reached is settled from the report, which is the truth.
    hideUploadProgress();
    settleFileStates(report);
    finishAnalysis(report);
    setProgress('');
    state.stages = {};
    state.readingSince = {};
    renderStageLine();
    renderTimeline();
    loadHistory();
  }
}

const PROVIDER_LABEL = { nvidia: 'NVIDIA', nebius: 'Nebius' };

function shortModel(model) {
  // The vendor prefix is already implied by "on NVIDIA"; the rest is the name a
  // reader can match against the transparency table. No truncation: a cut model
  // id with an ellipsis plus the line's own ellipsis reads as two omissions.
  const text = String(model || '');
  return text.includes('/') ? text.split('/').slice(1).join('/') : text;
}

function normName(value) {
  return String(value || '').toLowerCase().replace(/\.[a-z0-9]+$/, '');
}

function fileIndexForLabel(label) {
  const key = normName(label);
  if (!key) return -1;
  return state.files.findIndex((f) => f.name.toLowerCase() === String(label).toLowerCase()
    || normName(f.name) === key);
}

/** The one plain sentence that says what is happening right now. */
function stageLineText() {
  const events = Object.values(state.stages);
  if (!events.length) return '';
  events.sort((a, b) => (a.elapsed_ms || 0) - (b.elapsed_ms || 0));
  const last = events[events.length - 1];
  const active = events.filter((e) => e.stage !== 'done' && e.stage !== 'failed').pop() || last;
  const verb = {
    queued: 'Queued', reading: 'Reading', verifying: 'Verifying evidence',
    comparing: 'Comparing', done: 'Read', failed: 'Could not read',
  }[active.stage] || active.stage;
  let line = `${verb} ${active.document_label}`;
  if (active.model) {
    line += ` with ${shortModel(active.model)} on `
      + (PROVIDER_LABEL[active.provider] || active.provider || 'the vision model');
  }
  let seconds = Math.round((active.elapsed_ms || 0) / 1000);
  if (active.stage === 'reading' && state.readingSince[active.document_id]) {
    seconds = Math.round((Date.now() - state.readingSince[active.document_id]) / 1000);
  }
  return `${line}… ${seconds} s`;
}

function renderStageLine() {
  const host = $('#stage-line');
  if (host) host.textContent = stageLineText();
}

// ---------------------------------------------------------------------
// the analysing panel
// ---------------------------------------------------------------------

// A cached run can settle in ~150 ms. Rendering that fast swaps a skeleton for
// content between two frames, which hides the state change entirely, so the
// reveal waits until this card has been readable. The card tells the truth the
// whole time -- it says "ready" the moment it is -- so the wait delays the
// answer, never the claim.
const ANALYSIS_MIN_MS = 650;
const ANALYSIS_FAREWELL_MS = 420;

// Indeterminate by design: a model call has no honest percentage. The line names
// the phase and never claims how far through it is; real per-document stages
// from the SSE stream replace these the moment they arrive.
const ANALYSIS_STEPS = [
  [0, 'Reading the pages…'],
  [1300, 'Verifying the figures against the document text…'],
  [3000, 'Comparing what each document says…'],
  [6000, 'Scoring the risk…'],
];

const analysis = { active: false, shownAt: 0, timer: 0, hideTimer: 0, revealTimer: 0 };

function neutralAnalysisLine(ms) {
  let line = ANALYSIS_STEPS[0][1];
  for (const [after, text] of ANALYSIS_STEPS) if (ms >= after) line = text;
  return line;
}

function renderAnalysisChips() {
  const host = $('#analysis-chips');
  if (!host) return;
  host.innerHTML = '';
  state.files.forEach((file, i) => {
    host.appendChild(el('li', 'an-chip is-' + (state.fileStates[i] || 'queued'), file.name));
  });
}

function renderAnalysis() {
  if (!analysis.active) return;
  const line = $('#analysis-line');
  if (line) {
    line.textContent = stageLineText() || neutralAnalysisLine(Date.now() - analysis.shownAt);
  }
  renderAnalysisChips();
}

function startAnalysis() {
  const host = $('#analysis');
  if (!host) return;
  clearTimeout(analysis.hideTimer);
  clearTimeout(analysis.revealTimer);
  clearInterval(analysis.timer);
  analysis.active = true;
  analysis.shownAt = Date.now();
  host.hidden = false;
  renderAnalysis();
  analysis.timer = setInterval(renderAnalysis, 700);
}

/** Name the outcome. Called the moment the run settles, before the reveal. */
function markAnalysisReady(report) {
  if (!analysis.active) return;
  const line = $('#analysis-line');
  const docs = (report && report.documents) || [];
  if (line) {
    line.textContent = report && report.state === 'failed'
      ? 'Could not finish this run'
      : 'Reconciliation ready' + (docs.length ? ' — ' + docs.length + ' document(s)' : '');
  }
  renderAnalysisChips();
  clearInterval(analysis.timer);
}

/** Let it go: a beat to be read, unless this is a hard reset. */
function stopAnalysis(immediate = false) {
  const host = $('#analysis');
  if (!host || !analysis.active) return;
  analysis.active = false;
  clearInterval(analysis.timer);
  clearTimeout(analysis.revealTimer);
  const wait = immediate ? 0 : ANALYSIS_FAREWELL_MS;
  clearTimeout(analysis.hideTimer);
  analysis.hideTimer = setTimeout(() => { host.hidden = true; }, wait);
}

function finishAnalysis(report) {
  if (!analysis.active) return;
  markAnalysisReady(report);
  stopAnalysis(false);
}

/** After the run, no file row may still claim to be in flight. */
function settleFileStates(report) {
  let changed = false;
  state.files.forEach((file, i) => {
    const current = state.fileStates[i];
    if (current === 'done' || current === 'failed') return;
    const key = normName(file.name);
    const doc = (report.documents || []).find((d) => (
      normName(d.filename || '') === key || normName(d.label || '') === key));
    if (!doc) return;
    state.fileStates[i] = (doc.status === 'ok' || doc.status === 'cached') ? 'done' : 'failed';
    changed = true;
  });
  if (changed) paintFileStates();
}

function applyStage(ev) {
  state.stages[ev.document_id] = ev;
  if (ev.stage === 'reading') state.readingSince[ev.document_id] = Date.now();
  if (ev.stage === 'done' || ev.stage === 'failed') delete state.readingSince[ev.document_id];
  const index = fileIndexForLabel(ev.document_label);
  if (index >= 0 && UPLOAD_STATE[ev.stage]) {
    state.fileStates[index] = ev.stage;
    paintFileStates();
  }
  renderStageLine();
  renderTimeline();
  renderAnalysis();
}

const STAGE_LABEL = {
  queued: 'Queued',
  reading: 'Reading',
  verifying: 'Verifying evidence',
  comparing: 'Comparing',
  done: 'Done',
  failed: 'Failed',
};

/**
 * A per-document progress timeline fed by SSE stage events. Purely advisory:
 * the report event is the truth, and a run whose stream dies still ends here
 * correctly via the poll path — the timeline simply stays empty.
 */
function renderTimeline() {
  const host = $('#timeline');
  if (!host) return;
  const entries = Object.values(state.stages)
    .sort((a, b) => String(a.document_id).localeCompare(String(b.document_id)));
  if (!entries.length) {
    host.hidden = true;
    host.innerHTML = '';
    return;
  }
  host.hidden = false;
  host.innerHTML = '';
  for (const s of entries) {
    const row = el('div', 'timeline-row');
    // A pulsing dot for the document being read, a check that fades in when it
    // is done: the only two looping animations this panel is allowed.
    const icon = el('span', 'tl-icon tl-icon-' + s.stage, s.stage === 'done' ? '✓' : '');
    icon.setAttribute('aria-hidden', 'true');
    row.appendChild(icon);
    row.appendChild(el('b', 'tl-doc', s.document_label || s.document_id));
    row.appendChild(el('span', 'tl-stage tl-' + s.stage, STAGE_LABEL[s.stage] || s.stage));
    if (s.model) row.appendChild(el('span', 'tl-model', s.provider + ' · ' + s.model));
    row.appendChild(el('span', 'tl-elapsed', (s.elapsed_ms / 1000).toFixed(1) + ' s'));
    host.appendChild(row);
  }
}

/**
 * Follow a run: SSE pushes each settle, with the 1s poll kept as the fallback.
 *
 * The poll starts immediately and is dropped only once a stream event proves
 * the channel is alive. If EventSource is missing, the route is blocked, or the
 * stream errors mid-run, the interval is still there and the UI behaves exactly
 * as it did before — degradation, not a frozen spinner.
 */
function watchRun(shipmentId) {
  stopWatching();
  setProgress('Extracting…');
  state.startedAt = Date.now();
  state.stages = {};
  renderTimeline();
  state.elapsedTimer = setInterval(tickElapsed, 1000);
  startPollFallback();
  poll();

  if (typeof EventSource === 'undefined') return;
  let source;
  try {
    source = new EventSource(
      `/api/shipments/${encodeURIComponent(shipmentId)}/events`
    );
  } catch (err) {
    return;  // polling carries on
  }
  state.stream = source;

  source.addEventListener('report', (e) => {
    let report;
    try {
      report = JSON.parse(e.data);
    } catch (err) {
      return;
    }
    if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
    applyReport(report);
  });
  source.addEventListener('stage', (e) => {
    let ev;
    try {
      ev = JSON.parse(e.data);
    } catch (err) {
      return;
    }
    applyStage(ev);
  });
  source.addEventListener('done', () => closeStream());
  source.onerror = () => {
    closeStream();
    startPollFallback();
  };
}

function startPollFallback() {
  if (!state.pollTimer) state.pollTimer = setInterval(poll, 1000);
}

/**
 * A ticking progress line. A reasoning VLM takes 30-90 s per document, so
 * "Extracting…" alone reads as a hang; this says how far along it actually is.
 */
function tickElapsed() {
  const box = $('#progress');
  if (!state.startedAt || box.hidden) return;
  if (box.textContent.startsWith('Still working')) return;  // leave errors visible
  const report = state.report;
  const secs = Math.round((Date.now() - state.startedAt) / 1000);
  // The stage line carries the per-document seconds, so it has to tick too.
  renderStageLine();
  if (!report) {
    box.textContent = `Extracting — ${secs} s elapsed`;
    return;
  }
  const settled = report.documents.filter(
    (d) => d.status !== 'pending' && d.status !== 'running'
  ).length;
  box.textContent =
    `Extracting — ${settled}/${report.documents.length} documents settled · ` +
    `${secs} s elapsed`;
}

function closeStream() {
  if (state.stream) {
    state.stream.close();
    state.stream = null;
  }
}

function stopPolling() {
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
}

function stopWatching() {
  stopPolling();
  closeStream();
  if (state.elapsedTimer) { clearInterval(state.elapsedTimer); state.elapsedTimer = null; }
  state.startedAt = null;
}

function renderRunning() {
  const docs = $('#docs');
  docs.innerHTML = '';
  for (let i = 0; i < 3; i += 1) {
    const wrap = el('div', 'skeleton-group');
    wrap.appendChild(el('div', 'skeleton s-wide'));
    wrap.appendChild(el('div', 'skeleton s-mid'));
    docs.appendChild(wrap);
  }
}

// ---------------------------------------------------------------------
// render
// ---------------------------------------------------------------------

const BANNER_LABEL = {
  blocked: 'Blocked',
  review: 'Review needed',
  ready: 'Ready to ship',
  running: 'Still reconciling',
  empty: 'Nothing to report yet',
};

/**
 * The one line that says whether this shipment can go.
 *
 * Every level and every item here comes from the server's severity table, so
 * this function decides nothing: it prints `status_report` and hides the card if
 * an older run has no such record. The verdict, the risk score and the counts
 * are echoed beside the banner on purpose -- if they ever disagree, the
 * disagreement is visible instead of buried two panels down.
 */
function renderStatus(report) {
  const note = report.status_report;
  const card = $('#status-card');
  if (!note || !note.banner) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  card.className = 'panel banner is-' + note.banner;

  $('#status-flag').textContent =
    note.headline || BANNER_LABEL[note.banner] || note.banner;

  // Printed only when the banner and the engine verdict could read as two
  // different answers to the same question. Empty otherwise, so it never
  // becomes furniture under a report that already reads as one story.
  const bridge = $('#status-bridge');
  bridge.textContent = note.bridge || '';
  bridge.hidden = !note.bridge;

  const list = $('#status-list');
  list.textContent = '';
  (note.findings || []).filter((f) => f.severity !== 'info').forEach((f) => {
    const li = el('li', 'finding-' + f.severity);
    li.appendChild(el('span', 'finding-level', f.severity));
    li.appendChild(el('b', 'finding-label', f.label));
    li.appendChild(el('span', 'finding-detail', f.detail || ''));
    if (f.documents && f.documents.length) {
      li.appendChild(el('span', 'finding-docs', f.documents.join(' · ')));
    }
    list.appendChild(li);
  });

  const echo = `Engine verdict: ${note.verdict}`
    + ` · customs risk ${note.risk_score}/100`
    + ` · ${note.critical} critical, ${note.warning} warning, ${note.info} informational`;
  $('#status-note').textContent = note.caveat ? `${note.caveat} ${echo}` : echo;
}

/**
 * The drafts the report writes for itself.
 *
 * Rendered from `correction_emails`, which the server generated from the same
 * findings the banner reads -- no model call sits between a verdict and the text
 * a reviewer sends. The copy button hands over the body verbatim; nothing is
 * assembled client-side, because a draft that could be edited here would be a
 * draft that could be wrong here.
 */
function renderEmails(report) {
  const emails = report.correction_emails || [];
  const card = $('#emails-card');
  card.hidden = emails.length === 0;
  if (!emails.length) return;

  const list = $('#emails-list');
  list.textContent = '';
  emails.forEach((mail) => {
    const block = el('article', 'email');
    block.appendChild(el('h3', 'email-to', 'To ' + mail.recipient_role));
    block.appendChild(el('p', 'email-subject', mail.subject));
    block.appendChild(el('p', 'email-meta',
      mail.document_label + ' · ' + mail.critical + ' critical, '
      + mail.warning + ' warning'));

    const body = el('pre', 'email-body', mail.body);
    block.appendChild(body);

    const copy = el('button', 'btn btn-quiet', 'Copy this draft');
    copy.type = 'button';
    copy.addEventListener('click', async () => {
      try {
        await navigator.clipboard.writeText(mail.subject + '\n\n' + mail.body);
        copy.textContent = 'Copied.';
      } catch (err) {
        copy.textContent = 'Clipboard blocked — select the text instead.';
      }
      setTimeout(() => { copy.textContent = 'Copy this draft'; }, 2500);
    });
    block.appendChild(copy);
    list.appendChild(block);
  });
}

function renderReport(report) {
  const s = report.summary;
  renderStatus(report);
  const verdict = $('#verdict-headline');
  verdict.textContent = HEADLINE[s.verdict] || s.verdict;
  verdict.closest('.verdict').className = 'panel verdict is-' + s.verdict;

  // "live" is reserved for calls made on THIS run. A report replayed entirely
  // from cache is model-backed but not live, and must not claim otherwise.
  const src = report.run.result_source;
  const n = report.run.live_calls || 0;
  let provenance;
  if (report.run.sample) {
    // A sample is a replay on any deployment, so this outranks the live/cached
    // branches: saying "live" beside a bundled example would be the overclaim
    // this label exists to prevent.
    provenance = 'sample: cached model output — replayed from cache, '
      + 'no API call made for this run';
  } else if (src === 'live' && n > 0) {
    provenance = `live — ${n} document(s) read by ${report.run.model} on this run`;
  } else if (src === 'live') {
    provenance = 'model-backed, replayed from cache — no API call made for this run';
  } else if (src === 'authored') {
    provenance = 'demo cache — hand-authored values, not a model read';
  } else if (src === 'live+authored') {
    provenance = 'mixed — some values model-read, some hand-authored';
  } else {
    provenance = 'nothing extracted yet';
  }
  $('#verdict-sub').textContent =
    `${s.documents_ok} of ${s.documents_ok + s.documents_failed} documents extracted · ` +
    provenance + (report.state === 'running' ? ' · in progress' : '');

  renderCounts(s);
  $('#export').hidden = false;
  $('#export').href = `/api/shipments/${report.shipment_id}/export`;
  const pdf = $('#export-pdf');
  if (pdf) {
    pdf.hidden = false;
    pdf.href = `/api/shipments/${report.shipment_id}/report.pdf`;
  }

  renderNarrative(report);
  renderBriefing(report);
  renderEmails(report);
  renderComparison(report);
  renderMath(report);
  renderUnverified(report);
  renderDocuments(report);
  renderTransparency(report);
  renderRisk(report);
  renderSanity(report);
  renderLineItems(report);

  const run = report.run;
  const meta = $('#run-meta');
  meta.innerHTML = '';
  meta.appendChild(el('span', null,
    `model ${run.model} · structured output: ${run.json_schema_mode} · ` +
    `prompt ${run.prompt_version} · normalizer ${run.normalizer_version} · ` +
    `comparator ${run.comparator_version}`));
  meta.appendChild(el('span', null,
    'The model transcribes verbatim text only. All unit conversion, number parsing ' +
    'and comparison happens in deterministic Python.'));
}

function renderCounts(s) {
  const host = $('#counts');
  host.innerHTML = '';
  STATUS_FILTERS.forEach(([key, label]) => {
    const b = el('button', 'count c-' + key + (s[key] ? '' : ' zero'));
    b.type = 'button';
    b.setAttribute('aria-pressed', String(state.filter === key));
    b.disabled = !s[key];
    b.appendChild(el('b', null, s[key]));
    b.appendChild(el('span', null, label));
    b.addEventListener('click', () => {
      state.filter = state.filter === key ? null : key;
      renderCounts(s);
      renderComparison(state.report);
    });
    host.appendChild(b);
  });
}

const SOURCE_WORDS = {
  live_api: 'live call on this run',
  cached_live: 'cached model output',
  cached_authored: 'hand-authored fixture',
  none: 'no answer',
};

function renderTransparency(report) {
  const run = report.run;
  const docs = report.documents || [];
  const panel = $('#transparency');
  panel.hidden = !docs.length;
  if (!docs.length) return;

  const n = report.narrative || {};
  const b = report.briefing || {};
  const tokens = { prompt: 0, completion: 0, total: 0 };
  let ms = 0;
  const acc = (u, t) => {
    if (u) {
      tokens.prompt += u.prompt || 0;
      tokens.completion += u.completion || 0;
      tokens.total += u.total || 0;
    }
    ms += t || 0;
  };
  docs.forEach((d) => {
    acc(d.usage, d.timings_ms);
    if (d.consensus) acc(d.consensus.usage, d.consensus.latency_ms);
  });
  acc(n.usage, n.latency_ms);
  acc(b.usage, b.latency_ms);
  (report.comparisons || []).forEach((c) => {
    const a = c.adjudication;
    if (a && a.status !== 'pending') acc(a.usage, a.latency_ms);
  });

  $('#tp-source').textContent =
    `${run.vision_provider} · ${run.model} · structured output: ${run.json_schema_mode} · ` +
    `${run.live_calls} live call${run.live_calls === 1 ? '' : 's'} on this run`;

  // The honesty line. Absent for a genuine live run, explicit whenever the
  // numbers on screen did not come from a call made now.
  const notes = [];
  if (run.live_calls === 0) {
    notes.push('No API call was made on this run — every answer was replayed from the response cache.');
  }
  if (run.result_source === 'authored' || docs.some((d) => d.source === 'cached_authored')) {
    notes.push('At least one value comes from a hand-authored fixture, not from a model.');
  }
  // A model name that was never confirmed against the host is a different claim
  // from one that was, and the difference matters when an endpoint 404s. Skipped
  // in DEMO_MODE, where the line above already says nothing was called.
  if (run.model_selection === 'discovered') {
    notes.push(`Configured model is not served by this host; used ${run.model} instead.`);
  } else if (!run.demo_mode && run.model_selection !== 'configured') {
    notes.push(`'${run.model}' was not confirmed against the host's model list${
      run.model_note ? ': ' + run.model_note : ''}.`);
  }
  const splitReads = docs.reduce(
    (n, d) => n + ((d.consensus && d.consensus.divergences) ? d.consensus.divergences.length : 0), 0);
  if (splitReads) {
    notes.push(`${splitReads} field reading(s) disagreed between the two providers and are ` +
      'marked "needs review". Two readers agreeing is not proof either.');
  }
  const caveat = $('#tp-caveat');
  caveat.textContent = notes.join(' ');
  caveat.hidden = !notes.length;
  const table = $('#tp-table');
  table.innerHTML = '';
  const thead = el('thead');
  const head = el('tr');
  ['Step', 'Provider', 'Model', 'Answer came from', 'Tries', 'Tokens', 'Time'].forEach((t) =>
    head.appendChild(el('th', null, t))
  );
  thead.appendChild(head);
  table.appendChild(thead);

  const body = el('tbody');
  const addRow = (step, provider, model, source, tries, usage, timing) => {
    const tr = el('tr');
    [
      step,
      provider,
      model,
      SOURCE_WORDS[source] || source || '—',
      String(tries),
      usage && usage.total ? `${usage.prompt}+${usage.completion}` : '—',
      timing ? `${(timing / 1000).toFixed(1)} s` : '—',
    ].forEach((v, i) => tr.appendChild(el(i === 0 ? 'th' : 'td', i === 2 ? 'mono' : null, v)));
    body.appendChild(tr);
  };
  docs.forEach((d) => {
    addRow(d.label, d.provider, d.model, d.source, d.attempts, d.usage, d.timings_ms);
    const c = d.consensus;
    if (c && c.status !== 'pending') {
      addRow('second reader · ' + d.label, c.provider, c.model,
        c.source || c.status, 1, c.usage, c.latency_ms);
    }
  });
  if (n.status && n.status !== 'pending') {
    addRow(
      'Executive summary',
      n.provider || run.summary_provider,
      n.model || run.summary_model,
      n.source === 'generated' ? 'live_api' : n.status === 'cached' ? 'cached_live' : n.status,
      n.usage && n.usage.total ? 1 : 0,
      n.usage,
      n.latency_ms
    );
  }
  if (b.status === 'ok' || b.status === 'cached') {
    addRow(
      'Draft discrepancy email',
      b.provider || run.summary_provider,
      b.model || run.summary_model,
      b.source === 'generated' ? 'live_api' : b.status === 'cached' ? 'cached_live' : b.source,
      b.usage && b.usage.total ? 1 : 0,
      b.usage,
      b.latency_ms
    );
  }
  (report.comparisons || []).forEach((c) => {
    const a = c.adjudication;
    if (a && (a.status === 'ok' || a.status === 'cached')) {
      addRow(
        'Second opinion · ' + c.label,
        a.provider,
        a.model,
        a.source === 'generated' ? 'live_api' : a.status === 'cached' ? 'cached_live' : a.source,
        1,
        a.usage,
        a.latency_ms
      );
    }
  });
  table.appendChild(body);

  // The branding line is only worth printing when it is true about *this* run.
  const powered = document.querySelector('#transparency .badge-powered');
  if (powered) {
    powered.textContent = 'Powered by Nebius Token Factory + NVIDIA Nemotron · ' +
      (run.live_calls
        ? run.live_calls + ' live API call(s) on this run'
        : 'no live call on this run — responses were replayed from cache');
  }

  const raws = docs.flatMap((d) => (d.raw_files || []).map((f) => [d, f]));
  const rawHost = $('#tp-raw');
  rawHost.hidden = !raws.length;
  if (raws.length) {
    const list = rawHost.querySelector('.tp-files');
    list.textContent = '';
    raws.forEach(([doc, name]) => {
      const b = el('button', 'tp-file', doc.label + ' → ' + name);
      b.type = 'button';
      b.addEventListener('click', () => showRaw(report.shipment_id, name, b));
      list.appendChild(b);
    });
    $('#tp-body').hidden = true;
    $('#tp-body').textContent = '';
  }

  const cost = $('#tp-cost');
  const secs = `${(ms / 1000).toFixed(1)} s total`;
  if (tokens.total === 0) {
    cost.textContent = `${secs} · no tokens spent on this run`;
  } else if (run.usd_per_mtok > 0) {
    cost.textContent =
      `${tokens.total} tokens · ${secs} · ≈$` +
      ((tokens.total / 1e6) * run.usd_per_mtok).toFixed(5) +
      ` at $${run.usd_per_mtok}/1M (configured)`;
  } else {
    cost.textContent =
      `${tokens.total} tokens · ${secs} · price not configured, so none is shown` +
      ' (set USD_PER_MTOK)';
  }
}

async function showRaw(shipmentId, name, button) {
  const pre = $('#tp-body');
  document.querySelectorAll('.tp-file.on').forEach((b) => b.classList.remove('on'));
  button.classList.add('on');
  pre.hidden = false;
  pre.textContent = 'loading…';
  try {
    const res = await fetch(
      `/api/shipments/${encodeURIComponent(shipmentId)}/raw/${encodeURIComponent(name)}`
    );
    if (!res.ok) throw new Error('HTTP ' + res.status);
    // textContent, never innerHTML: this body is text extracted from someone
    // else's document and must stay data.
    pre.textContent = await res.text();
  } catch (err) {
    pre.textContent = 'could not load: ' + err.message;
  }
}

function renderRisk(report) {
  const risk = report.risk;
  const box = $('#risk');
  if (!risk || !risk.reasons || !risk.reasons.length) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  box.dataset.level = risk.level;
  $('#risk-level').textContent =
    risk.score ? `Customs risk: ${risk.level} · ${risk.score}/100` : `Customs risk: ${risk.level}`;
  $('#risk-reasons').textContent = risk.reasons.join('; ') + '.';

  // The number is useless without the arithmetic behind it, so every
  // contribution is listed with the points it added.
  const list = $('#risk-contrib');
  list.textContent = '';
  (risk.contributions || []).forEach((c) => {
    const li = el('li');
    li.appendChild(el('b', null, '+' + c.points));
    li.appendChild(el('code', null, c.code.replace(/_/g, ' ')));
    li.appendChild(el('span', null, ' — ' + c.detail));
    list.appendChild(li);
  });
  list.hidden = !list.children.length;

  // Say where the number came from. A risk statement that reads like a model
  // opinion would be the wrong kind of impressive.
  $('#risk-src').textContent = risk.source === 'deterministic'
    ? 'Scored by rule from the verdicts above — no model was asked. Bands at '
      + risk.medium_at + ' and ' + risk.high_at + ' points.'
    : '';
}

function renderSanity(report) {
  const list = $('#sanity');
  const findings = report.sanity || [];
  list.hidden = !findings.length;
  list.textContent = '';
  findings.forEach((f) => {
    const li = el('li', 'sanity-' + f.level);
    li.appendChild(el('span', 'sanity-tag', f.level));
    // The explanation quotes both the computed number and the threshold, so the
    // reviewer can disagree with the rule instead of trusting it.
    li.appendChild(el('span', 'sanity-text', f.explanation));
    list.appendChild(li);
  });
}

const LINEITEM_STATUS = {
  matched: 'same on every document',
  quantity_conflict: 'count differs',
  unit_differs: 'package basis differs',
  only_here: 'printed on one document',
};

/** Where the goods lines came from, said plainly. */
function lineItemsProvenance(li) {
  if (li.source === 'live_api') {
    return `read on this run by ${li.provider} · ${li.model}`;
  }
  if (li.source === 'cached_live') {
    return `replayed from a captured model read (${li.provider} · ${li.model})`;
  }
  if (li.source === 'cached_authored') {
    return 'hand-authored fixture — no model read these lines';
  }
  return 'no goods lines were read';
}

function renderLineItems(report) {
  const li = report.line_items || {};
  const card = $('#lineitems-card');
  // Off by default, so a run that never asked for this shows no panel at all.
  // An empty "Goods lines" card would read as a check that came out clean.
  if (!li.enabled) {
    card.hidden = true;
    return;
  }
  card.hidden = false;

  const tag = $('#lineitems-tag');
  tag.textContent = lineItemsProvenance(li);
  tag.className = 'flag'
    + (li.source === 'cached_authored' ? ' flag-warn'
       : li.source === 'live_api' ? ' flag-live' : '');

  const note = $('#lineitems-note');
  note.textContent = li.note || '';
  note.hidden = !li.note;

  const table = $('#lineitems');
  table.textContent = '';
  const matches = li.matches || [];
  if (li.status === 'pending') {
    $('#lineitems-hint').textContent = 'Reading the goods lines…';
    table.hidden = true;
    $('#lineitems-meta').textContent = '';
    return;
  }
  table.hidden = !matches.length;
  $('#lineitems-hint').textContent = matches.length
    ? `${li.matched} line(s) agree, ${li.conflicts} disagree, `
      + `${li.uncheckable} printed on one document only.`
    : 'No goods lines to compare.';

  if (matches.length) {
    const docs = li.documents || [];
    const head = el('tr');
    ['Item', ...docs, 'Status', 'What it means'].forEach((t, i) => {
      head.appendChild(i === 0 ? el('th', null, t) : el('td', null, t));
    });
    const thead = el('thead');
    thead.appendChild(head);
    table.appendChild(thead);

    const tbody = el('tbody');
    matches.forEach((m) => {
      const tr = el('tr', 'li-' + m.status);
      tr.appendChild(el('th', 'mono', m.label));
      docs.forEach((d) => tr.appendChild(el('td', 'mono', m.quantities[d] || '—')));
      tr.appendChild(el('td', null, LINEITEM_STATUS[m.status] || m.status));
      tr.appendChild(el('td', 'reason', m.detail || ''));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
  }

  $('#lineitems-meta').textContent =
    lineItemsProvenance(li)
    + (li.usage && li.usage.total ? ` · ${li.usage.total} tokens` : '')
    + (li.latency_ms ? ` · ${(li.latency_ms / 1000).toFixed(1)}s` : '')
    + ' · matching is exact, in Python; nothing here changes the verdict above.';
}

/**
 * Arithmetic inside a single document.
 *
 * `not_run` is rendered, not omitted. A card that only appears when something
 * failed would let an unchecked shipment look identical to a clean one, which is
 * the exact confusion this app is built to prevent.
 */
const MATH_LABEL = {
  line_total_math: 'quantity × unit price = line amount',
  line_total_sum: 'the goods lines add up to the stated total',
  row_weight_sum: 'the per-row weights add up to the stated total weight',
  net_above_gross_in_document: 'net weight is not above gross weight',
};

function renderMath(report) {
  const card = $('#math-card');
  const checks = report.internal_checks || [];
  card.hidden = checks.length === 0;
  if (!checks.length) return;

  const list = $('#math-list');
  list.textContent = '';
  checks.filter((c) => c.status !== 'pass').forEach((c) => {
    const li = el('li', 'math-' + c.status);
    li.appendChild(el('span', 'math-state', c.status === 'fail' ? 'fails' : 'not run'));
    li.appendChild(el('b', null, MATH_LABEL[c.code] || c.code));
    li.appendChild(el('span', 'math-doc', c.document_label));
    li.appendChild(el('span', 'math-detail', c.detail || ''));
    list.appendChild(li);
  });

  const passed = checks.filter((c) => c.status === 'pass').length;
  const failed = checks.filter((c) => c.status === 'fail').length;
  const skipped = checks.filter((c) => c.status === 'not_run').length;
  $('#math-meta').textContent =
    `${passed} check(s) passed, ${failed} failed, ${skipped} not run.`
    + (skipped ? ' Checks that did not run are not checks that passed — three of '
      + 'these need the goods lines, which are read only when ENABLE_LINE_ITEMS '
      + 'is on.' : '');
}

/**
 * Which fields were checked against the document's own text, and which could
 * not be. The second number matters as much as the first: on a phone photo the
 * app has nothing to compare a quote against, and "not checked" has to be
 * visible rather than implied by the absence of a warning.
 */
function renderUnverified(report) {
  const card = $('#unverified-card');
  const list = $('#unverified-list');
  const items = report.unverified || [];

  let verified = 0, unverified = 0, unchecked = 0, absent = 0;
  (report.documents || []).forEach((d) => {
    Object.values(d.verification || {}).forEach((v) => {
      if (v.state === 'verified') verified++;
      else if (v.state === 'unverified') unverified++;
      else if (v.state === 'not_checkable') unchecked++;
      else absent++;
    });
  });

  card.hidden = items.length === 0;
  list.textContent = '';
  items.forEach((u) => {
    const li = el('li');
    li.appendChild(el('b', null, u.label));
    li.appendChild(el('span', 'uv-doc', u.document_label));
    li.appendChild(el('span', 'uv-val mono', u.value || ''));
    li.appendChild(el('blockquote', 'uv-quote', '“' + (u.evidence_text || '') + '”'));
    li.appendChild(el('span', 'uv-why', u.reason || 'not found on the page'));
    if (u.advisor_note) {
      li.appendChild(el('span', 'uv-advisor',
        'AI-generated, advisory: ' + u.advisor_note));
    }
    list.appendChild(li);
  });

  const meta = $('#unverified-meta');
  if (!verified && !unverified && !unchecked) {
    meta.textContent = '';
  } else if (unchecked && !verified && !unverified) {
    meta.textContent = `No text source on any page — every quoted line is unverified `
      + `by necessity, not by failure. Upload a PDF with an embedded text layer to `
      + `enable the check.`;
  } else {
    meta.textContent = `${verified} quoted line(s) confirmed against the document's `
      + `own text, ${unverified} not found and excluded`
      + (unchecked ? `, ${unchecked} not checkable (no text layer on those pages)` : '')
      + '.';
  }
}

function renderBriefing(report) {
  const b = report.briefing || {};
  const card = $('#briefing-card');
  const show = b.status === 'ok' && !!b.text;
  card.hidden = !show;
  if (!show) return;
  // A draft, never a sent thing.
  $('#briefing-text').textContent = b.text;
  $('#briefing-meta').textContent =
    `${b.provider} · ${b.model} · ` +
    (b.source === 'cached' ? 'reused from a stored draft' : 'written on this run') +
    (b.usage && b.usage.total ? ` · ${b.usage.total} tokens` : '');
}

/** Field key -> per-document second-reader disagreements. */
function consensusByFieldMap(report) {
  const out = {};
  (report.documents || []).forEach((doc) => {
    const c = doc.consensus;
    if (!c || !c.divergences || !c.divergences.length) return;
    c.divergences.forEach((div) => {
      (out[div.field] = out[div.field] || []).push({ label: doc.label, div });
    });
  });
  return out;
}

function renderComparison(report) {
  const consensusByField = consensusByFieldMap(report);
  const table = $('#cmp');
  table.innerHTML = '';

  const head = el('tr');
  ['Field', 'Verdict', 'Why', ...report.documents.map((d) => d.label)].forEach((t, i) => {
    head.appendChild(i === 0 ? el('th', null, t) : el('td', null, t));
  });
  const thead = el('thead');
  thead.appendChild(head);
  table.appendChild(thead);

  const rows = report.comparisons.filter((c) => !state.filter || c.status === state.filter);
  const tbody = el('tbody');
  rows.forEach((comp) => {
    const tr = el('tr');

    const tdField = el('td', 'fld');
    tdField.appendChild(el('span', null, comp.label));
    tdField.appendChild(el('span', 'k', comp.field));
    tr.appendChild(tdField);

    const tdStatus = el('td');
    tdStatus.appendChild(el('span', 'flag flag-' + comp.status,
      (ICON[comp.status] || '') + '  ' + comp.status.replace(/_/g, ' ')));
    tr.appendChild(tdStatus);

    const reasonCell = el('td', 'reason');
    reasonCell.appendChild(el('div', null, comp.reason));
    const adj = comp.adjudication;
    if (adj && adj.status === 'ok') {
      // Advisory, and labelled that way: the verdict above it is the
      // deterministic one and this can never replace it.
      const stance = adj.same === true ? 'probably the same'
        : adj.same === false ? 'probably different' : 'declined to answer';
      reasonCell.appendChild(el('div', 'adj',
        `Reviewer note (advisory, not applied): ${adj.rationale} [${stance}, ` +
        `confidence ${(adj.confidence * 100).toFixed(0)}% · ${adj.model}]`));
    } else if (adj && (adj.status === 'failed' || adj.status === 'skipped')) {
      // Worth showing even when nothing was asked: a user with no key learns the
      // feature exists and what would switch it on.
      reasonCell.appendChild(el('div', 'adj-failed',
        (adj.status === 'failed' ? 'Second opinion unavailable: ' : 'No second opinion asked: ')
        + (adj.error || adj.status)));
    }
    const split = (consensusByField[comp.field] || []);
    if (split.length) {
      // A disagreement between readers is a prompt to look, not a verdict: both
      // transcriptions are shown and the human decides.
      const text = split.map((s) =>
        `${s.label}: ${s.div.primary_canonical || 'not read'} vs ` +
        `${s.div.secondary_canonical || 'not read'}`).join('; ');
      reasonCell.appendChild(el('div', 'diverge',
        `Needs review — the second reader disagreed (${text}).`));
    }
    tr.appendChild(reasonCell);

    report.documents.forEach((doc) => {
      const v = comp.values.find((x) => x.document_id === doc.id);
      tr.appendChild(valueCell(v, comp));
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  $('#no-matches').hidden = rows.length > 0;
}

function valueCell(v, comp) {
  const cell = el('td', 'val');
  if (!v || !v.raw_value) {
    cell.classList.add('missing');
    cell.textContent = 'not printed on this document';
    return cell;
  }
  if (v.is_outlier) cell.classList.add('outlier');

  cell.appendChild(el('div', 'v', v.raw_value));
  if (!state.showRaw && v.canonical && v.canonical !== v.raw_value) {
    cell.appendChild(el('div', 'n', '→ ' + v.canonical));
  }

  cell.appendChild(el('div', 'meta',
    `conf ${(v.confidence ?? 0).toFixed(2)}` + (v.is_outlier ? ' · outlier' : '')));
  cell.appendChild(pageChip(v, comp));

  (v.warnings || []).forEach((w) => cell.appendChild(el('div', 'w', '⚠ ' + w)));

  const corrected = correctedBy(v, comp.field);
  if (corrected) {
    // The reader's own answer is gone from this cell, so say plainly that what
    // is shown here is a human assertion, and keep it reversible.
    cell.classList.add('corrected');
    cell.appendChild(el('div', 'corr-tag',
      `corrected by ${corrected.actor} · model said ${corrected.original_value || 'nothing'}`));
    const undo = el('button', 'btn btn-quiet btn-tiny', 'Revert');
    undo.type = 'button';
    undo.addEventListener('click', () => revertValue(v.document_id, comp.field));
    cell.appendChild(undo);
  } else {
    const edit = el('button', 'btn btn-quiet btn-tiny edit-value', 'Correct');
    edit.type = 'button';
    edit.addEventListener('click', () => correctValue(v.document_id, comp.field, v.raw_value));
    cell.appendChild(edit);
  }
  return cell;
}

/** The active correction for this displayed value, if a reviewer replaced it. */
function correctedBy(v, field) {
  const doc = ((state.report && state.report.documents) || [])
    .find((d) => d.id === v.document_id);
  if (!doc || !doc.corrections) return null;
  const active = doc.corrections.filter((c) => c.field === field && !c.reverted_at);
  return active.length ? active[active.length - 1] : null;
}

function correctValue(documentId, field, current) {
  const dialog = $('#correction-dialog');
  state.pendingCorrection = { documentId, field };
  $('#correction-title').textContent = `Correct “${field}”`;
  $('#correction-context').textContent = current
    ? `The model read: ${current}. Enter the value exactly as it appears on the document.`
    : 'The model read nothing here. Enter the value exactly as it appears on the document.';
  const input = $('#correction-value');
  input.value = current || '';
  $('#correction-actor').value = state.reviewer || '';
  $('#correction-error').hidden = true;
  input.setCustomValidity('');
  dialog.showModal();
  input.focus();
  input.select();
}

async function submitCorrection(event) {
  event.preventDefault();
  const dialog = $('#correction-dialog');
  const value = $('#correction-value').value.trim();
  const actor = $('#correction-actor').value.trim() || 'reviewer';
  const target = state.pendingCorrection;
  const error = $('#correction-error');
  if (!target) return;
  if (!value) {
    error.textContent = 'An empty value is a deletion, not a correction — use Revert.';
    error.hidden = false;
    $('#correction-value').focus();
    return;
  }
  const save = $('#correction-save');
  save.disabled = true;
  error.hidden = true;
  try {
    state.reviewer = actor;
    await postCorrection(target.documentId, target.field, value, actor);
    dialog.close();
    setStatus('');
  } catch (err) {
    error.textContent = 'Could not apply that correction: ' + err.message;
    error.hidden = false;
  } finally {
    save.disabled = false;
  }
}

async function revertValue(documentId, field) {
  await request(
    `/api/shipments/${encodeURIComponent(state.shipmentId)}/documents/` +
    `${encodeURIComponent(documentId)}/fields/${encodeURIComponent(field)}/revert`,
    { method: 'POST' },
  );
}

async function postCorrection(documentId, field, value, actor = 'reviewer') {
  return mutate(
    `/api/shipments/${encodeURIComponent(state.shipmentId)}/documents/` +
    `${encodeURIComponent(documentId)}/fields/${encodeURIComponent(field)}`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value, actor }),
    },
  );
}

/** POST a mutation, repaint from the recomputed report, and let the caller handle failure. */
async function mutate(path, init) {
  const res = await fetch(path, init);
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || ('HTTP ' + res.status));
  state.report = body;
  renderReport(body);
  loadHistory();
  return body;
}

/** POST a correction and show the outcome in the page status; used by Revert. */
async function request(path, init) {
  try {
    await mutate(path, init);
    setStatus('');
  } catch (err) {
    setStatus('Could not apply that correction: ' + err.message, true);
  }
}

/**
 * Jump-to-page chip. The extraction contract carries page_index and the quoted
 * evidence line, so "show me the page this came from" is answerable exactly.
 * Where the PDF's own text layer placed that line, the page opens with the
 * rectangle drawn on it; where it did not -- a photo, a scan, a quote that joined
 * two table cells -- the page opens with the line written beneath it and says in
 * so many words that there are no coordinates, rather than drawing a guess.
 */
function pageChip(v, comp) {
  const doc = ((state.report && state.report.documents) || [])
    .find((d) => d.id === v.document_id);
  const page = doc && (doc.pages || []).find((p) => p.index === v.page_index);
  if (!page) {
    return el('div', 'meta', 'page ' + v.page_index);
  }

  const chip = el('button', 'page-chip', 'page ' + v.page_index + ' →');
  chip.type = 'button';
  chip.addEventListener('click', () => {
    const evidence = ((doc.extraction?.fields || {})[comp.field] || {}).evidence_text || '';
    const ver = (doc.verification || {})[comp.field] || {};
    // Only draw the rectangle on the page it belongs to. A model can quote a real
    // line and report the wrong page; the line is still true, but the coordinates
    // describe a different sheet, and putting them here would be a fabrication.
    const box = (ver.box && ver.box_page === page.index) ? ver.box : null;
    lightbox(
      `/page/${encodeURIComponent(state.shipmentId)}/${page.image_path}`,
      `${doc.label} — page ${page.index}`,
      evidence,
      box,
      chip,
    );
  });
  return chip;
}

function renderNarrative(report) {
  const card = document.getElementById('narrative-card');
  const n = report.narrative || {};
  if (!(n.text && (n.status === 'ok' || n.status === 'cached'))) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  document.getElementById('narrative-text').textContent = n.text;

  // Be explicit about whether a model wrote this or we did. Replaying a
  // hand-authored narrative under an "AI-generated" label would be a lie.
  const tag = document.getElementById('narrative-tag');
  if (n.source === 'authored') {
    tag.textContent = 'Demo text (hand-authored)';
    tag.classList.add('authored');
  } else {
    tag.textContent = 'AI-generated';
    tag.classList.remove('authored');
  }
  document.getElementById('narrative-meta').textContent =
    (n.provider ? n.provider + ' · ' : '') + (n.model || '') +
    ' — written from the verdicts below; it cannot change them. ' +
    'The comparison table is authoritative.';
}

function renderDocuments(report) {
  const wrap = $('#docs');
  wrap.innerHTML = '';

  report.documents.forEach((doc) => {
    const box = el('details', 'doc');
    box.open = doc.status === 'failed';

    const sum = el('summary', 'doc-head');
    sum.appendChild(el('span', 'dot dot-' + doc.status));
    sum.appendChild(el('strong', null, doc.label));
    sum.appendChild(el('span', 'flag flag-' + doc.status, doc.status.replace(/_/g, ' ')));
    sum.appendChild(el('span', 'spacer'));
    sum.appendChild(el('span', 'doc-tail',
      (doc.detected_doc_type || doc.declared_doc_type || 'unclassified') +
      ` · ${doc.pages.length} page(s)` +
      (doc.attempts ? ` · ${doc.attempts} attempt(s)` : '') +
      (doc.used_cache ? ' · cached' : '') +
      (doc.timings_ms ? ` · ${doc.timings_ms} ms` : '')));
    box.appendChild(sum);

    if (doc.error) box.appendChild(el('p', 'alert', doc.error));

    (doc.notes || []).forEach((n) => box.appendChild(el('p', 'note', n)));

    if (doc.status === 'failed') {
      const row = el('div', 'row-actions');
      const retry = el('button', 'btn btn-quiet', 'Retry this document');
      retry.type = 'button';
      retry.addEventListener('click', () => retryDocument(doc.id));
      row.appendChild(retry);
      box.appendChild(row);
    }

    if (doc.extraction) {
      const grid = el('div', 'evi-grid');
      const hdr = el('div', 'evi head');
      ['Field', 'Verbatim value', 'Evidence line quoted from the page', 'Page', 'Conf.']
        .forEach((t, i) => hdr.appendChild(el('span', i >= 3 ? 'n' : null, t)));
      grid.appendChild(hdr);

      Object.entries(doc.extraction.fields).forEach(([key, f]) => {
        const row = el('div', 'evi');
        row.appendChild(el('span', null, fieldLabel(report, key)));
        row.appendChild(el('code', null, f.present ? f.value : '— absent —'));
        row.appendChild(el('span', 'q', f.evidence_text || (f.present ? '' : 'nothing printed')));
        row.appendChild(el('span', 'n', f.present ? 'p' + f.page_index : ''));
        row.appendChild(el('span', 'n', f.present ? f.confidence.toFixed(2) : ''));
        grid.appendChild(row);
      });
      box.appendChild(el('div', 'doc-body', null)).appendChild(grid);
    }

    if (doc.pages.length) {
      const pages = el('div', 'pages');
      doc.pages.forEach((p) => {
        const fig = el('figure');
        const img = el('img');
        img.src = `/page/${report.shipment_id}/${p.image_path}`;
        img.alt = `${doc.label}, page ${p.index}`;
        img.loading = 'lazy';
        // A thumbnail is the other way into the evidence, so it has to open from
        // the keyboard too and take focus back when the overlay closes.
        img.tabIndex = 0;
        img.setAttribute('role', 'button');
        const openPage = () => lightbox(
          img.src, `${doc.label} — page ${p.index}`, null, null, img);
        img.addEventListener('click', openPage);
        img.addEventListener('keydown', (event) => {
          if (event.key === 'Enter' || event.key === ' ') {
            event.preventDefault();
            openPage();
          }
        });
        fig.appendChild(img);
        fig.appendChild(el('figcaption', null, `page ${p.index} · ${p.width}×${p.height}`));
        pages.appendChild(fig);
      });
      box.appendChild(pages);
    }
    wrap.appendChild(box);
  });
}

async function retryDocument(documentId) {
  setProgress('Retrying ' + documentId + '…');
  try {
    const res = await fetch(`/api/shipments/${state.shipmentId}/retry`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ document_id: documentId }),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    await poll();
  } catch (err) {
    showError('Retry failed: ' + err.message);
  }
  setProgress('');
}

function lbButton(cls, label, aria, hint) {
  const button = el('button', 'lb-btn ' + cls, label);
  button.type = 'button';
  button.setAttribute('aria-label', aria);
  button.title = hint;
  return button;
}

function lightbox(src, caption, evidence, box, trigger) {
  // The trigger is passed in rather than read off activeElement: a page
  // thumbnail is not focusable in every browser, and focus has to land back on
  // the thing that was clicked rather than on <body>, or the next Tab is lost.
  const opener = trigger instanceof HTMLElement ? trigger
    : (document.activeElement instanceof HTMLElement ? document.activeElement : null);

  const fig = el('figure', 'lightbox');
  fig.setAttribute('role', 'dialog');
  fig.setAttribute('aria-modal', 'true');
  fig.setAttribute('aria-label', caption);

  const bar = el('div', 'lb-bar');
  const zoomOut = lbButton('lb-out', '−', 'Zoom out', 'Zoom out (−)');
  const readout = el('output', 'lb-zoom', '100%');
  readout.setAttribute('aria-live', 'polite');
  const zoomIn = lbButton('lb-in', '+', 'Zoom in', 'Zoom in (+)');
  const fitBtn = lbButton('lb-fit', 'Fit', 'Fit to screen', 'Fit to screen (0)');
  const closeBtn = lbButton('lb-close', '×', 'Close', 'Close (Esc)');
  bar.append(zoomOut, readout, zoomIn, fitBtn, closeBtn);

  const stage = el('div', 'lb-stage');
  const canvas = el('div', 'lb-canvas');
  const img = el('img');
  img.src = src;
  img.alt = caption;
  img.draggable = false;
  canvas.appendChild(img);
  let mark = null;
  if (box && box.length === 4) {
    // Fractions of the page, measured from its top-left corner, in a canvas that
    // is exactly the image box. Zoom and pan transform the canvas, so the image
    // and its rectangle move as one -- percentages stay the page's own and the
    // mark cannot drift off the line it was drawn on.
    const pad = 0.004;
    mark = el('span', 'lb-box');
    mark.style.left = (Math.max(box[0] - pad, 0) * 100).toFixed(3) + '%';
    mark.style.top = (Math.max(box[1] - pad, 0) * 100).toFixed(3) + '%';
    mark.style.width = ((box[2] - box[0] + pad * 2) * 100).toFixed(3) + '%';
    mark.style.height = ((box[3] - box[1] + pad * 2) * 100).toFixed(3) + '%';
    mark.setAttribute('aria-hidden', 'true');
    canvas.appendChild(mark);
  }
  stage.appendChild(canvas);
  fig.append(bar, stage);
  fig.appendChild(el('figcaption', null,
    caption + ' · scroll or pinch to zoom, drag to pan, Esc closes'));
  // The quoted source line is the evidence; the image is context for it. Rendered
  // as text, never as markup -- it came out of someone else's document.
  if (evidence) fig.appendChild(el('blockquote', 'lb-evidence', '“' + evidence + '”'));
  if (evidence && !mark) {
    fig.appendChild(el('p', 'lb-nocoords',
      'No coordinates available for this source, so nothing is drawn on the page. '
      + 'The rectangle needs the PDF’s own text layer; this document’s quote came '
      + 'from a source that has none.'));
  }
  document.body.appendChild(fig);

  // ------------------------------------------------------------------
  // zoom and pan. scale 1 is "fit": the whole page in the stage.
  // ------------------------------------------------------------------
  const MIN_SCALE = 1;
  const MAX_SCALE = 8;
  const STEP = 1.5;
  let scale = 1;
  let tx = 0;
  let ty = 0;

  const render = () => {
    canvas.style.transform = `translate3d(${tx}px, ${ty}px, 0) scale(${scale})`;
    readout.textContent = Math.round(scale * 100) + '%';
    stage.classList.toggle('zoomed', scale > MIN_SCALE + 1e-3);
    zoomOut.disabled = scale <= MIN_SCALE + 1e-3;
    zoomIn.disabled = scale >= MAX_SCALE - 1e-3;
  };

  const clampPan = () => {
    // Bounds from the layout box, not getBoundingClientRect: the rect is the
    // transformed one, so its size already contains the scale and the centre
    // already contains the pan -- dividing it out again would double-count both.
    const roomX = Math.max(0, (canvas.offsetWidth * scale - stage.clientWidth) / 2);
    const roomY = Math.max(0, (canvas.offsetHeight * scale - stage.clientHeight) / 2);
    tx = Math.min(roomX, Math.max(-roomX, tx));
    ty = Math.min(roomY, Math.max(-roomY, ty));
  };

  const fitImage = () => {
    if (!img.naturalWidth || !stage.clientWidth) return;
    // One factor for both sides, so the ratio is the file's own at every size.
    const k = Math.min(stage.clientWidth / img.naturalWidth,
                       stage.clientHeight / img.naturalHeight, 1);
    img.style.width = Math.max(1, Math.floor(img.naturalWidth * k)) + 'px';
    img.style.height = 'auto';
    clampPan();
    render();
  };

  const setZoom = (next, anchor, from = { scale, tx, ty }) => {
    const to = Math.min(MAX_SCALE, Math.max(MIN_SCALE, next));
    if (Math.abs(to - from.scale) < 1e-6) return;
    if (anchor) {
      // The transform is translate-then-scale about the centre, so a content
      // point c sits at p = t + s*c. Holding p still under the pointer (or
      // between two fingers) gives t' = p - (p - t) * s'/s.
      const rect = stage.getBoundingClientRect();
      const px = anchor.x - (rect.left + rect.width / 2);
      const py = anchor.y - (rect.top + rect.height / 2);
      tx = px - (px - from.tx) * (to / from.scale);
      ty = py - (py - from.ty) * (to / from.scale);
    } else {
      tx = from.tx * (to / from.scale);
      ty = from.ty * (to / from.scale);
    }
    scale = to;
    clampPan();
    render();
  };

  const reset = () => {
    scale = 1;
    tx = 0;
    ty = 0;
    clampPan();
    render();
  };

  zoomIn.addEventListener('click', () => setZoom(scale * STEP));
  zoomOut.addEventListener('click', () => setZoom(scale / STEP));
  fitBtn.addEventListener('click', reset);

  const onWheel = (event) => {
    // A trackpad pinch arrives as ctrl+wheel and a plain wheel means the same
    // thing to someone reading a scanned page. Neither may scroll the report
    // behind the overlay.
    event.preventDefault();
    const factor = Math.exp(-event.deltaY * (event.ctrlKey ? 0.01 : 0.0015));
    setZoom(scale * factor, { x: event.clientX, y: event.clientY });
  };
  stage.addEventListener('wheel', onWheel, { passive: false });

  // Pointer events cover the mouse, a finger, and two fingers at once.
  const pointers = new Map();
  let pinch = null;
  let drag = null;
  let suppressClick = false;

  const beginPinch = () => {
    const [a, b] = [...pointers.values()];
    return { dist: Math.hypot(a.x - b.x, a.y - b.y) || 1, scale, tx, ty };
  };

  stage.addEventListener('pointerdown', (event) => {
    if (event.target.closest('.lb-btn')) return;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
    if (pointers.size === 2) {
      // Capture both fingers so neither one leaving the stage ends the gesture
      // early. Capturing is what makes the move events keep arriving.
      for (const id of pointers.keys()) {
        try {
          stage.setPointerCapture(id);
        } catch {
          // Synthetic events have no live pointer to capture; the handlers below
          // still track them by id, which is what the pinch test drives.
        }
      }
      drag = null;
      pinch = beginPinch();
      stage.classList.add('dragging');
    } else if (pointers.size === 1 && scale > MIN_SCALE + 1e-3) {
      // Only capture when this really is a pan. A capture also retargets the
      // click that ends it to the stage, and at fit a click on the page must
      // stay a click on the page.
      try {
        stage.setPointerCapture(event.pointerId);
      } catch { /* synthetic pointer */ }
      drag = { x: event.clientX, y: event.clientY, tx, ty, moved: false };
      stage.classList.add('dragging');
    }
  });

  stage.addEventListener('pointermove', (event) => {
    if (!pointers.has(event.pointerId)) return;
    pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });

    if (pinch && pointers.size >= 2) {
      const [a, b] = [...pointers.values()];
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      setZoom(pinch.scale * (dist / pinch.dist),
              { x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }, pinch);
      return;
    }
    if (!drag) return;
    const dx = event.clientX - drag.x;
    const dy = event.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) > 4) drag.moved = true;
    tx = drag.tx + dx;
    ty = drag.ty + dy;
    clampPan();
    render();
  });

  const endPointer = (event) => {
    if (!pointers.delete(event.pointerId)) return;
    if (pointers.size < 2) pinch = null;
    if (pointers.size === 0) {
      stage.classList.remove('dragging');
      // A drag ends with a click on the stage; that was a pan, not a request to
      // dismiss the evidence.
      if (drag && drag.moved) suppressClick = true;
      drag = null;
    }
  };
  stage.addEventListener('pointerup', endPointer);
  stage.addEventListener('pointercancel', endPointer);

  const close = () => {
    document.removeEventListener('keydown', onKey);
    window.removeEventListener('resize', onResize);
    fig.remove();
    if (opener && typeof opener.focus === 'function') opener.focus();
  };

  fig.addEventListener('click', (event) => {
    if (suppressClick) {
      suppressClick = false;
      return;
    }
    if (event.target.closest('.lb-btn')) return;
    // Only the dimmed area closes: the figure's own padding, or the stage around
    // the page. Clicking the image is how a reader gets the page out of the way
    // while zooming, and it must never dismiss the thing they are reading.
    if (event.target === fig || event.target === stage) close();
  });
  closeBtn.addEventListener('click', close);

  const onKey = (event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
    } else if (event.key === '+' || event.key === '=') {
      event.preventDefault();
      setZoom(scale * STEP);
    } else if (event.key === '-' || event.key === '_') {
      event.preventDefault();
      setZoom(scale / STEP);
    } else if (event.key === '0') {
      event.preventDefault();
      reset();
    }
  };
  const onResize = () => {
    fitImage();
    clampPan();
    render();
  };
  document.addEventListener('keydown', onKey);
  window.addEventListener('resize', onResize);

  img.addEventListener('load', fitImage);
  if (img.complete) fitImage();
  render();
  closeBtn.focus({ preventScroll: true });
}

async function copyRunLink() {
  const url = location.origin + '/?run=' + encodeURIComponent(state.shipmentId);
  try {
    await navigator.clipboard.writeText(url);
    setProgress('Link copied.');
    setTimeout(() => setProgress(''), 2500);
  } catch (err) {
    showError('Your browser blocked clipboard access. The link is in the address bar.');
  }
}

// ---------------------------------------------------------------------
// configuration drawer
// ---------------------------------------------------------------------

function wireDrawer() {
  buildSegmented($('#provider-seg'), PROVIDERS, 'nvidia', (v) => { state.formProvider = v; });
  wireApiKey();
  const token = $('#cfg-token');
  token.addEventListener('input', () => {
    // Memory only, for this tab. Saving a config token to localStorage would
    // put the thing that guards your API keys where any script on the origin
    // can read it.
    state.configToken = token.value;
    openDrawer();
  });
}

function configHeaders(extra = {}) {
  const headers = { ...extra };
  if (state.configToken) headers['X-Config-Token'] = state.configToken;
  if (state.apiKey) headers['X-API-Key'] = state.apiKey;
  return headers;
}

function wireApiKey() {
  const field = $('#cfg-apikey');
  const submit = $('#apikey-submit');
  if (!field || !submit) return;
  field.addEventListener('input', () => { state.apiKey = field.value; });
  submit.addEventListener('click', async () => {
    const key = (field.value || '').trim();
    if (!key) { setStatus('Paste an API key first.', true); return; }
    setStatus('Opening a session…');
    try {
      const res = await fetch('/api/session', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ key }),
      });
      if (res.status === 401) throw new Error('that key is not valid here');
      if (!res.ok) throw new Error('HTTP ' + res.status);
      // The cookie is now set, so forget the key itself: it has no further use
      // in this tab, and holding it invites it into a later request log.
      state.apiKey = '';
      field.value = '';
      closeDrawer();
      setStatus('Unlocked.', false, true);
      loadHealth();
      loadFixtures();
      loadHistory();
    } catch (err) {
      setStatus('Could not unlock: ' + err.message, true);
    }
  });
}

/** Ask for a key only when the instance actually refuses to answer without one. */
async function ensureAccess() {
  try {
    await getJSON('/api/fixtures');
    return true;
  } catch (err) {
    if (err.status !== 401) { showError(err.message); return false; }
    $('#drawer').hidden = false;
    $('#scrim').hidden = false;
    $('#apikey-field').hidden = false;
    $('#cfg-apikey').focus();
    setStatus('This instance is protected. Enter an API key to continue.', true);
    return false;
  }
}

async function openDrawer() {
  let config;
  try {
    const res = await fetch('/api/config', { headers: configHeaders() });
    if (res.status === 401) {
      // Keep the drawer open and show the field: this is the one case where
      // refusing the request has to invite the user to fix it.
      $('#token-field').hidden = false;
      setStatus('This instance requires a config token.', true);
      $('#drawer').hidden = false;
      $('#scrim').hidden = false;
      $('#cfg-token').focus();
      return;
    }
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || ('HTTP ' + res.status));
    config = await res.json();
  } catch (err) {
    showError('Could not read the current configuration: ' + err.message);
    return;
  }
  state.config = config;
  $('#token-field').hidden = !config.token_required && !state.configToken;
  state.clearedKeys.clear();
  populateDrawer(state.config);
  $('#drawer').hidden = false;
  $('#scrim').hidden = false;
  document.body.style.overflow = 'hidden';
  ($('#drawer').querySelector('.input:not([disabled])') || $('#close-config')).focus();
}

function closeDrawer() {
  $('#drawer').hidden = true;
  $('#scrim').hidden = true;
  document.body.style.overflow = '';
  $('#open-config').focus();
}

function populateDrawer(cfg) {
  const locked = $('#config-locked');
  const editable = cfg.editable !== false;

  locked.hidden = editable;
  if (!editable) {
    locked.textContent =
      'Runtime configuration is disabled on this deployment (GUI_CONFIG_ENABLED=false). ' +
      'Keys must be supplied as environment variables. This protects the server on a ' +
      'public URL, where one visitor\u2019s key would otherwise serve everyone.';
  }
  $('#save-config').hidden = !editable;

  state.formProvider = cfg.vision_provider;
  buildSegmented($('#provider-seg'), PROVIDERS, cfg.vision_provider, (v) => {
    state.formProvider = v;
  });

  // Scoped deliberately: the theme control is local to this browser and stays
  // usable even when the server refuses runtime changes.
  document.querySelectorAll('#drawer .input, #drawer [data-clear], #cfg-summary, #cfg-consensus, #provider-seg button')
    .forEach((n) => { n.disabled = !editable; });

  [['nvidia', '#key-nvidia', '#key-nvidia-state', cfg.keys.nvidia],
   ['nebius', '#key-nebius', '#key-nebius-state', cfg.keys.nebius]].forEach(([, sel, stateSel, k]) => {
    const input = $(sel);
    input.value = '';
    input.placeholder = k.masked || 'not set';
    $(stateSel).textContent = k.set ? 'set · ' + k.masked : 'not set';
  });

  $('#cfg-summary').checked = cfg.enable_summary;
  $('#cfg-consensus').checked = cfg.consensus === true;
  $('#m-nvidia-vision').value = cfg.models.nvidia_vision || '';
  $('#m-nebius-vision').value = cfg.models.nebius_vision || '';
  $('#m-nebius-summary').value = cfg.models.nebius_summary || '';
  setStatus('');
}

function clearKey(field) {
  const sel = field === 'nvidia_api_key' ? '#key-nvidia' : '#key-nebius';
  $(sel).value = '';
  state.clearedKeys.add(field);
  setStatus(field === 'nvidia_api_key'
    ? 'NVIDIA key will be cleared when you apply.'
    : 'Nebius key will be cleared when you apply.');
}

function buildUpdates(cfg) {
  const updates = {};

  const keyFor = (field, sel) => {
    const typed = $(sel).value.trim();
    if (typed) updates[field] = typed;
    else if (state.clearedKeys.has(field) && cfg.keys[field.split('_')[0]].set) updates[field] = '';
  };
  keyFor('nvidia_api_key', '#key-nvidia');
  keyFor('nebius_api_key', '#key-nebius');

  if (state.formProvider !== cfg.vision_provider) updates.vision_provider = state.formProvider;
  if ($('#cfg-summary').checked !== cfg.enable_summary) updates.enable_summary = $('#cfg-summary').checked;
  if ($('#cfg-consensus').checked !== (cfg.consensus === true)) updates.consensus = $('#cfg-consensus').checked;

  [['nvidia_vision', '#m-nvidia-vision', 'nvidia_vision_model'],
   ['nebius_vision', '#m-nebius-vision', 'nebius_vision_model'],
   ['nebius_summary', '#m-nebius-summary', 'nebius_summary_model']].forEach(([cur, sel, field]) => {
    const value = $(sel).value.trim();
    if (value !== (cfg.models[cur] || '')) updates[field] = value;
  });

  return updates;
}

async function saveConfig() {
  const updates = buildUpdates(state.config);
  if (!Object.keys(updates).length) {
    setStatus('Nothing to apply — the form matches the server.', true);
    return;
  }

  setStatus('Applying…');
  $('#save-config').classList.add('busy');
  try {
    const res = await fetch('/api/config', {
      method: 'POST',
      headers: configHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(updates),
    });
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || ('HTTP ' + res.status));

    state.config = body;
    populateDrawer(body);
    setStatus('Applied to this container. Not written to disk.', false, true);
    loadHealth();
    loadHistory();
    if (state.report) poll();
  } catch (err) {
    setStatus(err.message, true);
  } finally {
    $('#save-config').classList.remove('busy');
  }
}

function setStatus(msg, bad, ok) {
  const node = $('#config-status');
  node.textContent = msg || '';
  node.className = 'hint' + (bad ? ' status-bad' : ok ? ' status-ok' : '');
}

// ---------------------------------------------------------------------
// plumbing
// ---------------------------------------------------------------------

async function getJSON(url) {
  // Headers as well as the cookie, so a client that never posted /api/session
  // still works once it has supplied a key.
  const res = await fetch(url, { headers: configHeaders() });
  if (!res.ok) {
    const err = new Error((await res.json().catch(() => ({}))).detail
                          || ('HTTP ' + res.status));
    // The status travels as a number, not inside the message: callers decide on
    // 401 by identity, because the server's own wording has no reason to
    // contain the digits -- and one real deployment shipped a silent unlock
    // panel on exactly that assumption.
    err.status = res.status;
    throw err;
  }
  return res.json();
}

function showError(msg, opts = {}) {
  const box = $('#error');
  box.hidden = !msg;
  $('#error-text').textContent = msg || '';
  const retry = $('#error-retry');
  const offer = Boolean(msg && opts.retry && state.files.length);
  retry.hidden = !offer;
  // Put the keyboard where the next action is. A message that says "retry" and
  // leaves focus on the body is a message a keyboard user has to hunt for.
  if (offer) retry.focus({ preventScroll: true });
}

function setProgress(msg) {
  $('#progress').textContent = msg || '';
}
