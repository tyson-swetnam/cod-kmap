// network.js — Knowledge map (MVG, Map Visualization with Group restriction).
//
// Replaces the previous force-directed knowledge graph with a country-like
// map where each polygon is one research area (parent-collapsed when small),
// polygon area is proportional to how much OBSERVATORY CAPACITY, DATA and
// PEOPLE that area carries, and organisations + people sit inside their
// polygon. Cross-area edges reveal interdisciplinary collaboration.
//
// SIZING METRIC — why it is not the facility count
// ------------------------------------------------
// Region area used to be proportional to research_areas_active.n_facilities.
// Measured against the shipped site parquet, that made the map a state-park
// index: of 3,519 catalogued facilities, 3,309 are protected areas
// (facility_type LIKE 'protected-area%'), every one of them assigned to
// "Coastal terrestrial ecosystems", and they carry ZERO datasets and ZERO
// researchers. One region therefore claimed 94.4% of the map area while the
// 200 organisations that actually run observatories and publish data shared
// the remaining 5.6%.
//
// The cartogram weight is now DATA_AND_PEOPLE (see areaWeightSql):
//
//   weight = n_organisations
//          + W_DATA_PROVIDER * n_facilities_with_datasets
//          + W_DATASET       * n_datasets_produced
//          + W_PERSON        * n_people_anchored_here
//
// n_organisations counts non-protected-area facilities only. The person term
// sums three cohorts that all resolve to this region: facility_personnel
// (the site directory), registry researchers ROR-linked to a site in this
// region, and registry researchers with no site link whose dominant science
// domain is this region. Coefficients are declared as constants below so the
// ranking is auditable rather than buried in a SQL expression.
//
// Protected areas are NOT deleted — they stay in the catalogue, in the SQL
// console, and behind the off-by-default "Protected areas" layer, which
// draws them as one aggregate chip per region rather than 3,309 marks. They
// simply do not drive region area, because they carry nothing the map is
// about.
//
// Implements the KMap algorithm from Hossain, Moradi, Mondal & Kobourov,
// "Map Visualizations for Graphs with Group Restrictions" (Graphics
// Interface 2025, DOI 10.1145/3769872.3769900). Three steps:
//
//   1. Supergraph: one supernode per active research area, weight = the
//      data-and-people weight above, edges = cross-area facility-personnel
//      + co-author counts. Embed with d3-force using square collision so
//      each area gets a non-overlapping square sized by sqrt(weight).
//
//   2. Subgraph layout: for each area, run a small d3-force layout on its
//      facility + person nodes, then scale to fit inside its square.
//
//   3. Voronoi-merged polygons: compute Voronoi over all node positions
//      plus a ring of perimeter "anchor" points; for each area, union the
//      cells of its members via polygon-clipping. Smooth boundaries via
//      one Chaikin pass to soften the polygon outlines.
//
// Phase 3 (PCL refinement, custom force terms) and Phase 5 (canvas
// rendering + Web Worker) are documented in docs/map_visualization_plan.md
// and will land as follow-up commits.

import { getConn, whenReady, unwrapRow } from '../db.js';

// ── Module state ────────────────────────────────────────────────────
let _container = null;
let _layout = null;
let _d3Promise = null;
let _delaunayPromise = null;
let _polygonClippingPromise = null;
let _showFacility = true;
let _showPerson = true;
// d3.zoom behavior + svg selection + d3 module captured during render
// so the TOC sidebar (and polygon clicks) can call zoom.transform
// programmatically using d3.zoomIdentity.
let _zoomBehavior = null;
let _zoomSvg = null;
let _d3Mod = null;
let _colorOf = null;
// Last zoom level seen — used by the label-visibility logic. Default
// to 1 so the initial paint shows labels at constant size.
let _zoomK = 1;
// Reference to the labels/dots selections so onZoom can resize them
// without a full re-render.
let _labelSel = null;
let _areaLabelSel = null;
let _facLabelSel = null;
let _dotPersonSel = null;
let _dotFacSel = null;
// Root <g> that carries the composed viewBox + zoom transform. onZoom
// reads its screen CTM to size labels in real screen pixels.
let _rootG = null;
// Protected-area layer: OFF by default. One aggregate chip per region
// rather than 3,309 marks — see the protected_areas query.
let _showProtected = false;
let _paLabelSel = null;
let _paChipSel = null;
// Registry researcher name labels for the focused site, and the
// "(no site)" cohort captions for domain-placed researchers.
let _regLabelSel = null;
let _regDomainLabelSel = null;
// Selection for the facility sub-polygons themselves, captured so
// hover/zoom logic can address them later (e.g. dim non-hovered
// polygons in the same area to reveal a single institution).
let _facPolySel = null;

// ── Registry (researcher) layer state ───────────────────────────────
// The registry layer is OFF by default and its data is fetched lazily
// the first time it is switched on, so the initial map paint costs
// exactly what it costs today. See fetchRegistry() for the payload
// accounting.
let _showRegistry = false;
let _registry = null;            // indexed result of fetchRegistry()
let _registryPromise = null;     // in-flight fetch, so double-toggle is safe
// Which catalogued site is currently expanded into individual
// researcher nodes. null = no site selected (only site↔site ribbons
// are drawn). Exactly one site is expanded at a time — this is the
// gate that keeps 10,000 registry rows from ever becoming 10,000
// markers.
let _focusFacility = null;
// Selections owned by the registry layer, captured so onZoom() can
// counter-scale them and so a focus change can redraw only this layer.
let _regRootG = null;
let _regSiteLinkSel = null;
let _regNodeSel = null;
let _regNodeLinkSel = null;

// 33-step palette for area polygons. Tuned for distinguishability
// against a parchment background with low-alpha fills.
const AREA_PALETTE = [
  '#7c3aed', '#0d9488', '#d97706', '#dc2626', '#2563eb',
  '#059669', '#a16207', '#9333ea', '#0891b2', '#65a30d',
  '#e11d48', '#0284c7', '#ca8a04', '#7e22ce', '#16a34a',
  '#b45309', '#1d4ed8', '#15803d', '#a21caf', '#be123c',
  '#0369a1', '#4d7c0f', '#be185d', '#1e40af', '#166534',
  '#86198f', '#1e3a8a', '#854d0e', '#5b21b6', '#0c4a6e',
  '#365314', '#3f6212', '#172554',
];

const NODE_COLORS = {
  facility: '#0d6e6e',
  person:   '#0ea5e9',
  // Registry researchers are a THIRD colour, distinct from the
  // facility-directory people already on the map (#0ea5e9) and from the
  // teal facility markers (#0d6e6e).
  //
  // This was '#b45309', which IS AREA_PALETTE[15], so registry markers were
  // indistinguishable from whichever region drew that fill. Which region
  // that is depends on the data: colours are assigned by position in the
  // FILTERED (weight > 0) area list, so it moves whenever the weights move.
  // On the parquet shipped at the time of writing, index 15 is
  // ocean-acidification. (An earlier version of this comment named "Kelp
  // forests"; that region has weight 0, is filtered out before colouring,
  // and never receives a palette index at all.)
  //
  // '#7c2d12' is absent from the 33-entry palette and sits 33.5 RGB units
  // from its nearest member, 158 from the facility teal and 270 from the
  // person sky. Check any replacement the same way before changing it.
  registry: '#7c2d12',
  registryEdge: '#c2410c',
  // Protected-area context layer. A muted olive, chosen the same way the
  // registry colour was: it is absent from the 33-entry AREA_PALETTE, and
  // its nearest palette member ('#4d7c0f') sits 49.4 RGB units away while it
  // is 103.8 from the facility teal and 198.9 from the person sky (Euclidean
  // over 8-bit RGB, the same metric the registry figures above use). Check
  // any replacement the same way.
  protected: '#65803a',
};
const NODE_RADIUS = { facility: 4, person: 3 };

// Registry-layer tuning.
//   FOCUS_NODE_CAP  — hard ceiling on researcher markers drawn for the
//                     focused site. The largest site in registry_facilities
//                     (Scripps) has 82 linked researchers; 48 keeps the
//                     densest sub-polygon legible and the side panel lists
//                     the remainder.
//   SITE_LINK_MIN   — minimum co-publication total for a site↔site
//                     ribbon. Left at 1 deliberately: the aggregate is
//                     only 18 pairs, so there is no rendering reason to
//                     drop any, and a threshold would silently delete
//                     the six weight-1 pairs (six real site links)
//                     without saying so anywhere in the UI. The knob
//                     exists for when the registry grows; raising it
//                     means adding a note to the layer that it is set.
const FOCUS_NODE_CAP = 48;
const SITE_LINK_MIN  = 1;

// Layout tuning. Polygon area must be roughly proportional to area
// weight (n_facilities), so we size the supernode squares as
// side = SUPERNODE_SCALE * sqrt(weight) — a true cartogram.
//
// CRITICAL: with the previous (small) SUPERNODE_SCALE + a 50 px floor,
// dense areas (coastal-processes 70 facilities packed tightly) ended
// up with TINY Voronoi cells while sparse areas (great-lakes 2
// facilities far apart) got HUGE cells — visually inverted from the
// cartogram metric. Boosted scale to 24 and dropped the floor; we
// now also pepper interior 'decoration anchor' points inside every
// area's square so Voronoi cells tile the full square area, not just
// the immediate neighbourhood of real nodes. Result: polygon area
// closely tracks sqrt(weight)² = weight, as the paper intends.
const SUPERGRAPH_TICKS = 400;
const SUBGRAPH_TICKS   = 120;
const SUPER_PADDING    = 14;     // px gap between adjacent squares
const PERIMETER_PAD    = 0.18;   // anchor ring at 1+pad of layout bbox half-width
const PERIMETER_NODES  = 18;     // outer anchors around the entire layout
// side = SUPERNODE_SCALE * sqrt(weight). Rescaled with the weight metric:
// the old weight was a facility count spanning 1…3,311, the new
// data-and-people weight spans 0.35…196.7 on the shipped parquet, so a
// scale tuned for the former produced a layout roughly 3× too small.
// 34 keeps the total canvas comparable (largest side ≈ 477 px).
//
// SUPERNODE_MIN is deliberately set BELOW 34*sqrt(min weight) = 20.1 so
// the floor binds for nothing in the current data. That matters: a
// binding floor inflates the smallest region's AREA by (min/true)², which
// is exactly the cartogram-lying behaviour this change set out to remove.
// If a future region lands under it, the floor keeps it clickable — but
// check whether it is distorting the ranking before raising it.
const SUPERNODE_SCALE  = 34;     // side = scale * sqrt(weight)
const SUPERNODE_MIN    = 18;     // minimum side so a tiny region stays clickable
const DECOR_GRID       = 5;      // 5×5 = 25 decoration anchors per area square
const DECOR_JITTER     = 0.18;   // ±18% random jitter so cell boundaries aren't gridlike

// ── Cartogram weight coefficients (see the header note) ─────────────
// Region area ∝ this weight. An organisation is worth 1. Being a data
// PRODUCER is worth 3 on top of that, because "this site ships data" is
// the single property the user asked the map to emphasise and only 24 of
// 200 organisations have it. Each individual dataset adds 1.5, so a site
// with 8 datasets outweighs one with 1. A person is worth 0.35 so that
// the 163 registry researchers at the two marine-geology giants lift that
// region without letting head-count alone dominate a region with no data.
//
// Measured effect on the shipped site parquet (verify with the SQL in the
// summary): Coastal terrestrial ecosystems falls from 94.4% of map area to
// 0.5%; Marine ecosystems (72 orgs, 19 datasets, 10 data providers, 189
// people) rises to 34.1%; Marine geology rises from 1.0% to 15.7% on the
// strength of 7 datasets and 177 people despite holding only 6 orgs.
const W_DATA_PROVIDER = 3.0;
const W_DATASET       = 1.5;
const W_PERSON        = 0.35;

// ── Label sizing: on-screen pixels, not SVG units ───────────────────
// Every label class declares the size it should occupy ON THE SCREEN.
// onZoom() divides by the zoom factor k to get the SVG font-size, so the
// rendered result is the declared px at every k — the old
// `Math.min(1, 1/max(k,0.5))` clamp collapsed to exactly 1 for all k < 1,
// which left a mid-range area label at 6.5 px and a mid-range person label
// at 5.0 px on screen at the initial fit (k = 0.5). Across their full base
// ranges the old classes rendered 5.0–8.0 px (area, base 10–16) and
// 4.0–6.0 px (person, base 8–12) at that zoom.
//
// The floor is the legibility gate. The tooltip is the user's stated size
// reference: .network-tooltip is 0.82rem on a 14 px root = 11.5 px body,
// with .tt-sub at 0.78rem = 10.9 px and .tt-kind at 0.7rem = 9.8 px. So
// 9.8 px is the smallest text this site already asks anyone to read, and
// LABEL_MIN_PX is set there. Nothing is rendered below it — a class whose
// screen size would fall under the floor is HIDDEN, never shrunk.
//
// Every base size below is >= LABEL_MIN_PX by construction, and screenPx()
// enforces the floor a second time by returning null (which its callers
// render as display:none) rather than a reduced size, so no arithmetic
// change upstream can silently reintroduce sub-floor text. Note it HIDES;
// it does not clamp — a clamp would draw the label at the floor size and
// so change a declared size silently, which is the opposite of the intent.
const LABEL_MIN_PX = 9.8;
// Area (region) names: the top of the hierarchy, sized like the tooltip
// title (.tt-name 0.92rem = 12.9 px) at the small end and up to 20 px for
// the heaviest region.
const AREA_LABEL_PX     = { min: 13.0, max: 20.0 };
// Organisation names: tooltip-body size, never below the floor.
const FAC_LABEL_PX      = { min: 10.5, max: 14.0 };
// Researcher names: tooltip .tt-kind size at the small end.
const PERSON_LABEL_PX   = { min: 10.0, max: 13.5 };
// Registry-researcher names in the focused-site expansion.
const REG_LABEL_PX      = { min: 10.0, max: 12.5 };
// Protected-area aggregate chips.
const PA_LABEL_PX       = { min: 10.0, max: 12.0 };

// ── Zoom bands per label class ──────────────────────────────────────
// [kMin, kMax): a class is drawn only inside its band, so labels appear
// when they are relevant and disappear BOTH when the user is too far out
// (nothing to attach them to) AND too far in (the region they name is no
// longer on screen — its members are). Bands are half-open and overlap by
// design, so at any k at least one class is labelled.
//
// scaleExtent is [0.4, 12] and the initial fit lands near k = 0.5.
//   region       0.40 – 3.0   region names; gone once you are inside one
//   organisation 0.75 – 12    institution names; the mid-zoom reading
//   researcher   1.30 – 12    individual names; only once a site fills
//                             the frame, otherwise it is name soup
//   registry     1.00 – 12    focused-site roster names
//   protected    0.40 – 2.0   aggregate chips; coarse context only
const ZOOM_BANDS = {
  area:      { min: 0.40, max: 3.00 },
  facility:  { min: 0.75, max: 12.01 },
  person:    { min: 1.30, max: 12.01 },
  registry:  { min: 1.00, max: 12.01 },
  protected: { min: 0.40, max: 2.00 },
};

// True when zoom level k sits inside a class's band.
function inBand(band, k) {
  return k >= band.min && k < band.max;
}

// SVG font-size that renders as `px` CSS pixels on screen, given the
// composed world→screen scale s (viewBox fit × zoom k — NOT k alone).
// Returns null when the requested screen size is below the legibility
// floor, which callers treat as "hide this label", never "draw it smaller".
function screenPx(px, s) {
  if (!(px >= LABEL_MIN_PX)) return null;
  const ss = s > 0 ? s : 1;
  return px / ss;
}

// Map a value onto a class's screen-px range with sqrt damping, then
// clamp into [min, max]. Guarantees the result is >= LABEL_MIN_PX for
// every range declared above.
function rampPx(range, value, denom) {
  const t = denom > 0 ? Math.sqrt(Math.max(0, value) / denom) : 0;
  const px = range.min + t * (range.max - range.min);
  return Math.max(range.min, Math.min(range.max, px));
}


// ── Async-import helpers ────────────────────────────────────────────
function loadD3() {
  if (_d3Promise) return _d3Promise;
  _d3Promise = import('https://esm.sh/d3@7');
  return _d3Promise;
}
function loadDelaunay() {
  if (_delaunayPromise) return _delaunayPromise;
  _delaunayPromise = import('https://esm.sh/d3-delaunay@6');
  return _delaunayPromise;
}
function loadPolygonClipping() {
  if (_polygonClippingPromise) return _polygonClippingPromise;
  _polygonClippingPromise = import('https://esm.sh/polygon-clipping@0.15.7');
  return _polygonClippingPromise;
}


