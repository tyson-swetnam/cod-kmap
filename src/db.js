import { applyFilters } from './filters.js';
import { DATA_BASE } from './config.js';

let db = null;        // duckdb.AsyncDuckDB instance
let conn = null;
let ready = false;    // set once all parquet views are registered
let readyResolve = null;
const readyPromise = new Promise((r) => { readyResolve = r; });
let fallbackFeatures = null;

const PARQUET_BASE = `${DATA_BASE}parquet/`;

async function fetchJson(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

// ---------------------------------------------------------------------------
// Arrow → plain JS unwrap.
//
// DuckDB-Wasm returns LIST<STRUCT> columns as Arrow Vector objects
// (not plain JS arrays). True of 1.29.0 (core v1.1.1) and still true of
// the pinned 1.33.1-dev65.0 (core v1.5.6, apache-arrow 17): a LIST<VARCHAR>
// comes back as an Arrow Vector, Array.isArray() is false, v[0] is
// undefined and v.get(0) works. Re-check this after any version bump.
// They expose a numeric `.length` and integer-keyed access, but
// `Array.isArray()` returns false for them. This silently
// breaks views that gate on Array.isArray() (e.g. People + Network
// affiliation/area lists rendered "No facility roles recorded." even
// when the parquet had data).
//
// `arrowToPlain` walks any value returned by `row.toJSON()` and converts
// every Arrow Vector to a plain JS Array, every nested struct to a
// plain Object, and unwraps BigInts to Numbers (or strings if too big).
// Use it once after `row.toJSON()` and downstream code can treat
// everything as standard JS.
// ---------------------------------------------------------------------------

export function arrowToPlain(v) {
  if (v == null) return v;
  if (typeof v === 'bigint') {
    return (v <= Number.MAX_SAFE_INTEGER && v >= Number.MIN_SAFE_INTEGER)
      ? Number(v) : String(v);
  }
  if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') {
    return v;
  }
  if (v instanceof Date) {
    return v;
  }
  if (Array.isArray(v)) {
    // Filter out NULLs/undefineds — Arrow LIST columns can contain null
    // slots which the People view's render code is not defensive
    // against.
    return v.map(arrowToPlain).filter((x) => x != null);
  }
  // Arrow Vector / list-like object: numeric `.length`, indexable.
  // In apache-arrow ≥10 (DuckDB-Wasm 1.29 through 1.33.x all ship 17), `vector[i]`
  // returns undefined and you have to call `vector.get(i)` to read a
  // value. Try .get first, fall back to bracket access, drop null
  // entries either way.
  if (typeof v === 'object' && typeof v.length === 'number') {
    const arr = [];
    const useGet = (typeof v.get === 'function');
    for (let i = 0; i < v.length; i++) {
      const raw = useGet ? v.get(i) : v[i];
      if (raw == null) continue;
      arr.push(arrowToPlain(raw));
    }
    return arr;
  }
  if (typeof v === 'object') {
    // Plain object / Arrow struct row. Arrow struct rows expose their
    // children as own properties via the Proxy returned by row.toJSON()
    // recursively, but in some versions only `.toArray()` / `.toJSON()`
    // unwrap them. Try .toJSON if available, then fall back to
    // Object.keys.
    if (typeof v.toJSON === 'function') {
      try {
        const j = v.toJSON();
        if (j !== v) return arrowToPlain(j);
      } catch (_) { /* fall through */ }
    }
    const out = {};
    for (const k of Object.keys(v)) out[k] = arrowToPlain(v[k]);
    return out;
  }
  return v;
}

// Convenience: unwrap every column of a row object in place. Same as
// `Object.fromEntries(Object.entries(o).map(([k,v]) => [k, arrowToPlain(v)]))`
// but mutates the input for hot paths.
export function unwrapRow(o) {
  if (o == null) return o;
  for (const k of Object.keys(o)) o[k] = arrowToPlain(o[k]);
  return o;
}

export async function loadFallback() {
  const geojson = await fetchJson(`${DATA_BASE}facilities.geojson`);
  fallbackFeatures = geojson.features || [];
  return fallbackFeatures;
}

// Return the DuckDB connection only after every parquet view has been
// registered. Callers that need to run arbitrary SQL should always
// `await whenReady()` first (or null-check both conn AND ready).
// Lazy-registration state. _pendingLazyTables / _pendingHelperViews are filled
// by initDB and drained by ensureSqlTables(); _sqlTablesReady memoises the
// promise so concurrent callers share one registration pass rather than racing.
let _pendingLazyTables = null;
let _pendingHelperViews = null;
let _sqlTablesReady = null;

