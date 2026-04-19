import { applyFilters } from './filters.js';

let db = null;        // duckdb.AsyncDuckDB instance
let conn = null;
let fallbackFeatures = null;

const PARQUET_BASE = `${import.meta.env.BASE_URL}parquet/`;

async function fetchJson(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${path}: ${res.status}`);
  return res.json();
}

export async function loadFallback() {
  const geojson = await fetchJson(`${import.meta.env.BASE_URL}facilities.geojson`);
  fallbackFeatures = geojson.features || [];
  return fallbackFeatures;
}

export async function initDB() {
  const duckdb = await import('@duckdb/duckdb-wasm');
  const bundles = duckdb.getJsDelivrBundles();
  const bundle = await duckdb.selectBundle(bundles);
  const workerUrl = URL.createObjectURL(
    new Blob([`importScripts("${bundle.mainWorker}");`], { type: 'text/javascript' }),
  );
  const worker = new Worker(workerUrl);
  const logger = new duckdb.ConsoleLogger();
  db = new duckdb.AsyncDuckDB(logger, worker);
  await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
  URL.revokeObjectURL(workerUrl);
  conn = await db.connect();

  const tables = [
    'facilities', 'locations', 'funders', 'funding_links',
    'research_areas', 'area_links', 'networks', 'network_membership',
  ];
  for (const t of tables) {
    const url = `${PARQUET_BASE}${t}.parquet`;
    await conn.query(`CREATE OR REPLACE VIEW ${t} AS SELECT * FROM read_parquet('${url}')`);
  }
}

export async function query(filterState) {
  if (!conn) {
    return filterFallback(filterState);
  }
  const { where, params } = applyFilters(filterState);
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
           list(DISTINCT fu.name)   AS funders,
           list(DISTINCT ra.label)  AS areas,
           list(DISTINCT n.label)   AS networks
    FROM facilities f
    LEFT JOIN funding_links fl ON fl.facility_id = f.facility_id
    LEFT JOIN funders fu       ON fu.funder_id  = fl.funder_id
    LEFT JOIN area_links al    ON al.facility_id = f.facility_id
    LEFT JOIN research_areas ra ON ra.area_id   = al.area_id
    LEFT JOIN network_membership nm ON nm.facility_id = f.facility_id
    LEFT JOIN networks n       ON n.network_id  = nm.network_id
    ${where}
    GROUP BY f.facility_id, f.canonical_name, f.acronym, f.facility_type,
             f.country, f.hq_lat, f.hq_lng, f.url, f.parent_org
  `;
  const prepared = await conn.prepare(sql);
  const result = await prepared.query(...params);
  return result.toArray().map((row) => row.toJSON());
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
