// sql.js — DuckDB SQL console
//
// Runs ad-hoc SQL against the parquet views registered in src/db.js
// (facilities, facility_types, networks, research_areas, regions,
// funders, area_links, network_membership, facility_regions,
// funding_links, region_area_links). Includes a handful of curated
// queries the user can click to preview interesting slices of the
// cod-kmap schema.
//
// The registry queries at the end of EXAMPLES run over the unified
// person layer (person_registry, registry_collaborations,
// registry_facilities, person_identity_source). Two things about those
// tables shape every query written against them:
//
//   1. TIERING. The browser is served only `tier='core'` — 10,000 of
//      152,008 identities. Every count a registry query returns here is
//      a count within the core tier, not within the field. The console
//      surfaces the tier split explicitly (see 'registry-tier-split')
//      so the number is visible rather than implied.
//   2. GRAPH SCOPE. `registry_collaborations` was computed over the 618
//      pre-harvest identities, so most core-tier researchers carry no
//      edge at all. Degree 0 means "not measured", never "publishes
//      alone" — 'registry-degree-distribution' names that band as such
//      rather than folding it in with genuinely low-degree nodes.
//
// `coastal_works_count` is an upper bound on distinct coastal papers,
// not a count: OpenAlex lists a work under every topic it carries, so
// summing over the coastal topic set double-counts multi-topic papers.
// Registry queries label the column `coastal_volume` for that reason.

