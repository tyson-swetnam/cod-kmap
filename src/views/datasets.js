// datasets.js — Curated coastal dataset catalogue (#/data).
//
// The datasets a coastal observatory design has to integrate, each with
// its actual access endpoints: ERDDAP bases, THREDDS catalogues, OPeNDAP,
// OGC services, REST API roots, S3 buckets, DOIs. The endpoint URL is
// the point of the tab, so every one is both a link and copy-able.
//
// Routes:
//   #/data              → filterable catalogue grouped by program
//   #/data/<dataset_id> → that dataset's card scrolled into view
//
// Data source: coastal_datasets + dataset_endpoints (written by
// scripts/load_coastal_datasets.py from data/datasets/coastal_datasets.json).

import { getConn, whenReady, unwrapRow } from '../db.js';

let _container = null;
let _cached = null;
let _category = 'all';
let _program = 'all';
let _qFilter = '';

const CATEGORY_LABELS = {
  'observing-system'   : 'Observing systems',
  'monitoring-program' : 'Monitoring programs',
  'data-portal'        : 'Data portals',
  'remote-sensing'     : 'Remote sensing',
  'synthesis-network'  : 'Synthesis networks',
  'model-output'       : 'Model output',
  'archive'            : 'Archives',
  'mapping-product'    : 'Mapping products',
};

// Machine-readable services get a saturated badge; the human landing page
// is deliberately muted so the API endpoints read first.
const ENDPOINT_COLORS = {
  erddap    : '#0d9488',
  thredds   : '#0369a1',
  opendap   : '#1d4ed8',
  'ogc-wms' : '#7c3aed',
  'ogc-wfs' : '#9333ea',
  'ogc-api' : '#a21caf',
  'rest-api': '#16a34a',
  s3        : '#b45309',
  ftp       : '#78716c',
  stac      : '#c2410c',
  doi       : '#be185d',
  portal    : '#94a3b8',
};

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function fmtInt(n) {
  if (n == null) return '—';
  return Math.round(n).toLocaleString();
}

async function fetchDatasets() {
  await whenReady();
  const conn = getConn();
  if (!conn) throw new Error('DuckDB connection not ready');
  // Endpoints rolled up into a LIST<STRUCT> so the catalogue is one round
  // trip. unwrapRow turns the Arrow vectors into plain arrays/objects.
  const res = await conn.query(`
    WITH eps AS (
      SELECT dataset_id,
             list(struct_pack(
               kind  := endpoint_type,
               url   := url,
               label := label,
               fmt   := format_notes,
               auth  := auth_required
             ) ORDER BY endpoint_type) AS endpoints
      FROM dataset_endpoints
      GROUP BY dataset_id
    )
    SELECT d.*, eps.endpoints AS endpoints
    FROM coastal_datasets d
    LEFT JOIN eps ON eps.dataset_id = d.dataset_id
    ORDER BY COALESCE(d.program, d.name), d.name`);
  return res.toArray().map((row) => unwrapRow(row.toJSON()));
}

function endpointBadges(row) {
  const eps = Array.isArray(row.endpoints) ? row.endpoints : [];
  if (!eps.length) return '';
  const badges = eps.map((ep) => {
    const color = ENDPOINT_COLORS[ep.kind] || '#64748b';
    const title = [ep.label, ep.fmt, ep.auth ? 'authentication required' : null]
      .filter(Boolean).join(' — ');
    return `
      <span class="ds-ep" style="--ep:${color}">
        <a class="ds-ep-link" href="${esc(ep.url)}" target="_blank" rel="noopener"
           title="${esc(title || ep.url)}">${esc(ep.kind)}${ep.auth ? ' 🔒' : ''}</a>
        <button class="ds-ep-copy" data-url="${esc(ep.url)}"
                title="Copy ${esc(ep.url)}" aria-label="Copy endpoint URL">⧉</button>
      </span>`;
  }).join('');
  return `<div class="ds-eps">${badges}</div>`;
}

function coverage(row) {
  const years = row.temporal_start
    ? `${row.temporal_start}–${row.temporal_end ?? 'present'}`
    : null;
  return [row.spatial_coverage, years, row.update_frequency]
    .filter(Boolean).map((s) => `<span>${esc(s)}</span>`).join('');
}

