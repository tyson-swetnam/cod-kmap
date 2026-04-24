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

export async function loadFallback() {
  const geojson = await fetchJson(`${DATA_BASE}facilities.geojson`);
  fallbackFeatures = geojson.features || [];
  return fallbackFeatures;
}

// Return the DuckDB connection only after every parquet view has been
// registered. Callers that need to run arbitrary SQL should always
// `await whenReady()` first (or null-check both conn AND ready).
export function getConn() {
  return ready ? conn : null;
}

// Await this before issuing any SQL that doesn't go through query().
// Resolves once initDB() has finished registering all parquet views.
// Rejects if initDB is never called or fails (caller then falls back).
export function whenReady() {
  return readyPromise;
}

export async function initDB() {
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
    'facilities', 'facility_types', 'locations',
    'funders', 'funding_links',
    'research_areas', 'area_links', 'networks', 'network_membership',
    // Region-side (polygons as first-class rows + spatial containment edges).
    'regions', 'region_area_links', 'facility_regions',
  ];
  for (const t of tables) {
    const url = `${PARQUET_BASE}${t}.parquet`;
    await newConn.query(`CREATE OR REPLACE VIEW ${t} AS SELECT * FROM read_parquet('${url}')`);
  }

  // Only now — after every view is live — publish the connection to the
  // rest of the app and flip the readiness flag. This closes a race where
  // early readers (e.g. the Network tab loaded before initDB finishes)
  // would hit a connection with only the first few tables registered.
  conn = newConn;
  ready = true;
  if (readyResolve) readyResolve(conn);
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