// ── Data fetch ──────────────────────────────────────────────────────
async function fetchData() {
  await whenReady();
  const conn = getConn();
  if (!conn) throw new Error('DuckDB connection not ready');

  const queries = {
    // ACTIVE areas only — collapsed_into IS NULL means this area is its
    // own polygon. Collapsed areas are absorbed into their parent in the
    // facility/person primary tables already.
    //
    // `weight` is the DATA-AND-PEOPLE weight that sizes the region, NOT
    // n_facilities. n_facilities is still selected as raw_facilities so
    // the TOC and tooltips can state the catalogue count alongside the
    // metric that actually drives area. n_protected is reported so the
    // 3,309 excluded protected areas are visible in the UI instead of
    // silently dropped.
    //
    // Coefficients are interpolated from the W_* constants above, so the
    // metric is edited in one place.
    areas: `
      WITH org AS (
        SELECT g.primary_area_id AS area_id,
               f.facility_id,
               (f.facility_type LIKE 'protected-area%') AS is_protected
        FROM   facilities f
        JOIN   facility_primary_groups g ON g.facility_id = f.facility_id
        WHERE  g.primary_area_id IS NOT NULL
      ),
      ds AS (
        SELECT facility_id, COUNT(DISTINCT dataset_id) AS n_datasets
        FROM   dataset_facilities
        GROUP  BY facility_id
      ),
      reg_site AS (
        SELECT facility_id, COUNT(DISTINCT canonical_id) AS n_reg
        FROM   registry_facilities
        GROUP  BY facility_id
      ),
      dir_people AS (
        SELECT facility_id, COUNT(DISTINCT person_id) AS n_dir
        FROM   facility_personnel
        GROUP  BY facility_id
      ),
      per_area AS (
        SELECT o.area_id,
               COUNT(*) FILTER (WHERE NOT o.is_protected)      AS n_org,
               COUNT(*) FILTER (WHERE o.is_protected)          AS n_protected,
               COALESCE(SUM(ds.n_datasets), 0)                 AS n_datasets,
               COUNT(DISTINCT CASE WHEN ds.n_datasets > 0
                                   THEN o.facility_id END)     AS n_data_providers,
               COALESCE(SUM(reg_site.n_reg), 0)                AS n_reg_sited,
               COALESCE(SUM(dir_people.n_dir), 0)              AS n_directory
        FROM   org o
        LEFT   JOIN ds          ON ds.facility_id          = o.facility_id
        LEFT   JOIN reg_site    ON reg_site.facility_id    = o.facility_id
        LEFT   JOIN dir_people  ON dir_people.facility_id  = o.facility_id
        GROUP  BY o.area_id
      ),
      -- Registry researchers with NO site link, placed by dominant domain.
      -- They are people the region genuinely carries, so they count toward
      -- its weight even though they have no physical position.
      reg_domain AS (
        SELECT g.primary_area_id AS area_id, COUNT(*) AS n_reg_domain
        FROM   person_registry pr
        JOIN   person_primary_groups g ON g.person_id = pr.person_id
        WHERE  g.primary_area_id IS NOT NULL
          AND  pr.canonical_id NOT IN (SELECT canonical_id FROM registry_facilities)
        GROUP  BY g.primary_area_id
      )
      SELECT a.area_id                        AS id,
             a.label                          AS name,
             a.n_facilities                   AS raw_facilities,
             -- Every numeric column is cast to a plain SQL type that
             -- duckdb-wasm hands back as a JS number. This is load-bearing,
             -- not tidiness: multiplying an integer count by a JS-side
             -- decimal literal yields DECIMAL(38,2), and duckdb-wasm returns
             -- DECIMAL as an Arrow structured value, NOT a number. Number()
             -- on it is NaN, "NaN || 0" is 0, so "weight > 0" was false for
             -- EVERY area, data.areas came back empty, allNodes was empty,
             -- and the Voronoi bounds went NaN — "invalid bounds", blank map.
             -- HUGEINT (from SUM over BIGINT) has the same problem.
             -- duckdb-python returns a plain float for both, so this is
             -- invisible to any check that does not run in the browser.
             CAST(COALESCE(p.n_org, 0) AS INTEGER)            AS n_org,
             CAST(COALESCE(p.n_protected, 0) AS INTEGER)      AS n_protected,
             CAST(COALESCE(p.n_datasets, 0) AS INTEGER)       AS n_datasets,
             CAST(COALESCE(p.n_data_providers, 0) AS INTEGER) AS n_data_providers,
             CAST(COALESCE(p.n_reg_sited, 0)
                  + COALESCE(p.n_directory, 0)
                  + COALESCE(d.n_reg_domain, 0) AS INTEGER)   AS n_people,
             CAST(
               COALESCE(p.n_org, 0)
               + ${W_DATA_PROVIDER} * COALESCE(p.n_data_providers, 0)
               + ${W_DATASET}       * COALESCE(p.n_datasets, 0)
               + ${W_PERSON}        * (COALESCE(p.n_reg_sited, 0)
                                       + COALESCE(p.n_directory, 0)
                                       + COALESCE(d.n_reg_domain, 0))
               AS DOUBLE)                                     AS weight
      FROM   research_areas_active a
      LEFT   JOIN per_area   p ON p.area_id = a.area_id
      LEFT   JOIN reg_domain d ON d.area_id = a.area_id
      WHERE  a.collapsed_into IS NULL
      ORDER  BY a.area_id`,

    // One row per ORGANISATION with its primary area + display fields.
    // Protected areas are excluded here: they hold no datasets and no
    // researchers, and 3,309 of them would bury the 200 organisations that
    // do. They are still catalogued, still queryable in the SQL console,
    // and still drawn by the opt-in protected-areas layer (see the
    // protected_areas query below).
    facilities: `
      SELECT f.facility_id AS id,
             f.canonical_name AS name,
             f.acronym,
             f.country,
             f.facility_type AS f_type,
             f.url,
             g.primary_area_id AS area_id
      FROM   facilities f
      JOIN   facility_primary_groups g ON g.facility_id = f.facility_id
      WHERE  g.primary_area_id IS NOT NULL
        AND  f.facility_type NOT LIKE 'protected-area%'`,

    // Protected-area AGGREGATE, one row per region. Off-by-default layer.
    // Deliberately an aggregate, not 3,309 rows: the layer exists to say
    // "this region also contains N protected areas of these kinds", which
    // is legitimate coastal context, and a per-site rendering would both
    // cost a 3,309-row payload and re-create the visual problem this
    // change fixes.
    protected_areas: `
      SELECT g.primary_area_id AS area_id,
             COUNT(*)          AS n_protected,
             COUNT(DISTINCT f.facility_type) AS n_kinds,
             COUNT(*) FILTER (WHERE f.facility_type = 'protected-area-state')   AS n_state,
             COUNT(*) FILTER (WHERE f.facility_type = 'protected-area-federal') AS n_federal,
             COUNT(*) FILTER (WHERE f.facility_type = 'protected-area-private') AS n_private
      FROM   facilities f
      JOIN   facility_primary_groups g ON g.facility_id = f.facility_id
      WHERE  g.primary_area_id IS NOT NULL
        AND  f.facility_type LIKE 'protected-area%'
      GROUP  BY g.primary_area_id`,

    // Datasets produced per organisation — drives the "data provider"
    // ring on a site and its tooltip line.
    fac_datasets: `
      SELECT facility_id,
             COUNT(DISTINCT dataset_id) AS n_datasets
      FROM   dataset_facilities
      GROUP  BY facility_id`,

    // One row per person with primary area + their importance metrics.
    // Importance combines:
    //   - n_pubs      : SUM(n_publications) across all areas the person
    //                   has work in (from person_area_metrics)
    //   - n_coauth    : SUM(n_co_authors)  across all areas
    //   - facility_funding_usd
    //                 : SUM(facility_area_funding.total_usd_nominal)
    //                   across every facility the person works at —
    //                   their "associated funding base" (a person at
    //                   WHOI gets WHOI's $1.5B, a person at a small
    //                   NEP gets ~$5M).
    // Used downstream for node-radius scaling so prolific +
    // well-funded researchers are visually larger.
    people: `
      WITH per_pa AS (
        SELECT person_id,
               CAST(SUM(n_publications) AS DOUBLE) AS n_pubs,
               CAST(SUM(n_co_authors)   AS DOUBLE) AS n_coauth,
               SUM(total_citations)    AS total_citations
        FROM person_area_metrics
        GROUP BY person_id
      ),
      per_fund AS (
        SELECT fp.person_id,
               SUM(faf.total_usd_nominal) AS facility_funding_usd
        FROM facility_personnel fp
        JOIN facility_area_funding faf ON faf.facility_id = fp.facility_id
        GROUP BY fp.person_id
      )
      SELECT p.person_id AS id,
             p.name,
             p.orcid,
             p.openalex_id,
             p.homepage_url,
             g.primary_area_id        AS area_id,
             COALESCE(pa.n_pubs, 0)   AS n_pubs,
             COALESCE(pa.n_coauth, 0) AS n_coauth,
             COALESCE(pa.total_citations, 0) AS total_citations,
             COALESCE(pf.facility_funding_usd, 0) AS facility_funding_usd
      FROM   people p
      JOIN   person_primary_groups g ON g.person_id = p.person_id
      LEFT  JOIN per_pa  pa ON pa.person_id = p.person_id
      LEFT  JOIN per_fund pf ON pf.person_id = p.person_id
      WHERE  g.primary_area_id IS NOT NULL`,

    // Facility ↔ person via facility_personnel (intra+inter polygon).
    fac_pers: `
      SELECT facility_id AS source, person_id AS target,
             COUNT(*) AS w
      FROM   facility_personnel
      GROUP  BY facility_id, person_id`,

    // Per-person role/title/institution lookup for tooltips. A person
    // can hold roles at multiple facilities — we list-aggregate so the
    // tooltip can show each affiliation. Prefer key-personnel rows so
    // 'Director' / 'Principal Investigator' surfaces above 'Staff'.
    person_affiliations: `
      SELECT fp.person_id,
             list(struct_pack(
               role        := fp.role,
               title       := fp.title,
               facility_id := f.facility_id,
               facility    := COALESCE(f.acronym || ' — ' || f.canonical_name,
                                       f.canonical_name),
               is_key      := fp.is_key_personnel
             ) ORDER BY fp.is_key_personnel DESC, fp.role) AS roles
      FROM facility_personnel fp
      JOIN facilities f ON f.facility_id = fp.facility_id
      GROUP BY fp.person_id`,

    // Person → primary facility for the hierarchy layout. A person
    // might work at >1 facility; we pick their first key-personnel
    // row, falling back to alphabetic role if no key-flag set.
    person_primary_facility: `
      WITH ranked AS (
        SELECT person_id, facility_id,
               ROW_NUMBER() OVER (
                 PARTITION BY person_id
                 ORDER BY is_key_personnel DESC, role, facility_id
               ) AS rk
        FROM facility_personnel
      )
      SELECT person_id, facility_id
      FROM ranked WHERE rk = 1`,

    // Person ↔ person via co-authorship.
    coauthors: `
      SELECT person_a_id AS source, person_b_id AS target,
             co_pub_count AS w
      FROM   collaborations
      WHERE  co_pub_count >= 2`,
  };

  const out = {};
  for (const [k, sql] of Object.entries(queries)) {
    const r = await conn.query(sql);
    // unwrapRow converts Arrow Vector LIST<STRUCT> columns (e.g. the
    // `roles` array on person_affiliations) to plain JS arrays so the
    // downstream Array.isArray / .map / Map(...) usage works. Without
    // this, every person's tooltip showed an empty roles list because
    // Arrow Vectors fail Array.isArray.
    out[k] = r.toArray().map((row) => unwrapRow(row.toJSON()));
  }
  // Coerce BigInt counts to Number.
  for (const a of out.areas) {
    a.weight          = Number(a.weight) || 0;
    a.raw_facilities  = Number(a.raw_facilities) || 0;
    a.n_org           = Number(a.n_org) || 0;
    a.n_protected     = Number(a.n_protected) || 0;
    a.n_datasets      = Number(a.n_datasets) || 0;
    a.n_data_providers = Number(a.n_data_providers) || 0;
    a.n_people        = Number(a.n_people) || 0;
  }
  for (const e of out.fac_pers) e.w = Number(e.w) || 1;
  for (const e of out.coauthors) e.w = Number(e.w) || 1;
  for (const r of out.protected_areas) {
    r.n_protected = Number(r.n_protected) || 0;
    r.n_kinds     = Number(r.n_kinds) || 0;
    r.n_state     = Number(r.n_state) || 0;
    r.n_federal   = Number(r.n_federal) || 0;
    r.n_private   = Number(r.n_private) || 0;
  }

  // A region with weight 0 carries no organisation, no dataset and nobody,
  // so it has nothing to draw and no basis for an area. Dropping it here
  // (rather than giving it a min-side square) is what stops 15 empty
  // vocabulary terms from occupying map real estate. They remain in the
  // vocabulary and in the SQL console; the status line reports the count.
  const allAreas = out.areas;
  out.areas = allAreas.filter((a) => a.weight > 0);
  out.n_areas_empty = allAreas.length - out.areas.length;

  // Datasets produced, per organisation.
  const dsBy = new Map(out.fac_datasets.map(
    (r) => [r.facility_id, Number(r.n_datasets) || 0]));
  for (const f of out.facilities) f.n_datasets = dsBy.get(f.id) || 0;

  // Protected-area aggregate, keyed by region, for the opt-in layer.
  out.protectedByArea = new Map(out.protected_areas.map((r) => [r.area_id, r]));

  // Build lookup tables for hierarchy + tooltip enrichment.
  const affilsBy = new Map(out.person_affiliations.map(
    (r) => [r.person_id, r.roles || []]));
  const primaryFacBy = new Map(out.person_primary_facility.map(
    (r) => [r.person_id, r.facility_id]));
  for (const p of out.people) {
    p.affiliations = affilsBy.get(p.id) || [];
    p.primary_facility_id = primaryFacBy.get(p.id) || null;
  }
  return out;
}


// ── Registry data fetch (lazy — only on first layer switch-on) ───────
//
// PAYLOAD ACCOUNTING, measured off the shipped parquet footers rather
// than guessed. person_registry.parquet is 1.00 MB, of which this view
// reads 14 of 34 columns = 707 KB of column chunks; the 305 KB it does
// NOT touch is source_url, tier_score, affiliation_ror,
// two_yr_mean_citedness, i10_index, first_pub_year and the name parts.
// person_identity_source.parquet is 324 KB but only canonical_id +
// field are read = 107 KB, for the harvest-coverage flag.
// registry_collaborations is 41 KB and registry_facilities 5.7 KB, both
// read whole. Total ≈ 861 KB.
//
// That is why the layer is behind a switch instead of loading with the
// map: it roughly doubles the view's cold-start bytes, and a session
// that only wants the research-area cartogram should not pay for it.
// duckdb-wasm requests parquet over HTTP range reads, so the unread
// columns genuinely stay on the server — this is a column-projection
// saving, not a hopeful one.
//
// ROW COUNTS the browser actually receives (all verified against the
// rebuilt DuckDB, see the summary):
//   roster       263 rows — every researcher↔site link that resolves
//   collabs      771 rows — collaborator rows for the 41 linked
//                           researchers that have any edge at all
//   siteEdges     18 rows — after aggregation, pre-threshold
// Nothing here is 10,000 rows wide, because registry_facilities only
// links 263 of the shipped 10,000 core-tier researchers to a catalogued
// site. The rest have no map position to occupy.
async function fetchRegistry() {
  await whenReady();
  const conn = getConn();
  if (!conn) throw new Error('DuckDB connection not ready');

  const queries = {
    // Researcher ↔ site roster. `graph_harvested` distinguishes
    // "no collaborators found" from "this identity was never part of
    // the co-authorship harvest", which is the difference between a
    // real zero and a not-yet-computed one. The harvested cohort is
    // the 618 pre-harvest identities recorded in
    // person_identity_source under field 'canonical_id' / 'hydrate'.
    // affiliation is deliberately NOT selected here — it is the
    // ROR-matched institution, i.e. the site row we already have.
    roster: `
      WITH harvested AS (
        SELECT DISTINCT canonical_id
        FROM   person_identity_source
        WHERE  field IN ('canonical_id', 'hydrate')
      )
      SELECT rf.facility_id,
             p.canonical_id,
             p.display_name,
             p.orcid,
             p.openalex_id,
             p.homepage_url,
             p.affiliation_country,
             p.person_id,
             p.tier_rank,
             COALESCE(p.works_count, 0)         AS works_count,
             COALESCE(p.cited_by_count, 0)      AS cited_by_count,
             COALESCE(p.h_index, 0)             AS h_index,
             COALESCE(p.coastal_works_count, 0) AS coastal_works_count,
             p.coastal_share,
             (h.canonical_id IS NOT NULL)       AS graph_harvested
      FROM   registry_facilities rf
      JOIN   person_registry p ON p.canonical_id = rf.canonical_id
      LEFT   JOIN harvested h  ON h.canonical_id = rf.canonical_id
      ORDER  BY rf.facility_id, p.tier_rank`,

    // Collaborator rows, densified to one row per (linked researcher,
    // collaborator) direction so the client never has to test both
    // orientations of the a<b storage convention.
    // other_facility_id is NULL when the collaborator has no site link
    // — they exist in the registry but have no position on this map.
    collabs: `
      WITH placed AS (
        SELECT canonical_id, facility_id FROM registry_facilities
      ),
      ends AS (
        SELECT canonical_id_a AS cid, canonical_id_b AS other,
               co_pub_count AS w, first_year, last_year
        FROM   registry_collaborations
        UNION ALL
        SELECT canonical_id_b AS cid, canonical_id_a AS other,
               co_pub_count AS w, first_year, last_year
        FROM   registry_collaborations
      )
      SELECT e.cid,
             e.other,
             e.w              AS co_pubs,
             e.first_year,
             e.last_year,
             p2.display_name  AS other_name,
             p2.affiliation   AS other_affiliation,
             pl2.facility_id  AS other_facility_id
      FROM   ends e
      JOIN   placed pl ON pl.canonical_id = e.cid
      JOIN   person_registry p2 ON p2.canonical_id = e.other
      LEFT   JOIN placed pl2 ON pl2.canonical_id = e.other
      ORDER  BY e.cid, e.w DESC`,

    // Site ↔ site co-publication aggregate: sum co_pub_count over
    // every researcher pair whose two endpoints sit at DIFFERENT
    // catalogued sites. This is the map-scale edge — 18 rows, versus
    // 5,300 researcher-level edges, so it can be drawn unconditionally.
    site_edges: `
      WITH placed AS (
        SELECT canonical_id, facility_id FROM registry_facilities
      ),
      pair AS (
        SELECT a.facility_id AS fa, b.facility_id AS fb,
               e.co_pub_count AS w,
               e.canonical_id_a AS ca, e.canonical_id_b AS cb
        FROM   registry_collaborations e
        JOIN   placed a ON a.canonical_id = e.canonical_id_a
        JOIN   placed b ON b.canonical_id = e.canonical_id_b
      )
      SELECT CASE WHEN fa < fb THEN fa ELSE fb END AS source,
             CASE WHEN fa < fb THEN fb ELSE fa END AS target,
             CAST(SUM(w) AS DOUBLE)                AS co_pubs,
             COUNT(*)                              AS n_pairs
      FROM   pair
      WHERE  fa <> fb
      GROUP  BY 1, 2
      ORDER  BY co_pubs DESC`,

    // ── PLACEMENT ROUTE 2: dominant science domain ───────────────────
    // A registry researcher with no ROR match to a catalogued site has NO
    // physical position. Where person_primary_groups resolves a dominant
    // research area for them, they are placed in that REGION instead —
    // which is a claim about their science, not about where they work.
    //
    // Counts on the shipped site parquet: 10,000 core-tier rows, 263 with
    // a site link, 164 domain-placed here, 9,573 with neither. The three
    // numbers are surfaced in the status line and the scope note, because
    // "placed in a region" and "located at a site" are different
    // statements and the map must not conflate them.
    //
    // Aggregated per region deliberately: 164 individually-drawn markers
    // scattered inside region polygons would be indistinguishable from
    // sited researchers, which is exactly the confusion to avoid. One
    // dashed cohort badge per region, expandable into a list, keeps the
    // distinction visible.
    domain_placed: `
      SELECT g.primary_area_id AS area_id,
             COUNT(*)          AS n_people,
             list(struct_pack(
               canonical_id  := pr.canonical_id,
               display_name  := pr.display_name,
               orcid         := pr.orcid,
               openalex_id   := pr.openalex_id,
               homepage_url  := pr.homepage_url,
               person_id     := pr.person_id,
               affiliation   := pr.affiliation,
               h_index       := COALESCE(pr.h_index, 0),
               coastal_works := COALESCE(pr.coastal_works_count, 0)
             ) ORDER BY pr.tier_rank) AS people
      FROM   person_registry pr
      JOIN   person_primary_groups g ON g.person_id = pr.person_id
      WHERE  g.primary_area_id IS NOT NULL
        AND  pr.canonical_id NOT IN (SELECT canonical_id FROM registry_facilities)
      GROUP  BY g.primary_area_id`,

    // Registry-wide placement accounting, so the UI states coverage from
    // the data rather than from a hardcoded number.
    placement_totals: `
      WITH sited AS (SELECT DISTINCT canonical_id FROM registry_facilities)
      SELECT COUNT(*) AS n_registry,
             COUNT(*) FILTER (WHERE s.canonical_id IS NOT NULL) AS n_sited,
             COUNT(*) FILTER (WHERE s.canonical_id IS NULL
                                AND g.primary_area_id IS NOT NULL) AS n_domain,
             COUNT(*) FILTER (WHERE s.canonical_id IS NULL
                                AND g.primary_area_id IS NULL)     AS n_unplaced
      FROM   person_registry pr
      LEFT   JOIN sited s ON s.canonical_id = pr.canonical_id
      LEFT   JOIN person_primary_groups g ON g.person_id = pr.person_id`,

    // Intra-site co-publication, for the site tooltip ("N of the
    // researchers here publish with each other").
    site_internal: `
      WITH placed AS (
        SELECT canonical_id, facility_id FROM registry_facilities
      )
      SELECT a.facility_id       AS facility_id,
             CAST(SUM(e.co_pub_count) AS DOUBLE) AS co_pubs,
             COUNT(*)            AS n_pairs
      FROM   registry_collaborations e
      JOIN   placed a ON a.canonical_id = e.canonical_id_a
      JOIN   placed b ON b.canonical_id = e.canonical_id_b
      WHERE  a.facility_id = b.facility_id
      GROUP  BY 1`,
  };

  const out = {};
  for (const [k, sql] of Object.entries(queries)) {
    const r = await conn.query(sql);
    out[k] = r.toArray().map((row) => unwrapRow(row.toJSON()));
  }

  // ── Index into the shapes the renderer wants ─────────────────────
  const byFacility = new Map();
  for (const r of out.roster) {
    r.works_count         = Number(r.works_count) || 0;
    r.cited_by_count      = Number(r.cited_by_count) || 0;
    r.h_index             = Number(r.h_index) || 0;
    r.coastal_works_count = Number(r.coastal_works_count) || 0;
    r.tier_rank           = Number(r.tier_rank) || 0;
    r.graph_harvested     = !!r.graph_harvested;
    if (!byFacility.has(r.facility_id)) byFacility.set(r.facility_id, []);
    byFacility.get(r.facility_id).push(r);
  }
  // tier_rank ASC = better rank, so this is "most prominent first".
  for (const list of byFacility.values()) {
    list.sort((a, b) => a.tier_rank - b.tier_rank);
  }

  const collabsBy = new Map();
  for (const c of out.collabs) {
    c.co_pubs = Number(c.co_pubs) || 0;
    if (!collabsBy.has(c.cid)) collabsBy.set(c.cid, []);
    collabsBy.get(c.cid).push(c);
  }

  const siteEdges = out.site_edges
    .map((e) => ({ ...e, co_pubs: Number(e.co_pubs) || 0,
                   n_pairs: Number(e.n_pairs) || 0 }))
    .filter((e) => e.co_pubs >= SITE_LINK_MIN);

  const internalBy = new Map(out.site_internal.map((r) => [
    r.facility_id,
    { co_pubs: Number(r.co_pubs) || 0, n_pairs: Number(r.n_pairs) || 0 },
  ]));

  // Per-site rollup for the tooltip + the panel header.
  const siteSummary = new Map();
  for (const [fid, list] of byFacility.entries()) {
    let harvested = 0, coastalVolume = 0, maxH = 0, withEdges = 0;
    for (const r of list) {
      if (r.graph_harvested) harvested++;
      coastalVolume += r.coastal_works_count;
      if (r.h_index > maxH) maxH = r.h_index;
      if ((collabsBy.get(r.canonical_id) || []).length) withEdges++;
    }
    siteSummary.set(fid, {
      n_registry: list.length,
      n_harvested: harvested,
      n_with_edges: withEdges,
      coastal_volume: coastalVolume,
      max_h: maxH,
      internal: internalBy.get(fid) || null,
    });
  }

  // Domain-placed cohorts, keyed by region.
  const domainByArea = new Map();
  for (const r of out.domain_placed) {
    const people = (Array.isArray(r.people) ? r.people : []).map((p) => ({
      ...p,
      h_index: Number(p.h_index) || 0,
      coastal_works: Number(p.coastal_works) || 0,
    }));
    domainByArea.set(r.area_id, {
      area_id: r.area_id,
      n_people: Number(r.n_people) || people.length,
      people,
    });
  }

  const pt = out.placement_totals[0] || {};

  return {
    byFacility, collabsBy, siteEdges, siteSummary, domainByArea,
    totals: {
      n_placed: out.roster.length,
      n_sites: byFacility.size,
      n_site_edges: siteEdges.length,
      n_with_edges: collabsBy.size,
      // Placement accounting straight out of SQL.
      n_registry:  Number(pt.n_registry)  || 0,
      n_sited:     Number(pt.n_sited)     || 0,
      n_domain:    Number(pt.n_domain)    || 0,
      n_unplaced:  Number(pt.n_unplaced)  || 0,
      n_domain_regions: domainByArea.size,
    },
  };
}

