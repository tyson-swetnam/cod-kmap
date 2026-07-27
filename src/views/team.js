// team.js — COD project team view (#/team).
//
// Renders the project organisational chart: the PI and Co-PIs as hero
// cards, the Science Leadership Committee, then every WBS element with
// its members. Unfilled positions (TBD / TBH) are shown muted rather
// than hidden — an empty slot is information about the project.
//
// Data source: cod_team_members + cod_wbs (written by
// scripts/build_cod_team_lake.py), joined to people for scholarly
// identifiers and to person_area_metrics for publication metrics. The
// metric columns stay empty until scripts/enrich_people_openalex.py has
// run, so every metric render path tolerates nulls.

import { getConn, whenReady, unwrapRow } from '../db.js';

let _container = null;
let _cached = null;
let _collapsed = new Set();

// Institution chip colours, keyed by the slug in cod_team_members and
// following the org chart's own colour legend so the tab reads like the
// source document. Unknown slugs fall back to the neutral slate used
// everywhere else in the app.
const INSTITUTION_COLORS = {
  clemson          : '#F56600',
  yale             : '#00356B',
  unm              : '#BA0C2F',
  battelle         : '#00B0B9',
  vcu              : '#000000',
  pnnl             : '#00A6D6',
  unl              : '#D00000',
  obfs             : '#2E7D32',
  uga              : '#BA0C2F',
  arizona          : '#AB0520',
  delaware         : '#00539F',
  usc              : '#73000A',
  uidaho           : '#B3A369',
  'montana-state'  : '#00205B',
  alabama          : '#9E1B32',
  florida          : '#0021A5',
  'coastal-carolina': '#006F71',
  charleston       : '#7C2529',
  'other-university': '#475569',
  agency           : '#0369a1',
  company          : '#334155',
  various          : '#64748b',
};

function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function fmtInt(n) {
  if (!n && n !== 0) return '—';
  return Math.round(n).toLocaleString();
}

