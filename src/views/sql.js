// sql.js — DuckDB SQL console
//
// Runs ad-hoc SQL against the parquet views registered in src/db.js
// (facilities, facility_types, networks, research_areas, regions,
// funders, area_links, network_membership, facility_regions,
// funding_links, region_area_links). Includes a handful of curated
// queries the user can click to preview interesting slices of the
// cod-kmap schema.

import { getConn, whenReady } from '../db.js';

// ── Canned queries ──────────────────────────────────────────────────
//
// Keep these intentionally short (single SELECT, no VIEWs, no CTEs
// longer than a screen) so users can read them as teaching material
// before hitting Run. Each has a title that the UI renders as a
// button label and a description that shows under the editor while
// the query is active.
const EXAMPLES = [
  {
    id: 'facilities-by-type',
    title: 'Facilities by type',
    description:
      'How the 210 facilities break down across the 10 active facility types. ' +
      'Matches the map legend.',
    sql: `-- Facilities grouped by facility_type
SELECT ft.label            AS facility_type,
       COUNT(f.facility_id) AS n
FROM   facility_types ft
JOIN   facilities     f  ON f.facility_type = ft.slug
GROUP  BY ft.label
ORDER  BY n DESC;`,
  },
  {
    id: 'top-networks',
    title: 'Top networks by membership',
    description:
      'Which observing networks / consortia have the most member facilities in the dataset.',
    sql: `-- Network membership counts
SELECT n.label                  AS network,
       n.level                  AS level,
       COUNT(nm.facility_id)    AS members
FROM   networks           n
JOIN   network_membership nm ON nm.network_id = n.network_id
GROUP  BY n.label, n.level
ORDER  BY members DESC, network;`,
  },
  {
    id: 'hot-research-areas',
    title: 'Most-studied research areas',
    description:
      'Research areas ranked by how many facilities work on them. ' +
      'Useful for spotting concentration vs. coverage gaps.',
    sql: `-- Facility-weighted research-area ranking
SELECT ra.label                 AS research_area,
       COUNT(al.facility_id)    AS facilities
FROM   research_areas ra
JOIN   area_links     al ON al.area_id = ra.area_id
GROUP  BY ra.label
ORDER  BY facilities DESC, research_area
LIMIT  25;`,
  },
  {
    id: 'facilities-per-country',
    title: 'Facilities by country',
    description:
      'Geographic distribution of the coastal-observatory dataset. ' +
      'US-heavy by design — NOAA, EPA, university marine labs.',
    sql: `-- Countries ranked by facility count
SELECT f.country                 AS iso_2,
       COUNT(*)                  AS facilities
FROM   facilities f
GROUP  BY f.country
ORDER  BY facilities DESC, iso_2;`,
  },
  {
    id: 'regions-per-network',
    title: 'Region polygons per network',
    description:
      'Counts the overlay polygons (NMS sanctuaries, NERRs, NPS units, EPA regions, etc.) ' +
      'that each parent network contributes to the map.',
    sql: `-- Overlay regions grouped by network
SELECT n.label              AS network,
       r.kind               AS kind,
       COUNT(*)             AS regions
FROM   regions   r
JOIN   networks  n ON n.network_id = r.network_id
GROUP  BY n.label, r.kind
ORDER  BY regions DESC, network, kind;`,
  },
  {
    id: 'facilities-inside-nms',
    title: 'Facilities inside NMS sanctuaries',
    description:
      'Which facilities fall inside a National Marine Sanctuary polygon? ' +
      'Joins facility_regions (spatial point-in-polygon) to regions + facilities.',
    sql: `-- Facilities located within an NMS polygon
SELECT r.name                       AS sanctuary,
       r.acronym                    AS acronym,
       f.canonical_name             AS facility,
       f.facility_type              AS type
FROM   regions            r
JOIN   facility_regions   fr ON fr.region_id  = r.region_id
JOIN   facilities         f  ON f.facility_id = fr.facility_id
WHERE  r.kind = 'sanctuary'
ORDER  BY sanctuary, facility;`,
  },
  {
    id: 'funders-leaderboard',
    title: 'Top funders by linked facilities',
    description:
      'Funders ranked by distinct facilities they touch across the dataset — ' +
      'useful for spotting agency reach beyond a single grant.',
    sql: `-- Funders ranked by facility reach
SELECT fu.name                        AS funder,
       fu.type                        AS funder_type,
       COUNT(DISTINCT fl.facility_id) AS facilities
FROM   funders       fu
JOIN   funding_links fl ON fl.funder_id = fu.funder_id
GROUP  BY fu.name, fu.type
ORDER  BY facilities DESC, funder
LIMIT  20;`,
  },
];

// ── State ───────────────────────────────────────────────────────────
let _container = null;
let _activeId = EXAMPLES[0].id;