// Kick off the registry fetch at most once. Concurrent callers (an
// impatient double-click on the toggle) share the same promise, so we
// never issue the queries twice or leave a half-populated _registry.
function ensureRegistry() {
  if (_registry) return Promise.resolve(_registry);
  if (!_registryPromise) {
    _registryPromise = fetchRegistry()
      .then((r) => { _registry = r; return r; })
      .catch((err) => { _registryPromise = null; throw err; });
  }
  return _registryPromise;
}


// ── Step 1: supergraph layout (squares packed by area weight) ───────
function buildSupergraph(data) {
  const areaIds = new Set(data.areas.map((a) => a.id));
  const facById = new Map(data.facilities.map((f) => [f.id, f]));
  const perById = new Map(data.people.map((p) => [p.id, p]));

  // Cross-area edge weights from facility-person + co-author edges.
  const edgeW = new Map();
  function bump(a, b, w) {
    if (!a || !b || a === b) return;
    if (!areaIds.has(a) || !areaIds.has(b)) return;
    const k = a < b ? `${a}|${b}` : `${b}|${a}`;
    edgeW.set(k, (edgeW.get(k) || 0) + w);
  }
  for (const e of data.fac_pers) {
    const f = facById.get(e.source); const p = perById.get(e.target);
    if (f && p) bump(f.area_id, p.area_id, e.w);
  }
  for (const e of data.coauthors) {
    const a = perById.get(e.source); const b = perById.get(e.target);
    if (a && b) bump(a.area_id, b.area_id, e.w);
  }

  return {
    nodes: data.areas.map((a) => ({
      id: a.id, name: a.name, weight: a.weight,
      // True cartogram: side ∝ sqrt(weight) so AREA ∝ weight.
      // Min side just keeps 1-facility areas visible at all zoom levels.
      side: Math.max(SUPERNODE_MIN, SUPERNODE_SCALE * Math.sqrt(a.weight)),
    })),
    edges: [...edgeW.entries()].map(([k, w]) => {
      const [s, t] = k.split('|');
      return { source: s, target: t, w };
    }),
  };
}

async function layoutSupergraph(d3, sg, w, h) {
  const cx = w / 2, cy = h / 2;
  // Seed positions on a ring proportional to weight so the simulation
  // converges quickly and large groups end up roughly central.
  const sorted = [...sg.nodes].sort((a, b) => b.weight - a.weight);
  // Seed radius must scale with the AREA the squares need, not with the
  // viewport. A fixed 0.36*min(w,h) ring packs them tighter than they can
  // possibly fit once total square area is large, so the separation pass
  // below has to do all the work — and because it resolves on the axis of
  // least overlap, it does so along a single axis and stacks the map into a
  // tall thin column (measured aspect 0.37). Sizing the ring to
  // sqrt(total square area) leaves separation only local work to do.
  // 0.80 measured best on the shipped weights: fill (sum of square areas /
  // bounding-box area) 0.54 at aspect 0.83, against 0.41 / 0.93 with
  // residual overlap for the previous version.
  let _sumSq = 0;
  for (const n of sg.nodes) _sumSq += n.side * n.side;
  const maxR = Math.max(Math.sqrt(_sumSq) * 0.80, Math.min(w, h) * 0.18);
  sorted.forEach((n, i) => {
    const t = i / Math.max(sorted.length - 1, 1);
    const r = t * maxR * 0.85 + 0.05 * maxR;
    const a = i * (2 * Math.PI / Math.max(sorted.length, 6)) + 0.1 * i;
    n.x = cx + r * Math.cos(a);
    n.y = cy + r * Math.sin(a);
  });

  // Square-collision: forceCollide treats each node as a circle of
  // radius r; we set r = side/sqrt(2) + padding/2 so square bounding
  // boxes don't quite touch. Approximation but visually adequate.
  const sim = d3.forceSimulation(sg.nodes)
    .alphaDecay(0.04)
    .force('link', d3.forceLink(sg.edges)
      .id((d) => d.id)
      .distance((d) => 30 + Math.sqrt(d.w) * 8)
      .strength(0.4))
    .force('charge', d3.forceManyBody()
      .strength((d) => -120 - d.weight * 4))
    .force('collide', d3.forceCollide()
      .radius((d) => d.side * 0.71 + SUPER_PADDING)
      .strength(1)
      .iterations(2))
    .force('center', d3.forceCenter(cx, cy).strength(0.05))
    .stop();
  for (let i = 0; i < SUPERGRAPH_TICKS; i++) sim.tick();

  // After force-sim, the cluster of squares may have drifted away
  // from the stage centre. Recenter so the whole layout sits in the
  // middle of the viewport — otherwise the SVG viewBox (computed
  // from node positions later) ends up offset and the map renders
  // partly above the visible area on first paint.
  function recenter() {
    let mnX = Infinity, mnY = Infinity, mxX = -Infinity, mxY = -Infinity;
    for (const n of sg.nodes) {
      const half = n.side * 0.71;
      if (n.x - half < mnX) mnX = n.x - half;
      if (n.y - half < mnY) mnY = n.y - half;
      if (n.x + half > mxX) mxX = n.x + half;
      if (n.y + half > mxY) mxY = n.y + half;
    }
    const ccx = (mnX + mxX) / 2, ccy = (mnY + mxY) / 2;
    const dx = cx - ccx, dy = cy - ccy;
    if (Math.abs(dx) < 0.5 && Math.abs(dy) < 0.5) return;
    for (const n of sg.nodes) { n.x += dx; n.y += dy; }
  }
  recenter();

  // Resolve remaining overlap, then COMPACT.
  //
  // Two defects lived in the previous version of this pass, and both made
  // the map sparser than the cartogram it is meant to be:
  //
  //   1. It measured overlap as centre distance vs (sideA+sideB)/2, i.e. it
  //      treated squares as circles. Two squares offset diagonally read as
  //      "clear" at a distance where their corners still overlap — the pass
  //      terminated leaving real overlaps behind — while squares already
  //      separated on one axis got pushed apart anyway. Squares must be
  //      resolved on the axis of LEAST overlap.
  //   2. It only ever pushed, never pulled, so any dispersal the seed ring
  //      introduced was permanent.
  //
  // separateSquares() fixes (1); compactInward() adds the missing inward
  // pass, walking the outermost square toward the centroid and keeping each
  // step only when it introduces no overlap. Both are deterministic, so the
  // layout remains stable across reloads for a given input.
  const overlapsAny = (nodes, moveIdx, nx, ny) => {
    const m = nodes[moveIdx];
    for (let i = 0; i < nodes.length; i++) {
      if (i === moveIdx) continue;
      const o = nodes[i];
      const half = (m.side + o.side) * 0.5 + SUPER_PADDING * 0.5;
      if (Math.abs(nx - o.x) < half && Math.abs(ny - o.y) < half) return true;
    }
    return false;
  };

  function separateSquares(iters) {
    for (let r = 0; r < iters; r++) {
      let moved = false;
      for (let i = 0; i < sg.nodes.length; i++) {
        for (let j = i + 1; j < sg.nodes.length; j++) {
          const a = sg.nodes[i], b = sg.nodes[j];
          const dx = b.x - a.x, dy = b.y - a.y;
          const half = (a.side + b.side) * 0.5 + SUPER_PADDING;
          const ox = half - Math.abs(dx);
          const oy = half - Math.abs(dy);
          if (ox <= 0 || oy <= 0) continue;      // already clear on an axis
          if (ox <= oy) {
            const s = (dx >= 0 ? 1 : -1) * ox * 0.5;
            a.x -= s; b.x += s;
          } else {
            const s = (dy >= 0 ? 1 : -1) * oy * 0.5;
            a.y -= s; b.y += s;
          }
          moved = true;
        }
      }
      if (!moved) break;
    }
  }

  function compactInward(steps) {
    for (let s = 0; s < steps; s++) {
      let ccx = 0, ccy = 0;
      for (const n of sg.nodes) { ccx += n.x; ccy += n.y; }
      ccx /= sg.nodes.length; ccy /= sg.nodes.length;
      const order = [...sg.nodes.keys()].sort((i, j) => {
        const a = sg.nodes[i], b = sg.nodes[j];
        return Math.hypot(b.x - ccx, b.y - ccy) - Math.hypot(a.x - ccx, a.y - ccy);
      });
      let any = false;
      for (const i of order) {
        const n = sg.nodes[i];
        const vx = ccx - n.x, vy = ccy - n.y;
        const d = Math.hypot(vx, vy);
        if (d < 1e-9) continue;
        const step = Math.max(d * 0.05, 0.5);
        const nx = n.x + (vx / d) * step, ny = n.y + (vy / d) * step;
        if (!overlapsAny(sg.nodes, i, nx, ny)) { n.x = nx; n.y = ny; any = true; }
      }
      if (!any) break;
    }
  }

  separateSquares(400);
  compactInward(300);
  separateSquares(200);   // guarantee: no overlap survives compaction

  // One more recenter after the relax pass.
  recenter();

  return new Map(sg.nodes.map((n) => [n.id, n]));
}


// ── Step 2: per-group subgraph layout, scale to fit ─────────────────
function membersOfArea(areaId, data) {
  const facs = data.facilities.filter((f) => f.area_id === areaId)
    .map((f) => ({ id: f.id, name: f.name, kind: 'facility',
                   acronym: f.acronym, country: f.country, url: f.url,
                   f_type: f.f_type, area_id: areaId,
                   n_datasets: f.n_datasets || 0 }));
  const peo = data.people.filter((p) => p.area_id === areaId)
    .map((p) => {
      // Composite "importance" weight per the user's request:
      // prioritize funding + collaborators, then publications.
      // Coefficients chosen so a well-funded heavy collaborator (~$50M
      // facility, 30 co-authors, 50 pubs) lands around weight ≈ 18,
      // while a junior researcher (no funding, 0 co-authors, 5 pubs)
      // lands at ≈ 2.2 — both visible, but very different sizes.
      const fundM = (p.facility_funding_usd || 0) / 1e6;
      const w = 0.6 * Math.sqrt(p.n_pubs || 0)
              + 1.2 * Math.sqrt(p.n_coauth || 0)
              + 0.7 * Math.sqrt(fundM);
      return {
        id: p.id, name: p.name, kind: 'person',
        orcid: p.orcid, openalex_id: p.openalex_id,
        homepage_url: p.homepage_url, area_id: areaId,
        n_pubs: p.n_pubs, n_coauth: p.n_coauth,
        total_citations: p.total_citations,
        facility_funding_usd: p.facility_funding_usd,
        importance: w,
      };
    });
  return [...facs, ...peo];
}

function intraEdgesOfArea(members, data) {
  const ids = new Set(members.map((m) => m.id));
  const edges = [];
  for (const e of data.fac_pers) {
    if (ids.has(e.source) && ids.has(e.target)) {
      edges.push({ source: e.source, target: e.target, w: e.w });
    }
  }
  for (const e of data.coauthors) {
    if (ids.has(e.source) && ids.has(e.target)) {
      edges.push({ source: e.source, target: e.target, w: e.w });
    }
  }
  return edges;
}

// Decoration anchors per area: invisible nodes that own Voronoi cells
// inside the area's square, ensuring the resulting merged polygon
// closely matches the square's area (cartogram-correct sizing) instead
// of letting cells leak into sparse neighbours. Tagged with the area
// id so polygon-clipping rolls them up; tagged kind='__decor' so the
// renderer skips them.
function decorationAnchors(square, areaId) {
  const cx = square.x, cy = square.y;
  const half = square.side / 2;
  const out = [];
  const N = DECOR_GRID;
  for (let i = 0; i < N; i++) {
    for (let j = 0; j < N; j++) {
      // Cell-center (i+0.5)/N spans 0..1; map to ±half.
      const fx = (i + 0.5) / N - 0.5;
      const fy = (j + 0.5) / N - 0.5;
      // Random jitter so the resulting cell boundaries are irregular,
      // not a visible grid pattern. PRNG seeded by (area,i,j) so the
      // layout is reproducible across re-renders.
      const seed = (areaId.charCodeAt(0) || 0) * 31 + i * 7 + j * 13;
      const jx = (((seed * 9301 + 49297) % 233280) / 233280 - 0.5) * 2;
      const jy = (((seed * 4391 + 12347) % 233280) / 233280 - 0.5) * 2;
      const x = cx + (fx + jx * DECOR_JITTER) * 2 * half;
      const y = cy + (fy + jy * DECOR_JITTER) * 2 * half;
      out.push({
        id: `__decor_${areaId}_${i}_${j}`,
        kind: '__decor',
        area_id: areaId,
        x, y,
      });
    }
  }
  return out;
}

// Pack facility sub-circles inside an area's square, then scatter
// each facility's people inside the corresponding circle. Returns a
// flat list of (facility nodes + person nodes + decoration anchors)
// that the Voronoi step consumes. A side-effect map tracks each
// facility's circle position + radius so the renderer can draw the
// translucent sub-polygon ring per institution.
async function layoutAndFit(d3, members, edges, square, facCircles) {
  // Even an empty area gets decoration anchors so its polygon
  // still appears at the right cartogram size.
  if (!members.length) {
    return decorationAnchors(square, square.id);
  }

  const cx = square.x, cy = square.y;
  const facs = members.filter((m) => m.kind === 'facility');
  const peo  = members.filter((m) => m.kind === 'person');

  // ── 1. Pack facility sub-circles inside the square ──────────────
  // Each facility's "weight" = 1 (itself) + n_personnel-at-facility,
  // so an institution with many researchers gets a larger sub-circle.
  // Radius ∝ sqrt(weight) to make AREA ∝ weight.
  const peopleAt = new Map();
  for (const p of peo) {
    if (!p.primary_facility_id) continue;
    peopleAt.set(p.primary_facility_id,
      (peopleAt.get(p.primary_facility_id) || 0) + 1);
  }

  // If facility-list is empty (rare; can happen if all primary_area
  // facilities have no personnel listed), invent a single phantom
  // circle covering the whole square so people still get placed.
  const bubbles = facs.length
    ? facs.map((f) => ({
        id: f.id, name: f.name, acronym: f.acronym, country: f.country,
        f_type: f.f_type, url: f.url, area_id: f.area_id,
        n_datasets: f.n_datasets || 0,
        // Sub-circle weight now includes datasets produced, so a small
        // institution that ships data is not drawn smaller than a large
        // one that ships none.
        weight: 1 + (peopleAt.get(f.id) || 0)
                  + W_DATASET * (f.n_datasets || 0),
        kind: 'facility',
      }))
    : [{ id: `__phantom_${square.id}`, name: '', kind: 'facility',
         area_id: square.id, weight: 1 }];

  const totalWeight = bubbles.reduce((s, b) => s + Math.sqrt(b.weight), 0);
  const innerR = (square.side / 2) * 0.84;  // 16% inset from square edge
  // Per-bubble radius. Min 5 px so single-person facilities are visible;
  // max ~innerR so a giant institution can't dwarf the whole area.
  const RFAC = 0.62 * innerR / Math.max(totalWeight, 1);
  for (const b of bubbles) {
    b.r = Math.min(innerR * 0.65, Math.max(5, RFAC * Math.sqrt(b.weight) * 1.5));
    // Seed at a random point inside the inner circle.
    const a = Math.random() * 2 * Math.PI;
    const r = Math.random() * (innerR - b.r);
    b.x = cx + r * Math.cos(a);
    b.y = cy + r * Math.sin(a);
  }
  const bubSim = d3.forceSimulation(bubbles)
    .alphaDecay(0.05)
    .force('center', d3.forceCenter(cx, cy).strength(0.08))
    .force('charge', d3.forceManyBody().strength(-12))
    .force('collide',
      d3.forceCollide().radius((d) => d.r + 1.6).strength(1).iterations(2))
    .stop();
  for (let i = 0; i < 220; i++) bubSim.tick();

  // Clamp every bubble back inside the inner circle (the simulation
  // doesn't enforce containment); push toward center if it's drifted
  // outside. A few iterations because pushing one bubble can shove
  // its neighbour out.
  for (let pass = 0; pass < 30; pass++) {
    let moved = false;
    for (const b of bubbles) {
      const dx = b.x - cx, dy = b.y - cy;
      const d = Math.hypot(dx, dy) || 1e-6;
      const overshoot = d + b.r - innerR;
      if (overshoot > 0) {
        const k = (innerR - b.r) / d;
        b.x = cx + dx * k;
        b.y = cy + dy * k;
        moved = true;
      }
    }
    // Also re-resolve overlap via simple push.
    for (let i = 0; i < bubbles.length; i++) {
      for (let j = i + 1; j < bubbles.length; j++) {
        const a = bubbles[i], b = bubbles[j];
        const dx = b.x - a.x, dy = b.y - a.y;
        const minD = a.r + b.r + 1.6;
        const d = Math.hypot(dx, dy) || 1e-6;
        if (d < minD) {
          const push = (minD - d) / 2;
          const nx = dx / d, ny = dy / d;
          a.x -= nx * push; a.y -= ny * push;
          b.x += nx * push; b.y += ny * push;
          moved = true;
        }
      }
    }
    if (!moved) break;
  }

  // Record the bubble layout in the side-effect map for the renderer.
  for (const b of bubbles) {
    facCircles.set(b.id, { x: b.x, y: b.y, r: b.r,
                            area_id: square.id,
                            name: b.name, acronym: b.acronym,
                            country: b.country, f_type: b.f_type,
                            url: b.url,
                            n_datasets: b.n_datasets || 0,
                            n_people: peopleAt.get(b.id) || 0 });
  }

  // ── 2. Place each facility node at its bubble center; people inside ──
  for (const f of facs) {
    const b = facCircles.get(f.id);
    if (b) { f.x = b.x; f.y = b.y; }
  }
  // People scattered inside their primary facility's circle. Use a
  // golden-angle spiral so positions are deterministic. We extend the
  // spiral out toward the bubble's perimeter (88% radius) so people
  // don't crowd the centre, and the spacing scales with the bubble's
  // actual size so dense institutions get equally-spaced names.
  const PHI = Math.PI * (3 - Math.sqrt(5));   // golden angle
  const peoPerFac = new Map();
  for (const p of peo) {
    const fid = p.primary_facility_id;
    const b = (fid && facCircles.get(fid)) || facCircles.get(bubbles[0].id);
    if (!b) continue;
    const k = peoPerFac.get(b) || 0;
    peoPerFac.set(b, k + 1);
    const n = (peopleAt.get(fid) || 1);
    // Deterministic spiral up to bubble's inner 88%.
    const t = (k + 0.5) / Math.max(n, 1);
    const r = b.r * 0.88 * Math.sqrt(t);
    const a = (k + 1) * PHI;
    p.x = b.x + r * Math.cos(a);
    p.y = b.y + r * Math.sin(a);
  }

  // ── 3. Append decoration anchors so Voronoi tiles the area square ──
  // Anchors live in the gap between facility bubbles + the square
  // perimeter. They share the area_id so the outer polygon stretches
  // to the full square; they do NOT carry a facility_id, so they
  // don't end up inside any facility's sub-polygon should we ever
  // compute one.
  return [...facs, ...peo, ...decorationAnchors(square, square.id)];
}


