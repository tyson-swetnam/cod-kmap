// people.js — the single People view (#/people and #/people/<id>).
//
// This view replaces the three former human tabs (People / Team /
// Scholars). Its roster IS person_registry: every person the site ships,
// in one list, with cohort as a filter rather than as a separate page.
//
// What changed and why: person_registry resolved the three old tables
// (people, cod_team_members, community_scholars) onto persistent
// identifiers, so the same human no longer appears two or three times
// with no shared key. Before this view existed the registry was only
// wired in as an enrichment JOIN, so the ~10k harvested researchers were
// invisible in the UI — the three tabs each read their original narrow
// table (people 280 / cod_team_members 67 / community_scholars 523).
//
// Routes handled here:
//   #/people            → full roster, ranked by tier_rank
//   #/people/<id>       → jump to and highlight one person. <id> may be a
//                         canonical_id, or a legacy person_id (old
//                         #/people/<person_id> links, and the Network
//                         tab's click-through) or scholar_id (old
//                         #/scholars/<scholar_id> links).
// #/team and #/scholars are redirected here with the matching cohort
// preselected — see the route table in main.js.
//
// NOT handled here: the COD work-breakdown org chart. A WBS hierarchy is
// a management structure, not a roster filter, so it keeps its own view
// (src/views/orgchart.js, #/org).
//
// Data source: DuckDB-Wasm over the shipped parquet — person_registry,
// registry_collaborations, registry_facilities, facilities.

import { getConn, whenReady, unwrapRow } from '../db.js';

// Rows shipped to the browser are the 'core' tier only. The full registry
// is far larger and stays in the local DuckDB build; the header says so
// rather than letting 10,000 read as the whole population.
const REGISTRY_TOTAL = 152008;

// 10,000 cards cannot all be in the DOM at once, so the roster is paged.
// Filter and sort run over the whole in-memory roster; only one page is
// rendered.
const PAGE_SIZE = 60;

const COHORTS = {
  all     : { label: 'Everyone',        test: () => true },
  team    : { label: 'COD team',        test: (r) => !!r.is_team },
  site    : { label: 'Site personnel',  test: (r) => !!r.is_site_personnel },
  scholar : { label: 'Scholar roster',  test: (r) => !!r.is_scholar },
  multi   : { label: 'In 2+ cohorts',   test: (r) => r._nflags > 1 },
};

let _container = null;
let _rows = null;          // full roster, fetched once
let _view = [];            // current filtered + sorted slice source
let _cohort = 'all';
let _q = '';
let _sort = 'rank';
let _page = 0;
let _focus = null;         // canonical/person/scholar id to highlight
let _unresolved = null;    // {id, hit} when a deep link names someone absent
let _shellBuilt = false;

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function fmtInt(n) {
  if (n == null) return '—';
  return Math.round(Number(n)).toLocaleString();
}