async function fetchTeam() {
  await whenReady();
  const conn = getConn();
  if (!conn) throw new Error('DuckDB connection not ready');

  // One statement: org-chart position + scholarly identity + metrics.
  //
  // Publication and citation totals come from authorship/publications, NOT
  // from summing person_area_metrics. That table has one row per (person,
  // research area), so a paper spanning four areas is counted four times:
  // summing it reported 740 publications for a researcher with 100. Only
  // h_index is taken from there, where MAX over the person's areas is the
  // intended reading.
  const sql = `
    WITH pubs AS (
      SELECT a.person_id,
             COUNT(DISTINCT a.publication_id) AS n_pubs,
             SUM(p.cited_by_count)            AS citations
      FROM authorship  a
      JOIN publications p ON p.publication_id = a.publication_id
      GROUP BY a.person_id
    ),
    coauth AS (
      SELECT person_id, COUNT(DISTINCT other_id) AS n_coauth FROM (
        SELECT person_a_id AS person_id, person_b_id AS other_id FROM collaborations
        UNION ALL
        SELECT person_b_id AS person_id, person_a_id AS other_id FROM collaborations
      ) GROUP BY person_id
    ),
    hidx AS (
      SELECT person_id, MAX(h_index) AS h_index
      FROM person_area_metrics GROUP BY person_id
    ),
    metrics AS (
      SELECT COALESCE(pubs.person_id, coauth.person_id, hidx.person_id) AS person_id,
             pubs.n_pubs      AS n_pubs,
             pubs.citations   AS citations,
             hidx.h_index     AS h_index,
             coauth.n_coauth  AS n_coauth
      FROM pubs
      FULL OUTER JOIN coauth ON coauth.person_id = pubs.person_id
      FULL OUTER JOIN hidx   ON hidx.person_id   = COALESCE(pubs.person_id, coauth.person_id)
    ),
    -- Registry-side identity and reach. The person_id joins above only
    -- reach a member who has a people row AND publications attributed to
    -- it, which was true for 13 of 40 members. person_registry resolves the
    -- same humans on ORCID/OpenAlex equality and carries their metrics
    -- directly, so it fills in the rest.
    reg AS (
      SELECT person_id, canonical_id, orcid, openalex_id, google_scholar_id,
             homepage_url, affiliation, affiliation_ror, affiliation_country,
             h_index AS reg_h_index, works_count, cited_by_count,
             coastal_works_count, is_site_personnel, is_scholar
      FROM person_registry
      WHERE is_team AND person_id IS NOT NULL
    ),
    -- Who each member co-publishes with, split by which cohort the partner
    -- belongs to. This is the cross-cohort reach the org chart alone cannot
    -- show: a Team member's links out to the wider coastal community.
    reach AS (
      SELECT e.self_id AS canonical_id,
             COUNT(*)                                          AS reg_degree,
             COUNT(*) FILTER (WHERE o.is_scholar AND NOT o.is_team)         AS to_scholars,
             COUNT(*) FILTER (WHERE o.is_site_personnel AND NOT o.is_team)  AS to_site,
             COUNT(*) FILTER (WHERE o.is_team)                              AS to_team,
             MAX(e.co_pub_count)                               AS top_co_pubs
      FROM (
        SELECT canonical_id_a AS self_id, canonical_id_b AS other_id, co_pub_count
          FROM registry_collaborations
        UNION ALL
        SELECT canonical_id_b, canonical_id_a, co_pub_count
          FROM registry_collaborations
      ) e
      JOIN person_registry o ON o.canonical_id = e.other_id
      GROUP BY e.self_id
    )
    SELECT tm.member_id,
           tm.person_id,
           tm.display_name,
           tm.wbs_code,
           tm.role,
           tm.institution,
           tm.institution_slug,
           tm.is_pi,
           tm.is_copi,
           tm.is_leadership_committee,
           tm.committees,
           tm.status,
           tm.sort_order,
           tm.notes,
           w.title       AS wbs_title,
           w.parent_code AS wbs_parent,
           w.sort_order  AS wbs_sort,
           COALESCE(r.orcid, p.orcid)                         AS orcid,
           COALESCE(r.openalex_id, p.openalex_id)             AS openalex_id,
           COALESCE(r.google_scholar_id, p.google_scholar_id) AS google_scholar_id,
           COALESCE(r.homepage_url, p.homepage_url)           AS homepage_url,
           p.research_interests,
           m.n_pubs,
           m.citations,
           COALESCE(m.h_index, r.reg_h_index)                 AS h_index,
           m.n_coauth,
           r.canonical_id,
           r.affiliation          AS reg_affiliation,
           r.affiliation_ror,
           r.affiliation_country,
           r.works_count,
           r.cited_by_count,
           r.coastal_works_count,
           r.is_site_personnel,
           r.is_scholar,
           rc.reg_degree,
           rc.to_scholars,
           rc.to_site,
           rc.to_team,
           rc.top_co_pubs
    FROM cod_team_members tm
    LEFT JOIN cod_wbs w ON w.wbs_code  = tm.wbs_code
    LEFT JOIN people  p ON p.person_id = tm.person_id
    LEFT JOIN metrics m ON m.person_id = tm.person_id
    LEFT JOIN reg     r ON r.person_id = tm.person_id
    LEFT JOIN reach   rc ON rc.canonical_id = r.canonical_id
    ORDER BY w.sort_order, tm.sort_order, tm.display_name`;

  const res = await conn.query(sql);
  return res.toArray().map((row) => unwrapRow(row.toJSON()));
}

function instChip(row) {
  if (!row.institution && !row.institution_slug) return '';
  const color = INSTITUTION_COLORS[row.institution_slug] || '#64748b';
  const label = row.institution || row.institution_slug;
  return `<span class="team-inst" style="--inst:${color}">${esc(label)}</span>`;
}