// ── Step 3: Voronoi-merged country-like polygons ────────────────────
//
// CARTOGRAM ENFORCEMENT: after merging Voronoi cells per area, we
// INTERSECT each merged polygon with its supernode-square (slightly
// inflated). This guarantees polygon area ≤ square area, so dense
// areas can never be visually smaller than sparse ones — the
// cartogram math the paper assumes is now actually enforced.
//
// Without this clip, Voronoi cells along the periphery extend
// outward toward the anchor ring, ballooning the polygons of outer
// (sparse) areas. We brought the anchor ring much closer in too
// (1.05 × bbox half-radius instead of 1.2 ×) so even uncliped
// versions stay tighter, but the intersection is the real fix.
async function computePolygons(d3delaunay, polygonClipping, allNodes,
                                squares, w, h) {
  const PC = polygonClipping.default || polygonClipping;

  // Bounding box of all node positions, with modest padding.
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const n of allNodes) {
    if (n.x < minX) minX = n.x;
    if (n.y < minY) minY = n.y;
    if (n.x > maxX) maxX = n.x;
    if (n.y > maxY) maxY = n.y;
  }
  const padX = (maxX - minX) * PERIMETER_PAD + 30;
  const padY = (maxY - minY) * PERIMETER_PAD + 30;
  const bbMinX = minX - padX, bbMinY = minY - padY;
  const bbMaxX = maxX + padX, bbMaxY = maxY + padY;
  const bbW = bbMaxX - bbMinX, bbH = bbMaxY - bbMinY;

  // Perimeter anchor ring — close to the bounding box so outer cells
  // don't extend wildly. 1.05× max half-extent is just outside the
  // outermost real nodes.
  const cxA = (bbMinX + bbMaxX) / 2;
  const cyA = (bbMinY + bbMaxY) / 2;
  const ringR = Math.max(bbW, bbH) * 0.55;
  const anchors = [];
  for (let i = 0; i < PERIMETER_NODES; i++) {
    const a = (i / PERIMETER_NODES) * 2 * Math.PI;
    anchors.push({
      id: `__anchor_${i}`,
      kind: '__anchor',
      x: cxA + ringR * Math.cos(a),
      y: cyA + ringR * Math.sin(a),
    });
  }

  // Voronoi clip extent only slightly beyond the anchor ring.
  const all = [...allNodes, ...anchors];
  const points = all.map((n) => [n.x, n.y]);
  const delaunay = d3delaunay.Delaunay.from(points);
  const voronoi = delaunay.voronoi([
    cxA - ringR * 1.05, cyA - ringR * 1.05,
    cxA + ringR * 1.05, cyA + ringR * 1.05,
  ]);

  // Group cell indices by area_id (perimeter anchors excluded).
  const cellsByArea = new Map();
  for (let i = 0; i < all.length; i++) {
    const n = all[i];
    if (n.kind === '__anchor') continue;
    const cell = voronoi.cellPolygon(i);
    if (!cell) continue;
    const list = cellsByArea.get(n.area_id) || [];
    list.push(cell);
    cellsByArea.set(n.area_id, list);
  }

  // For each area: union its cells, then INTERSECT with the area's
  // supernode square (inflated by 12% so the intersection isn't
  // perfectly square — it preserves the irregular Voronoi boundary
  // wherever the cells stay inside the square). This is the
  // cartogram clamp.
  const result = new Map();
  for (const [area, cells] of cellsByArea.entries()) {
    if (!cells.length) continue;
    const square = squares.get(area);
    if (!square) continue;

    // Union all the area's Voronoi cells into one polygon.
    let merged;
    try {
      merged = PC.union(...cells.map((c) => [c]));
    } catch (e) {
      console.warn('[mvg] polygon union failed for', area, e);
      merged = [[cells[0]]];
    }

    // Build the cartogram clip — the supernode square inflated 12% so
    // adjacent areas can still touch and look glued together.
    const half = square.side * 0.5 * 1.12;
    const cx = square.x, cy = square.y;
    const clipBox = [
      [
        [cx - half, cy - half],
        [cx + half, cy - half],
        [cx + half, cy + half],
        [cx - half, cy + half],
        [cx - half, cy - half],
      ],
    ];

    // Intersect Voronoi-union with the clip square. polygon-clipping
    // returns a MultiPolygon — we keep the LARGEST resulting polygon
    // (in case the intersection broke into pieces, which can happen
    // when the area's nodes are spread far apart across the bbox).
    let clipped;
    try {
      clipped = PC.intersection(merged, [clipBox]);
    } catch (e) {
      console.warn('[mvg] polygon intersection failed for', area, e);
      clipped = merged;
    }

    let bestRing = null, bestArea = -Infinity;
    for (const poly of clipped) {
      if (!poly || !poly[0] || poly[0].length < 3) continue;
      const a = Math.abs(d3PolygonArea(poly[0]));
      if (a > bestArea) { bestArea = a; bestRing = poly[0]; }
    }
    if (bestRing) result.set(area, chaikin(bestRing, 1));
  }
  return { polygons: result, bbox: { x: bbMinX, y: bbMinY, w: bbW, h: bbH } };
}

// Shoelace area (positive only used for picking largest ring).
function d3PolygonArea(ring) {
  let a = 0;
  for (let i = 0, n = ring.length, j = n - 1; i < n; j = i++) {
    a += ring[j][0] * ring[i][1] - ring[i][0] * ring[j][1];
  }
  return a / 2;
}

// One pass of Chaikin's corner-cutting smoothing. Each edge contributes
// two new vertices at 1/4 and 3/4 along it. Closes the ring naturally.
function chaikin(ring, passes = 1) {
  let pts = ring;
  if (pts[0][0] === pts[pts.length - 1][0] && pts[0][1] === pts[pts.length - 1][1]) {
    pts = pts.slice(0, -1);
  }
  for (let p = 0; p < passes; p++) {
    const out = [];
    const n = pts.length;
    for (let i = 0; i < n; i++) {
      const a = pts[i], b = pts[(i + 1) % n];
      out.push([0.75 * a[0] + 0.25 * b[0], 0.75 * a[1] + 0.25 * b[1]]);
      out.push([0.25 * a[0] + 0.75 * b[0], 0.25 * a[1] + 0.75 * b[1]]);
    }
    pts = out;
  }
  pts.push(pts[0]);  // close the ring
  return pts;
}


// ── Wait for the stage to have a real size ──────────────────────────
async function waitForStage(stage) {
  for (let i = 0; i < 20; i++) {
    const w = stage.clientWidth, h = stage.clientHeight;
    if (w > 0 && h > 0) return { w, h };
    await new Promise((r) => requestAnimationFrame(r));
  }
  return { w: stage.clientWidth || 1000, h: stage.clientHeight || 700 };
}


// ── Top-level layout ────────────────────────────────────────────────
async function buildLayout(data, w, h) {
  const d3 = await loadD3();
  const d3delaunay = await loadDelaunay();
  const polygonClipping = await loadPolygonClipping();

  const sg = buildSupergraph(data);
  const squares = await layoutSupergraph(d3, sg, w, h);

  // Per-area subgraph layout. Now hierarchical — facilities are
  // packed as sub-circles inside each area's square, and people sit
  // inside their primary facility's circle. The facCircles map is
  // populated as a side-effect for the renderer.
  const facCircles = new Map();
  const allNodes = [];
  for (const a of data.areas) {
    const square = squares.get(a.id);
    if (!square) continue;
    const members = membersOfArea(a.id, data);
    const edges = intraEdgesOfArea(members, data);
    const placed = await layoutAndFit(d3, members, edges, square, facCircles);
    allNodes.push(...placed);
  }

  const polyOut = await computePolygons(d3delaunay, polygonClipping,
                                         allNodes, squares, w, h);

  // Sub-Voronoi: each facility gets its own polygon territory inside
  // its area polygon. Replaces the dashed sub-circles. Computed by
  // running a small Voronoi over (facility centers + perimeter
  // anchors sampled along the area polygon edge), then clipping each
  // facility's cell to the area polygon via polygon-clipping
  // intersection so cells don't poke outside the country boundary.
  const facPolygons = computeFacilitySubPolygons(
    d3delaunay, polygonClipping,
    data, facCircles, polyOut.polygons,
  );

  // Cross-area edges for rendering (one row per pair, weight summed).
  const memberArea = new Map(allNodes.map((n) => [n.id, n.area_id]));
  const crossW = new Map();
  function edgeKey(s, t) { return s < t ? `${s}|${t}` : `${t}|${s}`; }
  function addCross(s, t, w) {
    if (!memberArea.has(s) || !memberArea.has(t)) return;
    if (memberArea.get(s) === memberArea.get(t)) return;
    const k = edgeKey(s, t);
    crossW.set(k, (crossW.get(k) || 0) + w);
  }
  for (const e of data.fac_pers) addCross(e.source, e.target, e.w);
  for (const e of data.coauthors) addCross(e.source, e.target, e.w);
  const crossEdges = [...crossW.entries()].map(([k, w]) => {
    const [s, t] = k.split('|');
    return { source: s, target: t, w };
  });

  // Polygon centroids for label placement.
  const labels = new Map();
  for (const a of data.areas) {
    const ring = polyOut.polygons.get(a.id);
    if (!ring) continue;
    let cx = 0, cy = 0, n = 0;
    for (let i = 0; i < ring.length - 1; i++) { cx += ring[i][0]; cy += ring[i][1]; n++; }
    if (n) labels.set(a.id, { x: cx / n, y: cy / n, name: a.name, weight: a.weight });
  }

  return {
    polygons: polyOut.polygons,
    bbox: polyOut.bbox,
    nodes: allNodes,
    crossEdges,
    labels,
    areas: data.areas,
    facCircles,
    facPolygons,
    // Protected-area aggregate per region + the count of vocabulary terms
    // that carry no organisation, dataset or person and so have no polygon.
    protectedByArea: data.protectedByArea || new Map(),
    nAreasEmpty: data.n_areas_empty || 0,
  };
}


// ── Protected-area layer (opt-in, aggregate only) ───────────────────
// Protected areas are legitimate coastal context — 3,309 of them are
// catalogued — but they hold zero datasets and zero researchers, so they
// do not size a region and they are not drawn as individual marks. This
// layer draws ONE chip per region stating the count and the split by
// jurisdiction, which is the whole of what the data supports.
function drawProtectedLayer(root) {
  if (_paChipSel) { _paChipSel.remove(); _paChipSel = null; }
  _paLabelSel = null;
  if (!_showProtected || !_layout || !_layout.protectedByArea) return;

  const chips = [];
  for (const [areaId, agg] of _layout.protectedByArea.entries()) {
    const lab = _layout.labels.get(areaId);
    if (!lab || !agg || !agg.n_protected) continue;
    chips.push({
      area_id: areaId, x: lab.x, y: lab.y, ...agg,
      // Chip text is the count, not a name — the region name is already
      // rendered by the area-label class directly above it.
      display: `▤ ${agg.n_protected} protected`,
      // Sized within PA_LABEL_PX by how many protected areas the chip
      // stands for, so a region holding 1,800 of them reads louder than
      // one holding 3. This used to pin every chip at PA_LABEL_PX.min,
      // which left the declared .max dead and made a 10.0-12.0 range a
      // misleading way to write "always 10.0".
      __basePx: PA_LABEL_PX.min,
    });
  }
  if (!chips.length) return;
  // Ramp against the largest chip in this render, so the scale is relative
  // to what is actually on screen rather than to a hard-coded maximum.
  const paMax = chips.reduce((m, c) => Math.max(m, c.n_protected || 0), 0);
  for (const c of chips) {
    c.__basePx = rampPx(PA_LABEL_PX, c.n_protected || 0, paMax);
  }

  const g = root.append('g').attr('class', 'mvg-pa-chips');
  _paChipSel = g;
  // Offset below the region label so the two never collide.
  _paLabelSel = g.attr('text-anchor', 'middle')
    .attr('font-family', 'system-ui, sans-serif')
    .attr('pointer-events', 'none')
    .selectAll('text').data(chips).enter().append('text')
    .attr('x', (d) => d.x)
    .attr('y', (d) => d.y + 22)
    .attr('font-weight', 600)
    .attr('fill', '#3f6212')
    .attr('stroke', '#f7fee7')
    .attr('stroke-width', 2.0)
    .attr('stroke-linejoin', 'round')
    .attr('paint-order', 'stroke')
    .text((d) => d.display);
  _paLabelSel.each(function (d) { d.__baseFont = d.__basePx; });
}


// Sub-Voronoi: per area, partition the area's polygon into facility
// territories. Returns Map(facility_id → {ring, area_id, name,
// acronym, country, f_type, url, n_people}).
function computeFacilitySubPolygons(d3delaunay, polygonClipping,
                                     data, facCircles, areaRings) {
  const PC = polygonClipping.default || polygonClipping;
  const result = new Map();
  // Group facilities by area_id.
  const facsByArea = new Map();
  for (const f of data.facilities) {
    if (!facsByArea.has(f.area_id)) facsByArea.set(f.area_id, []);
    facsByArea.get(f.area_id).push(f);
  }
  // For each area, run a small Voronoi over facility positions plus
  // anchors sampled along the area polygon edge so cells stay bounded.
  for (const [areaId, areaRing] of areaRings.entries()) {
    const facs = facsByArea.get(areaId) || [];
    if (!facs.length || !areaRing || areaRing.length < 4) continue;
    // Bbox of the area polygon for the Voronoi clip extent.
    let mnX = Infinity, mnY = Infinity, mxX = -Infinity, mxY = -Infinity;
    for (const [x, y] of areaRing) {
      if (x < mnX) mnX = x; if (y < mnY) mnY = y;
      if (x > mxX) mxX = x; if (y > mxY) mxY = y;
    }
    const padW = (mxX - mnX) * 0.4 + 10;
    const padH = (mxY - mnY) * 0.4 + 10;
    // Collect facility seed points (their bubble centers).
    const seeds = [];
    const seedFids = [];
    for (const f of facs) {
      const c = facCircles.get(f.id);
      if (!c) continue;
      seeds.push([c.x, c.y]);
      seedFids.push(f.id);
    }
    if (!seeds.length) continue;
    // Sample 12-32 anchor points along the polygon perimeter to bound
    // the Voronoi cells; spaced proportionally to perimeter length.
    const peri = polygonPerimeter(areaRing);
    const nAnchors = Math.max(12, Math.min(32, Math.round(peri / 28)));
    const peripheryAnchors = samplePerimeter(areaRing, nAnchors);
    // Push anchors slightly OUTWARD so seed cells own the inner area.
    // (Compute centroid; nudge each anchor 6% further along the radius.)
    let cgx = 0, cgy = 0;
    for (const [x, y] of areaRing) { cgx += x; cgy += y; }
    cgx /= areaRing.length; cgy /= areaRing.length;
    for (const a of peripheryAnchors) {
      const dx = a[0] - cgx, dy = a[1] - cgy;
      a[0] = cgx + dx * 1.05;
      a[1] = cgy + dy * 1.05;
    }
    const allPts = [...seeds, ...peripheryAnchors];
    let voro;
    try {
      const dl = d3delaunay.Delaunay.from(allPts);
      voro = dl.voronoi([
        mnX - padW, mnY - padH, mxX + padW, mxY + padH,
      ]);
    } catch (e) {
      console.warn('[mvg] sub-voronoi delaunay failed for area', areaId, e);
      continue;
    }
    const areaPoly = [areaRing];   // polygon-clipping wants a Polygon (rings)
    for (let i = 0; i < seeds.length; i++) {
      const cell = voro.cellPolygon(i);
      if (!cell) continue;
      let clipped;
      try {
        clipped = PC.intersection([cell], [areaPoly]);
      } catch (e) {
        // If clipping fails, fall back to the unclipped cell.
        clipped = [[cell]];
      }
      // Pick the largest sub-polygon.
      let bestRing = null, bestArea = -Infinity;
      for (const poly of clipped) {
        if (!poly || !poly[0] || poly[0].length < 3) continue;
        const ar = Math.abs(d3PolygonArea(poly[0]));
        if (ar > bestArea) { bestArea = ar; bestRing = poly[0]; }
      }
      if (!bestRing) continue;
      const fid = seedFids[i];
      const meta = facCircles.get(fid) || {};
      result.set(fid, {
        ring: chaikin(bestRing, 1),
        area_id: areaId,
        name: meta.name, acronym: meta.acronym, country: meta.country,
        f_type: meta.f_type, url: meta.url, n_people: meta.n_people || 0,
        n_datasets: meta.n_datasets || 0,
      });
    }
  }
  return result;
}

function polygonPerimeter(ring) {
  let p = 0;
  for (let i = 1; i < ring.length; i++) {
    p += Math.hypot(ring[i][0] - ring[i - 1][0],
                    ring[i][1] - ring[i - 1][1]);
  }
  return p;
}

function samplePerimeter(ring, n) {
  const peri = polygonPerimeter(ring);
  const step = peri / n;
  const out = [];
  let acc = 0;
  let next = step / 2;
  for (let i = 1; i < ring.length; i++) {
    const ax = ring[i - 1][0], ay = ring[i - 1][1];
    const bx = ring[i][0], by = ring[i][1];
    const segLen = Math.hypot(bx - ax, by - ay) || 1e-6;
    while (next <= acc + segLen) {
      const t = (next - acc) / segLen;
      out.push([ax + (bx - ax) * t, ay + (by - ay) * t]);
      next += step;
    }
    acc += segLen;
  }
  return out;
}