// Register the SQL-console-only tables and the helper views that depend on
// them. Idempotent and safe to call from several places at once: the first call
// does the work, every later call awaits the same promise.
//
// Call this before running arbitrary user SQL. Rendering views must NOT depend
// on it — if a view needs one of these tables, move that table back into the
// eager `tables` list in initDB instead of calling this from the view.
export async function ensureSqlTables() {
  if (_sqlTablesReady) return _sqlTablesReady;
  _sqlTablesReady = (async () => {
    const c = getConn();
    if (!c) throw new Error('ensureSqlTables called before initDB completed');
    if (_pendingLazyTables && _pendingLazyTables.length) {
      // Per-table tolerance, same as the eager path: one missing parquet
      // must not block the rest of the SQL console.
      await Promise.all(_pendingLazyTables.map((t) => c.query(
        `CREATE OR REPLACE VIEW ${t} AS SELECT * FROM read_parquet('${PARQUET_BASE}${t}.parquet')`,
      ).catch((err) => {
        console.warn(`[db] lazy view create failed for ${t}:`, err.message);
      })));
      _pendingLazyTables = null;
    }
    // Helper views must come after their base tables and are created
    // sequentially: some reference others, and a failure in one should not
    // abort the rest (the same tolerance the eager path had).
    if (_pendingHelperViews) {
      for (const sql of _pendingHelperViews) {
        try { await c.query(sql); }
        catch (err) { console.warn('[db] helper view create failed:', err.message); }
      }
      _pendingHelperViews = null;
    }
  })();
  // A failed pass must not poison every later attempt — clear the memo so a
  // retry (e.g. after a transient fetch failure) can try again.
  _sqlTablesReady.catch(() => { _sqlTablesReady = null; });
  return _sqlTablesReady;
}

export function getConn() {
  return ready ? conn : null;
}

// Await this before issuing any SQL that doesn't go through query().
// Resolves with the connection once initDB() has registered the parquet
// views. Never rejects: it resolves with null if initDB fails or has not
// finished within 30 s, so callers must null-check (getConn() does).
export function whenReady() {
  return readyPromise;
}