function escHtml(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

// Convert a DuckDB-Wasm Arrow RecordBatch-backed Table to a simple
// array of plain JS objects. Handles BigInt (COUNT() results) by
// converting to Number where it's safe.
function resultToRows(result) {
  return result.toArray().map((row) => {
    const o = row.toJSON();
    for (const k of Object.keys(o)) {
      const v = o[k];
      if (typeof v === 'bigint') {
        // BigInt -> Number if under Number.MAX_SAFE_INTEGER. Keep as
        // BigInt (coerced to string for display) otherwise.
        o[k] = (v <= Number.MAX_SAFE_INTEGER && v >= Number.MIN_SAFE_INTEGER)
          ? Number(v) : String(v);
      } else if (v instanceof Date) {
        o[k] = v.toISOString();
      }
    }
    return o;
  });
}

function renderTable(rows) {
  if (!rows.length) return '<p class="sql-empty">Query returned 0 rows.</p>';
  const cols = Object.keys(rows[0]);
  const head = cols.map((c) => `<th>${escHtml(c)}</th>`).join('');
  const body = rows.slice(0, 500).map((r) =>
    `<tr>${cols.map((c) => {
      const v = r[c];
      const display = v == null ? '' : typeof v === 'object' ? JSON.stringify(v) : String(v);
      const num = typeof v === 'number' ? ' class="num"' : '';
      return `<td${num}>${escHtml(display)}</td>`;
    }).join('')}</tr>`,
  ).join('');
  const truncated = rows.length > 500
    ? `<p class="sql-trunc">Showing the first 500 of ${rows.length.toLocaleString()} rows.</p>`
    : '';
  return `<table class="sql-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>${truncated}`;
}

// ── Run ─────────────────────────────────────────────────────────────
async function run(sql) {
  const statusEl = _container.querySelector('#sql-status');
  const resultsEl = _container.querySelector('#sql-results');
  statusEl.textContent = 'Running…';
  resultsEl.innerHTML = '';
  const t0 = performance.now();
  try {
    await whenReady();
    const conn = getConn();
    if (!conn) throw new Error('DuckDB connection not ready');
    const result = await conn.query(sql);
    const rows = resultToRows(result);
    const ms = (performance.now() - t0).toFixed(0);
    statusEl.innerHTML = `<strong>${rows.length.toLocaleString()}</strong> row${rows.length === 1 ? '' : 's'} · ${ms} ms`;
    resultsEl.innerHTML = renderTable(rows);
  } catch (err) {
    const ms = (performance.now() - t0).toFixed(0);
    statusEl.innerHTML = `<span class="sql-err">Error after ${ms} ms</span>`;
    resultsEl.innerHTML = `<pre class="sql-error">${escHtml(err.message || String(err))}</pre>`;
    console.error('[sql]', err);
  }
}

// ── Init ────────────────────────────────────────────────────────────
function pickExample(id) {
  const ex = EXAMPLES.find((e) => e.id === id) || EXAMPLES[0];
  _activeId = ex.id;
  const editor = _container.querySelector('#sql-editor');
  const desc = _container.querySelector('#sql-description');
  editor.value = ex.sql;
  desc.textContent = ex.description;
  _container.querySelectorAll('.sql-example').forEach((btn) => {
    btn.classList.toggle('active', btn.dataset.id === ex.id);
  });
}

export function initSqlView(container) {
  _container = container;

  const exampleBtns = EXAMPLES.map((ex) => `
    <button type="button" class="sql-example" data-id="${ex.id}">${escHtml(ex.title)}</button>
  `).join('');

  _container.innerHTML = `
    <div class="sql-view">
      <header class="sql-header">
        <div>
          <h2>DuckDB SQL console</h2>
          <p class="sql-sub">Queries run client-side in WebAssembly against the parquet
          views in <code>public/parquet/</code>. No server round-trip; the whole dataset
          lives in your browser. Pick an example to get started, or edit and run your own.</p>
        </div>
      </header>
      <div class="sql-layout">
        <aside class="sql-examples">
          <div class="sql-examples-title">Example queries</div>
          ${exampleBtns}
          <div class="sql-schema-title">Tables in scope</div>
          <ul class="sql-schema">
            <li>facilities · facility_types · locations</li>
            <li>networks · network_membership</li>
            <li>research_areas · area_links</li>
            <li>regions · region_area_links · facility_regions</li>
            <li>funders · funding_links</li>
          </ul>
        </aside>
        <section class="sql-main">
          <p id="sql-description" class="sql-description"></p>
          <textarea id="sql-editor" class="sql-editor" spellcheck="false"></textarea>
          <div class="sql-actions">
            <button id="sql-run" type="button" class="btn-primary">Run query</button>
            <span id="sql-status" class="sql-status">Ready.</span>
          </div>
          <div id="sql-results" class="sql-results"></div>
        </section>
      </div>
    </div>`;

  _container.querySelectorAll('.sql-example').forEach((btn) => {
    btn.addEventListener('click', () => pickExample(btn.dataset.id));
  });

  _container.querySelector('#sql-run').addEventListener('click', () => {
    const sql = _container.querySelector('#sql-editor').value.trim();
    if (!sql) return;
    run(sql);
  });

  // Keyboard shortcut: Cmd/Ctrl+Enter runs the query.
  _container.querySelector('#sql-editor').addEventListener('keydown', (ev) => {
    if ((ev.metaKey || ev.ctrlKey) && ev.key === 'Enter') {
      ev.preventDefault();
      const sql = ev.target.value.trim();
      if (sql) run(sql);
    }
  });

  pickExample(_activeId);
}

// Called on route activation. Lazy-runs the active example the very
// first time the tab is visited so the user sees a real result right
// away; subsequent visits just re-show the existing editor + results.
let _firstRunDone = false;
export function renderSqlView() {
  if (!_container) return;
  if (_firstRunDone) return;
  _firstRunDone = true;
  const sql = _container.querySelector('#sql-editor').value.trim();
  if (sql) run(sql);
}