function cardHtml(row) {
  const title = row.acronym && row.acronym !== row.name
    ? `${esc(row.acronym)} — ${esc(row.name)}`
    : esc(row.name);
  const footer = [];
  if (row.homepage_url) {
    footer.push(`<a href="${esc(row.homepage_url)}" target="_blank" rel="noopener">Homepage</a>`);
  }
  if (row.doi) {
    const href = String(row.doi).startsWith('http')
      ? row.doi : `https://doi.org/${row.doi}`;
    footer.push(`<a href="${esc(href)}" target="_blank" rel="noopener">DOI</a>`);
  }
  if (row.source_url && row.source_url !== row.homepage_url) {
    footer.push(`<a href="${esc(row.source_url)}" target="_blank" rel="noopener">Source</a>`);
  }

  return `
    <article class="ds-card" id="ds-${esc(row.dataset_id)}" data-id="${esc(row.dataset_id)}">
      <header class="ds-card-head">
        <h3>${title}</h3>
        <span class="ds-cat">${esc(CATEGORY_LABELS[row.category] || row.category)}</span>
      </header>
      <p class="ds-provider">${esc(row.provider)}</p>
      ${row.description ? `<p class="ds-desc">${esc(row.description)}</p>` : ''}
      ${endpointBadges(row)}
      <div class="ds-coverage">${coverage(row)}</div>
      ${row.variables ? `<p class="ds-vars"><strong>Variables:</strong> ${esc(row.variables)}</p>` : ''}
      ${row.license ? `<p class="ds-license">${esc(row.license)}</p>` : ''}
      ${row.notes ? `<p class="ds-note">${esc(row.notes)}</p>` : ''}
      ${footer.length ? `<footer class="ds-links">${footer.join(' · ')}</footer>` : ''}
    </article>`;
}

function applyFilter(rows) {
  let out = rows;
  if (_category !== 'all') out = out.filter((r) => r.category === _category);
  if (_program !== 'all') out = out.filter((r) => (r.program || '—') === _program);
  const q = _qFilter.trim().toLowerCase();
  if (q) {
    out = out.filter((r) => {
      const eps = Array.isArray(r.endpoints) ? r.endpoints : [];
      return [r.name, r.acronym, r.provider, r.program, r.description,
              r.variables, r.spatial_coverage,
              ...eps.map((e) => `${e.kind} ${e.url}`)]
        .join(' ').toLowerCase().includes(q);
    });
  }
  return out;
}

const OTHER_GROUP = 'Individual programs';

function groupedHtml(rows) {
  // Group by program so the 15 IOOS records read as one family. Most
  // programs contribute a single dataset, though, and a heading per card
  // is worse than no heading at all — so only a program with more than
  // one dataset earns its own section; the rest pool together.
  const counts = new Map();
  for (const r of rows) {
    const key = r.program || OTHER_GROUP;
    counts.set(key, (counts.get(key) || 0) + 1);
  }
  const groups = new Map();
  for (const r of rows) {
    const program = r.program || OTHER_GROUP;
    const key = counts.get(program) > 1 ? program : OTHER_GROUP;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  }
  return [...groups.entries()]
    .sort((a, b) => {
      // Keep the catch-all last however big it grows.
      if (a[0] === OTHER_GROUP) return 1;
      if (b[0] === OTHER_GROUP) return -1;
      return b[1].length - a[1].length || a[0].localeCompare(b[0]);
    })
    .map(([program, items]) => `
      <section class="ds-group">
        <h2>${esc(program)} <span class="ds-count">${items.length}</span></h2>
        <div class="ds-grid">${items
          .sort((a, b) => String(a.name).localeCompare(String(b.name)))
          .map(cardHtml).join('')}</div>
      </section>`).join('');
}