async function fetchRoster() {
  await whenReady();
  const conn = getConn();
  if (!conn) throw new Error('DuckDB connection not ready');

  // One statement: registry identity + metrics, co-authorship reach split
  // by the partner's cohort, and ROR-matched catalogued sites.
  //
  // registry_collaborations stores each edge once with
  // canonical_id_a < canonical_id_b, so it is unioned both ways to get an
  // undirected adjacency. The shipped parquet carries only edges whose
  // BOTH endpoints are core tier.
  const sql = `
    WITH edges AS (
      SELECT canonical_id_a AS self_id, canonical_id_b AS other_id, co_pub_count
        FROM registry_collaborations
      UNION ALL
      SELECT canonical_id_b, canonical_id_a, co_pub_count
        FROM registry_collaborations
    ),
    reach AS (
      SELECT e.self_id                                       AS canonical_id,
             COUNT(*)                                        AS reg_degree,
             CAST(SUM(e.co_pub_count) AS DOUBLE)              AS co_pub_total,
             MAX(e.co_pub_count)                              AS top_co_pubs,
             COUNT(*) FILTER (WHERE o.is_team)                AS to_team,
             COUNT(*) FILTER (WHERE o.is_site_personnel)      AS to_site,
             COUNT(*) FILTER (WHERE o.is_scholar)             AS to_scholars
      FROM edges e
      JOIN person_registry o ON o.canonical_id = e.other_id
      GROUP BY e.self_id
    ),
    sites AS (
      SELECT rf.canonical_id,
             COUNT(DISTINCT rf.facility_id)                          AS n_sites,
             string_agg(DISTINCT COALESCE(f.acronym, f.canonical_name), ' · ') AS site_names
      FROM registry_facilities rf
      JOIN facilities f ON f.facility_id = rf.facility_id
      GROUP BY rf.canonical_id
    )
    SELECT r.canonical_id,
           r.display_name,
           r.orcid,
           r.openalex_id,
           r.google_scholar_id,
           r.homepage_url,
           r.affiliation,
           r.affiliation_ror,
           r.affiliation_country,
           r.is_team,
           r.is_site_personnel,
           r.is_scholar,
           r.person_id,
           r.scholar_id,
           r.works_count,
           r.cited_by_count,
           r.h_index,
           r.i10_index,
           r.coastal_works_count,
           r.coastal_share,
           r.first_pub_year,
           r.tier,
           r.tier_rank,
           r.source,
           r.source_url,
           r.confidence,
           rc.reg_degree,
           rc.co_pub_total,
           rc.top_co_pubs,
           rc.to_team,
           rc.to_site,
           rc.to_scholars,
           s.n_sites,
           s.site_names
    FROM person_registry r
    LEFT JOIN reach rc ON rc.canonical_id = r.canonical_id
    LEFT JOIN sites s  ON s.canonical_id  = r.canonical_id
    ORDER BY r.tier_rank
  `;
  const res = await conn.query(sql);
  return res.toArray().map((row) => {
    const r = unwrapRow(row.toJSON());
    r._nflags = (r.is_team ? 1 : 0) + (r.is_site_personnel ? 1 : 0)
              + (r.is_scholar ? 1 : 0);
    // Precomputed search haystack: rebuilding this per keystroke over
    // 10,000 rows is the one thing in this view that would actually be
    // slow.
    r._hay = [r.display_name, r.affiliation, r.affiliation_country,
              r.orcid, r.openalex_id, r.site_names]
      .filter(Boolean).join(' ').toLowerCase();
    return r;
  });
}

function cohortChips(r) {
  const out = [];
  if (r.is_team) out.push('<span class="reg-chip reg-chip-team">COD team</span>');
  if (r.is_site_personnel) out.push('<span class="reg-chip reg-chip-site">Site personnel</span>');
  if (r.is_scholar) out.push('<span class="reg-chip reg-chip-scholar">Scholar roster</span>');
  return out.join('');
}

function idLinks(r) {
  const links = [];
  if (r.homepage_url) {
    links.push(`<a href="${esc(r.homepage_url)}" target="_blank" rel="noopener">Homepage</a>`);
  }
  if (r.orcid) {
    links.push(`<a href="https://orcid.org/${esc(r.orcid)}" target="_blank" rel="noopener">ORCID</a>`);
  }
  if (r.openalex_id) {
    links.push(`<a href="https://openalex.org/${esc(r.openalex_id)}" target="_blank" rel="noopener">OpenAlex</a>`);
  }
  if (r.google_scholar_id) {
    links.push(`<a href="https://scholar.google.com/citations?user=${esc(r.google_scholar_id)}" target="_blank" rel="noopener">Scholar</a>`);
  }
  if (r.affiliation_ror) {
    links.push(`<a href="https://ror.org/${esc(r.affiliation_ror)}" target="_blank" rel="noopener">ROR</a>`);
  }
  if (r.source_url) {
    links.push(`<a href="${esc(r.source_url)}" target="_blank" rel="noopener">Source</a>`);
  }
  return links;
}