function profileLinks(row) {
  const links = [];
  if (row.homepage_url) links.push(`<a href="${esc(row.homepage_url)}" target="_blank" rel="noopener">Homepage</a>`);
  if (row.orcid) links.push(`<a href="https://orcid.org/${esc(row.orcid)}" target="_blank" rel="noopener">ORCID</a>`);
  if (row.openalex_id) links.push(`<a href="https://openalex.org/${esc(row.openalex_id)}" target="_blank" rel="noopener">OpenAlex</a>`);
  if (row.google_scholar_id) {
    links.push(`<a href="https://scholar.google.com/citations?user=${esc(row.google_scholar_id)}" target="_blank" rel="noopener">Scholar</a>`);
  }
  if (row.person_id) links.push(`<a href="#/people/${esc(row.person_id)}">Directory</a>`);
  return links;
}

function hasMetrics(row) {
  return row.n_pubs != null || row.citations != null || row.h_index != null
      || row.works_count != null || row.reg_degree != null;
}

function metricsRow(row) {
  if (!hasMetrics(row)) return '';
  // works_count is the OpenAlex lifetime total from person_registry;
  // n_pubs counts only publications attributed inside this catalogue, which
  // is a much smaller and censored number. Prefer the registry figure and
  // say which one is shown.
  const pubs = row.works_count != null ? row.works_count : row.n_pubs;
  const pubLabel = row.works_count != null ? 'works' : 'pubs in catalogue';
  const cites = row.cited_by_count != null ? row.cited_by_count : row.citations;
  return `
    <div class="team-metrics">
      <span><strong>${fmtInt(pubs)}</strong> ${pubLabel}</span>
      <span><strong>${fmtInt(cites)}</strong> citations</span>
      <span><strong>${fmtInt(row.h_index)}</strong> h-index</span>
      ${row.coastal_works_count != null
        ? `<span><strong>${fmtInt(row.coastal_works_count)}</strong> coastal output</span>` : ''}
    </div>`;
}

// Cross-cohort reach: how far this member's co-authorship extends into the
// wider registry. This is the question the org chart cannot answer — a WBS
// code says what someone is responsible for, not who they publish with.
//
// A null reg_degree means the member has no registry identity yet (no ORCID
// or OpenAlex id resolved), which is NOT the same as having no collaborators.
// The two states are rendered differently on purpose.
function reachRow(row) {
  if (!row.canonical_id) {
    return `<div class="team-reach team-reach-unknown">
      <span>Co-authorship not yet resolved — no persistent identifier on file</span>
    </div>`;
  }
  if (row.reg_degree == null) {
    return `<div class="team-reach team-reach-unknown">
      <span>No co-publications found with anyone else in the registry</span>
    </div>`;
  }
  const parts = [
    `<span><strong>${fmtInt(row.reg_degree)}</strong> co-authors in the registry</span>`,
  ];
  if (row.to_scholars) parts.push(`<span>${fmtInt(row.to_scholars)} in the scholar roster</span>`);
  if (row.to_site) parts.push(`<span>${fmtInt(row.to_site)} at catalogued sites</span>`);
  if (row.to_team) parts.push(`<span>${fmtInt(row.to_team)} on the COD team</span>`);
  return `<div class="team-reach">${parts.join('')}</div>`;
}

function heroCard(row, kind) {
  const links = profileLinks(row);
  // A person can lead several WBS elements; the hero card names them all
  // so the chart's "5.0 / 5.1 / 5.6" pattern survives the flattening.
  const roles = (row._allRoles || [row])
    .map((r) => `${esc(r.wbs_code)} ${esc(r.wbs_title || r.role)}`)
    .join(' · ');
  return `
    <article class="team-hero team-hero-${kind}">
      <div class="team-hero-badge">${kind === 'pi' ? 'Principal Investigator' : 'Co-Investigator'}</div>
      <h3>${esc(row.display_name)}</h3>
      <p class="team-hero-role">${esc(row.role)}</p>
      ${instChip(row)}
      <p class="team-hero-wbs">${roles}</p>
      ${metricsRow(row)}
      ${reachRow(row)}
      ${row.research_interests
        ? `<p class="team-interests">${esc(row.research_interests)}</p>` : ''}
      ${links.length ? `<footer class="team-links">${links.join(' · ')}</footer>` : ''}
    </article>`;
}