export async function initDB() {
  // 30-second timeout backstop. On a slow mobile link the parquet footer
  // fetches behind every CREATE VIEW can stall; rather than leave every
  // consumer of whenReady() hung forever, release it with null after 30 s
  // so the views fall back (filterFallback() for the map, an error message
  // elsewhere). A late-finishing init still publishes the connection.
  const timeoutId = setTimeout(() => {
    if (!ready && readyResolve) {
      console.warn('[db] init timeout — releasing whenReady() so views can fall back');
      const r = readyResolve;
      readyResolve = null;
      r(null);
    }
  }, 30000);

  try {
    const duckdb = await import('@duckdb/duckdb-wasm');
    const bundles = duckdb.getJsDelivrBundles();
    const bundle = await duckdb.selectBundle(bundles);
    const workerUrl = URL.createObjectURL(
      new Blob([`importScripts("${bundle.mainWorker}");`], { type: 'text/javascript' }),
    );
    const worker = new Worker(workerUrl);
    // Silent logger — the default ConsoleLogger streams every query-plan,
    // worker message, and parquet fetch event to the browser console at
    // INFO level, which quickly buries real warnings under hundreds of
    // {level:2, origin:4, …} entries per page load. Swap in a no-op logger
    // that only surfaces ERROR level events if DuckDB ever reports one.
    const logger = {
      log: (entry) => {
        if (entry && entry.level && entry.level <= 1) {
          console.error('[duckdb]', entry);
        }
      },
    };
    db = new duckdb.AsyncDuckDB(logger, worker);
    await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
    URL.revokeObjectURL(workerUrl);
    const newConn = await db.connect();

    const tables = [
      'facilities', 'facility_types',
      // funding_links and facility_regions are LEFT JOINed by query() below —
      // the geographic map's main data path — so they are core, not lazy.
      'funders', 'funding_links',
      'research_areas', 'area_links', 'networks', 'network_membership',
      // Region-side (polygons as first-class rows + spatial containment edges).
      // region_area_links is SQL-tab only — see lazyTables.
      'regions', 'facility_regions',
      // People-side (staff, administrators, scientists, publications,
      // co-authorship graph). Empty tables are served as zero-row parquet
      // until the enrichment scripts populate them.
      'people', 'facility_personnel', 'publications', 'authorship',
      'collaborations',
      // MVG (knowledge-map) precomputed groupings — written by
      // scripts/compute_primary_groups.py. One row per facility/person
      // assigning a single primary research area; one row per area with
      // its post-collapse status. Drives src/views/network.js.
      'facility_primary_groups', 'person_primary_groups',
      'research_areas_active',
      // Per-area dashboard metrics — written by
      // scripts/compute_area_metrics.py. Drives src/views/stats.js.
      'person_area_metrics', 'facility_area_funding',
      'funder_area_funding', 'area_coverage_matrix',
      // COD project org chart — written by scripts/build_cod_team_lake.py.
      // Drives src/views/orgchart.js (#/org).
      'cod_wbs', 'cod_team_members',
      // Coastal-science community roster — written by
      // scripts/build_community_scholars.py. No longer drives a tab of its own:
      // person_registry resolved this roster onto persistent ids and the People
      // view (#/people) reads the registry, with 'Scholar roster' as a cohort
      // filter. Kept registered because the SQL tab and schema/schema.sql both
      // still expose it as a table.
      'community_scholars',
      // Curated dataset catalogue + access endpoints — written by
      // scripts/load_coastal_datasets.py. Drives src/views/datasets.js.
      // dataset_facilities is the dataset → stewarding-site edge, derived
      // offline by scripts/link_dataset_facilities.py; it covers 39 of the
      // 72 datasets, so the view must treat an absent edge as unresolved
      // rather than as "no steward".
      'coastal_datasets', 'dataset_endpoints', 'dataset_facilities',
      // Unified person identity across the three human layers (people,
      // cod_team_members, community_scholars) plus the co-publication graph
      // over that node set — written by scripts/build_person_registry.py and
      // scripts/compute_registry_collaborations.py. Only the 'core' tier ships
      // here; the full population stays in the local DuckDB.
      // person_registry is the roster of the People view (src/views/people.js),
      // not just an enrichment join.
      'person_registry', 'person_identity_source', 'registry_collaborations',
      // Researcher ↔ catalogued-site links, joined on ROR equality — written
      // by scripts/link_registry_facilities.py.
      'registry_facilities',
      // NOTE: person_validation, coauthor_edges and coauthor_candidates were
      // originally added to THIS list. They are read by no rendering view, and
      // they are the three largest tables the site ships (15.51 MB combined), so
      // they now live in lazyTables below. See the comment there.
    ];

    // Tables NO rendering view reads — only the SQL tab's canned queries and
    // free-form console. Registering a view is not free: DuckDB-Wasm binds
    // eagerly (CREATE VIEW over a missing file throws), so each entry costs an
    // HTTP range request for the parquet footer, and the loop below awaits them
    // one at a time. Deferring these 8 removes 8 round-trips and 19.37 MB from
    // the path to first paint: 23.00 MB eager -> 3.63 MB.
    //
    // The three validation/co-author tables dominate that figure. They were
    // written by scripts/validate_registry.py and originally registered eagerly;
    // no rendering view reads any of them, so eager registration cost the browser
    // 15.51 MB before first paint — more than the whole page needed before this
    // work started. What each is:
    //
    //   person_validation    identifier-validation verdicts over person_registry,
    //                        one row per (registry row, check, run) so a
    //                        re-validation appends rather than overwrites —
    //                        filter to the newest run_id before counting.
    //                        (schema.sql defines v_person_validation_latest for
    //                        exactly this, but it is NOT in helperViews below,
    //                        so that name does not exist in the browser.)
    //   coauthor_edges       provenanced co-authorship edges over the registry
    //                        node set. Distinct from registry_collaborations:
    //                        every row names the OpenAlex Work that proves it
    //                        (exemplar_work_id) and the identifier-equality rule
    //                        that matched it (match_method). Only the core-to-core
    //                        subset ships here.
    //   coauthor_candidates  review queue of co-authors seen on registry members'
    //                        works who are not registry rows yet. Nothing here is
    //                        a personnel record until a curator promotes it.
    //
    // Anything listed here MUST be unreferenced outside src/views/sql.js.
    // Before moving a table into this list, grep ALL of src/ — not just
    // src/views/ — because src/filters.js and src/map.js read tables that no
    // view file mentions (facility_types, research_areas, area_links,
    // network_membership are used by the geographic map's filter panel).
    // Conversely, when a view LEARNS to read one of these, move it back out.
    const lazyTables = [
      'locations', 'funding_events', 'region_area_links', 'person_areas',
      'publication_topics',
      'person_validation', 'coauthor_edges', 'coauthor_candidates',
    ];

    _pendingLazyTables = lazyTables;

    // Register the core tables CONCURRENTLY. Each CREATE VIEW is an independent
    // footer read with no ordering dependency, so awaiting them sequentially
    // serialised ~31 network round-trips for no reason. Promise.all issues them
    // together and the browser's connection pool handles the rest.
    //
    // Each registration is isolated: a missing or unreadable parquet (a 404
    // after a table was added locally but never `git add -f`-staged, say) loses
    // only that table. Views that read it fail on their own query, rather than
    // one rejection aborting initDB and leaving every tab without a connection.
    await Promise.all(tables.map((t) => newConn.query(
      `CREATE OR REPLACE VIEW ${t} AS SELECT * FROM read_parquet('${PARQUET_BASE}${t}.parquet')`,
    ).catch((err) => {
      console.warn(`[db] view create failed for ${t} (parquet missing?):`, err.message);
    })));

    // Helper views the SQL tab + future visualisations rely on. These
    // are defined in schema/schema.sql for the canonical DuckDB but
    // they don't survive a parquet round-trip (you can't COPY a view to
    // parquet without materialising it; we keep them computed). Recreate
    // them in DuckDB-Wasm so the app's SQL canned queries work.
    //
    // Mirror rule: each view here carries the same SQL text as its
    // schema/schema.sql definition (change one, change both). Keep this SQL
    // to DuckDB 1.5 syntax: the pinned wasm core is v1.5.6.
    const helperViews = [
      `CREATE OR REPLACE VIEW v_facility_map AS
         SELECT f.facility_id           AS id,
                f.canonical_name        AS name,
                f.acronym               AS acronym,
                f.facility_type         AS type,
                f.country               AS country,
                f.region                AS region,
                f.hq_lat                AS lat,
                f.hq_lng                AS lng,
                f.url                   AS url,
                f.parent_org            AS parent_org
         FROM facilities f
         WHERE f.hq_lat IS NOT NULL AND f.hq_lng IS NOT NULL`,

      // Binds the lazy region_area_links, so it can only live in this
      // deferred list.
      `CREATE OR REPLACE VIEW v_region_enriched AS
         SELECT r.region_id,
                r.name,
                r.acronym,
                r.kind,
                r.network_id,
                n.label         AS network_label,
                r.url,
                r.manager,
                r.designated,
                r.state,
                r.description,
                list(DISTINCT ra.label)         AS research_areas,
                list(DISTINCT f.canonical_name) AS facilities,
                count(DISTINCT fr.facility_id)  AS facility_count
         FROM regions r
         LEFT JOIN networks n             ON n.network_id  = r.network_id
         LEFT JOIN region_area_links ral  ON ral.region_id = r.region_id
         LEFT JOIN research_areas ra      ON ra.area_id    = ral.area_id
         LEFT JOIN facility_regions fr    ON fr.region_id  = r.region_id
         LEFT JOIN facilities f           ON f.facility_id = fr.facility_id
         GROUP BY r.region_id, r.name, r.acronym, r.kind, r.network_id, n.label,
                  r.url, r.manager, r.designated, r.state, r.description`,

      `CREATE OR REPLACE VIEW v_facility_funding_by_year AS
         SELECT f.facility_id,
                f.canonical_name              AS facility,
                f.acronym,
                fe.fiscal_year,
                SUM(fe.amount_usd)            AS total_usd_nominal,
                COUNT(*)                      AS n_awards,
                list(DISTINCT fu.name)        AS funders
         FROM facilities       f
         JOIN funding_events   fe ON fe.facility_id = f.facility_id
         JOIN funders          fu ON fu.funder_id   = fe.funder_id
         WHERE fe.fiscal_year IS NOT NULL AND fe.amount_usd IS NOT NULL
         GROUP BY f.facility_id, f.canonical_name, f.acronym, fe.fiscal_year`,

      `CREATE OR REPLACE VIEW v_funder_funding_by_year AS
         SELECT fu.funder_id,
                fu.name                       AS funder,
                fu.type                       AS funder_type,
                fe.fiscal_year,
                SUM(fe.amount_usd)            AS total_usd_nominal,
                COUNT(*)                      AS n_awards,
                COUNT(DISTINCT fe.facility_id) AS n_facilities
         FROM funders         fu
         JOIN funding_events  fe ON fe.funder_id = fu.funder_id
         WHERE fe.fiscal_year IS NOT NULL AND fe.amount_usd IS NOT NULL
         GROUP BY fu.funder_id, fu.name, fu.type, fe.fiscal_year`,

      `CREATE OR REPLACE VIEW v_facility_key_personnel AS
         SELECT f.facility_id,
                f.canonical_name         AS facility,
                f.acronym                AS facility_acronym,
                p.person_id,
                p.name,
                fp.role,
                fp.title,
                p.orcid,
                p.openalex_id,
                p.homepage_url,
                fp.start_date,
                fp.source_url
         FROM facility_personnel fp
         JOIN people     p ON p.person_id   = fp.person_id
         JOIN facilities f ON f.facility_id = fp.facility_id
         WHERE fp.is_key_personnel = true
           AND (fp.end_date IS NULL OR fp.end_date > CURRENT_DATE)`,

      `CREATE OR REPLACE VIEW v_funding_ledger AS
         SELECT fe.event_id, fe.fiscal_year, fe.period_start, fe.period_end,
                fu.name AS funder, fu.type AS funder_type,
                f.canonical_name AS facility, f.acronym AS facility_acronym,
                f.facility_type  AS facility_kind, f.country,
                fe.amount_usd    AS amount_usd_nominal, fe.amount_currency,
                fe.award_id, fe.award_title, fe.program, fe.relation,
                fe.source, fe.source_url, fe.retrieved_at, fe.confidence, fe.notes
         FROM funding_events fe
         JOIN funders    fu ON fu.funder_id  = fe.funder_id
         JOIN facilities f  ON f.facility_id = fe.facility_id`,

      `CREATE OR REPLACE VIEW v_person_enriched AS
         SELECT p.person_id,
                p.name,
                p.name_family,
                p.orcid,
                p.openalex_id,
                p.homepage_url,
                p.research_interests,
                p.status,
                list(DISTINCT f.canonical_name)  AS facilities,
                list(DISTINCT fp.role)           AS roles,
                list(DISTINCT ra.label)          AS research_areas,
                COUNT(DISTINCT a.publication_id) AS n_publications,
                MAX(pub.pub_year)                AS latest_pub_year
         FROM people p
         LEFT JOIN facility_personnel fp ON fp.person_id   = p.person_id
         LEFT JOIN facilities         f  ON f.facility_id  = fp.facility_id
         LEFT JOIN person_areas       pa ON pa.person_id   = p.person_id
         LEFT JOIN research_areas     ra ON ra.area_id     = pa.area_id
         LEFT JOIN authorship         a  ON a.person_id    = p.person_id
         LEFT JOIN publications       pub ON pub.publication_id = a.publication_id
         GROUP BY p.person_id, p.name, p.name_family, p.orcid, p.openalex_id,
                  p.homepage_url, p.research_interests, p.status`,

      // Binds the lazy person_areas, like v_person_enriched above.
      `CREATE OR REPLACE VIEW v_person_areas_enriched AS
         SELECT p.person_id,
                p.name                       AS person,
                ra.area_id,
                ra.label                     AS area,
                pa.weight,
                pa.evidence_count,
                pa.source,
                f0.facilities                AS facilities
         FROM person_areas pa
         JOIN people         p  ON p.person_id  = pa.person_id
         JOIN research_areas ra ON ra.area_id   = pa.area_id
         LEFT JOIN (
             SELECT fp.person_id, list(DISTINCT f.canonical_name) AS facilities
             FROM facility_personnel fp
             JOIN facilities f ON f.facility_id = fp.facility_id
             GROUP BY fp.person_id
         ) f0 ON f0.person_id = p.person_id`,

      `CREATE OR REPLACE VIEW v_cod_team_enriched AS
         SELECT tm.member_id,
                tm.display_name,
                tm.wbs_code,
                w.title       AS wbs_title,
                w.parent_code AS wbs_parent_code,
                tm.role,
                tm.institution,
                tm.institution_slug,
                tm.is_pi,
                tm.is_copi,
                tm.is_leadership_committee,
                tm.committees,
                tm.status,
                tm.sort_order,
                w.sort_order  AS wbs_sort_order,
                p.person_id,
                p.orcid,
                p.openalex_id,
                p.google_scholar_id,
                p.homepage_url,
                p.research_interests
         FROM cod_team_members tm
         LEFT JOIN cod_wbs w ON w.wbs_code  = tm.wbs_code
         LEFT JOIN people  p ON p.person_id = tm.person_id`,
    ];
    // Helper views are NOT created here. Every one of them is read only by the
    // SQL tab, and several JOIN a lazy table (v_funding_ledger needs
    // funding_events, v_region_enriched needs region_area_links,
    // v_person_enriched and v_person_areas_enriched need person_areas), so
    // creating them eagerly would defeat the deferral — CREATE VIEW binds its
    // dependencies immediately. They are created by ensureSqlTables() alongside
    // the lazy tables, on first use of the SQL tab.
    _pendingHelperViews = helperViews;

    // Only now — after every view is live — publish the connection to the
    // rest of the app and flip the readiness flag. This closes a race where
    // early readers (e.g. the Network tab loaded before initDB finishes)
    // would hit a connection with only the first few tables registered.
    conn = newConn;
    ready = true;
    if (readyResolve) {
      const r = readyResolve;
      readyResolve = null;
      r(conn);
    }
  } catch (err) {
    // Init failed before the connection was published. Surface the error
    // but always release whenReady() so consumers can fall back instead of
    // hanging on an unresolved promise.
    console.error('[db] init failed:', err);
    if (readyResolve) {
      const r = readyResolve;
      readyResolve = null;
      r(null);
    }
    throw err;
  } finally {
    clearTimeout(timeoutId);
  }
}