// Co-authorship reach. Three states, deliberately worded apart:
//   - edges computed and present
//   - no edges computed for this person (the graph was built over the 618
//     pre-harvest identities, so most core-tier people fall here)
// "Not computed" is not the same claim as "has no collaborators", and the
// UI must not collapse the two.
function reachHtml(r) {
  if (r.reg_degree == null) {
    return `<p class="reg-reach reg-reach-none">
      Co-authorship not computed for this person — the co-publication graph
      covers the pre-harvest identities, not the whole shipped roster. This
      is not a finding that they publish alone.
    </p>`;
  }
  const parts = [
    `<span><strong>${fmtInt(r.reg_degree)}</strong> co-authors in the registry</span>`,
  ];
  if (r.co_pub_total != null) {
    parts.push(`<span><strong>${fmtInt(r.co_pub_total)}</strong> co-authored works</span>`);
  }
  const to = [];
  if (r.to_team) to.push(`${fmtInt(r.to_team)} on the COD team`);
  if (r.to_site) to.push(`${fmtInt(r.to_site)} at catalogued sites`);
  if (r.to_scholars) to.push(`${fmtInt(r.to_scholars)} in the scholar roster`);
  if (to.length) parts.push(`<span class="reg-reach-split">${esc(to.join(' · '))}</span>`);
  return `<p class="reg-reach">${parts.join('')}</p>`;
}

function siteHtml(r) {
  if (!r.n_sites) {
    return `<p class="reg-site reg-site-none">No catalogued site — this
      person's ROR does not match an organisation in the facilities
      catalogue, so they cannot be placed at a physical location.</p>`;
  }
  return `<p class="reg-site">At <strong>${fmtInt(r.n_sites)}</strong>
    catalogued site${r.n_sites > 1 ? 's' : ''}: ${esc(r.site_names)}</p>`;
}

function metricsHtml(r) {
  const cells = [
    [fmtInt(r.works_count), 'works'],
    [fmtInt(r.cited_by_count), 'citations'],
    [fmtInt(r.h_index), 'h-index'],
    [fmtInt(r.i10_index), 'i10'],
    // coastal_works_count is an upper bound, not a distinct-paper count:
    // OpenAlex lists a work under every topic it carries, so a paper with
    // three coastal topics is counted three times. Labelled as volume,
    // never as papers.
    [fmtInt(r.coastal_works_count), 'coastal output volume'],
  ];
  if (r.first_pub_year != null) cells.push([fmtInt(r.first_pub_year), 'first pub.']);
  return `<div class="reg-metrics">${cells.map(([v, k]) =>
    `<span class="reg-metric"><strong>${v}</strong><br>${esc(k)}</span>`).join('')}</div>`;
}

function cardHtml(r) {
  const links = idLinks(r);
  const aff = [r.affiliation, r.affiliation_country].filter(Boolean).join(' · ');
  return `
    <article class="reg-card" data-cid="${esc(r.canonical_id)}">
      <header class="reg-card-head">
        <h3>${esc(r.display_name)}</h3>
        <span class="reg-rank" title="Composite tier rank">#${fmtInt(r.tier_rank)}</span>
        ${cohortChips(r)}
      </header>
      ${aff ? `<p class="reg-aff">${esc(aff)}</p>` : ''}
      ${metricsHtml(r)}
      ${reachHtml(r)}
      ${siteHtml(r)}
      <footer class="reg-foot">
        ${links.length ? `<span class="reg-links">${links.join(' · ')}</span>` : ''}
        <span class="reg-prov">
          <span class="reg-conf reg-conf-${esc(r.confidence || 'unknown')}">${esc(r.confidence || 'confidence unrecorded')}</span>
          ${r.source ? `<span class="reg-source">${esc(r.source)}</span>` : ''}
        </span>
      </footer>
    </article>`;
}

const SORTS = {
  rank      : (a, b) => (a.tier_rank || 1e9) - (b.tier_rank || 1e9),
  name      : (a, b) => String(a.display_name).localeCompare(String(b.display_name)),
  works     : (a, b) => (b.works_count || 0) - (a.works_count || 0),
  citations : (a, b) => (b.cited_by_count || 0) - (a.cited_by_count || 0),
  h_index   : (a, b) => (b.h_index || 0) - (a.h_index || 0),
  coastal   : (a, b) => (b.coastal_works_count || 0) - (a.coastal_works_count || 0),
  reach     : (a, b) => (b.reg_degree || 0) - (a.reg_degree || 0),
};