function memberRow(row) {
  const links = profileLinks(row);
  // 'collective' is staffed work (the chart's NEON Staff box), so it is not
  // dimmed like a vacancy — it just has no individual to link to.
  const open = row.status === 'tbd' || row.status === 'tbh';
  return `
    <li class="team-member${open ? ' team-member-open' : ''}">
      <div class="team-member-main">
        <span class="team-member-name">${esc(row.display_name)}</span>
        <span class="team-member-role">${esc(row.role)}</span>
      </div>
      <div class="team-member-meta">
        ${instChip(row)}
        ${row.status !== 'active'
          ? `<span class="team-open-badge">${esc(row.status === 'collective' ? 'GROUP' : row.status.toUpperCase())}</span>`
          : ''}
        ${hasMetrics(row)
          ? `<span class="team-member-metric">h ${fmtInt(row.h_index)} · ${fmtInt(
              row.works_count != null ? row.works_count : row.n_pubs)} ${
              row.works_count != null ? 'works' : 'pubs in catalogue'}${
              row.reg_degree != null ? ` · ${fmtInt(row.reg_degree)} co-authors` : ''}</span>`
          : ''}
        ${links.length ? `<span class="team-links">${links.join(' · ')}</span>` : ''}
      </div>
      ${row.notes ? `<p class="team-note">${esc(row.notes)}</p>` : ''}
    </li>`;
}

