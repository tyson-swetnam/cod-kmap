const FACILITY_TYPES = [
  'federal', 'state', 'local-gov',
  'university-marine-lab', 'university-institute',
  'nonprofit', 'foundation', 'network',
  'international-federal', 'international-university', 'international-nonprofit',
  'industry', 'vessel', 'observatory', 'virtual',
];

const COUNTRIES = [
  'US', 'CA', 'MX', 'BZ', 'GT', 'HN', 'SV', 'NI', 'CR', 'PA',
  'CO', 'VE', 'EC', 'PE', 'CL', 'AR', 'UY', 'BR',
  'PR', 'VI', 'CU', 'JM', 'DO', 'HT', 'BS', 'KY', 'TC',
];

export function initFilters(container, state) {
  container.innerHTML = `
    <h2>Facility type</h2>
    <div id="f-type">${FACILITY_TYPES.map(checkbox('type')).join('')}</div>
    <h2>Country / territory</h2>
    <div id="f-country">${COUNTRIES.map(checkbox('country')).join('')}</div>
  `;

  container.addEventListener('change', (ev) => {
    const input = ev.target;
    if (!(input instanceof HTMLInputElement)) return;
    const { facet, value } = input.dataset;
    if (!facet) return;
    const key = facet === 'type' ? 'types' : facet === 'country' ? 'countries' : facet;
    const set = new Set(state.filters[key]);
    if (input.checked) set.add(value);
    else set.delete(value);
    state.setFilters({ [key]: set });
  });
}

function checkbox(facet) {
  return (value) =>
    `<label><input type="checkbox" data-facet="${facet}" data-value="${value}" /> ${value}</label>`;
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