// ── Registry layer geometry + rendering ─────────────────────────────
//
// PERFORMANCE POSTURE, stated plainly. The registry ships 10,000
// core-tier researchers and 5,300 edges. This layer never draws either
// number:
//
//   * Only 263 of those 10,000 have a site link, so only 263 could ever
//     be placed. The other 9,737 have no coordinate on this map.
//   * At rest the layer draws ONE ribbon per site pair — 18 of them,
//     aggregated from the 61 researcher-level edges whose two endpoints
//     both sit at catalogued sites — and nothing else. That is the whole
//     map-scale view.
//   * Individual researcher markers appear only for the ONE site the
//     user selects, capped at FOCUS_NODE_CAP=48. The largest site
//     (Scripps, 82 links) therefore draws 48 markers and lists all 82
//     in the side panel.
//
// So the marker count is bounded by 48 + 26 site anchors regardless of
// how the registry grows, and the SQL that feeds it returns 263 + 771 +
// 18 + 3 rows. No canvas fallback or worker is needed at this size.

// Where on the map does a catalogued site live? Prefer the centroid of
// its facility sub-polygon (the same point the facility dot uses), fall
// back to its packed bubble centre. Returns null for sites that are not
// on the map at all — a registry link can point at a facility with no
// primary research area, which means no polygon and no position.
function siteAnchor(facilityId) {
  if (!_layout) return null;
  const sp = _layout.facPolygons && _layout.facPolygons.get(facilityId);
  if (sp && sp.ring && sp.ring.length) {
    let cx = 0, cy = 0;
    for (const [x, y] of sp.ring) { cx += x; cy += y; }
    return { x: cx / sp.ring.length, y: cy / sp.ring.length, meta: sp };
  }
  const c = _layout.facCircles && _layout.facCircles.get(facilityId);
  if (c) return { x: c.x, y: c.y, r: c.r, meta: c };
  return null;
}

// Radius available for scattering researcher markers inside a site's
// sub-polygon. Uses the smaller bbox half-extent so the spiral stays
// inside even for elongated Voronoi cells, and clamps to a sane range
// so a huge cell doesn't fling markers to its corners.
function siteScatterRadius(facilityId) {
  const sp = _layout && _layout.facPolygons && _layout.facPolygons.get(facilityId);
  if (sp && sp.ring && sp.ring.length) {
    let mnX = Infinity, mnY = Infinity, mxX = -Infinity, mxY = -Infinity;
    for (const [x, y] of sp.ring) {
      if (x < mnX) mnX = x; if (y < mnY) mnY = y;
      if (x > mxX) mxX = x; if (y > mxY) mxY = y;
    }
    const half = Math.min(mxX - mnX, mxY - mnY) / 2;
    return Math.max(8, Math.min(90, half * 0.78));
  }
  const c = _layout && _layout.facCircles && _layout.facCircles.get(facilityId);
  return c ? Math.max(8, c.r * 0.85) : 20;
}

// Deterministic golden-angle spiral placement inside the focused site.
// Same construction the people-inside-a-bubble layout already uses, so
// the two layers look like they belong to the same map.
function placeFocusNodes(facilityId, rows) {
  const a0 = siteAnchor(facilityId);
  if (!a0) return [];
  const R = siteScatterRadius(facilityId);
  const PHI = Math.PI * (3 - Math.sqrt(5));
  const n = rows.length;
  return rows.map((r, k) => {
    const t = (k + 0.5) / Math.max(n, 1);
    const rad = R * Math.sqrt(t);
    const ang = (k + 1) * PHI;
    return { ...r,
             x: a0.x + rad * Math.cos(ang),
             y: a0.y + rad * Math.sin(ang) };
  });
}

// Marker radius from coastal output volume. coastal_works_count is an
// UPPER BOUND on distinct coastal papers (OpenAlex lists a work under
// every topic it carries), so it is used only as a monotone size cue
// and is always labelled "coastal output volume", never "papers".
function registryRadius(d) {
  return 2.2 + Math.min(3.4, Math.sqrt(d.coastal_works_count || 0) * 0.22);
}

function registryTipHtml(d) {
  const collabs = (_registry && _registry.collabsBy.get(d.canonical_id)) || [];
  const lines = [`<strong>${escapeHtml(d.display_name)}</strong>`];
  const sub = [d.affiliation_country, d.h_index ? `h-index ${d.h_index}` : null]
    .filter(Boolean).join(' · ');
  if (sub) lines.push(`<small>${escapeHtml(sub)}</small>`);
  if (d.coastal_works_count) {
    lines.push(`<small style="color:#b45309">coastal output volume ≤ ${d.coastal_works_count}`
      + (d.works_count ? ` of ${d.works_count} works` : '') + '</small>');
  }
  if (collabs.length) {
    // collabs is ordered by co_pubs DESC in SQL, so [0] is the strongest
    // edge. Naming the collaborator's institution is what makes the edge
    // legible — "who, and where" rather than a bare count.
    const top = collabs[0];
    const where = top.other_affiliation
      ? ` <span style="color:#64748b">at ${escapeHtml(top.other_affiliation)}</span>`
      : '';
    lines.push(`<small style="color:#0c4a6e">${collabs.length} registry co-author`
      + `${collabs.length === 1 ? '' : 's'}</small>`
      + `<br><small>strongest: ${escapeHtml(top.other_name || '?')}`
      + ` — ${top.co_pubs} co-publication${top.co_pubs === 1 ? '' : 's'}${where}</small>`);
  } else if (d.graph_harvested) {
    lines.push('<small style="color:#64748b">no registry co-authors found</small>');
  } else {
    // The honest distinction the caveats demand: this identity was
    // never part of the co-authorship harvest, so an empty edge list
    // means "not computed", not "publishes alone".
    lines.push('<small style="color:#92400e">co-authorship not yet computed '
      + 'for this identity</small>');
  }
  lines.push('<small style="color:#94a3b8">click for ORCID / profile</small>');
  return lines.join('<br>');
}

// Site ribbon tooltip: names both endpoints and is explicit that the
// weight is a summed pair count, not a paper count.
function siteLinkTipHtml(e) {
  const a = (_layout.facPolygons && _layout.facPolygons.get(e.source))
         || (_layout.facCircles && _layout.facCircles.get(e.source)) || {};
  const b = (_layout.facPolygons && _layout.facPolygons.get(e.target))
         || (_layout.facCircles && _layout.facCircles.get(e.target)) || {};
  const nm = (m, id) => escapeHtml(m.acronym || m.name || id);
  return '<strong>Co-publication between sites</strong>'
    + `<br>${nm(a, e.source)} ↔ ${nm(b, e.target)}`
    + `<br><small>${e.co_pubs} co-publication${e.co_pubs === 1 ? '' : 's'}`
    + ` across ${e.n_pairs} researcher pair${e.n_pairs === 1 ? '' : 's'}</small>`
    + '<br><small style="color:#94a3b8">click a site to list its researchers</small>';
}

// Draw (or redraw) everything the registry layer owns into _regRootG.
// The group itself is created once during render() at a fixed z
// position, so a focus change only swaps its children — no layer
// re-append, no z-order drift, no orphaned <g> left behind.
function drawRegistryLayer(d3, tip) {
  if (!_regRootG) return;
  _regRootG.selectAll('*').remove();
  _regSiteLinkSel = null;
  _regNodeSel = null;
  _regNodeLinkSel = null;
  _regLabelSel = null;
  _regDomainLabelSel = null;
  if (!_showRegistry || !_registry || !_layout) return;

  // ── Site ↔ site ribbons (always on when the layer is on) ─────────
  const linkData = [];
  for (const e of _registry.siteEdges) {
    const a = siteAnchor(e.source);
    const b = siteAnchor(e.target);
    if (!a || !b) continue;          // one endpoint has no map position
    linkData.push({ ...e, x1: a.x, y1: a.y, x2: b.x, y2: b.y });
  }
  if (linkData.length) {
    const maxW = Math.max(...linkData.map((e) => e.co_pubs));
    const g = _regRootG.append('g').attr('class', 'mvg-reg-links')
      .attr('fill', 'none');
    // Wide transparent hit line first, visible ribbon on top — same
    // two-layer pattern the cross-area edges use, so thin ribbons are
    // still hoverable.
    g.append('g')
      .attr('stroke', 'transparent').attr('stroke-width', 9)
      .attr('stroke-linecap', 'round')
      .style('pointer-events', 'stroke').style('cursor', 'help')
      .selectAll('line').data(linkData).enter().append('line')
      .attr('x1', (e) => e.x1).attr('y1', (e) => e.y1)
      .attr('x2', (e) => e.x2).attr('y2', (e) => e.y2)
      .on('mouseenter', (ev, e) => showTip(tip, ev, siteLinkTipHtml(e)))
      .on('mousemove', (ev, e) => showTip(tip, ev, siteLinkTipHtml(e)))
      .on('mouseleave', () => hideTip(tip));
    _regSiteLinkSel = g.append('g')
      .attr('stroke', NODE_COLORS.registryEdge)
      .attr('stroke-opacity', 0.5)
      .attr('stroke-linecap', 'round')
      .style('pointer-events', 'none')
      .selectAll('line').data(linkData).enter().append('line')
      .attr('x1', (e) => e.x1).attr('y1', (e) => e.y1)
      .attr('x2', (e) => e.x2).attr('y2', (e) => e.y2)
      .attr('stroke-width', (e) => 0.6 + 2.4 * Math.sqrt(e.co_pubs / maxW));
    _regSiteLinkSel.each(function (e) {
      e.__baseW = 0.6 + 2.4 * Math.sqrt(e.co_pubs / maxW);
    });
  }

  // ── Domain-placed cohorts: one badge per region ───────────────────
  // These researchers have NO site. The badge is drawn as a DASHED,
  // hollow ring at the region centroid — deliberately unlike the solid
  // filled site markers — with a "(no site)" caption, so a domain-placed
  // person can never be mistaken for someone located at a facility.
  const domainData = [];
  for (const [areaId, coh] of (_registry.domainByArea || new Map()).entries()) {
    const lab = _layout.labels.get(areaId);
    if (!lab || !coh.n_people) continue;
    domainData.push({ ...coh, x: lab.x, y: lab.y - 26,
                      area_name: lab.name || areaId });
  }
  if (domainData.length) {
    const maxN = Math.max(...domainData.map((d) => d.n_people));
    const domRadius = (d) => 5 + 7 * Math.sqrt(d.n_people / maxN);
    const g = _regRootG.append('g').attr('class', 'mvg-reg-domain');
    const rings = g.selectAll('circle').data(domainData).enter().append('circle')
      .attr('cx', (d) => d.x).attr('cy', (d) => d.y)
      .attr('r', domRadius)
      .attr('fill', 'none')
      .attr('stroke', NODE_COLORS.registry)
      .attr('stroke-width', 1.6)
      .attr('stroke-dasharray', '3,2.5')
      .style('cursor', 'pointer')
      .on('mouseenter', (ev, d) => showTip(tip, ev, domainCohortTipHtml(d)))
      .on('mouseleave', () => hideTip(tip))
      .on('click', (ev, d) => { ev.stopPropagation(); renderDomainPanel(d); });
    rings.each(function (d) { d.__baseR = domRadius(d); });
    // Caption. Its own text class, banded with the REGION labels
    // (ZOOM_BANDS.area, not .registry) — it annotates a region, so it
    // belongs on screen exactly while region names are. See the
    // applyLabels call in onZoom().
    const caps = g.append('g').attr('text-anchor', 'middle')
      .attr('font-family', 'system-ui, sans-serif')
      .attr('pointer-events', 'none')
      .selectAll('text').data(domainData).enter().append('text')
      .attr('x', (d) => d.x)
      .attr('y', (d) => d.y - domRadius(d) - 3)
      .attr('font-weight', 600)
      .attr('fill', NODE_COLORS.registry)
      .attr('stroke', '#fff7ed')
      .attr('stroke-width', 2.0)
      .attr('stroke-linejoin', 'round')
      .attr('paint-order', 'stroke')
      .text((d) => `${d.n_people} no site`);
    caps.each(function (d) {
      d.__basePx = PA_LABEL_PX.min;
      d.__baseFont = d.__basePx;
    });
    _regDomainLabelSel = caps;
  } else {
    _regDomainLabelSel = null;
  }

  // ── Focused site: individual researcher markers ──────────────────
  if (!_focusFacility) { renderRegistryPanel(null); return; }
  const roster = _registry.byFacility.get(_focusFacility) || [];
  const shown = roster.length
    ? placeFocusNodes(_focusFacility, roster.slice(0, FOCUS_NODE_CAP))
    : [];
  if (!shown.length) {
    // Either the site has no registry links, or it has no position on
    // this map (a registry link can point at a facility with no primary
    // research area, hence no polygon). Either way: no markers, and the
    // panel must not keep showing the previous site's roster.
    renderRegistryPanel(null);
    return;
  }
  const posBy = new Map(shown.map((d) => [d.canonical_id, d]));

  // Researcher ↔ researcher edges for the focused set. Two cases:
  //   both endpoints in view → straight line between the two markers
  //   collaborator at another placed site → line to that site's anchor
  // Collaborators with no site link are NOT drawn (they have no
  // position); the panel reports how many were omitted.
  const nodeLinks = [];
  let offMapEdges = 0;
  for (const d of shown) {
    for (const c of (_registry.collabsBy.get(d.canonical_id) || [])) {
      const near = posBy.get(c.other);
      if (near) {
        // Draw each intra-focus pair once.
        if (d.canonical_id > c.other) continue;
        nodeLinks.push({ x1: d.x, y1: d.y, x2: near.x, y2: near.y,
                         co_pubs: c.co_pubs, kind: 'internal',
                         a: d.display_name, b: c.other_name });
      } else if (c.other_facility_id) {
        const anch = siteAnchor(c.other_facility_id);
        if (!anch) { offMapEdges++; continue; }
        nodeLinks.push({ x1: d.x, y1: d.y, x2: anch.x, y2: anch.y,
                         co_pubs: c.co_pubs, kind: 'external',
                         a: d.display_name, b: c.other_name });
      } else {
        offMapEdges++;
      }
    }
  }
  if (nodeLinks.length) {
    _regNodeLinkSel = _regRootG.append('g').attr('class', 'mvg-reg-node-links')
      .attr('fill', 'none')
      .attr('stroke', NODE_COLORS.registryEdge)
      .style('pointer-events', 'none')
      .selectAll('line').data(nodeLinks).enter().append('line')
      .attr('x1', (e) => e.x1).attr('y1', (e) => e.y1)
      .attr('x2', (e) => e.x2).attr('y2', (e) => e.y2)
      .attr('stroke-opacity', (e) => e.kind === 'internal' ? 0.55 : 0.28)
      .attr('stroke-dasharray', (e) => e.kind === 'external' ? '2,2' : null)
      .attr('stroke-width', (e) => 0.4 + Math.log(1 + e.co_pubs) * 0.28);
    _regNodeLinkSel.each(function (e) {
      e.__baseW = 0.4 + Math.log(1 + e.co_pubs) * 0.28;
    });
  }

  _regNodeSel = _regRootG.append('g').attr('class', 'mvg-reg-nodes')
    .selectAll('circle').data(shown).enter().append('circle')
    .attr('cx', (d) => d.x).attr('cy', (d) => d.y)
    .attr('r', registryRadius)
    .attr('fill', NODE_COLORS.registry)
    .attr('fill-opacity', 0.9)
    .attr('stroke', '#fff')
    .attr('stroke-width', 0.7)
    .style('cursor', 'pointer')
    .on('mouseenter', (ev, d) => showTip(tip, ev, registryTipHtml(d)))
    .on('mouseleave', () => hideTip(tip))
    .on('click', (ev, d) => { ev.stopPropagation(); openRegistryProfile(d); });
  _regNodeSel.each(function (d) { d.__baseR = registryRadius(d); });

  // Researcher NAME LABELS at the focused site. These are the only
  // registry names drawn on the map, and only inside ZOOM_BANDS.registry
  // (k >= 1.0), because at the fit zoom a site sub-polygon is a few dozen
  // pixels across and 48 names would be unreadable regardless of size.
  const maxH = Math.max(1, ...shown.map((d) => d.h_index || 0));
  _regLabelSel = _regRootG.append('g').attr('class', 'mvg-reg-labels')
    .attr('text-anchor', 'middle')
    .attr('font-family', 'system-ui, sans-serif')
    .attr('pointer-events', 'none')
    .selectAll('text').data(shown).enter().append('text')
    .attr('x', (d) => d.x)
    .attr('y', (d) => d.y - 4)
    .attr('font-weight', 600)
    .attr('fill', '#7c2d12')
    .attr('stroke', '#fff7ed')
    .attr('stroke-width', 2.0)
    .attr('stroke-linejoin', 'round')
    .attr('paint-order', 'stroke')
    .text((d) => shortName(d.display_name));
  _regLabelSel.each(function (d) {
    d.__basePx = rampPx(REG_LABEL_PX, d.h_index || 0, maxH);
    d.__baseFont = d.__basePx;
  });

  renderRegistryPanel(roster, shown.length, offMapEdges);
}

// Click-through for a registry researcher. Registry rows are keyed on
// canonical_id, not people.person_id, so the People directory route
// only works for the minority that carry a person_id. Everyone else
// gets their ORCID (or OpenAlex) profile, which is the identifier the
// registry is actually built on.
function openRegistryProfile(d) {
  if (!d) return;
  if (d.person_id) {
    location.hash = `#/people/${encodeURIComponent(d.person_id)}`;
    return;
  }
  const url = d.orcid ? `https://orcid.org/${d.orcid}`
            : d.openalex_id ? `https://openalex.org/${d.openalex_id}`
            : d.homepage_url || null;
  if (url) window.open(url, '_blank', 'noopener');
}

// Focus a site (or clear focus with null) and redraw only the registry
// layer. Cheap enough to call on every click — it touches one <g>.
function setFocusFacility(facilityId) {
  _focusFacility = (_focusFacility === facilityId) ? null : facilityId;
  if (!_d3Mod) return;
  drawRegistryLayer(_d3Mod, ensureTooltip());
  onZoom(_zoomK);
  if (!_focusFacility) renderRegistryPanel(null);
}

// Side panel listing the focused site's roster. Shows every linked
// researcher — including the ones past FOCUS_NODE_CAP that have no
// marker — so the cap never silently hides people.
function renderRegistryPanel(roster, nDrawn, offMapEdges) {
  const panel = _container && _container.querySelector('#net-reg-panel');
  if (!panel) return;
  if (!roster || !roster.length || !_focusFacility) {
    panel.hidden = true;
    panel.innerHTML = '';
    return;
  }
  const meta = (_layout.facPolygons && _layout.facPolygons.get(_focusFacility))
            || (_layout.facCircles && _layout.facCircles.get(_focusFacility)) || {};
  const sum = _registry.siteSummary.get(_focusFacility) || {};
  const title = escapeHtml(meta.acronym || meta.name || _focusFacility);
  const rows = roster.map((r, i) => {
    const collabs = _registry.collabsBy.get(r.canonical_id) || [];
    const badge = collabs.length
      ? `<span class="mvg-reg-badge">${collabs.length}</span>`
      : (r.graph_harvested
          ? '<span class="mvg-reg-badge is-zero" title="Harvested — no registry co-authors found">0</span>'
          : '<span class="mvg-reg-badge is-unknown" title="Co-authorship not yet computed for this identity">–</span>');
    return `<li class="${i < nDrawn ? '' : 'is-unplotted'}">
      <button type="button" class="mvg-reg-row" data-cid="${escapeHtml(r.canonical_id)}">
        <span class="mvg-reg-name">${escapeHtml(r.display_name)}</span>
        ${badge}
      </button></li>`;
  }).join('');
  const capNote = roster.length > nDrawn
    ? `<p class="mvg-reg-note">${nDrawn} of ${roster.length} plotted on the map;
       the rest are listed here.</p>`
    : '';
  // n_with_edges counts researchers holding at least one edge, which is NOT
  // the same as "co-authorship computed": a harvested researcher can be
  // computed and still have zero edges inside the registry. n_harvested is
  // the coverage figure; n_with_edges is the result. Reporting them
  // separately keeps the badge legend ("–" means not harvested) honest.
  const edgeNote = sum.n_with_edges != null
    ? `<p class="mvg-reg-note">${sum.n_harvested} of ${roster.length} have
       co-authorship computed; ${sum.n_with_edges} hold at least one link here.
       <span class="mvg-reg-badge is-unknown">–</span>
       means not yet harvested, not zero.</p>`
    : '';
  const offNote = offMapEdges
    ? `<p class="mvg-reg-note">${offMapEdges} co-author link${offMapEdges === 1 ? '' : 's'}
       point outside the catalogued sites and are not drawn.</p>`
    : '';
  panel.hidden = false;
  panel.innerHTML = `
    <div class="mvg-reg-head">
      <h3>${title}</h3>
      <button type="button" id="net-reg-clear" class="btn-ghost">Clear</button>
    </div>
    <p class="mvg-reg-note">${roster.length} core-tier researcher${roster.length === 1 ? '' : 's'}
      linked by ROR match${sum.internal
        ? ` · ${sum.internal.co_pubs} co-publications among them` : ''}</p>
    ${capNote}${edgeNote}${offNote}
    <ol class="mvg-reg-list">${rows}</ol>`;
  panel.querySelector('#net-reg-clear')
    .addEventListener('click', () => setFocusFacility(null));
  panel.querySelectorAll('.mvg-reg-row').forEach((btn) => {
    btn.addEventListener('click', () => {
      const r = roster.find((x) => x.canonical_id === btn.dataset.cid);
      if (r) openRegistryProfile(r);
    });
  });
}