export async function query(filterState) {
  if (!ready || !conn) {
    return filterFallback(filterState);
  }
  const { where, params } = applyFilters(filterState);
  // NOTE: we LEFT JOIN facility_regions + regions so every facility row
  // comes back with the list of overlay polygons it sits inside. The list
  // can be empty (e.g., an offshore research vessel that falls outside every
  // NMS / NERR / NPS / NEP / NEON / EPA polygon). This lets the popup show
  // "Inside: <sanctuary>, <EPA region>, <NEON domain>" without a second
  // round-trip for each click.
  const sql = `
    SELECT f.facility_id AS id,
           f.canonical_name AS name,
           f.acronym,
           f.facility_type AS type,
           f.country,
           f.hq_lat AS lat,
           f.hq_lng AS lng,
           f.url,
           f.parent_org,
           list(DISTINCT fu.name)        AS funders,
           list(DISTINCT ra.label)       AS areas,
           list(DISTINCT n.label)        AS networks,
           list(DISTINCT r.name)         AS regions,
           list(DISTINCT r.kind)         AS region_kinds
    FROM facilities f
    LEFT JOIN funding_links fl  ON fl.facility_id = f.facility_id
    LEFT JOIN funders fu        ON fu.funder_id  = fl.funder_id
    LEFT JOIN area_links al     ON al.facility_id = f.facility_id
    LEFT JOIN research_areas ra ON ra.area_id    = al.area_id
    LEFT JOIN network_membership nm ON nm.facility_id = f.facility_id
    LEFT JOIN networks n        ON n.network_id   = nm.network_id
    LEFT JOIN facility_regions fr ON fr.facility_id = f.facility_id
    LEFT JOIN regions r         ON r.region_id   = fr.region_id
    ${where}
    GROUP BY f.facility_id, f.canonical_name, f.acronym, f.facility_type,
             f.country, f.hq_lat, f.hq_lng, f.url, f.parent_org
  `;
  const prepared = await conn.prepare(sql);
  const result = await prepared.query(...params);

  // Emit the same GeoJSON Feature shape loadFallback() returns, so the map
  // source always sees real Features (with a geometry). If we pass raw rows
  // into a FeatureCollection, MapLibre silently drops every point because
  // the members have no `geometry`.
  return result.toArray().map((row) => {
    const o = row.toJSON();
    return {
      type: 'Feature',
      geometry: (o.lat != null && o.lng != null)
        ? { type: 'Point', coordinates: [o.lng, o.lat] }
        : null,
      properties: o,
    };
  }).filter((f) => f.geometry);
}

function filterFallback(filterState) {
  if (!fallbackFeatures) return [];
  const types = filterState.types?.size ? filterState.types : null;
  const countries = filterState.countries?.size ? filterState.countries : null;
  // areas/networks not available in GeoJSON; skip those filters in fallback mode
  const q = (filterState.q || '').toLowerCase();
  return fallbackFeatures.filter((feat) => {
    const p = feat.properties;
    if (types && !types.has(p.type)) return false;
    if (countries && !countries.has(p.country)) return false;
    if (q && !(`${p.name ?? ''} ${p.acronym ?? ''}`.toLowerCase().includes(q))) return false;
    return true;
  });
}