function recompute() {
  const test = (COHORTS[_cohort] || COHORTS.all).test;
  const q = _q.trim().toLowerCase();
  let out = _rows.filter(test);
  if (q) out = out.filter((r) => r._hay.includes(q));
  out.sort(SORTS[_sort] || SORTS.rank);
  _view = out;
  const maxPage = Math.max(0, Math.ceil(_view.length / PAGE_SIZE) - 1);
  if (_page > maxPage) _page = maxPage;
}

function shellHtml() {
  const counts = Object.fromEntries(Object.entries(COHORTS)
    .map(([k, c]) => [k, _rows.filter(c.test).length]));
  const withReach = _rows.filter((r) => r.reg_degree != null).length;
  const withSite = _rows.filter((r) => r.n_sites).length;

  const chips = Object.entries(COHORTS).map(([k, c]) => `
    <button class="reg-cohort${k === _cohort ? ' reg-cohort-on' : ''}" data-cohort="${k}">
      ${esc(c.label)} <span class="reg-cohort-n">${fmtInt(counts[k])}</span>
    </button>`).join('');

  const sortOpts = [
    ['rank', 'Composite rank (default)'],
    ['name', 'Name (A→Z)'],
    ['works', 'Works'],
    ['citations', 'Citations'],
    ['h_index', 'h-index'],
    ['coastal', 'Coastal output volume'],
    ['reach', 'Co-authors in registry'],
  ].map(([v, label]) =>
    `<option value="${v}"${_sort === v ? ' selected' : ''}>${esc(label)}</option>`).join('');

  return `
    <div class="reg-page">
      <header class="reg-header">
        <h1>People</h1>
        <p class="reg-summary">
          Every person the site ships, in one list. The roster is
          <code>person_registry</code>, which resolved the project team, the
          personnel of catalogued sites, and the coastal-science scholar
          roster onto persistent identifiers (ORCID / OpenAlex), so one human
          is one row. Cohort is a filter below, not a separate tab.
        </p>
        <ul class="reg-caveats">
          <li><strong>${fmtInt(_rows.length)}</strong> rows ship to the browser —
            the <code>core</code> tier. The full registry holds
            ${fmtInt(REGISTRY_TOTAL)} identities; the remainder exists only in
            the local DuckDB build and is not queryable here.</li>
          <li>Co-authorship is computed for <strong>${fmtInt(withReach)}</strong>
            of ${fmtInt(_rows.length)}. The co-publication graph was built over
            the pre-harvest identities, so for most people here it is
            <em>not computed</em> — which is a different statement from
            <em>none</em>.</li>
          <li>Only <strong>${fmtInt(withSite)}</strong> have a ROR match to an
            organisation in the facilities catalogue; the rest cannot be
            placed at a physical location.</li>
          <li>&ldquo;Coastal output volume&rdquo; is an upper bound, not a paper
            count: OpenAlex lists a work under every topic it carries, so a
            paper with several coastal topics is counted several times.</li>
          <li>The COD work-breakdown structure is a management hierarchy, not
            a roster filter — it lives on the
            <a href="#/org">Org chart</a> tab.</li>
        </ul>
        <div class="reg-controls">
          <div class="reg-cohorts">${chips}</div>
          <input id="reg-q" type="search" value="${esc(_q)}"
                 placeholder="Search name, affiliation, country, ORCID, OpenAlex id…">
          <label>Sort by:
            <select id="reg-sort">${sortOpts}</select>
          </label>
        </div>
        <p class="reg-count" id="reg-count"></p>
      </header>
      <div id="reg-notice-slot"></div>
      <div class="reg-grid" id="reg-grid"></div>
      <div class="reg-pager" id="reg-pager"></div>
    </div>`;
}