// ── Render ─────────────────────────────────────────────────────────
async function render() {
  const statusEl = _container.querySelector('#net-status');
  const stage = _container.querySelector('#net-stage');
  if (!stage) return;
  try {
    const { w, h } = await waitForStage(stage);
    statusEl.textContent = 'Loading data…';
    const d3 = await loadD3();
    _d3Mod = d3;
    if (!_layout) {
      const data = await fetchData();
      statusEl.textContent = 'Computing knowledge map (this takes 5-10 s)…';
      _layout = await buildLayout(data, w, h);
    }
    updateStatus();

    stage.innerHTML = '';
    // viewBox is computed to focus on the WEIGHTED CORE of the map —
    // not the bounding box of every polygon. The previous "tightest
    // bbox of all rings" logic let one or two single-facility outlier
    // polygons (which the force sim sometimes flings to the periphery)
    // stretch the box, so the central dense cluster — coastal-processes,
    // marine-ecosystems, biogeochemistry — ended up off-center and tiny
    // while the user saw mostly empty parchment with an outlier in the
    // top corner.
    //
    // Instead, we:
    //   1. Compute the weighted centroid of all area polygons, weighted
    //      by polygon weight (= n_facilities).
    //   2. Rank polygons by distance from that centroid.
    //   3. Walk the ranked list inward → outward, accumulating bbox AND
    //      cumulative weight. Stop once cumulative weight covers
    //      VIEWBOX_WEIGHT_FRAC of the total.
    //
    // Outlier polygons remain rendered (you can pan/zoom-out to see
    // them), they just don't dictate the default frame. Tuned so the
    // 4-5 biggest cartograms always land on screen at first paint.
    const VIEWBOX_WEIGHT_FRAC = 0.80;
    let bx, by, bw, bh;
    {
      // Polygon centroids + weights.
      const cents = [];
      for (const a of _layout.areas) {
        const ring = _layout.polygons.get(a.id);
        if (!ring || ring.length === 0) continue;
        let cx = 0, cy = 0;
        for (const [x, y] of ring) { cx += x; cy += y; }
        cx /= ring.length; cy /= ring.length;
        cents.push({ a, ring, cx, cy, w: a.weight || 1 });
      }
      const totalW = cents.reduce((s, c) => s + c.w, 0) || 1;

      // Weighted centroid (importance-weighted "center of mass").
      let sx = 0, sy = 0;
      for (const c of cents) { sx += c.cx * c.w; sy += c.cy * c.w; }
      const wcx = sx / totalW, wcy = sy / totalW;

      // Sort by distance from weighted centroid; bigger polygons
      // additionally win ties (and pull the inclusion threshold
      // toward themselves) by penalising their distance with a
      // sqrt-weight bonus.
      cents.sort((p, q) => {
        const dp = Math.hypot(p.cx - wcx, p.cy - wcy) / Math.sqrt(p.w);
        const dq = Math.hypot(q.cx - wcx, q.cy - wcy) / Math.sqrt(q.w);
        return dp - dq;
      });

      // Accumulate bbox until we cover VIEWBOX_WEIGHT_FRAC of weight.
      let mnX = Infinity, mnY = Infinity, mxX = -Infinity, mxY = -Infinity;
      let cumW = 0;
      for (const { ring, w } of cents) {
        for (const [x, y] of ring) {
          if (x < mnX) mnX = x; if (y < mnY) mnY = y;
          if (x > mxX) mxX = x; if (y > mxY) mxY = y;
        }
        cumW += w;
        if (cumW / totalW >= VIEWBOX_WEIGHT_FRAC) break;
      }

      // Fall-through guard if for any reason no polygons were captured.
      if (mnX === Infinity) {
        for (const ring of _layout.polygons.values()) {
          for (const [x, y] of ring) {
            if (x < mnX) mnX = x; if (y < mnY) mnY = y;
            if (x > mxX) mxX = x; if (y > mxY) mxY = y;
          }
        }
      }

      // 8% padding so polygons aren't flush against the edge.
      const padW = (mxX - mnX) * 0.08;
      const padH = (mxY - mnY) * 0.08;
      bx = mnX - padW; by = mnY - padH;
      bw = (mxX - mnX) + 2 * padW;
      bh = (mxY - mnY) + 2 * padH;
    }
    const svg = d3.select(stage).append('svg')
      .attr('viewBox', `${bx} ${by} ${bw} ${bh}`)
      .attr('preserveAspectRatio', 'xMidYMid meet')
      .attr('class', 'mvg-svg');
    const root = svg.append('g').attr('class', 'mvg-root');
    _rootG = root;

    // d3.zoom — captured so the TOC + polygon clicks can call it.
    const zoom = d3.zoom().scaleExtent([0.4, 12]).on('zoom', (ev) => {
      root.attr('transform', ev.transform);
      onZoom(ev.transform.k);
    });
    svg.call(zoom);
    _zoomBehavior = zoom;
    _zoomSvg = svg;

    const tip = ensureTooltip();

    // Layer 1: polygons
    const areaList = _layout.areas;
    const colorOf = new Map(areaList.map((a, i) => [a.id, AREA_PALETTE[i % AREA_PALETTE.length]]));
    _colorOf = colorOf;
    const polyG = root.append('g').attr('class', 'mvg-polys');
    polyG.selectAll('path').data(areaList).enter().append('path')
      .attr('d', (a) => {
        const ring = _layout.polygons.get(a.id);
        return ring ? `M${ring.map((p) => p.join(',')).join('L')}Z` : '';
      })
      .attr('fill', (a) => colorOf.get(a.id))
      .attr('fill-opacity', 0.18)
      .attr('stroke', (a) => colorOf.get(a.id))
      .attr('stroke-opacity', 0.9)
      .attr('stroke-width', 1.4)
      .style('cursor', 'pointer')
      .on('mouseenter', function (ev, a) {
        d3.select(this).attr('fill-opacity', 0.32);
        const lab = _layout.labels.get(a.id);
        if (lab) showTip(tip, ev, areaTipHtml(a));
      })
      .on('mouseleave', function () {
        d3.select(this).attr('fill-opacity', 0.18);
        hideTip(tip);
      })
      .on('click', (ev, a) => zoomToArea(a.id));

    // Layer 1.5: facility SUB-POLYGONS inside each area polygon.
    // Each institution gets its own Voronoi territory, clipped to its
    // area polygon. Fill-opacity NOW VARIES BY PERSONNEL COUNT — the
    // bigger the institution (more researchers mapped here), the
    // stronger the fill — so adjacent sub-polygons are visually
    // distinguishable instead of all reading as the same shade.
    if (_showFacility && _layout.facPolygons && _layout.facPolygons.size) {
      const facData = [..._layout.facPolygons.entries()]
        .map(([id, p]) => ({ id, ...p }));
      // Map n_people → fill-opacity. Range 0.06–0.24 so even tiny
      // institutions register without large ones blasting saturation.
      const maxPeople = Math.max(1, ...facData.map((d) => d.n_people || 0));
      const baseOpacity = (d) => {
        const t = Math.sqrt((d.n_people || 0) / maxPeople); // sqrt-damped
        return 0.06 + t * 0.18;
      };
      _facPolySel = root.append('g').attr('class', 'mvg-facpolys')
        .selectAll('path').data(facData).enter().append('path')
        .attr('d', (d) => d.ring
          ? `M${d.ring.map((p) => p.join(',')).join('L')}Z`
          : '')
        .attr('fill', (d) => colorOf.get(d.area_id) || '#94a3b8')
        .attr('fill-opacity', baseOpacity)
        .attr('stroke', (d) => colorOf.get(d.area_id) || '#64748b')
        .attr('stroke-opacity', 0.7)
        .attr('stroke-width', 0.6)
        .style('cursor', 'pointer');
      _facPolySel.each(function (d) { d.__baseOpacity = baseOpacity(d); });
      _facPolySel
        .on('mouseenter', function (ev, d) {
          // Bump THIS polygon, dim the others in the same area so the
          // hovered institution + its people stand out within the
          // country.
          d3.select(this).attr('fill-opacity', Math.min(0.45, (d.__baseOpacity || 0.1) * 2.5));
          if (_facPolySel) {
            _facPolySel.filter((o) => o !== d && o.area_id === d.area_id)
              .attr('fill-opacity', (o) => (o.__baseOpacity || 0.1) * 0.45);
          }
          showTip(tip, ev, facilityCircleTipHtml(d));
        })
        .on('mouseleave', function () {
          if (_facPolySel) {
            _facPolySel.attr('fill-opacity', (o) => o.__baseOpacity || 0.1);
          }
          hideTip(tip);
        })
        .on('click', (ev, d) => {
          // With the registry layer on, a site click means "show me who
          // works here" — the map is the navigation surface. With it off
          // the historical behaviour (open the site's website) stands.
          if (_showRegistry) { setFocusFacility(d.id); return; }
          if (d.url) window.open(d.url, '_blank', 'noopener');
        });
    } else {
      _facPolySel = null;
    }

    // Layer 2: cross-area edges. THREE visibility buckets so edges
    // don't dangle into invisible nodes:
    //   - facility ↔ facility       → Facilities ON
    //   - person ↔ facility         → BOTH ON  (person-bridging that
    //                                  terminates on a facility dot)
    //   - person ↔ person           → People ON
    // Previously a person↔facility edge was bucketed as "person" and
    // kept showing after Facilities was toggled off, leaving ghost
    // lines pointing at hidden facility dots.
    const nodeIdx = new Map(_layout.nodes.map((n) => [n.id, n]));
    const edgeKind = (e) => {
      const a = nodeIdx.get(e.source); const b = nodeIdx.get(e.target);
      const ak = a ? a.kind : null; const bk = b ? b.kind : null;
      if (ak === 'person' && bk === 'person') return 'pp';
      if (ak === 'person' || bk === 'person') return 'pf';
      return 'ff';
    };
    const buckets = { ff: [], pf: [], pp: [] };
    for (const e of _layout.crossEdges) {
      const k = edgeKind(e);
      if (buckets[k]) buckets[k].push(e);
    }
    // Area-id → display name lookup for the edge tooltip.
    const areaName = new Map(_layout.areas.map((a) => [a.id, a.name]));
    // Tooltip HTML for an edge — describes the two endpoints, the
    // research areas they bridge, and the underlying weight (the
    // count of facility-personnel + co-author connections that
    // collapsed into this single line).
    const edgeTipHtml = (e, kind) => {
      const a = nodeIdx.get(e.source) || {};
      const b = nodeIdx.get(e.target) || {};
      const aArea = areaName.get(a.area_id) || a.area_id || '';
      const bArea = areaName.get(b.area_id) || b.area_id || '';
      const labelKind = kind === 'pp' ? 'Co-authorship'
                      : kind === 'pf' ? 'Researcher ↔ Facility'
                      :                  'Facility ↔ Facility';
      const aLine = `${escapeHtml(a.name || a.id || '?')} <small style="color:#64748b">(${a.kind || '?'}, ${escapeHtml(aArea)})</small>`;
      const bLine = `${escapeHtml(b.name || b.id || '?')} <small style="color:#64748b">(${b.kind || '?'}, ${escapeHtml(bArea)})</small>`;
      const wLabel = kind === 'pp' ? `${e.w} co-publication${e.w === 1 ? '' : 's'}`
                   : kind === 'pf' ? `${e.w} shared author${e.w === 1 ? '' : 's'} / appointment${e.w === 1 ? '' : 's'}`
                   :                  `${e.w} shared connection${e.w === 1 ? '' : 's'}`;
      return `<strong>${labelKind}</strong>`
           + `<br>${aLine}`
           + `<br>${bLine}`
           + `<br><small style="color:#0c4a6e">bridges <em>${escapeHtml(aArea)}</em> ↔ <em>${escapeHtml(bArea)}</em></small>`
           + `<br><small>${wLabel}</small>`;
    };
    // Draws TWO line layers per bucket: an invisible wide "hit"
    // line for hover precision (thin strokes are otherwise nearly
    // impossible to hover with a mouse), and the visible coloured
    // line on top. Hover handlers live on the hit line.
    const drawEdges = (cls, arr, stroke, opacity, baseW, kind) => {
      if (!arr.length) return;
      const g = root.append('g').attr('class', cls).attr('fill', 'none');

      // Invisible hit line — wide, transparent, clickable.
      g.append('g').attr('class', `${cls}-hit`)
        .attr('stroke', 'transparent')
        .attr('stroke-width', 8)
        .attr('stroke-linecap', 'round')
        .style('pointer-events', 'stroke')
        .style('cursor', 'help')
        .selectAll('line').data(arr).enter().append('line')
        .attr('x1', (e) => (nodeIdx.get(e.source) || {}).x)
        .attr('y1', (e) => (nodeIdx.get(e.source) || {}).y)
        .attr('x2', (e) => (nodeIdx.get(e.target) || {}).x)
        .attr('y2', (e) => (nodeIdx.get(e.target) || {}).y)
        .on('mouseenter', function (ev, e) {
          showTip(tip, ev, edgeTipHtml(e, kind));
          // Highlight the matching VISIBLE line so the user can see
          // which one they're inspecting.
          const idx = arr.indexOf(e);
          d3.select(this.parentNode.parentNode)
            .select(`g.${cls}-vis`).selectAll('line')
            .attr('stroke-opacity', (_, i) => i === idx ? Math.min(1, opacity * 3) : opacity);
        })
        .on('mousemove', function (ev, e) { showTip(tip, ev, edgeTipHtml(e, kind)); })
        .on('mouseleave', function () {
          hideTip(tip);
          d3.select(this.parentNode.parentNode)
            .select(`g.${cls}-vis`).selectAll('line')
            .attr('stroke-opacity', opacity);
        });

      // Visible coloured line — pointer-events none so the wide hit
      // line below it owns the cursor interactions.
      g.append('g').attr('class', `${cls}-vis`)
        .attr('stroke', stroke)
        .attr('stroke-opacity', opacity)
        .style('pointer-events', 'none')
        .selectAll('line').data(arr).enter().append('line')
        .attr('x1', (e) => (nodeIdx.get(e.source) || {}).x)
        .attr('y1', (e) => (nodeIdx.get(e.source) || {}).y)
        .attr('x2', (e) => (nodeIdx.get(e.target) || {}).x)
        .attr('y2', (e) => (nodeIdx.get(e.target) || {}).y)
        .attr('stroke-width', (e) => baseW + Math.log(1 + e.w) * 0.3);
    };
    if (_showFacility) drawEdges('mvg-edges-ff', buckets.ff, '#94a3b8', 0.18, 0.35, 'ff');
    if (_showFacility && _showPerson) drawEdges('mvg-edges-pf', buckets.pf, '#7dd3fc', 0.30, 0.45, 'pf');
    if (_showPerson) drawEdges('mvg-edges-pp', buckets.pp, '#0ea5e9', 0.55, 0.6, 'pp');

    // Layer 3: nodes. Researchers now render as NAME LABELS (top-N
    // per area by composite importance) so the map looks like the
    // UArizona KMap reference image. Lower-importance researchers
    // still get small dots so they're not invisible. Facilities stay
    // as small dots at their bubble centres.
    //
    // Top-N per area is computed from the composite weight set in
    // membersOfArea(). Font-size scales by sqrt(importance) so the
    // most-prominent name is biggest.
    const facNodes = _layout.nodes.filter((n) => n.kind === 'facility');
    const perNodes = _layout.nodes.filter((n) => n.kind === 'person');

    // Bucket people by area; pick the top N labels per area.
    const PER_AREA_LABEL_LIMIT = 14;
    const personByArea = new Map();
    for (const p of perNodes) {
      if (!personByArea.has(p.area_id)) personByArea.set(p.area_id, []);
      personByArea.get(p.area_id).push(p);
    }
    const labelledIds = new Set();
    for (const list of personByArea.values()) {
      list.sort((a, b) => (b.importance || 0) - (a.importance || 0));
      for (const p of list.slice(0, PER_AREA_LABEL_LIMIT)) labelledIds.add(p.id);
    }

    if (_showFacility) {
      // When facility sub-polygons exist, place each facility's dot at
      // its sub-polygon centroid (the cleanest "this institution lives
      // here" marker). When sub-polygons are absent (small or
      // single-facility areas) fall back to the force-sim x,y. Either
      // way, dots are intentionally smaller than the person dots so
      // they read as "place markers" not data points.
      const facCentroid = (d) => {
        const sp = _layout.facPolygons && _layout.facPolygons.get(d.id);
        if (!sp || !sp.ring || sp.ring.length === 0) return [d.x, d.y];
        let cx = 0, cy = 0;
        for (const [x, y] of sp.ring) { cx += x; cy += y; }
        return [cx / sp.ring.length, cy / sp.ring.length];
      };
      _dotFacSel = root.append('g').attr('class', 'mvg-fac-dots')
        .selectAll('circle').data(facNodes).enter().append('circle')
        .attr('cx', (d) => facCentroid(d)[0])
        .attr('cy', (d) => facCentroid(d)[1])
        .attr('r', 2.6)
        .attr('fill', NODE_COLORS.facility)
        .attr('fill-opacity', 0.85)
        .attr('stroke', '#fff')
        .attr('stroke-width', 0.6)
        .style('cursor', 'pointer')
        .on('mouseenter', (ev, d) => showTip(tip, ev, nodeTipHtml(d)))
        .on('mouseleave', () => hideTip(tip))
        .on('click', (ev, d) => {
          if (_showRegistry) { setFocusFacility(d.id); return; }
          const url = d.url || d.homepage_url;
          if (url) window.open(url, '_blank', 'noopener');
        });
      // A data-producing site gets a visibly larger marker: the map is
      // meant to emphasise data, and only 24 of 200 organisations produce
      // any. onZoom counter-scales from this base.
      _dotFacSel
        .attr('r', (d) => (d.n_datasets > 0 ? 4.2 : 2.6))
        .attr('stroke', (d) => (d.n_datasets > 0 ? '#f8fafc' : '#fff'))
        .attr('stroke-width', (d) => (d.n_datasets > 0 ? 1.1 : 0.6));
      _dotFacSel.each(function (d) {
        d.__baseR = d.n_datasets > 0 ? 4.2 : 2.6;
      });
    } else {
      _dotFacSel = null;
    }

    if (_showPerson) {
      // Small dots for non-labelled people. Radius now scales with
      // composite importance (sqrt-damped) so junior researchers stay
      // readable at ~1.6 px while heavy-collab/well-funded researchers
      // sit at ~3.5 px before their name kicks in. Stroke-width also
      // scales so the white halo doesn't dominate the small dots.
      const dotPeople = perNodes.filter((p) => !labelledIds.has(p.id));
      const dotRadius = (d) => {
        const w = d.importance || 0;
        return 1.6 + Math.min(2.2, Math.sqrt(w) * 0.55);
      };
      _dotPersonSel = root.append('g').attr('class', 'mvg-per-dots')
        .selectAll('circle').data(dotPeople).enter().append('circle')
        .attr('cx', (d) => d.x).attr('cy', (d) => d.y)
        .attr('r', dotRadius)
        .attr('fill', NODE_COLORS.person)
        .attr('fill-opacity', 0.75)
        .attr('stroke', '#fff')
        .attr('stroke-width', 0.5)
        .style('cursor', 'pointer')
        .on('mouseenter', (ev, d) => showTip(tip, ev, nodeTipHtml(d)))
        .on('mouseleave', () => hideTip(tip))
        .on('click', (ev, d) => onPersonClick(d));
      // Tag base radius so onZoom can counter-scale per-dot.
      _dotPersonSel.each(function (d) { d.__baseR = dotRadius(d); });

      // Name labels for the top N per area. We store the BASE font
      // size on the datum so onZoom() can rescale relative to it.
      const labelPeople = perNodes.filter((p) => labelledIds.has(p.id));
      // SCREEN-px size, not an SVG font-size. Range floor is
      // PERSON_LABEL_PX.min = 10.0 px, above LABEL_MIN_PX, so a person
      // label is either drawn at >= 10 px on screen or hidden by its band.
      // The old range was 8–12 SVG units, which rendered 4.0–6.0 px at the
      // initial fit (k = 0.5) — 5.0 px at the middle of the range.
      const maxImportance = Math.max(1, ...labelPeople.map((d) => d.importance || 0));
      const personBaseFont = (d) =>
        rampPx(PERSON_LABEL_PX, d.importance || 0, maxImportance);
      _labelSel = root.append('g').attr('class', 'mvg-per-labels')
        .attr('text-anchor', 'middle')
        .attr('font-family', 'system-ui, sans-serif')
        .selectAll('text').data(labelPeople).enter().append('text')
        .attr('x', (d) => d.x).attr('y', (d) => d.y)
        .attr('font-size', personBaseFont)
        .attr('font-weight', 500)
        .attr('fill', '#0c4a6e')
        .attr('stroke', '#fff')
        .attr('stroke-width', 2.4)
        .attr('stroke-linejoin', 'round')
        .attr('paint-order', 'stroke')
        .style('cursor', 'pointer')
        .text((d) => shortName(d.name))
        .on('mouseenter', (ev, d) => showTip(tip, ev, nodeTipHtml(d)))
        .on('mouseleave', () => hideTip(tip))
        .on('click', (ev, d) => onPersonClick(d));
      // Tag each label with its target SCREEN size so onZoom can divide
      // by the measured world→screen scale. __baseFont is kept as an alias
      // because cullSelection ranks by it.
      _labelSel.each(function (d) {
        d.__basePx = personBaseFont(d);
        d.__baseFont = d.__basePx;
      });
    } else {
      _labelSel = null;
      _dotPersonSel = null;
    }

    // Layer 3.4: REGISTRY layer container. Appended once, last, so a focus
    // change only swaps this group's children. stage.innerHTML = '' above
    // already discarded the previous SVG, so the stale selections must be
    // dropped before we repopulate.
    //
    // Paint order: SVG has no z-index, so document order decides. This group
    // is appended after the person-label group and therefore paints ABOVE
    // those labels, not below them. That is the intended reading for the
    // ribbons and site markers — they are the layer the user just switched
    // on — but it does mean a marker can occlude a person label beneath it.
    // Do not "fix" this by reordering without checking the ribbons still
    // read against the cartogram fill.
    _regSiteLinkSel = null;
    _regNodeSel = null;
    _regNodeLinkSel = null;
    _regRootG = root.append('g').attr('class', 'mvg-registry');
    if (_showRegistry) {
      if (_registry) {
        drawRegistryLayer(d3, tip);
      } else {
        // Data not in yet (first switch-on races the first paint).
        // Fetch, then draw into the group we just created — but only if
        // the layer is still on and the group is still the live one.
        const g = _regRootG;
        ensureRegistry().then(() => {
          if (_showRegistry && _regRootG === g) {
            drawRegistryLayer(d3, tip);
            onZoom(_zoomK);
            updateStatus();
          }
        }).catch((err) => {
          console.error('[mvg] registry layer failed', err);
          const s = _container.querySelector('#net-reg-note');
          if (s) s.textContent = 'Researcher layer unavailable — registry query failed.';
        });
      }
    } else {
      renderRegistryPanel(null);
    }

    // Layer 3.5: FACILITY NAME LABELS. Same progressive-reveal +
    // collision-culling logic as person labels — at the default zoom
    // only the largest institutions' names are visible, and as the
    // user zooms in more facility names appear because their world-
    // space bbox shrinks. Placed at sub-polygon centroid, font sized
    // by sqrt(n_people). Hidden entirely below k = 0.7 (would
    // overcrowd the default frame).
    if (_showFacility && _layout.facPolygons && _layout.facPolygons.size) {
      // Label weight = people here + datasets produced, so a data provider
      // is labelled ahead of a similarly-staffed site that ships nothing
      // (cullSelection ranks by __baseFont, i.e. by this size).
      const facLabWeight = (sp) =>
        (sp.n_people || 0) + W_DATASET * (sp.n_datasets || 0);
      const facLabMax = Math.max(1, ...[..._layout.facPolygons.values()]
        .map(facLabWeight));
      const facLabData = [..._layout.facPolygons.entries()].map(([id, sp]) => {
        const ring = sp.ring || [];
        let cx = 0, cy = 0;
        for (const [x, y] of ring) { cx += x; cy += y; }
        if (ring.length) { cx /= ring.length; cy /= ring.length; }
        const display = sp.acronym && sp.acronym.length <= 8
          ? sp.acronym
          : shortFacilityName(sp.name);
        return {
          id, x: cx, y: cy,
          display, name: sp.name, acronym: sp.acronym,
          country: sp.country, f_type: sp.f_type, url: sp.url,
          n_people: sp.n_people || 0, area_id: sp.area_id,
          n_datasets: sp.n_datasets || 0,
          // SCREEN px. The old range was 7–11 SVG units and the class was
          // hidden below k = 0.7. Under the old scale (min(1, 1/max(k,0.5)),
          // so = 1 for every k < 1) the on-screen size was base*k below k=1
          // and base above it: the SMALL end rendered 4.9 px at k = 0.7,
          // 5.6 px at k = 0.8 and never exceeded 7.0 px at ANY zoom, so the
          // majority of this class sat under a 9.8 px floor everywhere; only
          // the large end (11 px) cleared the floor, and only from k ≈ 0.9.
          // The floor is now FAC_LABEL_PX.min = 10.5 px on screen.
          baseFont: rampPx(FAC_LABEL_PX, facLabWeight(sp), facLabMax),
        };
      });
      _facLabelSel = root.append('g').attr('class', 'mvg-fac-labels')
        .attr('text-anchor', 'middle')
        .attr('font-family', 'system-ui, sans-serif')
        .attr('pointer-events', 'none')
        .selectAll('text').data(facLabData).enter().append('text')
        .attr('x', (d) => d.x).attr('y', (d) => d.y)
        .attr('font-size', (d) => d.baseFont)
        .attr('font-weight', 600)
        .attr('fill', '#0f172a')
        .attr('stroke', '#fef9f0')
        .attr('stroke-width', 1.8)
        .attr('stroke-linejoin', 'round')
        .attr('paint-order', 'stroke')
        .text((d) => d.display);
      _facLabelSel.each(function (d) {
        d.__basePx = d.baseFont;
        d.__baseFont = d.baseFont;
      });
    } else {
      _facLabelSel = null;
    }

    // Initial label-visibility/scaling pass so the very-first paint
    // matches whatever zoom level we're starting at.
    onZoom(_zoomK);
    // Populate the left-hand TOC of research areas (clickable to zoom).
    populateToc();

    // Layer 4: polygon (research area) labels. Captured into _areaLabelSel
    // so onZoom() can counter-scale them just like the person labels —
    // otherwise the area names balloon at high zoom.
    const labelG = root.append('g').attr('class', 'mvg-labels')
      .attr('text-anchor', 'middle')
      .attr('font-family', 'system-ui, sans-serif')
      .attr('pointer-events', 'none');
    const areaMaxW = Math.max(1, ...areaList.map((a) => a.weight || 0));
    const areaLabData = areaList
      .filter((a) => _layout.labels.has(a.id))
      .map((a) => {
        const lab = _layout.labels.get(a.id);
        return {
          id: a.id, name: lab.name, x: lab.x, y: lab.y,
          // SCREEN px, 13.0–20.0. The old range was 10–16 SVG units, which
          // rendered 5.0–8.0 px at the initial fit (k = 0.5) — 6.5 px at the
          // middle of the range — because the counter-scale clamped to 1 for
          // every k < 1 and the transform then shrank the text. Sized by the
          // data-and-people weight, so the biggest name belongs to the
          // region carrying the most observatories, datasets and people.
          baseFont: rampPx(AREA_LABEL_PX, a.weight || 0, areaMaxW),
        };
      });
    _areaLabelSel = labelG.selectAll('text').data(areaLabData).enter().append('text')
      .attr('x', (d) => d.x).attr('y', (d) => d.y)
      .attr('font-size', (d) => d.baseFont)
      .attr('font-weight', 600)
      .attr('fill', '#1f2937')
      .attr('stroke', '#fff')
      .attr('stroke-width', 2.2)
      .attr('stroke-linejoin', 'round')
      .attr('paint-order', 'stroke')
      .text((d) => d.name);
    _areaLabelSel.each(function (d) {
      d.__basePx = d.baseFont;
      d.__baseFont = d.baseFont;
    });

    // Layer 5: protected-area aggregate chips (opt-in layer). Drawn last
    // so the chip sits above the region fill, and only when the layer is
    // switched on. One chip per region — never one mark per protected area.
    drawProtectedLayer(root);

    // Final sizing pass now that every label class exists AND the SVG is
    // in the document, so getScreenCTM() returns the real composed scale.
    // The earlier onZoom() call above ran before the area labels existed.
    onZoom(_zoomK);
  } catch (err) {
    console.error('[mvg] render failed', err);
    if (statusEl) statusEl.textContent = `Knowledge map render failed: ${err.message}`;
  }
}


