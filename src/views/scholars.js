// scholars.js — Coastal ocean science community roster (#/scholars).
//
// A field-wide view of who the pre-eminent scholars are, who is
// publishing most right now, and who is coming up — as opposed to the
// People tab, which is the staff of catalogued facilities.
//
// Routes:
//   #/scholars              → cohort-filterable card grid
//   #/scholars/<scholar_id> → that scholar's card scrolled into view
//
// Data source: community_scholars (written by
// scripts/build_community_scholars.py). The roster ships hand-curated
// with metric columns NULL; the --harvest run measures the field against
// OpenAlex and fills them in. Every metric path therefore tolerates
// nulls, and the cohort counts come from the flags rather than being
// assumed to be 100/100/50.

import { getConn, whenReady, unwrapRow } from '../db.js';

let _container = null;
let _cached = null;
let _cohorts = new Set(['preeminent', 'most_active', 'rising']);
let _sort = 'default';
let _qFilter = '';

const COHORT_META = {
  preeminent  : { flag: 'is_preeminent',  rank: 'rank_preeminent',
                  label: 'Pre-eminent',   cls: 'sch-badge-pre' },
  most_active : { flag: 'is_most_active', rank: 'rank_most_active',
                  label: 'Most active',   cls: 'sch-badge-act' },
  rising      : { flag: 'is_rising',      rank: 'rank_rising',
                  label: 'Rising',        cls: 'sch-badge-ris' },
};

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function fmtInt(n) {
  if (n == null) return '—';
  return Math.round(n).toLocaleString();
}

async function fetchScholars() {
  await whenReady();
  const conn = getConn();
  if (!conn) throw new Error('DuckDB connection not ready');
  // Flat table, no joins needed. Ordered so the default view leads with
  // the highest-ranked pre-eminent scholars and falls back to name when
  // no ranks exist yet (the curated-only state).
  const res = await conn.query(`
    SELECT * FROM community_scholars
    ORDER BY COALESCE(rank_preeminent, 9999),
             COALESCE(rank_most_active, 9999),
             COALESCE(rank_rising, 9999),
             name`);
  return res.toArray().map((row) => unwrapRow(row.toJSON()));
}

function cohortBadges(row) {
  return Object.entries(COHORT_META)
    .filter(([, m]) => row[m.flag])
    .map(([, m]) => {
      const rank = row[m.rank];
      return `<span class="sch-badge ${m.cls}">${m.label}${rank ? ` #${rank}` : ''}</span>`;
    }).join('');
}

function hasMetrics(row) {
  return row.h_index != null || row.cited_by_count != null || row.works_count != null;
}

function metricsHtml(row) {
  if (!hasMetrics(row)) {
    return `<p class="sch-pending">Bibliometrics pending — curated entry,
      not yet measured against OpenAlex.</p>`;
  }
  return `
    <div class="sch-metrics">
      <span><strong>${fmtInt(row.h_index)}</strong> h-index</span>
      <span><strong>${fmtInt(row.cited_by_count)}</strong> citations</span>
      <span><strong>${fmtInt(row.works_count)}</strong> works</span>
      <span><strong>${fmtInt(row.coastal_works_count)}</strong> coastal</span>
      <span><strong>${fmtInt(row.coastal_recent_works)}</strong> last 5y</span>
    </div>`;
}

function topicChips(row) {
  if (!row.top_topics) return '';
  const chips = String(row.top_topics)
    .split(/[;,]/).map((t) => t.trim()).filter(Boolean).slice(0, 6)
    .map((t) => `<span class="sch-topic">${esc(t)}</span>`).join('');
  return chips ? `<div class="sch-topics">${chips}</div>` : '';
}