function paint() {
  const grid = _container.querySelector('#reg-grid');
  const countEl = _container.querySelector('#reg-count');
  const pager = _container.querySelector('#reg-pager');
  if (!grid) return;

  const slot = _container.querySelector('#reg-notice-slot');
  if (slot) {
    slot.innerHTML = _unresolved
      ? unresolvedHtml(_unresolved.id, _unresolved.hit) : '';
  }

  const start = _page * PAGE_SIZE;
  const page = _view.slice(start, start + PAGE_SIZE);
  grid.innerHTML = page.length
    ? page.map(cardHtml).join('')
    : '<p class="reg-empty">No one matches this filter.</p>';

  if (countEl) {
    const shown = page.length
      ? `${fmtInt(start + 1)}–${fmtInt(start + page.length)}`
      : '0';
    countEl.innerHTML = `Showing <strong>${shown}</strong> of
      <strong>${fmtInt(_view.length)}</strong>
      ${_view.length === _rows.length ? '' : `(filtered from ${fmtInt(_rows.length)})`}`;
  }

  if (pager) {
    const nPages = Math.max(1, Math.ceil(_view.length / PAGE_SIZE));
    pager.innerHTML = nPages > 1 ? `
      <button data-page="first" ${_page === 0 ? 'disabled' : ''}>&laquo; First</button>
      <button data-page="prev"  ${_page === 0 ? 'disabled' : ''}>&lsaquo; Prev</button>
      <span class="reg-pageno">Page ${fmtInt(_page + 1)} of ${fmtInt(nPages)}</span>
      <button data-page="next" ${_page >= nPages - 1 ? 'disabled' : ''}>Next &rsaquo;</button>
      <button data-page="last" ${_page >= nPages - 1 ? 'disabled' : ''}>Last &raquo;</button>` : '';
    for (const btn of pager.querySelectorAll('button[data-page]')) {
      btn.addEventListener('click', () => {
        const nP = Math.max(1, Math.ceil(_view.length / PAGE_SIZE));
        const to = { first: 0, prev: _page - 1, next: _page + 1, last: nP - 1 }[btn.dataset.page];
        _page = Math.min(Math.max(0, to), nP - 1);
        paint();
        const head = _container.querySelector('.reg-header');
        if (head) head.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    }
  }

  if (_focus) {
    for (const card of grid.querySelectorAll('.reg-card')) {
      if (card.dataset.cid === _focus) {
        card.classList.add('reg-card-active');
        requestAnimationFrame(() => card.scrollIntoView({
          behavior: 'smooth', block: 'center',
        }));
        break;
      }
    }
  }
}

function wireShell() {
  for (const btn of _container.querySelectorAll('.reg-cohort')) {
    btn.addEventListener('click', () => {
      _cohort = btn.dataset.cohort;
      _page = 0;
      _focus = null;
      _unresolved = null;
      for (const b of _container.querySelectorAll('.reg-cohort')) {
        b.classList.toggle('reg-cohort-on', b.dataset.cohort === _cohort);
      }
      recompute();
      paint();
    });
  }
  // Only the grid, the count and the pager are re-rendered on a state
  // change, so the search input survives and keeps focus and caret — no
  // focus-restore dance is needed here.
  const qEl = _container.querySelector('#reg-q');
  if (qEl) {
    qEl.addEventListener('input', (ev) => {
      _q = ev.target.value;
      _page = 0;
      _focus = null;
      _unresolved = null;
      recompute();
      paint();
    });
  }
  const sEl = _container.querySelector('#reg-sort');
  if (sEl) {
    sEl.addEventListener('change', (ev) => {
      _sort = ev.target.value;
      _page = 0;
      recompute();
      paint();
    });
  }
}

// Resolve a deep-link id against every identifier the old routes used:
// canonical_id (this view), person_id (#/people/<person_id> from the
// Network tab and the org chart) and scholar_id (#/scholars/<scholar_id>).
function resolveId(id) {
  if (!id) return null;
  const hit = _rows.find((r) => r.canonical_id === id)
           || _rows.find((r) => r.person_id === id)
           || _rows.find((r) => r.scholar_id === id);
  return hit || null;
}

// A deep link can name someone who has no registry row. person_registry is
// keyed on a persistent identifier, and only 185 of the 280 rows in `people`
// (and 442 of the 523 in community_scholars) resolved one — so the Network
// tab, which links every node it draws, can hand us a person_id that is
// genuinely absent here. Look the name up in the narrow source tables and
// say so, rather than dropping the visitor on an unfiltered roster with no
// explanation. Parameterised: the id comes out of the URL hash.
async function lookupUnresolved(id) {
  const conn = getConn();
  if (!conn) return null;
  try {
    const prepared = await conn.prepare(`
      SELECT name AS display_name, 'people' AS src FROM people WHERE person_id = ?
      UNION ALL
      SELECT name, 'community_scholars' FROM community_scholars WHERE scholar_id = ?
      LIMIT 1`);
    const res = await prepared.query(id, id);
    const rows = res.toArray().map((r) => unwrapRow(r.toJSON()));
    return rows.length ? rows[0] : null;
  } catch (e) {
    console.warn('[people] legacy id lookup failed', e);
    return null;
  }
}

function unresolvedHtml(id, hit) {
  const who = hit
    ? `<strong>${esc(hit.display_name)}</strong> is in the
       <code>${esc(hit.src)}</code> table but`
    : `The id <code>${esc(id)}</code>`;
  return `<div class="reg-notice">
    ${who} has no row in <code>person_registry</code>: no ORCID or OpenAlex
    identifier was resolved for them, and the registry is keyed on a
    persistent id. They are therefore not in the roster below — that is a
    gap in identity resolution, not evidence they do not exist.
    <a href="#/people">Show the full roster</a>.
  </div>`;
}

async function render(target) {
  if (!_container) return;

  if (!_rows) {
    try {
      _rows = await fetchRoster();
    } catch (e) {
      _container.innerHTML = `<div class="reg-page"><p class="no-data">
        Failed to load the person registry: ${esc(e.message)}</p></div>`;
      console.error(e);
      return;
    }
  }
  if (!_rows.length) {
    _container.innerHTML = `<div class="reg-page"><p class="no-data">
      person_registry is empty. Run
      <code>python scripts/build_person_registry.py</code> then
      <code>python scripts/export_parquet.py</code>.</p></div>`;
    return;
  }

  // A deep link has to land on the page the person is actually on, which
  // depends on the active sort — so resolve the row, clear the cohort
  // filter (the target may not be in it) and compute the page index.
  _focus = null;
  _unresolved = null;
  if (target) {
    const hit = resolveId(target);
    if (hit) {
      _focus = hit.canonical_id;
      _cohort = 'all';
    } else {
      _unresolved = { id: target, hit: await lookupUnresolved(target) };
    }
  }

  if (!_shellBuilt) {
    _container.innerHTML = shellHtml();
    wireShell();
    _shellBuilt = true;
  } else {
    // Keep the shell (and the search box) but resync the cohort chips,
    // which a redirected legacy route may have changed.
    for (const b of _container.querySelectorAll('.reg-cohort')) {
      b.classList.toggle('reg-cohort-on', b.dataset.cohort === _cohort);
    }
  }

  recompute();
  if (_focus) {
    const idx = _view.findIndex((r) => r.canonical_id === _focus);
    if (idx >= 0) _page = Math.floor(idx / PAGE_SIZE);
  }
  paint();
}

export function initPeopleView(container) {
  _container = container;
  _container.innerHTML = `
    <div class="reg-page">
      <p class="reg-loading">Person registry loading…</p>
    </div>`;
}

// Preselect a cohort before navigation — used by the retired #/team and
// #/scholars routes, which redirect here.
export function setPeopleCohort(name) {
  if (COHORTS[name]) {
    _cohort = name;
    _page = 0;
  }
}

// renderPeopleView(targetId) — targetId is a canonical_id, person_id or
// scholar_id to highlight; pass null for the plain roster.
export function renderPeopleView(targetId) {
  if (!_container) return;
  render(targetId).catch((e) => {
    console.error('[people] render failed', e);
  });
}