// Status line. The registry clause is appended only once the layer's
// data is in, and it names the tier explicitly — the map shows the
// 10,000-row core tier of person_registry, of which only the
// ROR-linkable subset has a position here.
function updateStatus() {
  const statusEl = _container && _container.querySelector('#net-status');
  if (!statusEl || !_layout) return;
  const nOrg = _layout.areas.reduce((s, a) => s + (a.n_org || 0), 0);
  const nDs  = _layout.areas.reduce((s, a) => s + (a.n_datasets || 0), 0);
  const nPa  = _layout.areas.reduce((s, a) => s + (a.n_protected || 0), 0);
  let html = `<strong>${_layout.areas.length}</strong> research-area polygons`
    + ` sized by data and people, `
    + `<strong>${nOrg}</strong> organisations, `
    + `<strong>${nDs}</strong> dataset links, `
    + `<strong>${_layout.crossEdges.length}</strong> cross-area edges`;
  if (_layout.nAreasEmpty) {
    html += ` · <strong>${_layout.nAreasEmpty}</strong> vocabulary terms carry`
      + ' no organisation, dataset or person and have no region';
  }
  if (nPa) {
    html += ` · <strong>${nPa}</strong> protected areas catalogued but excluded`
      + ' from region size (no datasets, no researchers)';
  }
  if (_showRegistry && _registry) {
    const t = _registry.totals;
    // Placement split, stated as a split. n_sited people have a physical
    // position; n_domain are placed by science domain and have none;
    // n_unplaced have neither and are not on the map at all.
    html += ` · registry placement: <strong>${t.n_sited}</strong> at`
      + ` <strong>${t.n_sites}</strong> sites,`
      + ` <strong>${t.n_domain}</strong> by science domain across`
      + ` <strong>${t.n_domain_regions}</strong> regions (no site),`
      + ` <strong>${t.n_unplaced}</strong> of ${t.n_registry} not placeable`
      + ` · <strong>${t.n_site_edges}</strong> site↔site co-publication links`;
  }
  statusEl.innerHTML = html;
}


// ── Tooltip helpers ─────────────────────────────────────────────────
function ensureTooltip() {
  let t = _container.querySelector('.network-tooltip');
  if (!t) {
    t = document.createElement('div');
    t.className = 'network-tooltip';
    t.style.display = 'none';
    _container.appendChild(t);
  }
  return t;
}
function showTip(t, ev, html) {
  t.innerHTML = html;
  t.style.display = 'block';
  t.style.left = `${ev.clientX + 14}px`;
  t.style.top  = `${ev.clientY + 14}px`;
}
function hideTip(t) { t.style.display = 'none'; }

function nodeTipHtml(d) {
  if (d.kind === 'facility') {
    const sub = [d.acronym, d.country, (d.f_type || '').replace(/-/g, ' ')]
      .filter(Boolean).join(' · ');
    return `<strong>${escapeHtml(d.name)}</strong>` +
      (sub ? `<br><small>${escapeHtml(sub)}</small>` : '') +
      (d.url ? '<br><small style="color:#7dd3fc">click to open website</small>' : '');
  }
  // Person tooltip: name + role(s) + institution(s), then the
  // metrics that drove their node size. We deliberately omit
  // person_id / openalex_id / orcid from the visible chrome — those
  // are used only for the click-through link below.
  const lines = [`<strong>${escapeHtml(d.name)}</strong>`];

  const affils = Array.isArray(d.affiliations) ? d.affiliations : [];
  if (affils.length) {
    // Show up to 2 affiliations; collapse the rest into "+N more".
    const shown = affils.slice(0, 2);
    for (const a of shown) {
      const role = a.title || a.role || '';
      const fac  = a.facility || '';
      lines.push(`<small>${escapeHtml(role)}${role && fac ? '<br>' : ''}${escapeHtml(fac)}</small>`);
    }
    if (affils.length > shown.length) {
      lines.push(`<small style="color:#94a3b8">+${affils.length - shown.length} more affiliation${affils.length - shown.length === 1 ? '' : 's'}</small>`);
    }
  }

  const metrics = [];
  if (d.n_pubs)   metrics.push(`${d.n_pubs} pubs`);
  if (d.n_coauth) metrics.push(`${d.n_coauth} co-authors`);
  if (d.facility_funding_usd) {
    const m = d.facility_funding_usd / 1e6;
    metrics.push(`$${m >= 100 ? Math.round(m) : m.toFixed(1)}M facility funding`);
  }
  if (metrics.length) {
    lines.push(`<small style="color:#7dd3fc">${metrics.join(' · ')}</small>`);
  }
  return lines.join('<br>');
}

// Region tooltip. States the sizing metric and the terms that produced it,
// so the reason one region is bigger than another is inspectable from the
// map instead of buried in this file. The protected-area count is shown
// whenever it is non-zero — including the case where it is the region's
// entire catalogue entry, which is the honest reading of "3,309 protected
// areas, no data, no people".
function areaTipHtml(a) {
  const bits = [];
  if (a.n_org) bits.push(`${a.n_org} organisation${a.n_org === 1 ? '' : 's'}`);
  if (a.n_data_providers) {
    bits.push(`${a.n_data_providers} data provider${a.n_data_providers === 1 ? '' : 's'}`);
  }
  if (a.n_datasets) bits.push(`${a.n_datasets} dataset${a.n_datasets === 1 ? '' : 's'}`);
  if (a.n_people) bits.push(`${a.n_people} people`);
  const paLine = a.n_protected
    ? `<br><small style="color:#4d7c0f">${a.n_protected} protected area${a.n_protected === 1 ? '' : 's'}`
      + ' also catalogued here — no datasets, no researchers, excluded from'
      + ' region size</small>'
    : '';
  return `<strong>${escapeHtml(a.name)}</strong>`
    + (bits.length ? `<br><small>${bits.join(' · ')}</small>` : '')
    + `<br><small style="color:#0c4a6e">region size = data-and-people weight`
    + ` ${(a.weight || 0).toFixed(1)}</small>`
    + paLine
    + '<br><small>click to zoom</small>';
}

// Tooltip for a domain-placed cohort. Says in words that these people are
// NOT at a site, because the whole risk of placing them in a region is
// that the map implies a location it does not have.
function domainCohortTipHtml(d) {
  return `<strong>${escapeHtml(d.area_name)}</strong>`
    + `<br><small>${d.n_people} registry researcher${d.n_people === 1 ? '' : 's'}`
    + ` placed by <em>dominant science domain</em></small>`
    + '<br><small style="color:#b45309">No ROR match to a catalogued site —'
    + ' these are not located here. The region is their subject, not their'
    + ' address.</small>'
    + '<br><small>click to list them</small>';
}

// Side panel for a domain-placed cohort. Reuses the registry panel slot
// but is labelled unambiguously, and the rows carry no facility.
function renderDomainPanel(coh) {
  const panel = _container && _container.querySelector('#net-reg-panel');
  if (!panel || !coh) return;
  const CAP = 200;
  const list = coh.people.slice(0, CAP);
  const rows = list.map((p) => `<li>
      <button type="button" class="mvg-reg-row" data-cid="${escapeHtml(p.canonical_id)}">
        <span class="mvg-reg-name">${escapeHtml(p.display_name)}</span>
        <span class="mvg-reg-badge is-unknown"
              title="Placed by science domain — no site link">◌</span>
      </button></li>`).join('');
  panel.hidden = false;
  panel.innerHTML = `
    <div class="mvg-reg-head">
      <h3>${escapeHtml(coh.area_name)}</h3>
      <button type="button" id="net-reg-clear" class="btn-ghost">Clear</button>
    </div>
    <p class="mvg-reg-note mvg-reg-note-domain">${coh.n_people} researcher${coh.n_people === 1 ? '' : 's'}
      placed by <strong>dominant science domain</strong>, not by location.
      None of them has a ROR match to a catalogued site, so none of them has
      a physical position on this map.</p>
    ${coh.n_people > list.length
      ? `<p class="mvg-reg-note">${list.length} of ${coh.n_people} listed here.</p>`
      : ''}
    <ol class="mvg-reg-list">${rows}</ol>`;
  panel.querySelector('#net-reg-clear')
    .addEventListener('click', () => { panel.hidden = true; panel.innerHTML = ''; });
  panel.querySelectorAll('.mvg-reg-row').forEach((btn) => {
    btn.addEventListener('click', () => {
      const p = list.find((x) => x.canonical_id === btn.dataset.cid);
      if (p) openRegistryProfile(p);
    });
  });
}

function facilityCircleTipHtml(c) {
  const sub = [c.acronym, c.country, (c.f_type || '').replace(/-/g, ' ')]
    .filter(Boolean).join(' · ');
  const peopleLine = c.n_people
    ? `<br><small style="color:#7dd3fc">${c.n_people} researcher${c.n_people === 1 ? '' : 's'} mapped here</small>`
    : '';
  // A data provider is the property the map is built to surface, so it is
  // stated first and in its own colour.
  const dataLine = c.n_datasets
    ? `<br><small style="color:#0d9488"><strong>data provider</strong> — `
      + `${c.n_datasets} dataset${c.n_datasets === 1 ? '' : 's'} produced</small>`
    : '';
  return `<strong>${escapeHtml(c.name || c.id)}</strong>` +
    (sub ? `<br><small>${escapeHtml(sub)}</small>` : '') +
    dataLine +
    peopleLine +
    (c.url ? '<br><small style="color:#7dd3fc">click to open website</small>' : '');
}
function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

