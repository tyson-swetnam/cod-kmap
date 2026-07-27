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
// scripts/load_coastal_datasets.py from data/datasets/coastal_datasets.json)
// + dataset_facilities (written by scripts/link_dataset_facilities.py).
//
// dataset_facilities answers "who stewards this dataset" by joining the
// free-text `provider` back to catalogued sites. It resolves 39 of the 72
// datasets; the remaining 33 name organisations that are not rows in
// `facilities` at all (NASA centres, EPA, EU/UN bodies). That distinction
// matters in the UI: a card with no producer row means *unresolved*, not
// *unproduced*, and it says so rather than rendering an empty slot.

import { getConn, whenReady, unwrapRow } from '../db.js';

let _container = null;
let _cached = null;
let _category = 'all';
let _program = 'all';
let _access = 'all';
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

// A `portal` is a human landing page and a `doi` is a citation target;
// neither lets a pipeline pull data. Everything else here is a service a
// client can query, which is the distinction the summary line counts on.
const MACHINE_TYPES = new Set([
  'erddap', 'thredds', 'opendap', 'ogc-wms', 'ogc-wfs', 'ogc-api',
  'rest-api', 's3', 'stac', 'ftp',
]);

// dataset_facilities.role — what the matched provider text said the
// organisation does. 'steward' is the default reading of `provider`.
const ROLE_LABELS = {
  steward     : 'Steward',
  operator    : 'Operator',
  host        : 'Host',
  archive     : 'Archive',
  distributor : 'Distributor',
  network     : 'Network',
};

const ACCESS_FILTERS = {
  all     : { label: 'Any access',        test: () => true },
  machine : { label: 'Machine service',   test: (r) => r.n_machine > 0 },
  portal  : { label: 'Landing page only', test: (r) => r.n_machine === 0 },
  open    : { label: 'No authentication', test: (r) => r.n_auth === 0 },
};

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
// The views re-render by replacing container.innerHTML, which throws away the
// focused element. Put focus and the caret back on the replacement so typing
// in a search box is not interrupted after every keystroke.
function restoreFocus(selector, caret) {
  const el = _container && _container.querySelector(selector);
  if (!el) return;
  el.focus();
  if (caret != null && el.setSelectionRange) {
    try { el.setSelectionRange(caret, caret); } catch { /* non-text input */ }
  }
}

function fmtInt(n) {
  if (n == null) return '—';
  return Math.round(n).toLocaleString();
}