function cardHtml(row) {
  const links = [];
  if (row.homepage_url) links.push(`<a href="${esc(row.homepage_url)}" target="_blank" rel="noopener">Homepage</a>`);
  if (row.orcid) links.push(`<a href="https://orcid.org/${esc(row.orcid)}" target="_blank" rel="noopener">ORCID</a>`);
  if (row.openalex_id) links.push(`<a href="https://openalex.org/${esc(row.openalex_id)}" target="_blank" rel="noopener">OpenAlex</a>`);
  if (row.google_scholar_id) {
    links.push(`<a href="https://scholar.google.com/citations?user=${esc(row.google_scholar_id)}" target="_blank" rel="noopener">Scholar</a>`);
  }
  if (row.person_id) links.push(`<a href="#/people/${esc(row.person_id)}">In this dataset</a>`);
  if (!links.length && row.source_url) {
    links.push(`<a href="${esc(row.source_url)}" target="_blank" rel="noopener">Source</a>`);
  }

  const affiliation = [row.affiliation, row.affiliation_country]
    .filter(Boolean).join(' · ');

  return `
    <article class="sch-card" id="sch-${esc(row.scholar_id)}" data-id="${esc(row.scholar_id)}">
      <header class="sch-card-head">
        <h3>${esc(row.name)}</h3>
        <div class="sch-badges">${cohortBadges(row)}</div>
      </header>
      ${affiliation ? `<p class="sch-aff">${esc(affiliation)}</p>` : ''}
      ${metricsHtml(row)}
      ${topicChips(row)}
      ${row.rationale ? `<p class="sch-rationale">${esc(row.rationale)}</p>` : ''}
      ${links.length ? `<footer class="sch-links">${links.join(' · ')}</footer>` : ''}
    </article>`;
}

function applyFilterSort(rows) {
  const active = [..._cohorts];
  let out = rows.filter((r) => active.some((c) => r[COHORT_META[c].flag]));

  const q = _qFilter.trim().toLowerCase();
  if (q) {
    out = out.filter((r) => [r.name, r.affiliation, r.affiliation_country,
                             r.top_topics, r.rationale]
      .join(' ').toLowerCase().includes(q));
  }

  const cmp = {
    // Default: best rank in any selected cohort, so switching chips
    // re-orders sensibly instead of always leading with pre-eminence.
    default: (a, b) => bestRank(a) - bestRank(b)
                    || String(a.name).localeCompare(String(b.name)),
    name      : (a, b) => String(a.name).localeCompare(String(b.name)),
    h_index   : (a, b) => (b.h_index || 0) - (a.h_index || 0),
    citations : (a, b) => (b.cited_by_count || 0) - (a.cited_by_count || 0),
    coastal   : (a, b) => (b.coastal_works_count || 0) - (a.coastal_works_count || 0),
    recent    : (a, b) => (b.coastal_recent_works || 0) - (a.coastal_recent_works || 0),
  }[_sort] || ((a, b) => bestRank(a) - bestRank(b));
  return out.sort(cmp);

  function bestRank(r) {
    const ranks = [..._cohorts]
      .map((c) => r[COHORT_META[c].rank])
      .filter((v) => v != null);
    return ranks.length ? Math.min(...ranks) : 9999;
  }
}