function committeeSection(rows) {
  const members = rows.filter((r) => r.is_leadership_committee);
  if (!members.length) return '';
  // De-dup: a committee member who also leads a WBS element appears twice
  // in the flat query result.
  const seen = new Set();
  const unique = members.filter((r) => {
    const key = r.person_id || r.display_name;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  const chips = unique.map((r) => {
    const links = profileLinks(r);
    const chair = /co-chair/i.test(r.role || '');
    return `
      <div class="team-slc${chair ? ' team-slc-chair' : ''}">
        <span class="team-slc-name">${esc(r.display_name)}</span>
        ${chair ? '<span class="team-slc-badge">Co-Chair</span>' : ''}
        ${instChip(r)}
        ${links.length ? `<span class="team-links">${links.join(' · ')}</span>` : ''}
      </div>`;
  }).join('');
  return `
    <section class="team-section team-slc-section">
      <h2>Science Leadership Committee <span class="team-count">${unique.length}</span></h2>
      <p class="team-section-note">
        Refines the Grand Challenge questions for coastal observing and
        identifies strategic partners and data.
      </p>
      <div class="team-slc-grid">${chips}</div>
    </section>`;
}

function wbsSections(rows) {
  // Group by top-level WBS element (the '1' of '1.4.2') so the page reads
  // like the chart's seven columns rather than 52 flat headings.
  const groups = new Map();
  for (const row of rows) {
    const top = String(row.wbs_code || '').split('.')[0] + '.0';
    if (!groups.has(top)) groups.set(top, []);
    groups.get(top).push(row);
  }
  const titleOf = (code) => {
    const exact = rows.find((r) => r.wbs_code === code);
    return exact ? exact.wbs_title : code;
  };

  return [...groups.entries()]
    .sort((a, b) => Number(a[0].split('.')[0]) - Number(b[0].split('.')[0]))
    .map(([top, members]) => {
      const collapsed = _collapsed.has(top);
      const open = members.filter((m) => m.status !== 'active').length;
      const items = members
        .sort((a, b) => (a.wbs_sort || 0) - (b.wbs_sort || 0)
                     || (a.sort_order || 0) - (b.sort_order || 0))
        .map(memberRow).join('');
      return `
        <section class="team-section" data-wbs="${esc(top)}">
          <h2 class="team-wbs-head" data-toggle="${esc(top)}">
            <span class="team-caret">${collapsed ? '▸' : '▾'}</span>
            <span class="team-wbs-code">${esc(top)}</span>
            ${esc(titleOf(top))}
            <span class="team-count">${members.length}</span>
            ${open ? `<span class="team-count team-count-open">${open} open</span>` : ''}
          </h2>
          <ul class="team-list"${collapsed ? ' hidden' : ''}>${items}</ul>
        </section>`;
    }).join('');
}

async function renderTeam() {
  if (!_container) return;
  const status = _container.querySelector('.team-status');
  if (status) status.textContent = 'Loading…';

  if (!_cached) {
    try {
      _cached = await fetchTeam();
    } catch (e) {
      if (status) status.textContent = `Failed to load: ${e.message}`;
      console.error(e);
      return;
    }
  }
  const rows = _cached;
  if (!rows.length) {
    _container.innerHTML = `
      <div class="team-page">
        <header class="team-header"><h1>COD project team</h1></header>
        <p class="no-data">
          No team rows yet. Run <code>python scripts/build_cod_team_lake.py</code>
          to load the org chart from <code>data/seed/cod_team_members.csv</code>.
        </p>
      </div>`;
    return;
  }

  // Collapse each leader's multiple WBS rows into one hero card.
  const byPerson = new Map();
  for (const r of rows) {
    if (!r.is_pi && !r.is_copi) continue;
    const key = r.person_id || r.display_name;
    if (!byPerson.has(key)) byPerson.set(key, { ...r, _allRoles: [] });
    byPerson.get(key)._allRoles.push(r);
  }
  const leaders = [...byPerson.values()];
  const pi = leaders.filter((r) => r.is_pi);
  const copis = leaders.filter((r) => r.is_copi && !r.is_pi)
    .sort((a, b) => (a.sort_order || 0) - (b.sort_order || 0));

  const people = new Set(rows.filter((r) => r.person_id).map((r) => r.person_id));
  const openSlots = rows.filter((r) => r.status !== 'active').length;
  const withMetrics = rows.some(hasMetrics);

  _container.innerHTML = `
    <div class="team-page">
      <header class="team-header">
        <h1>COD project team</h1>
        <p class="team-summary">
          <strong>${people.size}</strong> named investigators and staff across
          <strong>${new Set(rows.map((r) => String(r.wbs_code).split('.')[0])).size}</strong>
          work-breakdown tracks, plus <strong>${openSlots}</strong> positions
          still to be filled. Transcribed from the project organisational
          chart; scholarly identifiers come from the researcher directory.
        </p>
      </header>

      <section class="team-section">
        <h2>Project leadership</h2>
        <div class="team-hero-grid">
          ${pi.map((r) => heroCard(r, 'pi')).join('')}
          ${copis.map((r) => heroCard(r, 'copi')).join('')}
        </div>
      </section>

      ${committeeSection(rows)}
      ${wbsSections(rows)}

      <p class="team-status">
        ${withMetrics
          ? 'Publication metrics from OpenAlex via the researcher directory.'
          : 'Publication metrics appear here once the OpenAlex enrichment scripts have run (see Docs → Team, Scholars &amp; Data Methods).'}
      </p>
    </div>`;

  for (const head of _container.querySelectorAll('.team-wbs-head')) {
    head.addEventListener('click', () => {
      const code = head.dataset.toggle;
      if (_collapsed.has(code)) _collapsed.delete(code);
      else _collapsed.add(code);
      renderTeam();
    });
  }
}

export function initTeamView(container) {
  _container = container;
  _container.innerHTML = `
    <div class="team-page">
      <p class="team-status" style="padding:24px;color:#64748b">
        COD project team loading…
      </p>
    </div>`;
}

export function renderTeamView() {
  if (!_container) return;
  renderTeam().catch((e) => {
    console.error('[team] render failed', e);
    const s = _container.querySelector('.team-status');
    if (s) s.textContent = `Render failed: ${e.message}`;
  });
}