async function renderDatasets(targetId) {
  if (!_container) return;
  const status = _container.querySelector('.ds-status');
  if (status) status.textContent = 'Loading…';

  if (!_cached) {
    try {
      _cached = await fetchDatasets();
    } catch (e) {
      if (status) status.textContent = `Failed to load: ${e.message}`;
      console.error(e);
      return;
    }
  }

  if (!_cached.length) {
    _container.innerHTML = `
      <div class="ds-page">
        <header class="ds-header"><h1>Curated coastal datasets</h1></header>
        <p class="no-data">
          Catalogue not loaded yet. Run
          <code>python scripts/load_coastal_datasets.py</code> to load
          <code>data/datasets/coastal_datasets.json</code>.
        </p>
      </div>`;
    return;
  }

  const rows = applyFilter(_cached);
  const nEps = _cached.reduce(
    (n, r) => n + (Array.isArray(r.endpoints) ? r.endpoints.length : 0), 0);
  const nMachine = _cached.filter((r) => (Array.isArray(r.endpoints) ? r.endpoints : [])
    .some((e) => e.kind !== 'portal')).length;

  const cats = [...new Set(_cached.map((r) => r.category))].sort();
  const programs = [...new Set(_cached.map((r) => r.program || '—'))].sort();

  _container.innerHTML = `
    <div class="ds-page">
      <header class="ds-header">
        <h1>Curated coastal datasets</h1>
        <p class="ds-summary">
          <strong>${fmtInt(_cached.length)}</strong> datasets with
          <strong>${fmtInt(nEps)}</strong> access endpoints;
          <strong>${fmtInt(nMachine)}</strong> expose a machine-readable
          service (ERDDAP, THREDDS, OPeNDAP, OGC, REST, S3, STAC). Click an
          endpoint badge to open it, or use ⧉ to copy the URL.
        </p>
        <div class="ds-controls">
          <label>Category:
            <select id="ds-cat">
              <option value="all">All (${_cached.length})</option>
              ${cats.map((c) => `<option value="${esc(c)}"${_category === c ? ' selected' : ''}>${
                esc(CATEGORY_LABELS[c] || c)} (${_cached.filter((r) => r.category === c).length})</option>`).join('')}
            </select>
          </label>
          <label>Program:
            <select id="ds-prog">
              <option value="all">All</option>
              ${programs.map((p) => `<option value="${esc(p)}"${_program === p ? ' selected' : ''}>${
                esc(p)} (${_cached.filter((r) => (r.program || '—') === p).length})</option>`).join('')}
            </select>
          </label>
          <input id="ds-q" type="search" placeholder="Search name, provider, variable, endpoint…"
                 value="${esc(_qFilter)}">
        </div>
        <p class="ds-count-line">Showing <strong>${fmtInt(rows.length)}</strong>
          of <strong>${fmtInt(_cached.length)}</strong>.</p>
      </header>
      ${groupedHtml(rows)}
      <p class="ds-status">Done.</p>
    </div>`;

  _container.querySelector('#ds-cat').addEventListener('change', (ev) => {
    _category = ev.target.value;
    renderDatasets(targetId);
  });
  _container.querySelector('#ds-prog').addEventListener('change', (ev) => {
    _program = ev.target.value;
    renderDatasets(targetId);
  });
  _container.querySelector('#ds-q').addEventListener('input', (ev) => {
    _qFilter = ev.target.value;
    renderDatasets(targetId);
  });

  for (const btn of _container.querySelectorAll('.ds-ep-copy')) {
    btn.addEventListener('click', async (ev) => {
      ev.preventDefault();
      const url = btn.dataset.url;
      try {
        await navigator.clipboard.writeText(url);
      } catch {
        // clipboard API needs a secure context; file:// and plain http
        // on a LAN address don't get one, so fall back to a selection.
        const ta = document.createElement('textarea');
        ta.value = url;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        try { document.execCommand('copy'); } catch { /* nothing left to try */ }
        document.body.removeChild(ta);
      }
      const prev = btn.textContent;
      btn.textContent = '✓';
      setTimeout(() => { btn.textContent = prev; }, 1200);
    });
  }

  if (targetId) {
    const el = _container.querySelector(`#ds-${CSS.escape(targetId)}`);
    if (el) {
      el.classList.add('ds-card-active');
      requestAnimationFrame(() => el.scrollIntoView({
        behavior: 'smooth', block: 'start',
      }));
    }
  }
}

export function initDatasetsView(container) {
  _container = container;
  _container.innerHTML = `
    <div class="ds-page">
      <p class="ds-status" style="padding:24px;color:#64748b">
        Dataset catalogue loading…
      </p>
    </div>`;
}

export function renderDatasetsView(targetId) {
  if (!_container) return;
  renderDatasets(targetId).catch((e) => {
    console.error('[datasets] render failed', e);
    const s = _container.querySelector('.ds-status');
    if (s) s.textContent = `Render failed: ${e.message}`;
  });
}
