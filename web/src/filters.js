import { fetchCSV } from './csv.js';
import { DATA_BASE as BASE } from './config.js';

const COUNTRIES = [
  'US', 'CA', 'MX', 'BZ', 'GT', 'HN', 'SV', 'NI', 'CR', 'PA',
  'CO', 'VE', 'EC', 'PE', 'CL', 'AR', 'UY', 'BR',
  'PR', 'VI', 'CU', 'JM', 'DO', 'HT', 'BS', 'KY', 'TC',
];

/** Build a collapsible facet section element. */
function makeFacetSection(id, title, bodyHtml, collapsed = false) {
  const sec = document.createElement('div');
  sec.className = 'facet-section' + (collapsed ? ' collapsed' : '');
  sec.id = id;
  sec.innerHTML = `
    <div class="facet-header">
      <h2>${title}</h2>
      <span class="facet-toggle">${collapsed ? '&#9660;' : '&#9650;'}</span>
    </div>
    <div class="facet-body">${bodyHtml}</div>
  `;
  sec.querySelector('.facet-header').addEventListener('click', () => {
    sec.classList.toggle('collapsed');
    sec.querySelector('.facet-toggle').innerHTML =
      sec.classList.contains('collapsed') ? '&#9660;' : '&#9650;';
  });
  return sec;
}

function checkbox(facet, value, label) {
  const safeVal = String(value).replace(/"/g, '&quot;');
  const safeLabel = String(label ?? value).replace(/[&<>"]/g, (c) =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  return `<label><input type="checkbox" data-facet="${facet}" data-value="${safeVal}" /> ${safeLabel}</label>`;
}

/** Build tree HTML for research_areas (parent/child) */
function buildAreaTree(rows) {
  const roots = rows.filter((r) => !r.parent_slug);
  const childMap = {};
  rows.filter((r) => r.parent_slug).forEach((r) => {
    (childMap[r.parent_slug] ??= []).push(r);
  });
  let html = '';
  for (const root of roots) {
    html += checkbox('area', root.slug, root.label);
    if (childMap[root.slug]) {
      html += '<div class="child-item">';
      html += childMap[root.slug].map((c) => checkbox('area', c.slug, c.label)).join('');
      html += '</div>';
    }
  }
  return html;
}

export async function initFilters(container, state) {
  // "Clear all" link (only clears facility-filter checkboxes, not overlays)
  const clearLink = document.createElement('a');
  clearLink.id = 'clear-all';
  clearLink.textContent = 'Clear all filters';
  clearLink.href = '#';
  clearLink.addEventListener('click', (e) => {
    e.preventDefault();
    container.querySelectorAll('input[type=checkbox]').forEach((cb) => { cb.checked = false; });
    state.setFilters({
      types: new Set(), countries: new Set(),
      areas: new Set(), networks: new Set(),
    });
  });
  container.appendChild(clearLink);

  // ── Facility type (loaded synchronously from hardcoded slugs first, labels async) ──
  const typeSlugs = [
    'federal','state','local-gov','university-marine-lab','university-institute',
    'nonprofit','foundation','network','international-federal','international-university',
    'international-nonprofit','industry','vessel','observatory','virtual',
  ];
  const typeSection = makeFacetSection(
    'f-type', 'Facility type',
    typeSlugs.map((s) => checkbox('type', s, s)).join(''),
    true,
  );
  container.appendChild(typeSection);

  // ── Country ──
  const countrySection = makeFacetSection(
    'f-country', 'Country / territory',
    COUNTRIES.map((c) => checkbox('country', c, c)).join(''),
    true,
  );
  container.appendChild(countrySection);

  // Async: load vocab CSVs, then insert research-area + network sections before type
  (async () => {
    try {
      const [areaRows, networkRows, typeRows] = await Promise.all([
        fetchCSV(`${BASE}vocab/research_areas.csv`),
        fetchCSV(`${BASE}vocab/networks.csv`),
        fetchCSV(`${BASE}vocab/facility_types.csv`),
      ]);

      // Update type labels now that we have the CSV
      const typeBody = typeSection.querySelector('.facet-body');
      typeBody.innerHTML = typeRows.map((r) => checkbox('type', r.slug, r.label)).join('');

      // Network section
      const netSection = makeFacetSection(
        'f-network', 'Network',
        networkRows.map((r) => checkbox('network', r.slug, r.label)).join(''),
        true,
      );
      container.insertBefore(netSection, typeSection);

      // Research area section
      const areaSection = makeFacetSection(
        'f-area', 'Research area',
        buildAreaTree(areaRows),
        true,
      );
      container.insertBefore(areaSection, netSection);
    } catch (e) {
      console.warn('Could not load vocab CSVs for filters:', e);
    }
  })();

  // Unified change handler
  container.addEventListener('change', (ev) => {
    const input = ev.target;
    if (!(input instanceof HTMLInputElement)) return;
    const { facet, value } = input.dataset;
    if (!facet) return;

    const keyMap = { type: 'types', country: 'countries', area: 'areas', network: 'networks' };
    const key = keyMap[facet];
    if (!key) return;
    const set = new Set(state.filters[key]);
    if (input.checked) set.add(value);
    else set.delete(value);
    state.setFilters({ [key]: set });
  });
}

export function applyFilters(filterState) {
  const clauses = [];
  const params = [];

  if (filterState.types?.size) {
    clauses.push(`f.facility_type IN (${Array.from(filterState.types).map(() => '?').join(',')})`);
    params.push(...filterState.types);
  }
  if (filterState.countries?.size) {
    clauses.push(`f.country IN (${Array.from(filterState.countries).map(() => '?').join(',')})`);
    params.push(...filterState.countries);
  }
  if (filterState.areas?.size) {
    const slugs = Array.from(filterState.areas);
    clauses.push(
      `f.facility_id IN (SELECT al.facility_id FROM area_links al ` +
      `WHERE al.area_id IN (${slugs.map(() => '?').join(',')}))`
    );
    params.push(...slugs);
  }
  if (filterState.networks?.size) {
    const slugs = Array.from(filterState.networks);
    clauses.push(
      `f.facility_id IN (SELECT nm.facility_id FROM network_membership nm ` +
      `WHERE nm.network_id IN (${slugs.map(() => '?').join(',')}))`
    );
    params.push(...slugs);
  }
  if (filterState.q) {
    clauses.push(`(lower(f.canonical_name) LIKE ? OR lower(f.acronym) LIKE ?)`);
    const q = `%${filterState.q.toLowerCase()}%`;
    params.push(q, q);
  }

  return {
    where: clauses.length ? `WHERE ${clauses.join(' AND ')}` : '',
    params,
  };
}