// Render a researcher's name compactly: first initial + last name when
// the full name is long. Keeps map labels legible at default zoom.
function shortName(full) {
  if (!full) return '';
  const parts = String(full).trim().split(/\s+/);
  if (parts.length === 1) return parts[0];
  const last = parts[parts.length - 1];
  // If full name is short enough, keep it; else collapse to "F. Last".
  if (full.length <= 20) return full;
  return `${parts[0][0]}. ${last}`;
}

// Render a facility name compactly. Long institutional names ("Virginia
// Institute of Marine Science") would never fit inside their sub-
// polygon, so when the name is over ~22 chars and an acronym isn't
// available we drop boilerplate (Institute / University / etc.) and
// fall back to the first 18 chars.
function shortFacilityName(full) {
  if (!full) return '';
  const s = String(full).trim();
  if (s.length <= 22) return s;
  // Strip common boilerplate words to shorten without disambiguation
  // loss.
  const slim = s
    .replace(/\b(Institute|Institution|University|Department|Center|Centre|of|the|for|National|Marine|Coastal|Research|Laboratory|Lab)\b/g, '')
    .replace(/\s{2,}/g, ' ')
    .trim();
  if (slim && slim.length <= 22) return slim;
  return s.slice(0, 18) + '…';
}

// Click on a researcher → take the user to their detail card in the
// People directory tab (which we ship as #/people/<person_id>). The
// directory page handles loading the per-person record.
function onPersonClick(d) {
  if (!d || !d.id) return;
  location.hash = `#/people/${encodeURIComponent(d.id)}`;
}

// World→screen scale of the map root, i.e. how many CSS pixels one SVG
// user unit occupies. It is the PRODUCT of two factors:
//
//   * the viewBox fit — the SVG has width/height 100% and a computed
//     viewBox, so preserveAspectRatio scales world units to the stage
//     independently of zoom. This factor is NOT 1 in general, and the old
//     code ignored it entirely;
//   * the d3.zoom factor k.
//
// Measuring the composed transform off the DOM is the only way to be
// right about both. Falls back to k when the CTM is unavailable (detached
// node, jsdom, a browser mid-layout), which reproduces the old assumption
// rather than throwing.
function worldToScreenScale() {
  const node = _rootG && _rootG.node && _rootG.node();
  if (node && typeof node.getScreenCTM === 'function') {
    try {
      const m = node.getScreenCTM();
      // Uniform scale: preserveAspectRatio 'meet' and d3.zoom are both
      // isotropic, so |a| is the whole story. Guard against 0 (hidden
      // element) and against a stale matrix during a transition.
      if (m && Math.abs(m.a) > 1e-6) return Math.abs(m.a);
    } catch (_) { /* fall through to the k-only estimate */ }
  }
  return _zoomK > 0 ? _zoomK : 1;
}

// Show / hide / rescale every label class in response to zoom level.
//
// TRUE CONSTANT SCREEN SIZE: each class declares the size it wants ON THE
// SCREEN (the *_LABEL_PX constants). The SVG font-size is that value
// divided by the measured world→screen scale, so the rendered text is the
// declared number of CSS pixels at EVERY zoom level — not just k >= 1.
//
// The predecessor computed `Math.min(1, 1 / Math.max(k, 0.5))`, which is
// min(1, >=1) = exactly 1 for every k < 1. Fonts therefore stayed at base
// size in world units and the zoom transform shrank them on screen: at the
// initial fit (k = 0.5) area labels rendered 7.0 px, person labels 5.0 px
// and facility labels were hidden. The comment claiming constant screen
// size was true only for k >= 1.
//
// HARD FLOOR: LABEL_MIN_PX (9.8 px, the site's own smallest tooltip text).
// A class whose declared size is under the floor is HIDDEN, never shrunk —
// screenPx() returns null and the caller sets display:none.
//
// ZOOM BANDS: each class is drawn only inside its ZOOM_BANDS range, so
// labels disappear both when the user is too far OUT and too far IN.
//
// COLLISION CULLING: after sizing, walk visible labels largest-first,
// measure world-space bounding boxes and hide overlaps. As the user zooms
// in, more labels survive because world-space boxes shrink.
function onZoom(k) {
  _zoomK = k || 1;
  const s = worldToScreenScale();

  // Marks (dots, ribbons) get constant apparent size too, but their world
  // growth is capped at MARK_MAX_GROWTH so zooming far out can't inflate a
  // 2.6-unit dot into a blob that swallows its sub-polygon. Text has no
  // such cap because a clipped label is useless whereas a slightly small
  // dot still reads as a position.
  const MARK_MAX_GROWTH = 2.0;
  const markScale = Math.min(MARK_MAX_GROWTH, 1 / (s > 0 ? s : 1));
  if (_dotPersonSel) {
    _dotPersonSel.attr('r', (d) => (d.__baseR || 2.0) * markScale);
  }
  if (_dotFacSel) {
    _dotFacSel.attr('r', (d) => (d.__baseR || 2.6) * markScale);
  }
  if (_regNodeSel) {
    _regNodeSel.attr('r', (d) => (d.__baseR || 2.5) * markScale)
      .attr('stroke-width', 0.7 * markScale);
  }
  if (_regSiteLinkSel) {
    _regSiteLinkSel.attr('stroke-width', (e) => (e.__baseW || 1) * markScale);
  }
  if (_regNodeLinkSel) {
    _regNodeLinkSel.attr('stroke-width', (e) => (e.__baseW || 0.5) * markScale);
  }

  // One helper per class so the band test, the floor test and the halo
  // scaling can never drift apart between classes.
  const applyLabels = (sel, band, haloPx, cull) => {
    if (!sel) return;
    if (!inBand(band, _zoomK)) { sel.style('display', 'none'); return; }
    let anyVisible = false;
    sel.each(function (d) {
      const fs = screenPx(d.__basePx, s);
      if (fs == null) { this.style.display = 'none'; return; }
      anyVisible = true;
      this.style.display = '';
      this.setAttribute('font-size', fs);
      this.setAttribute('stroke-width', haloPx / (s > 0 ? s : 1));
    });
    if (anyVisible && cull) cullSelection(sel);
  };

  applyLabels(_areaLabelSel,      ZOOM_BANDS.area,      2.2, false);
  applyLabels(_paLabelSel,        ZOOM_BANDS.protected, 2.0, false);
  // Cohort captions share the region band: they annotate a region, so they
  // belong on screen exactly while region names are.
  applyLabels(_regDomainLabelSel, ZOOM_BANDS.area,      2.0, false);
  applyLabels(_facLabelSel,   ZOOM_BANDS.facility,  1.8, true);
  applyLabels(_regLabelSel,   ZOOM_BANDS.registry,  2.0, true);
  applyLabels(_labelSel,      ZOOM_BANDS.person,    2.0, true);
}

// Hide labels whose world-space bounding boxes overlap higher-priority
// labels. Higher priority = larger base font. Generalised to take ANY
// selection so person labels and facility labels share the same logic
// while remaining INDEPENDENT (each cull pass operates on its own
// bucket — facility labels overlapping person labels is fine because
// they read as different colours / weights).
function cullSelection(sel) {
  if (!sel) return;
  const nodes = sel.nodes();
  const order = nodes.map((_, i) => i)
    .sort((a, b) => (nodes[b].__data__.__baseFont || 0)
                  - (nodes[a].__data__.__baseFont || 0));
  const placed = [];
  for (const i of order) {
    const el = nodes[i];
    // A label hidden by the legibility floor must STAY hidden: this loop
    // clears display before measuring, which would otherwise resurrect it
    // at a sub-floor size. Every current class declares a base >= the
    // floor, so this is a guard against a future range being lowered, not
    // a live case.
    const basePx = el.__data__ && el.__data__.__basePx;
    if (basePx != null && !(basePx >= LABEL_MIN_PX)) {
      el.style.display = 'none';
      continue;
    }
    el.style.display = '';
    let bb;
    try { bb = el.getBBox(); }
    catch (_) { continue; }
    const r = { x: bb.x - 2, y: bb.y - 2,
                w: bb.width + 4, h: bb.height + 4 };
    let hit = false;
    for (const p of placed) {
      if (r.x < p.x + p.w && r.x + r.w > p.x
       && r.y < p.y + p.h && r.y + r.h > p.y) { hit = true; break; }
    }
    if (hit) el.style.display = 'none';
    else placed.push(r);
  }
}

// Programmatic zoom-to-polygon. Computes the polygon's bounding box,
// then animates a d3.zoom transform that fits it (with margin) into
// the SVG viewport.
function zoomToArea(areaId) {
  if (!_zoomBehavior || !_zoomSvg || !_layout) return;
  const ring = _layout.polygons.get(areaId);
  if (!ring) return;
  let mnX = Infinity, mnY = Infinity, mxX = -Infinity, mxY = -Infinity;
  for (const [x, y] of ring) {
    if (x < mnX) mnX = x; if (y < mnY) mnY = y;
    if (x > mxX) mxX = x; if (y > mxY) mxY = y;
  }
  const cx = (mnX + mxX) / 2, cy = (mnY + mxY) / 2;
  const polyW = mxX - mnX, polyH = mxY - mnY;
  // SVG viewBox dimensions (read from attribute).
  const vb = (_zoomSvg.attr('viewBox') || '').split(/\s+/).map(Number);
  if (vb.length !== 4) return;
  const [vbx, vby, vbw, vbh] = vb;
  const margin = 1.2;  // 20% breathing room
  const scaleX = vbw / (polyW * margin);
  const scaleY = vbh / (polyH * margin);
  const k = Math.max(0.6, Math.min(8, Math.min(scaleX, scaleY)));
  // d3.zoom transform composes as: screen = T + k * world.
  // We want world point (cx, cy) to map to viewBox center (vbx + vbw/2,
  // vby + vbh/2). So tx = vbx + vbw/2 - k * cx, similarly for ty.
  const tx = (vbx + vbw / 2) - k * cx;
  const ty = (vby + vbh / 2) - k * cy;
  if (!_d3Mod || !_d3Mod.zoomIdentity) return;
  // Build the d3 transform: identity → translate → scale composes
  // such that screen = T + k * world. We computed tx/ty above for k.
  const zoomT = _d3Mod.zoomIdentity.translate(tx, ty).scale(k);
  _zoomSvg.transition().duration(650).call(_zoomBehavior.transform, zoomT);
}


// ── Public API ──────────────────────────────────────────────────────
export function initNetworkView(container) {
  _container = container;
  _container.innerHTML = `
    <div class="network-view">
      <header class="network-header">
        <div>
          <h2>Knowledge map</h2>
          <p class="network-sub">Each polygon is one research area, sized by a
          data-and-people weight. Hover for details, click to open homepage /
          ORCID.
          <a href="#" class="network-help-toggle"
             data-net-help="open">How to read this map</a></p>
          <div class="network-help" id="net-help" hidden>
            <p><strong>Region size</strong> is a data-and-people weight —
            organisations, plus a bonus for each site that produces datasets,
            plus the datasets themselves, plus the researchers anchored there.
            It is <em>not</em> the catalogue count: 3,309 of the 3,519
            catalogued facilities are protected areas holding zero datasets and
            zero researchers, and sizing by raw count gave them 94% of the map.
            They stay catalogued behind the off-by-default <em>Protected
            areas</em> layer, drawn as one aggregate chip per region.</p>
            <p><strong>Inside a region</strong>, sub-polygons are individual
            institutions; data-producing sites carry a larger marker.
            Researchers (sky-blue) sit inside their primary institution.</p>
            <p><strong>Edges.</strong> Toggling Facilities or People also
            toggles their cross-area edges: gray = facility-facility shared
            programs, sky-blue = researchers bridging two areas
            (interdisciplinary potential). Switch on <em>Researchers &amp;
            co-authorship</em> for the person registry: amber ribbons are
            co-publication between two catalogued sites, a solid marker is a
            researcher with a ROR-matched site, and a dashed hollow ring is a
            cohort placed by science domain with <em>no</em> site. Clicking
            either opens its roster.</p>
            <p><strong>Algorithm.</strong> KMap from Hossain et al.
            GI&nbsp;'25, with hierarchical institution sub-polygons.</p>
          </div>
        </div>
        <div class="network-actions">
          <label class="net-toggle">
            <input type="checkbox" data-toggle="facility" checked>
            <span class="net-swatch" style="background:${NODE_COLORS.facility}"></span>
            Facilities
          </label>
          <label class="net-toggle">
            <input type="checkbox" data-toggle="person" checked>
            <span class="net-swatch" style="background:${NODE_COLORS.person}"></span>
            People
          </label>
          <label class="net-toggle">
            <input type="checkbox" data-toggle="registry">
            <span class="net-swatch" style="background:${NODE_COLORS.registry}"></span>
            Researchers &amp; co-authorship
          </label>
          <label class="net-toggle" title="3,309 catalogued protected areas.
They hold no datasets and no researchers, so they do not size any region.">
            <input type="checkbox" data-toggle="protected">
            <span class="net-swatch net-swatch-pa"
                  style="background:${NODE_COLORS.protected}"></span>
            Protected areas (context)
          </label>
          <button id="net-restart" class="btn-ghost" title="Recompute layout from scratch">Recompute layout</button>
        </div>
      </header>
      <p id="net-reg-note" class="network-scope-note" hidden>
        The researcher layer draws the <strong>core tier</strong> of the person
        registry — 10,000 of 152,008 unified identities. It is not the whole
        field. Placement is in two kinds and they mean different things:
        <strong>263</strong> researchers have a ROR match to a catalogued site
        and are drawn as solid markers <em>at</em> that site;
        <strong>164</strong> have no site match and are placed by their dominant
        science domain, shown as a dashed hollow ring per region labelled
        “N no site” — a claim about their subject, not their address; the
        remaining <strong>9,573</strong> have neither and are not on the map.
        Marker size is coastal <em>output volume</em>
        (an upper bound, not a paper count); click a site or a cohort ring to
        list who is in it.
      </p>
      <div id="net-status" class="network-status">Loading…</div>
      <div class="mvg-shell">
        <aside id="net-toc" class="mvg-toc" aria-label="Research areas">
          <h3>Research areas</h3>
          <ol id="net-toc-list" class="mvg-toc-list"><li class="mvg-toc-empty">Loading…</li></ol>
          <button id="net-toc-reset" class="btn-ghost" type="button">Reset zoom</button>
        </aside>
        <div id="net-stage" class="network-stage"></div>
        <aside id="net-reg-panel" class="mvg-reg-panel"
               aria-label="Researchers at the selected site" hidden></aside>
      </div>
    </div>`;

  _container.querySelectorAll('.net-toggle input').forEach((el) => {
    el.addEventListener('change', () => {
      const k = el.dataset.toggle;
      if (k === 'registry') {
        // The registry layer owns exactly one <g>, so it never needs the
        // full re-render the other two toggles trigger — switching it
        // redraws that group and nothing else.
        _showRegistry = el.checked;
        const note = _container.querySelector('#net-reg-note');
        if (note) note.hidden = !_showRegistry;
        if (!_showRegistry) {
          _focusFacility = null;
          if (_d3Mod) drawRegistryLayer(_d3Mod, ensureTooltip());
          renderRegistryPanel(null);
          updateStatus();
          return;
        }
        ensureRegistry().then(() => {
          if (!_showRegistry || !_d3Mod) return;
          drawRegistryLayer(_d3Mod, ensureTooltip());
          onZoom(_zoomK);
          updateStatus();
        }).catch((err) => {
          console.error('[mvg] registry layer failed', err);
          if (note) {
            note.hidden = false;
            note.textContent = 'Researcher layer unavailable — the registry '
              + 'query failed. See the console for details.';
          }
        });
        return;
      }
      if (k === 'protected') {
        // Aggregate-only layer over data already in _layout, so it needs
        // neither a fetch nor a re-render — redraw its own <g> and resize.
        _showProtected = el.checked;
        if (_rootG) { drawProtectedLayer(_rootG); onZoom(_zoomK); }
        return;
      }
      if (k === 'facility') _showFacility = el.checked;
      else if (k === 'person') _showPerson = el.checked;
      // Toggle changes don't need a re-layout — just re-render.
      render().catch((err) => console.error(err));
    });
  });
  // The "how to read this map" explanation is collapsed by default: inline
  // it ran to four paragraphs and pushed the map itself below the fold.
  const helpLink = _container.querySelector('.network-help-toggle');
  const helpBox  = _container.querySelector('#net-help');
  if (helpLink && helpBox) {
    helpLink.addEventListener('click', (ev) => {
      ev.preventDefault();
      helpBox.hidden = !helpBox.hidden;
      helpLink.textContent = helpBox.hidden
        ? 'How to read this map' : 'Hide guide';
    });
  }
  _container.querySelector('#net-restart').addEventListener('click', () => {
    // Recomputing the layout moves every site anchor, so the registry
    // layer's geometry and its focus selection are both invalid.
    invalidateNetworkData();
    render().catch((err) => console.error(err));
  });
  _container.querySelector('#net-toc-reset').addEventListener('click', () => {
    if (_zoomBehavior && _zoomSvg && _d3Mod && _d3Mod.zoomIdentity) {
      _zoomSvg.transition().duration(450)
        .call(_zoomBehavior.transform, _d3Mod.zoomIdentity);
    }
  });
}

// Populate the TOC sidebar with one row per active research area,
// sorted by facility count desc. Click → zoomToArea.
function populateToc() {
  if (!_layout || !_container || !_colorOf) return;
  const list = _container.querySelector('#net-toc-list');
  if (!list) return;
  // Ranked by the same data-and-people weight that sizes the regions, so
  // the sidebar order and the map areas agree. The count column shows the
  // three terms the reader can check — organisations / datasets / people —
  // rather than a single opaque score.
  const sorted = [..._layout.areas].sort(
    (a, b) => (b.weight || 0) - (a.weight || 0));
  list.innerHTML = sorted.map((a) => `
    <li>
      <button type="button" data-area="${escapeHtml(a.id)}" class="mvg-toc-row"
              title="weight ${(a.weight || 0).toFixed(1)} — ${a.n_org || 0} organisations, ${a.n_data_providers || 0} data providers, ${a.n_datasets || 0} datasets, ${a.n_people || 0} people${a.n_protected ? `; ${a.n_protected} protected areas excluded` : ''}">
        <span class="mvg-toc-swatch" style="background:${_colorOf.get(a.id) || '#94a3b8'}"></span>
        <span class="mvg-toc-label">${escapeHtml(a.name)}</span>
        <span class="mvg-toc-count">${a.n_org || 0}·${a.n_datasets || 0}·${a.n_people || 0}</span>
      </button>
    </li>`).join('');
  list.querySelectorAll('.mvg-toc-row').forEach((btn) => {
    btn.addEventListener('click', () => {
      zoomToArea(btn.dataset.area);
    });
  });
}

export async function renderNetworkView() {
  if (!_container) return;
  try {
    await render();
  } catch (e) {
    console.error('knowledge map render failed', e);
  }
}

export function invalidateNetworkData() {
  _layout = null;
  // The registry layer's selections point into an SVG that is about to
  // be discarded, and its geometry is derived from _layout, so drop
  // both. The fetched rows themselves stay cached — they don't depend
  // on the layout and re-querying them would be wasted work.
  _regRootG = null;
  _regSiteLinkSel = null;
  _regNodeSel = null;
  _regNodeLinkSel = null;
  _regLabelSel = null;
  _regDomainLabelSel = null;
  // The protected-area chips and every label selection point into the SVG
  // that is about to be discarded, and _rootG is that SVG's root group.
  // Leaving them set would make onZoom() operate on detached nodes and
  // drawProtectedLayer() append into a dead tree.
  _rootG = null;
  _paChipSel = null;
  _paLabelSel = null;
  _areaLabelSel = null;
  _facLabelSel = null;
  _labelSel = null;
  _dotPersonSel = null;
  _dotFacSel = null;
  _facPolySel = null;
  _focusFacility = null;
  renderRegistryPanel(null);
}
