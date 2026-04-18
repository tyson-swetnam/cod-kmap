import { initMap, renderFacilities } from './map.js';
import { initFilters } from './filters.js';
import { initDB, loadFallback, query } from './db.js';

const state = {
  filters: { types: new Set(), countries: new Set(), areas: new Set(), networks: new Set(), q: '' },
  setFilters(update) {
    Object.assign(this.filters, update);
    refresh();
  },
};

const statusEl = document.getElementById('status');
const map = initMap(document.getElementById('map'), state);
initFilters(document.getElementById('filters'), state);

document.getElementById('q').addEventListener('input', (ev) => {
  state.setFilters({ q: ev.target.value });
});

async function refresh() {
  statusEl.textContent = 'Querying…';
  try {
    const features = await query(state.filters);
    renderFacilities(features);
    statusEl.textContent = `${features.length.toLocaleString()} facilities shown`;
  } catch (err) {
    console.error(err);
    statusEl.textContent = `Query failed: ${err.message}`;
  }
}

(async () => {
  // First paint from GeoJSON
  try {
    const fallback = await loadFallback();
    renderFacilities(fallback);
    statusEl.textContent = `${fallback.length.toLocaleString()} facilities (fallback)`;
  } catch (e) {
    statusEl.textContent = 'No data yet — run the ingest pipeline.';
  }
  // Load DuckDB-Wasm in the background, then re-query with full schema.
  try {
    await initDB();
    await refresh();
  } catch (e) {
    console.warn('DuckDB-Wasm unavailable, staying on GeoJSON fallback.', e);
  }
})();
