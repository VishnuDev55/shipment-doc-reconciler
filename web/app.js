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
  ['other', 'Other'],
];

const FIELD_LABELS = {
  quantity: 'Quantity',
  gross_weight: 'Gross weight',
  container_number: 'Container no.',
  hs_code: 'HS code',
  consignee_name: 'Consignee',
  invoice_value: 'Invoice value',
};

const ICON = {
  consistent: '✓', suspicious: '⚠', conflict: '✗',
  single_source: '•', absent: '–',
};

const HEADLINE = {
  clean: 'All cross-checked fields agree',
  review: 'No hard conflicts — a few fields deserve a glance',
  conflicts: 'Conflicts found — reconcile before the goods move',
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
  shipmentId: null,
  pollTimer: null,
  showRaw: false,
  filter: null,
  report: null,
  health: null,
  config: null,
  clearedKeys: new Set(),
};

// ---------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------

document.addEventListener('DOMContentLoaded', () => {
  initTheme();
  wireDropzone();
  wireButtons();
  wireDrawer();

  loadHealth();
  loadFixtures();
  loadHistory();

  const params = new URLSearchParams(location.search);
  const wanted = params.get('demo');
  if (wanted) runFixture(wanted);
  else if (params.get('run')) openRun(params.get('run'));
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

async function loadHealth() {
  try {
    const h = await getJSON('/api/health');
    state.health = h;
    renderModeChip(h);
    renderConnection(h);
    renderKeyWarning(h);
  } catch (err) {
    const chip = $('#mode-chip');
    chip.hidden = false;
    chip.className = 'flag flag-bad';
    chip.textContent = 'cannot reach the server';
  }
}

function renderModeChip(h) {
  const chip = $('#mode-chip');
  chip.hidden = false;
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
    /* the rail is a convenience; the file picker still works without it */
  }
}

async function loadHistory() {
  try {
    const data = await getJSON('/api/shipments');
    renderHistory(data.shipments || []);
  } catch (err) {
    renderHistory([]);
  }
}

function renderHistory(rows) {
  const host = $('#history');
  host.innerHTML = '';
  if (!rows.length) {
    host.appendChild(el('p', 'run-empty', 'No runs in this container yet.'));
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
  state.files.push(...accepted.slice(0, room));
  accepted.slice(0, room).forEach(() => state.declared.push(''));
  renderUploads();
}

function renderUploads() {
  const list = $('#upload-list');
  list.innerHTML = '';
  const count = state.files.length;

  if (!count) {
    list.hidden = true;
    $('#analyze').disabled = true;
    $('#reset').hidden = true;
    $('#intake-count').textContent = '';
    $('#dropzone').hidden = false;
    return;
  }
  list.hidden = false;
  $('#reset').hidden = false;
  $('#intake-count').textContent = `${count} of 5 documents`;

  state.files.forEach((file, i) => {
    const li = el('li', 'upload');
    li.appendChild(el('span', 'name', file.name));

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
      renderUploads();
    });
    li.appendChild(rm);
    list.appendChild(li);
  });

  $('#analyze').disabled = false;
}

function wireButtons() {
  $('#analyze').addEventListener('click', uploadAndAnalyse);
  $('#reset').addEventListener('click', () => {
    stopPolling();
    state.files = []; state.declared = []; state.shipmentId = null; state.report = null;
    renderUploads();
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
  document.querySelectorAll('[data-clear]').forEach((btn) =>
    btn.addEventListener('click', () => clearKey(btn.dataset.clear)));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !$('#drawer').hidden) closeDrawer();
  });
}

// ---------------------------------------------------------------------
// run
// ---------------------------------------------------------------------

async function uploadAndAnalyse() {
  showError(null);
  const form = new FormData();
  state.files.forEach((f) => form.append('files', f, f.name));
  form.append('doc_types', JSON.stringify(state.declared));

  const btn = $('#analyze');
  btn.classList.add('busy');
  btn.setAttribute('aria-disabled', 'true');
  setProgress('Uploading ' + state.files.length + ' document(s)…');
  try {
    const res = await fetch('/api/shipments', { method: 'POST', body: form });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || ('HTTP ' + res.status));
    const data = await res.json();
    beginRun(data.shipment_id);
  } catch (err) {
    showError('Upload failed: ' + err.message);
    setProgress('');
  } finally {
    btn.classList.remove('busy');
    btn.removeAttribute('aria-disabled');
  }
}

async function runFixture(name) {
  showError(null);
  setProgress('Reading the bundled sample shipment…');
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
  }
}

async function openRun(shipmentId) {
  showError(null);
  try {
    const report = await getJSON(`/api/shipments/${encodeURIComponent(shipmentId)}/report`);
    state.shipmentId = shipmentId;
    state.report = report;
    showResults();
    renderReport(report);
    loadHistory();
    if (report.state === 'running') {
      setProgress('Extracting…');
      state.pollTimer = setInterval(poll, 1000);
    } else {
      setProgress('');
    }
  } catch (err) {
    showError('Could not open that run: ' + err.message);
  }
}

function beginRun(shipmentId) {
  state.shipmentId = shipmentId;
  state.report = null;
  state.filter = null;
  showResults();
  setProgress('Extracting…');
  renderRunning();
  poll();
  state.pollTimer = setInterval(poll, 1000);
}

