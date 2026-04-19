import { initMap, renderFacilities } from './map.js';
import { initFilters } from './filters.js';
import { initDB, loadFallback, query } from './db.js';
import { initListView, renderList } from './views/list.js';
import { initStatsView, renderStats } from './views/stats.js';
import { initDocsView } from './views/docs.js';
import { initRouter, currentPath } from './router.js';

const state = {
  filters: { types: new Set(), countries: new Set(), areas: new Set(), networks: new Set(), q: '' },
  lastFeatures: [],
  setFilters(update) {
    Object.assign(this.filters, update);
    refresh();
  },
};

const statusEl = document.getElementById('status');

// ── Init map into its container ──────────────────────────────────────
const map = initMap(document.getElementById('map'), state);

// ── Init filter sidebar ──────────────────────────────────────────────
initFilters(document.getElementById('filters'), state);

// ── Init other views ─────────────────────────────────────────────────
initListView(document.getElementById('browse'));
initStatsView(document.getElementById('stats'));

// docs view is lazy-initialized on first activation

// ── Debounced search + clear button ─────────────────────────────────
const qEl = document.getElementById('q');
const qClear = document.getElementById('q-clear');

function debounce(fn, ms) {
  let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

qEl.addEventListener('input', debounce((ev) => {
  const val = ev.target.value;
  qClear.classList.toggle('visible', val.length > 0);
  state.setFilters({ q: val });
}, 200));

qClear.addEventListener('click', () => {
  qEl.value = '';
  qClear.classList.remove('visible');
  state.setFilters({ q: '' });
});

// ── Refresh: query + update active view ─────────────────────────────
async function refresh() {
  statusEl.textContent = 'Querying…';
  try {
    const features = await query(state.filters);
    state.lastFeatures = features;

    const path = currentPath() || '/';
    if (path === '/') {
      renderFacilities(features);
    } else if (path === '/browse') {
      renderList(features);
    } else if (path === '/stats') {
      renderStats(features);
    }
    statusEl.textContent = `${features.length.toLocaleString()} facilities shown`;
  } catch (err) {
    console.error(err);
    statusEl.textContent = `Query failed: ${err.message}`;
  }
}

// ── View sections ────────────────────────────────────────────────────
const views = {
  '/':       document.getElementById('view-map'),
  '/browse': document.getElementById('view-browse'),
  '/stats':  document.getElementById('view-stats'),
  '/docs':   document.getElementById('view-docs'),
};

// ── Router setup ────────────────────────────────────────────────────
initRouter({
  '/': () => {
    showView('/');
    renderFacilities(state.lastFeatures);
  },
  '/browse': () => {
    showView('/browse');
    renderList(state.lastFeatures);
  },
  '/stats': () => {
    showView('/stats');
    renderStats(state.lastFeatures);
  },
  '/docs': () => {
    showView('/docs');
    initDocsView(document.getElementById('docs'));
  },
});

function showView(path) {
  Object.entries(views).forEach(([p, el]) => {
    el.classList.toggle('active', p === path);
  });
}

// ── Bootstrap ───────────────────────────────────────────────────────
(async () => {
  // First paint from GeoJSON fallback
  try {
    const fallback = await loadFallback();
    state.lastFeatures = fallback;
    renderFacilities(fallback);
    statusEl.textContent = `${fallback.length.toLocaleString()} facilities (fallback)`;
  } catch (e) {
    statusEl.textContent = 'No data yet — run the ingest pipeline.';
  }

  // Load DuckDB-Wasm in the background, then re-query with full schema
  try {
    await initDB();
    await refresh();
  } catch (e) {
    console.warn('DuckDB-Wasm unavailable, staying on GeoJSON fallback.', e);
  }
})();