import { getConn, whenReady, unwrapRow, ensureSqlTables } from '../db.js';

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
  {
    id: 'funding-by-year',
    title: 'Facility funding by year',
    description:
      'Nominal USD per facility per fiscal year, pulled from the time-series ' +
      'funding_events table. Only facilities with at least one dollar amount ' +
      'recorded show up; empty cells mean we haven\u2019t ingested that year yet.',
    sql: `-- Facility × fiscal_year totals (nominal USD)
SELECT facility,
       fiscal_year,
       total_usd_nominal,
       n_awards,
       funders
FROM   v_facility_funding_by_year
ORDER  BY facility, fiscal_year;`,
  },
  {
    id: 'funder-year-rollup',
    title: 'Funder totals by year',
    description:
      'How much each funder allocated across the tracked facilities, per fiscal ' +
      'year. Answers "how much NSF money flowed through this dataset in 2021?"',
    sql: `-- Funder × fiscal_year rollup
SELECT funder,
       funder_type,
       fiscal_year,
       total_usd_nominal,
       n_awards,
       n_facilities
FROM   v_funder_funding_by_year
ORDER  BY fiscal_year DESC, total_usd_nominal DESC;`,
  },
  {
    id: 'key-personnel',
    title: 'Current key personnel (Directors, Chief Scientists...)',
    description:
      'Today\u2019s Directors, Deputy Directors, Chief Scientists, and Head ' +
      'Administrators across the facility network. Populated from the ' +
      'facility_personnel table — empty until you run load_facility_personnel.py ' +
      'with a seed CSV or enrich_people_openalex.py against the API.',
    sql: `-- Current key personnel (is_key_personnel=true, end_date NULL or future)
SELECT facility_acronym,
       facility,
       name,
       role,
       title,
       orcid,
       homepage_url,
       email
FROM   v_facility_key_personnel
ORDER  BY facility, role, name;`,
  },
  {
    id: 'top-researchers-by-facility',
    title: 'Top researchers per facility',
    description:
      'Every researcher linked to a facility via facility_personnel, ranked by ' +
      'publication count. Populated by seed_people_from_openalex.py (top authors ' +
      'from each facility\u2019s OpenAlex institution profile).',
    sql: `-- Researchers grouped by facility, sorted by pub count
SELECT f.canonical_name       AS facility,
       f.acronym              AS acronym,
       p.name                 AS researcher,
       fp.role,
       p.orcid,
       p.openalex_id,
       COUNT(DISTINCT a.publication_id) AS n_pubs,
       p.research_interests
FROM   facilities         f
JOIN   facility_personnel fp ON fp.facility_id = f.facility_id
JOIN   people             p  ON p.person_id    = fp.person_id
LEFT   JOIN authorship    a  ON a.person_id    = p.person_id
GROUP  BY f.canonical_name, f.acronym, p.name, fp.role,
         p.orcid, p.openalex_id, p.research_interests
ORDER  BY facility, n_pubs DESC, researcher
LIMIT  500;`,
  },
  {
    id: 'person-research-areas',
    title: 'Person research areas (by publication topics)',
    description:
      'Each researcher mapped to cod-kmap research areas via the OpenAlex topics ' +
      'on their publications. `weight` is the average per-publication match score ' +
      '(0..1) and `evidence_count` is how many of their papers landed in that area. ' +
      'Populated by scripts/compute_person_areas.py.',
    sql: `-- Person × research_area derived from publication topics
SELECT p.name                        AS researcher,
       ra.label                      AS research_area,
       ROUND(pa.weight, 3)           AS weight,
       pa.evidence_count             AS evidence_pubs,
       pa.source
FROM   person_areas pa
JOIN   people         p  ON p.person_id = pa.person_id
JOIN   research_areas ra ON ra.area_id  = pa.area_id
WHERE  pa.evidence_count >= 2
ORDER  BY weight DESC, researcher, research_area
LIMIT  500;`,
  },
  {
    id: 'top-collaborations',
    title: 'Top co-authorship pairs',
    description:
      'Strongest co-authorship pairs across all tracked facilities, from the ' +
      'collaborations table computed by scripts/compute_collaborations.py. ' +
      'Each row is one canonical (A, B) pair with A.person_id < B.person_id.',
    sql: `-- Top 50 collaboration pairs by shared publication count
SELECT pa.name                         AS person_a,
       pb.name                         AS person_b,
       c.co_pub_count                  AS shared_pubs,
       c.first_year,
       c.last_year,
       ROUND(c.strength, 2)            AS strength,
       list(DISTINCT fa.acronym || '/' || fb.acronym) AS facility_pairs
FROM   collaborations c
JOIN   people pa ON pa.person_id = c.person_a_id
JOIN   people pb ON pb.person_id = c.person_b_id
LEFT   JOIN facility_personnel fap ON fap.person_id = pa.person_id
LEFT   JOIN facilities         fa  ON fa.facility_id = fap.facility_id
LEFT   JOIN facility_personnel fbp ON fbp.person_id = pb.person_id
LEFT   JOIN facilities         fb  ON fb.facility_id = fbp.facility_id
GROUP  BY pa.name, pb.name, c.co_pub_count,
         c.first_year, c.last_year, c.strength
ORDER  BY shared_pubs DESC
LIMIT  50;`,
  },

  // ── Unified person registry ────────────────────────────────────────
  //
  // Everything below runs over person_registry / registry_collaborations /
  // registry_facilities / person_identity_source. Read the tiering and
  // graph-scope notes at the top of this file before quoting any count
  // these return: the browser holds the 10,000-row core tier, and the
  // co-authorship graph covers 618 nodes, not all 152,008 identities.
  {
    id: 'registry-tier-split',
    title: 'Registry: tier split',
    description:
      'The unified person registry holds 152,008 identities, but the browser is ' +
      'served only the 10,000-row `core` tier — the whole COD roster plus the ' +
      'highest-scoring coastal authors. Run this in the browser and you get one ' +
      'row: `core`. Run it against the local DuckDB and you get two. Every other ' +
      'registry query on this page is scoped to whichever set you are querying, ' +
      'so this is the first thing worth checking.',
    sql: `-- Registry tier split: what ships to the browser vs. what stays local
SELECT tier,
       COUNT(*)                                   AS identities,
       ROUND(AVG(h_index), 1)                     AS avg_h_index,
       ROUND(AVG(coastal_works_count), 1)         AS avg_coastal_volume,
       COUNT(DISTINCT affiliation_country)        AS countries,
       COUNT(orcid)                               AS with_orcid
FROM   person_registry
GROUP  BY tier
ORDER  BY identities DESC;`,
  },
  {
    id: 'registry-multi-cohort',
    title: 'Registry: who spans more than one cohort',
    description:
      'The registry merges the three human layers — Team org chart, site ' +
      'personnel, community scholars — on ORCID or OpenAlex id equality, never on ' +
      'name. These 11 people hold more than one role at once, a fact the ' +
      'pre-registry schema could not represent because each layer had its own ' +
      'key.',
    sql: `-- Researchers holding more than one cohort role at once
SELECT pr.display_name                     AS researcher,
       CONCAT_WS(' + ',
         CASE WHEN pr.is_team           THEN 'Team'           END,
         CASE WHEN pr.is_site_personnel THEN 'Site personnel' END,
         CASE WHEN pr.is_scholar        THEN 'Scholar'        END) AS cohorts,
       CAST(pr.is_team AS INT)
     + CAST(pr.is_site_personnel AS INT)
     + CAST(pr.is_scholar AS INT)          AS n_cohorts,
       pr.affiliation,
       pr.affiliation_country              AS country,
       pr.orcid,
       pr.h_index,
       pr.coastal_works_count              AS coastal_volume,
       pr.canonical_id
FROM   person_registry pr
WHERE  CAST(pr.is_team AS INT)
     + CAST(pr.is_site_personnel AS INT)
     + CAST(pr.is_scholar AS INT) > 1
ORDER  BY n_cohorts DESC, pr.h_index DESC NULLS LAST, researcher;`,
  },
  {
    id: 'registry-top-edges',
    title: 'Registry: strongest co-publication edges',
    description:
      'The 50 heaviest co-publication edges over the registry node set, with each ' +
      'endpoint\'s cohort. `shared_areas` and `shared_facilities` are sparse — ' +
      'populated for 137 and 14 of 5,300 edges respectively — so most rows show ' +
      'null there.',
    sql: `-- Strongest co-publication edges over the registry node set
SELECT a.display_name   AS person_a,
       CASE WHEN a.is_team THEN 'Team'
            WHEN a.is_site_personnel THEN 'Site personnel'
            ELSE 'Scholar' END AS cohort_a,
       b.display_name   AS person_b,
       CASE WHEN b.is_team THEN 'Team'
            WHEN b.is_site_personnel THEN 'Site personnel'
            ELSE 'Scholar' END AS cohort_b,
       e.co_pub_count   AS co_pubs,
       e.first_year,
       e.last_year,
       e.shared_areas,
       e.shared_facilities
FROM   registry_collaborations e
JOIN   person_registry a ON a.canonical_id = e.canonical_id_a
JOIN   person_registry b ON b.canonical_id = e.canonical_id_b
ORDER  BY e.co_pub_count DESC, person_a, person_b
LIMIT  50;`,
  },
  {
    id: 'registry-cohort-edge-census',
    title: 'Registry: cross-cohort edge census',
    description:
      'Every edge classified by the cohort pair at its ends. This is the query ' +
      'the old `collaborations` table structurally could not answer: it was keyed ' +
      'on `people(person_id)`, so a Team↔Scholar edge had nowhere to live. Note ' +
      'the Team↔Team row.',
    sql: `-- Cross-cohort edge census. LEAST/GREATEST folds each unordered
-- cohort pair onto one row (canonical_id_a < canonical_id_b orders the
-- ids, not the roles, so Team<->Scholar and Scholar<->Team are one type).
WITH labelled AS (
  SELECT CASE WHEN a.is_team THEN 'Team'
              WHEN a.is_site_personnel THEN 'Site personnel'
              ELSE 'Scholar' END AS role_a,
         CASE WHEN b.is_team THEN 'Team'
              WHEN b.is_site_personnel THEN 'Site personnel'
              ELSE 'Scholar' END AS role_b,
         e.co_pub_count
  FROM   registry_collaborations e
  JOIN   person_registry a ON a.canonical_id = e.canonical_id_a
  JOIN   person_registry b ON b.canonical_id = e.canonical_id_b
)
SELECT LEAST(role_a, role_b) || ' <-> ' || GREATEST(role_a, role_b) AS edge_type,
       COUNT(*)                     AS edges,
       SUM(co_pub_count)            AS co_pubs_total,
       MAX(co_pub_count)            AS strongest_edge,
       ROUND(AVG(co_pub_count), 1)  AS mean_co_pubs
FROM   labelled
GROUP  BY edge_type
ORDER  BY edges DESC;`,
  },
  {
    id: 'registry-team-internal-edges',
    title: 'Registry: Team-internal co-authorship (all of it)',
    description:
      'Fourteen COD Team members share exactly one internal co-publication edge — ' +
      'Bond-Lamberty↔Myers-Pigg, 18 papers, 2019–2026. One row is the complete ' +
      'answer, not a truncated one. The Team\'s other 127 edges all reach outward, ' +
      'to scholars and site personnel.',
    sql: `-- Every co-authorship edge internal to the COD Team. There is one.
SELECT a.display_name  AS team_member_a,
       b.display_name  AS team_member_b,
       e.co_pub_count  AS co_pubs,
       e.first_year,
       e.last_year
FROM   registry_collaborations e
JOIN   person_registry a ON a.canonical_id = e.canonical_id_a AND a.is_team
JOIN   person_registry b ON b.canonical_id = e.canonical_id_b AND b.is_team
ORDER  BY co_pubs DESC;`,
  },
  {
    id: 'registry-site-roster-by-ror',
    title: 'Registry: researchers at a given site (via ROR)',
    description:
      '`registry_facilities` links a researcher to a catalogued site when their ' +
      'OpenAlex affiliation ROR equals `facilities.ror` — identifier equality ' +
      'only, no name matching. Edit the acronym list to pick sites; delete the ' +
      'WHERE clause for all of them. Only 69 of ~209 research organisations carry ' +
      'a ROR so far, and the 3,309 protected areas never will — a state park is a ' +
      'place, not an organisation.',
    sql: `-- Researchers linked to a catalogued site by ROR equality.
-- Swap the acronym in the WHERE clause, or delete the clause for all sites.
SELECT f.canonical_name       AS site,
       f.acronym,
       f.ror,
       pr.display_name        AS researcher,
       pr.orcid,
       pr.h_index,
       pr.coastal_works_count AS coastal_volume,
       pr.tier,
       rf.method              AS link_method,
       rf.confidence
FROM   registry_facilities rf
JOIN   facilities      f  ON f.facility_id   = rf.facility_id
JOIN   person_registry pr ON pr.canonical_id = rf.canonical_id
WHERE  f.acronym IN ('SIO', 'WHOI', 'MBARI')
ORDER  BY site, pr.h_index DESC NULLS LAST, researcher;`,
  },
  {
    id: 'registry-sites-ranked',
    title: 'Registry: sites ranked by resolved researchers',
    description:
      'Which catalogued sites have the deepest researcher rosters resolved ' +
      'through ROR. Counts are a floor, bounded by both ROR coverage on the ' +
      'facility side and the core tier on the researcher side — the local DuckDB ' +
      'resolves 1,467 links across 39 sites where the browser sees 263 across 26.',
    sql: `-- Sites ranked by how many registry researchers resolve to their ROR
SELECT f.canonical_name                AS site,
       f.acronym,
       f.ror,
       f.country,
       COUNT(DISTINCT rf.canonical_id) AS researchers,
       ROUND(AVG(pr.h_index), 1)       AS avg_h_index,
       SUM(pr.coastal_works_count)     AS coastal_volume_sum
FROM   registry_facilities rf
JOIN   facilities      f  ON f.facility_id   = rf.facility_id
JOIN   person_registry pr ON pr.canonical_id = rf.canonical_id
GROUP  BY f.canonical_name, f.acronym, f.ror, f.country
ORDER  BY researchers DESC, site;`,
  },
  {
    id: 'registry-identifier-coverage',
    title: 'Registry: identifier coverage',
    description:
      'How many registry rows carry each persistent identifier. `pct` is against ' +
      'the rows in scope, so it reads differently in the browser (core tier) than ' +
      'locally (full population) — core-tier rows are far better identified ' +
      'because tier scoring rewarded the same bibliometric completeness. Google ' +
      'Scholar ids are nearly absent: OpenAlex does not populate `ids.scholar` ' +
      'for most authors.',
    sql: `-- Persistent-identifier coverage across the registry rows in scope
SELECT 'ORCID' AS identifier, COUNT(orcid) AS populated, COUNT(*) AS rows_in_scope,
       ROUND(100.0 * COUNT(orcid) / COUNT(*), 1) AS pct
FROM   person_registry
UNION ALL SELECT 'OpenAlex author id', COUNT(openalex_id), COUNT(*),
       ROUND(100.0 * COUNT(openalex_id) / COUNT(*), 1) FROM person_registry
UNION ALL SELECT 'ROR-bearing affiliation', COUNT(affiliation_ror), COUNT(*),
       ROUND(100.0 * COUNT(affiliation_ror) / COUNT(*), 1) FROM person_registry
UNION ALL SELECT 'Homepage URL', COUNT(homepage_url), COUNT(*),
       ROUND(100.0 * COUNT(homepage_url) / COUNT(*), 1) FROM person_registry
UNION ALL SELECT 'Google Scholar id', COUNT(google_scholar_id), COUNT(*),
       ROUND(100.0 * COUNT(google_scholar_id) / COUNT(*), 1) FROM person_registry
ORDER  BY populated DESC;`,
  },
  {
    id: 'registry-orcid-conflicts',
    title: 'Registry: ORCID conflicts awaiting curation',
    description:
      'Two cases where OpenAlex reports a different ORCID than the registry ' +
      'holds. They are logged with `field=\'orcid-conflict\'` and deliberately NOT ' +
      'applied — auto-merging on a contested identifier is how wrong-person ' +
      'attributions get made. Both need a human to adjudicate.',
    sql: `-- ORCID conflicts logged for curation, never auto-applied
SELECT pis.canonical_id,
       pr.display_name AS researcher,
       pr.orcid        AS registry_orcid,
       pis.value       AS conflicting_orcid,
       pis.method,
       pis.evidence,
       pis.source_url,
       pis.confidence
FROM   person_identity_source pis
LEFT   JOIN person_registry pr ON pr.canonical_id = pis.canonical_id
WHERE  pis.field = 'orcid-conflict'
ORDER  BY researcher NULLS LAST;`,
  },
  {
    id: 'registry-identity-provenance',
    title: 'Registry: identifier provenance by rule',
    description:
      'Every identifier in the registry names the rule that produced it. ' +
      '`openalex-topic-harvest` is the bulk topic-filtered ingest; ' +
      '`orcid-equality` and `openalex_id-equality` are the only two merge rules, ' +
      'and they account for 17 assertions across 15 identities — merges are rare ' +
      'by design.',
    sql: `-- Provenance: which rule produced each identifier assertion
SELECT pis.field,
       pis.method,
       pis.confidence,
       COUNT(*)                         AS assertions,
       COUNT(DISTINCT pis.canonical_id) AS identities
FROM   person_identity_source pis
GROUP  BY pis.field, pis.method, pis.confidence
ORDER  BY assertions DESC;`,
  },
  {
    id: 'registry-degree-distribution',
    title: 'Registry: co-authorship degree bands',
    description:
      'Degree bands over the registry. The first band is the important one: the ' +
      'co-authorship graph was built over the 618 pre-harvest identities, so the ' +
      '9,490 core-tier researchers outside it have no edge COMPUTED — that is a ' +
      'measurement boundary, not a finding about how they publish. Two of ' +
      'fourteen Team members do sit inside the graph with zero edges, which is a ' +
      'real zero.',
    sql: `-- Co-authorship degree bands. The graph was built over the 618
-- pre-harvest identities, so degree 0 means "no edge computed for this
-- node", not "this researcher has no collaborators".
WITH in_graph AS (
  SELECT canonical_id_a AS canonical_id FROM registry_collaborations
  UNION
  SELECT canonical_id_b FROM registry_collaborations
),
deg AS (
  SELECT pr.canonical_id,
         pr.is_team,
         g.canonical_id IS NOT NULL AS measured,
         COUNT(e.canonical_id_a)    AS degree
  FROM   person_registry pr
  LEFT   JOIN in_graph g ON g.canonical_id = pr.canonical_id
  LEFT   JOIN registry_collaborations e
         ON  e.canonical_id_a = pr.canonical_id
         OR  e.canonical_id_b = pr.canonical_id
  GROUP  BY pr.canonical_id, pr.is_team, measured
)
SELECT CASE WHEN NOT measured   THEN 'not in graph (no edge computed)'
            WHEN degree <= 5    THEN '1-5'
            WHEN degree <= 20   THEN '6-20'
            WHEN degree <= 50   THEN '21-50'
            ELSE '51+' END                        AS degree_band,
       COUNT(*)                                   AS researchers,
       SUM(CASE WHEN is_team THEN 1 ELSE 0 END)   AS team_members
FROM   deg
GROUP  BY degree_band
ORDER  BY MIN(CASE WHEN measured THEN degree ELSE -1 END);`,
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
// array of plain JS objects. Delegates to unwrapRow (db.js) which
// handles BigInts, Arrow Vectors (LIST<STRUCT>), and nested structs.
// Date stringification is added on top here since the SQL view renders
// dates verbatim in the result table.
function resultToRows(result) {
  return result.toArray().map((row) => {
    const o = unwrapRow(row.toJSON());
    for (const k of Object.keys(o)) {
      if (o[k] instanceof Date) {
        o[k] = o[k].toISOString();
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
    // The SQL-console-only tables (publication_topics, funding_events,
    // locations, …) and the v_* helper views are registered on demand rather
    // than at init — they are 3.85 MB and 5 round-trips that the map does not
    // need. Arbitrary user SQL may reference any of them, so register before
    // executing. Idempotent; the cost is paid once per session.
    await ensureSqlTables();
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
            <li>research_areas · research_areas_active · area_links</li>
            <li>regions · region_area_links · facility_regions</li>
            <li>funders · funding_links · funding_events</li>
            <li>people · facility_personnel · person_areas</li>
            <li>publications · authorship · publication_topics · collaborations</li>
            <li>cod_wbs · cod_team_members · community_scholars</li>
            <li>coastal_datasets · dataset_endpoints</li>
            <li>facility_primary_groups · person_primary_groups</li>
            <li>person_area_metrics · facility_area_funding ·
                funder_area_funding · area_coverage_matrix</li>
            <li><strong>person_registry</strong> · person_identity_source</li>
            <li><strong>registry_collaborations</strong> · registry_facilities</li>
          </ul>
          <p class="sql-sub" style="font-size:.76rem">
            The registry tables unify the Team, site-personnel and scholar
            layers on one key per human (<code>canonical_id</code>). This
            browser holds the <code>core</code> tier — <strong>10,000</strong>
            of 152,008 identities — so registry counts here describe the
            shipped subset, not the whole field. The co-authorship graph was
            computed over 618 nodes, so a researcher with no edge is
            <em>unmeasured</em>, not solitary.
          </p>
          <p class="sql-sub" style="font-size:.76rem">
            Helper views: <code>v_facility_funding_by_year</code>,
            <code>v_funder_funding_by_year</code>,
            <code>v_facility_key_personnel</code>.
          </p>
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