async function renderScholars(targetId) {
  if (!_container) return;
  const status = _container.querySelector('.sch-status');
  if (status) status.textContent = 'Loading…';

  if (!_cached) {
    try {
      _cached = await fetchScholars();
    } catch (e) {
      if (status) status.textContent = `Failed to load: ${e.message}`;
      console.error(e);
      return;
    }
  }

  if (!_cached.length) {
    _container.innerHTML = `
      <div class="sch-page">
        <header class="sch-header"><h1>Coastal ocean science scholars</h1></header>
        <p class="no-data">
          Scholar roster not built yet. Run
          <code>python scripts/build_community_scholars.py --seed</code> for the
          curated roster, or <code>--harvest</code> (with
          <code>OPENALEX_EMAIL</code> set) to measure the field against
          OpenAlex. See Docs → Team, Scholars &amp; Data Methods.
        </p>
      </div>`;
    return;
  }

  const rows = applyFilterSort(_cached);
  const counts = Object.fromEntries(Object.entries(COHORT_META)
    .map(([key, m]) => [key, _cached.filter((r) => r[m.flag]).length]));
  const measured = _cached.filter(hasMetrics).length;

  const chips = Object.entries(COHORT_META).map(([key, m]) => `
    <button class="sch-chip${_cohorts.has(key) ? ' sch-chip-on' : ''}"
            data-cohort="${key}">
      ${m.label} <span class="sch-chip-n">${counts[key]}</span>
    </button>`).join('');

  _container.innerHTML = `
    <div class="sch-page">
      <header class="sch-header">
        <h1>Coastal ocean science scholars</h1>
        <p class="sch-summary">
          <strong>${fmtInt(_cached.length)}</strong> researchers working across
          coastal and estuarine science — the pre-eminent scholars who defined
          the field, those publishing most in the last five years, and the
          early-career researchers coming up. Distinct from the
          <a href="#/people">researcher directory</a>, which lists the staff of
          catalogued facilities.
          ${measured === 0
            ? 'Currently a hand-curated roster: bibliometrics arrive with the OpenAlex harvest.'
            : `<strong>${fmtInt(measured)}</strong> measured against OpenAlex.`}
        </p>
        <div class="sch-controls">
          <div class="sch-chips">${chips}</div>
          <input id="sch-q" type="search" placeholder="Search name, affiliation, topic…"
                 value="${esc(_qFilter)}">
          <label>Sort by:
            <select id="sch-sort">
              <option value="default"${_sort === 'default' ? ' selected' : ''}>Cohort rank</option>
              <option value="name"${_sort === 'name' ? ' selected' : ''}>Name (A→Z)</option>
              <option value="h_index"${_sort === 'h_index' ? ' selected' : ''}>h-index</option>
              <option value="citations"${_sort === 'citations' ? ' selected' : ''}>Citations</option>
              <option value="coastal"${_sort === 'coastal' ? ' selected' : ''}>Coastal works</option>
              <option value="recent"${_sort === 'recent' ? ' selected' : ''}>Coastal works, last 5y</option>
            </select>
          </label>
        </div>
        <p class="sch-count">Showing <strong>${fmtInt(rows.length)}</strong>
          of <strong>${fmtInt(_cached.length)}</strong>.</p>
      </header>
      <div class="sch-grid">${rows.map(cardHtml).join('')}</div>
      <p class="sch-status">Done.</p>
    </div>`;

  for (const btn of _container.querySelectorAll('.sch-chip')) {
    btn.addEventListener('click', () => {
      const key = btn.dataset.cohort;
      if (_cohorts.has(key)) _cohorts.delete(key);
      else _cohorts.add(key);
      // Deselecting every chip would show an empty page with no way back.
      if (!_cohorts.size) _cohorts = new Set(Object.keys(COHORT_META));
      renderScholars(targetId);
    });
  }
  _container.querySelector('#sch-q').addEventListener('input', (ev) => {
    _qFilter = ev.target.value;
    renderScholars(targetId);
  });
  _container.querySelector('#sch-sort').addEventListener('change', (ev) => {
    _sort = ev.target.value;
    renderScholars(targetId);
  });

  if (targetId) {
    const el = _container.querySelector(`#sch-${CSS.escape(targetId)}`);
    if (el) {
      el.classList.add('sch-card-active');
      requestAnimationFrame(() => el.scrollIntoView({
        behavior: 'smooth', block: 'start',
      }));
    }
  }
}

export function initScholarsView(container) {
  _container = container;
  _container.innerHTML = `
    <div class="sch-page">
      <p class="sch-status" style="padding:24px;color:#64748b">
        Scholar roster loading…
      </p>
    </div>`;
}

export function renderScholarsView(targetId) {
  if (!_container) return;
  renderScholars(targetId).catch((e) => {
    console.error('[scholars] render failed', e);
    const s = _container.querySelector('.sch-status');
    if (s) s.textContent = `Render failed: ${e.message}`;
  });
}