async function fetchDatasets() {
  await whenReady();
  const conn = getConn();
  if (!conn) throw new Error('DuckDB connection not ready');
  // Endpoints and producers both rolled up into LIST<STRUCT> so the whole
  // catalogue is one round trip. unwrapRow turns the Arrow vectors into
  // plain arrays/objects.
  //
  // The producer join is LEFT on purpose and stays LEFT: a third of the
  // catalogue has no matching facility row, and those datasets must still
  // appear. `dataset_facilities` may carry several rows per dataset (a
  // regional association plus its host institution) — that is the answer,
  // not a duplicate to collapse.
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
    ),
    prod AS (
      SELECT l.dataset_id,
             list(struct_pack(
               facility_id := l.facility_id,
               name        := f.canonical_name,
               acronym     := f.acronym,
               ftype       := f.facility_type,
               ror         := f.ror,
               role        := l.role,
               method      := l.method,
               confidence  := l.confidence,
               evidence    := l.evidence
             ) ORDER BY l.confidence, f.canonical_name) AS producers
      FROM dataset_facilities l
      JOIN facilities f ON f.facility_id = l.facility_id
      GROUP BY l.dataset_id
    )
    SELECT d.*,
           eps.endpoints  AS endpoints,
           prod.producers AS producers
    FROM coastal_datasets d
    LEFT JOIN eps  ON eps.dataset_id  = d.dataset_id
    LEFT JOIN prod ON prod.dataset_id = d.dataset_id
    ORDER BY COALESCE(d.program, d.name), d.name`);
  return res.toArray().map((row) => {
    const r = unwrapRow(row.toJSON());
    // Derive the counts the summary line, the access filter and the card
    // badges all need, once per row rather than per re-render.
    const eps = Array.isArray(r.endpoints) ? r.endpoints : [];
    r.endpoints = eps;
    r.producers = Array.isArray(r.producers) ? r.producers : [];
    r.n_endpoints = eps.length;
    r.n_machine = eps.filter((e) => MACHINE_TYPES.has(e.kind)).length;
    r.n_auth = eps.filter((e) => e.auth).length;
    return r;
  });
}

function endpointBadges(row) {
  const eps = row.endpoints || [];
  if (!eps.length) {
    return `<p class="ds-empty">No access endpoint recorded.</p>`;
  }
  // Machine services first — they are the reason the catalogue exists, and
  // burying an ERDDAP base under a landing page inverts that.
  const ordered = [...eps].sort((a, b) => {
    const ma = MACHINE_TYPES.has(a.kind) ? 0 : 1;
    const mb = MACHINE_TYPES.has(b.kind) ? 0 : 1;
    return ma - mb || String(a.kind).localeCompare(String(b.kind));
  });
  const badges = ordered.map((ep) => {
    const color = ENDPOINT_COLORS[ep.kind] || '#64748b';
    // The tooltip is where the format actually lives, so name every part
    // of it — a bare URL tooltip told the reader nothing they couldn't see.
    const title = [
      ep.label,
      ep.fmt ? `format: ${ep.fmt}` : null,
      ep.auth ? 'authentication required' : 'no authentication recorded',
      ep.url,
    ].filter(Boolean).join(' — ');
    return `
      <span class="ds-ep${MACHINE_TYPES.has(ep.kind) ? ' ds-ep-machine' : ''}"
            style="--ep:${color}">
        <a class="ds-ep-link" href="${esc(ep.url)}" target="_blank" rel="noopener"
           title="${esc(title)}">${esc(ep.kind)}${ep.auth ? ' 🔒' : ''}</a>
        <button class="ds-ep-copy" data-url="${esc(ep.url)}"
                title="Copy ${esc(ep.url)}" aria-label="Copy endpoint URL">⧉</button>
      </span>`;
  }).join('');

  // Say what the badge row adds up to. '3 endpoints · 2 machine services'
  // is the sentence a reader was otherwise counting badges to reconstruct.
  const bits = [`${row.n_endpoints} endpoint${row.n_endpoints === 1 ? '' : 's'}`];
  bits.push(row.n_machine
    ? `${row.n_machine} machine service${row.n_machine === 1 ? '' : 's'}`
    : 'landing page only');
  if (row.n_auth) bits.push(`${row.n_auth} need${row.n_auth === 1 ? 's' : ''} auth`);
  return `
    <div class="ds-eps">${badges}</div>
    <p class="ds-ep-note">${esc(bits.join(' · '))}</p>`;
}

// Producer edges. The empty case carries the weight here: an unmatched
// dataset is not a dataset without a steward, it is one whose steward is
// not a row in `facilities`, and the copy has to say which.
function producerBlock(row) {
  const prods = row.producers || [];
  if (!prods.length) {
    return `
      <p class="ds-producers ds-producers-none" title="dataset_facilities has no row for this dataset">
        <span class="ds-label">Stewarded by</span>
        <span class="ds-unresolved">not resolved to a catalogued site</span>
      </p>`;
  }
  const chips = prods.map((p) => {
    const label = p.acronym && p.acronym !== p.name ? p.acronym : p.name;
    const role = ROLE_LABELS[p.role] || p.role || 'Steward';
    const title = [
      p.name,
      `${role.toLowerCase()} — matched by ${p.method} (${p.confidence} confidence)`,
      p.evidence,
      p.ror ? `ROR ${p.ror}` : null,
    ].filter(Boolean).join('\n');
    return `
      <span class="ds-prod ds-prod-${esc(p.confidence)}" title="${esc(title)}">
        <span class="ds-prod-role">${esc(role)}</span>
        <span class="ds-prod-name">${esc(label)}</span>
      </span>`;
  }).join('');
  return `
    <p class="ds-producers">
      <span class="ds-label">Stewarded by</span>${chips}
    </p>`;
}

// Where the record itself came from. Every dataset row carries a
// source_url and a confidence by repo convention; showing them turns the
// card from an assertion into a citable one.
function provenanceLine(row) {
  const bits = [];
  if (row.confidence) {
    bits.push(`<span class="ds-conf ds-conf-${esc(row.confidence)}"
      title="Curator confidence in this catalogue record">${esc(row.confidence)} confidence</span>`);
  }
  if (row.source) bits.push(`<span>${esc(row.source)}</span>`);
  if (row.retrieved_at) {
    const d = row.retrieved_at instanceof Date
      ? row.retrieved_at.toISOString().slice(0, 10) : String(row.retrieved_at);
    bits.push(`<span>retrieved ${esc(d)}</span>`);
  }
  if (!bits.length) return '';
  return `<p class="ds-prov">${bits.join(' · ')}</p>`;
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
    footer.push(`<a href="${esc(row.source_url)}" target="_blank" rel="noopener"
      title="The page this catalogue record was read from">Provenance source</a>`);
  }

  return `
    <article class="ds-card" id="ds-${esc(row.dataset_id)}" data-id="${esc(row.dataset_id)}">
      <header class="ds-card-head">
        <h3>${title}</h3>
        <span class="ds-cat">${esc(CATEGORY_LABELS[row.category] || row.category)}</span>
      </header>
      <p class="ds-provider">${esc(row.provider)}</p>
      ${producerBlock(row)}
      ${row.description ? `<p class="ds-desc">${esc(row.description)}</p>` : ''}
      ${endpointBadges(row)}
      <div class="ds-coverage">${coverage(row)}</div>
      ${row.variables ? `<p class="ds-vars"><strong>Variables:</strong> ${esc(row.variables)}</p>` : ''}
      ${row.license ? `<p class="ds-license">${esc(row.license)}</p>` : ''}
      ${row.notes ? `<p class="ds-note">${esc(row.notes)}</p>` : ''}
      ${provenanceLine(row)}
      ${footer.length ? `<footer class="ds-links">${footer.join(' · ')}</footer>` : ''}
    </article>`;
}

function applyFilter(rows) {
  let out = rows;
  if (_category !== 'all') out = out.filter((r) => r.category === _category);
  if (_program !== 'all') out = out.filter((r) => (r.program || '—') === _program);
  if (_access !== 'all' && ACCESS_FILTERS[_access]) {
    out = out.filter(ACCESS_FILTERS[_access].test);
  }
  const q = _qFilter.trim().toLowerCase();
  if (q) {
    out = out.filter((r) => {
      const eps = r.endpoints || [];
      const prods = r.producers || [];
      return [r.name, r.acronym, r.provider, r.program, r.description,
              r.variables, r.spatial_coverage,
              ...eps.map((e) => `${e.kind} ${e.url}`),
              ...prods.map((p) => `${p.name} ${p.acronym || ''} ${p.role}`)]
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
    .map(([program, items]) => {
      const eps = items.reduce((n, r) => n + r.n_endpoints, 0);
      const linked = items.filter((r) => (r.producers || []).length > 0).length;
      return `
      <section class="ds-group">
        <h2>${esc(program)}
          <span class="ds-count">${items.length} dataset${items.length === 1 ? '' : 's'}</span>
          <span class="ds-count">${eps} endpoint${eps === 1 ? '' : 's'}</span>
          <span class="ds-count ds-count-linked"
                title="Datasets in this group resolved to a stewarding site">${
            linked}/${items.length} stewarded</span>
        </h2>
        <div class="ds-grid">${items
          .sort((a, b) => String(a.name).localeCompare(String(b.name)))
          .map(cardHtml).join('')}</div>
      </section>`;
    }).join('');
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
          <code>data/datasets/coastal_datasets.json</code>, then
          <code>python scripts/link_dataset_facilities.py --export-parquet</code>
          to derive the dataset → stewarding-site edges.
        </p>
      </div>`;
    return;
  }

  const rows = applyFilter(_cached);
  const nEps = _cached.reduce((n, r) => n + r.n_endpoints, 0);
  const nMachine = _cached.filter((r) => r.n_machine > 0).length;
  const nPortalOnly = _cached.length - nMachine;
  const nAuth = _cached.filter((r) => r.n_auth > 0).length;
  const nLinked = _cached.filter((r) => (r.producers || []).length > 0).length;
  const nSites = new Set(
    _cached.flatMap((r) => (r.producers || []).map((p) => p.facility_id))).size;
  const epCounts = _cached.map((r) => r.n_endpoints).sort((a, b) => a - b);
  const medEps = epCounts.length
    ? (epCounts.length % 2
      ? epCounts[(epCounts.length - 1) / 2]
      : (epCounts[epCounts.length / 2 - 1] + epCounts[epCounts.length / 2]) / 2)
    : 0;

  const cats = [...new Set(_cached.map((r) => r.category))].sort();
  const programs = [...new Set(_cached.map((r) => r.program || '—'))].sort();
  const accessKeys = Object.keys(ACCESS_FILTERS);

  _container.innerHTML = `
    <div class="ds-page">
      <header class="ds-header">
        <h1>Curated coastal datasets</h1>
        <!-- The two totals used to be a single run-on sentence, so a reader
             asking "how many of these can I actually pull?" had to do the
             arithmetic. Break them into counted facts instead. -->
        <ul class="ds-facts">
          <li><strong>${fmtInt(_cached.length)}</strong> datasets,
            <strong>${fmtInt(nEps)}</strong> access endpoints
            (median ${esc(medEps)} per dataset)</li>
          <li><strong>${fmtInt(nMachine)}</strong> expose a queryable service
            — ERDDAP, THREDDS, OPeNDAP, OGC, REST, S3, STAC;
            <strong>${fmtInt(nPortalOnly)}</strong> offer only a landing page</li>
          <li><strong>${fmtInt(nAuth)}</strong> have at least one endpoint
            behind authentication (🔒)</li>
          <li><strong>${fmtInt(nLinked)}</strong> of ${fmtInt(_cached.length)}
            are resolved to a stewarding site in this catalogue
            (<strong>${fmtInt(nSites)}</strong> distinct sites). The other
            ${fmtInt(_cached.length - nLinked)} name organisations that are
            not catalogued facilities — NASA centres, EPA, EU and UN bodies —
            so their steward is <em>unresolved</em>, not absent.</li>
        </ul>
        <p class="ds-summary">
          Click an endpoint badge to open it, or ⧉ to copy the URL. Hover a
          steward chip for the match method and the text it matched on.
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
          <label>Access:
            <select id="ds-access">
              ${accessKeys.map((k) => `<option value="${esc(k)}"${_access === k ? ' selected' : ''}>${
                esc(ACCESS_FILTERS[k].label)} (${_cached.filter(ACCESS_FILTERS[k].test).length})</option>`).join('')}
            </select>
          </label>
          <input id="ds-q" type="search"
                 placeholder="Search name, provider, steward, variable, endpoint…"
                 value="${esc(_qFilter)}">
        </div>
        <p class="ds-count-line">Showing <strong>${fmtInt(rows.length)}</strong>
          of <strong>${fmtInt(_cached.length)}</strong> datasets ·
          <strong>${fmtInt(rows.reduce((n, r) => n + r.n_endpoints, 0))}</strong>
          of <strong>${fmtInt(nEps)}</strong> endpoints.</p>
      </header>
      ${rows.length ? groupedHtml(rows) : `
        <p class="no-data">No dataset matches these filters.</p>`}
      <p class="ds-status">Done.</p>
    </div>`;

  _container.querySelector('#ds-cat').addEventListener('change', (ev) => {
    _category = ev.target.value;
    renderDatasets(null);
  });
  _container.querySelector('#ds-prog').addEventListener('change', (ev) => {
    _program = ev.target.value;
    renderDatasets(null);
  });
  _container.querySelector('#ds-access').addEventListener('change', (ev) => {
    _access = ev.target.value;
    renderDatasets(null);
  });
  _container.querySelector('#ds-q').addEventListener('input', (ev) => {
    _qFilter = ev.target.value;
    const caret = ev.target.selectionStart;
    // See the note in scholars.js: the re-render destroys this input, so
    // focus and caret must be restored on its replacement.
    renderDatasets(null).then(() => restoreFocus('#ds-q', caret));
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