function showResults() {
  $('#empty').hidden = true;
  $('#results').hidden = false;
  $('#export').hidden = true;
  $('#copy-link').hidden = false;
  history.replaceState(null, '', '?run=' + encodeURIComponent(state.shipmentId));
}

async function poll() {
  if (!state.shipmentId) return;
  try {
    const report = await getJSON(`/api/shipments/${state.shipmentId}/report`);
    state.report = report;
    renderReport(report);
    if (report.state !== 'running') {
      stopPolling();
      setProgress('');
      loadHistory();
    }
  } catch (err) {
    setProgress('Still working — the last poll failed: ' + err.message);
  }
}

function stopPolling() {
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
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

function renderReport(report) {
  const s = report.summary;
  const verdict = $('#verdict-headline');
  verdict.textContent = HEADLINE[s.verdict] || s.verdict;
  verdict.closest('.verdict').className = 'panel verdict is-' + s.verdict;

  // "live" is reserved for calls made on THIS run. A report replayed entirely
  // from cache is model-backed but not live, and must not claim otherwise.
  const src = report.run.result_source;
  const n = report.run.live_calls || 0;
  let provenance;
  if (src === 'live' && n > 0) {
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

  renderNarrative(report);
  renderComparison(report);
  renderDocuments(report);
  renderTransparency(report);

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
  const tokens = { prompt: 0, completion: 0, total: 0 };
  let ms = 0;
  [
    ...docs.map((d) => d),
    { usage: n.usage, timings_ms: n.latency_ms },
  ].forEach((row) => {
    const u = row.usage;
    if (u) {
      tokens.prompt += u.prompt || 0;
      tokens.completion += u.completion || 0;
      tokens.total += u.total || 0;
    }
    ms += row.timings_ms || 0;
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
  docs.forEach((d) =>
    addRow(d.label, d.provider, d.model, d.source, d.attempts, d.usage, d.timings_ms)
  );
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
  table.appendChild(body);

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

function renderComparison(report) {
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
    tdField.appendChild(el('span', null, FIELD_LABELS[comp.field] || comp.label));
    tdField.appendChild(el('span', 'k', comp.field));
    tr.appendChild(tdField);

    const tdStatus = el('td');
    tdStatus.appendChild(el('span', 'flag flag-' + comp.status,
      (ICON[comp.status] || '') + '  ' + comp.status.replace(/_/g, ' ')));
    tr.appendChild(tdStatus);

    tr.appendChild(el('td', 'reason', comp.reason));

    report.documents.forEach((doc) => {
      const v = comp.values.find((x) => x.document_id === doc.id);
      tr.appendChild(valueCell(v));
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  $('#no-matches').hidden = rows.length > 0;
}

function valueCell(v) {
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
    `p${v.page_index} · conf ${v.confidence.toFixed(2)}` +
    (v.is_outlier ? ' · outlier' : '')));

  (v.warnings || []).forEach((w) => cell.appendChild(el('div', 'w', '⚠ ' + w)));
  return cell;
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

    if (doc.status === 'failed') {
      const row = el('div', 'row-actions');
      const retry = el('button', 'btn btn-quiet', 'Retry this document');
      retry.type = 'button';
      retry.addEventListener('click', () => retryDocument(doc.id));
      row.appendChild(retry);
      box.appendChild(row);
    }

    if (doc.extraction) {
      const grid = el('div');
      const hdr = el('div', 'evi head');
      ['Field', 'Verbatim value', 'Evidence line quoted from the page', 'Page', 'Conf.']
        .forEach((t, i) => hdr.appendChild(el('span', i >= 3 ? 'n' : null, t)));
      grid.appendChild(hdr);

      Object.entries(doc.extraction.fields).forEach(([key, f]) => {
        const row = el('div', 'evi');
        row.appendChild(el('span', null, FIELD_LABELS[key] || key));
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
        img.addEventListener('click', () => lightbox(img.src, `${doc.label} — page ${p.index}`));
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

function lightbox(src, caption) {
  const fig = el('figure', 'lightbox');
  const img = el('img');
  img.src = src;
  img.alt = caption;
  fig.appendChild(img);
  fig.appendChild(el('figcaption', null, caption + ' · click anywhere to close'));
  fig.addEventListener('click', () => fig.remove());
  document.body.appendChild(fig);
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
}

async function openDrawer() {
  try {
    state.config = await getJSON('/api/config');
  } catch (err) {
    showError('Could not read the current configuration: ' + err.message);
    return;
  }
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
  document.querySelectorAll('#drawer .input, #drawer [data-clear], #cfg-summary, #provider-seg button')
    .forEach((n) => { n.disabled = !editable; });

  [['nvidia', '#key-nvidia', '#key-nvidia-state', cfg.keys.nvidia],
   ['nebius', '#key-nebius', '#key-nebius-state', cfg.keys.nebius]].forEach(([, sel, stateSel, k]) => {
    const input = $(sel);
    input.value = '';
    input.placeholder = k.masked || 'not set';
    $(stateSel).textContent = k.set ? 'set · ' + k.masked : 'not set';
  });

  $('#cfg-summary').checked = cfg.enable_summary;
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
      headers: { 'Content-Type': 'application/json' },
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
  const res = await fetch(url);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || ('HTTP ' + res.status));
  return res.json();
}

function showError(msg) {
  const box = $('#error');
  box.hidden = !msg;
  box.textContent = msg || '';
}

function setProgress(msg) {
  $('#progress').textContent = msg || '';
}
