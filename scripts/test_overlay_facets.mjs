// Exercises src/overlay-facets.js against the real manifest + vocab.
//
//     node scripts/test_overlay_facets.mjs      # exit 0 = green
//
// Regression guard (ported from the lto fork): filtering the facility
// points left the estuary (NERR/NEP) and sanctuary overlays painted, so the
// map read as unfiltered. Assertions cover both directions — the layers
// must vanish when the facets exclude them AND survive when they don't —
// plus tag-table hygiene (every id is a real overlay, every slug is in the
// vocab CSV), which is what silently rots when a layer is renamed.
// No test framework: this is plain node, and it stays off the live site
// because deploy.yml ships only index.html, src/, public/ and docs/.
import { readFileSync } from 'node:fs';
import {
  NETWORK_TAGS, TYPE_TAGS, RESULTS_DUPLICATING,
  facetsNarrowing, overlayHiddenByFilters,
} from '../src/overlay-facets.js';

const ROOT = new URL('..', import.meta.url).pathname;
const manifest = JSON.parse(readFileSync(`${ROOT}/public/overlays/manifest.json`, 'utf8'));
const slugs = (f) => new Set(readFileSync(`${ROOT}/public/vocab/${f}`, 'utf8')
  .trim().split('\n').slice(1).map((l) => l.split(',')[0]));
const networkSlugs = slugs('networks.csv');
const typeSlugs = slugs('facility_types.csv');

let fail = 0;
const ok = (cond, msg) => { if (!cond) { fail++; console.log(`FAIL ${msg}`); } else console.log(`ok   ${msg}`); };
// cod-kmap's filter state shape (src/main.js `state.filters`).
const F = (o) => ({ types: new Set(), countries: new Set(), areas: new Set(),
                    networks: new Set(), q: '', ...o });
const hidden = (id, f) => overlayHiddenByFilters(id, f);

// 1. every tagged id exists in the manifest, every tag in the vocab
for (const [id, nets] of Object.entries(NETWORK_TAGS)) {
  ok(id in manifest, `NETWORK_TAGS ${id} is a real overlay`);
  for (const n of nets) ok(networkSlugs.has(n), `network slug ${n} (${id}) is in networks.csv`);
}
for (const [id, kinds] of Object.entries(TYPE_TAGS)) {
  ok(id in manifest, `TYPE_TAGS ${id} is a real overlay`);
  for (const t of kinds) ok(typeSlugs.has(t), `facility type ${t} (${id}) is in facility_types.csv`);
}
for (const id of RESULTS_DUPLICATING) ok(id in manifest, `RESULTS_DUPLICATING ${id} is a real overlay`);

// 1b. a feature-level network_slug, where the overlay carries one, agrees
// with NETWORK_TAGS (neon-domains / epa-regions are admin context and are
// deliberately untagged, so they are skipped).
for (const id of Object.keys(NETWORK_TAGS)) {
  const gj = JSON.parse(readFileSync(`${ROOT}/public/overlays/${id}.geojson`, 'utf8'));
  const own = new Set(gj.features.map((f) => f.properties?.network_slug).filter(Boolean));
  for (const s of own) ok(NETWORK_TAGS[id].includes(s), `${id} features' network_slug ${s} is in its NETWORK_TAGS`);
}

// 2. no facets → nothing hides (the default view keeps every layer)
ok(!facetsNarrowing(F({})), 'no facets selected → not narrowing');
for (const id of Object.keys(manifest)) {
  ok(!hidden(id, F({})), `no facets → ${id} stays visible`);
}

// 3. the reported bug: pick NEON, the estuary + marine layers go away
const neonOnly = F({ networks: new Set(['neon']) });
ok(hidden('nerr-reserves', neonOnly), 'NEON network hides NERR estuary reserves');
ok(hidden('nep-programs', neonOnly), 'NEON network hides National Estuary Program');
ok(hidden('marine-sanctuaries', neonOnly), 'NEON network hides marine sanctuaries');
ok(hidden('marine-monuments', neonOnly), 'NEON network hides marine monuments');
ok(hidden('nps-coastal', neonOnly), 'NEON network hides NPS coastal units');
ok(!hidden('neon-sites', neonOnly), 'NEON network keeps NEON site polygons');
ok(!hidden('epa-regions', neonOnly), 'NEON network leaves EPA admin context alone');
ok(!hidden('neon-domains', neonOnly), 'NEON network leaves NEON domain context alone');

// 4. a network keeps its own layer (no over-hiding) and drops the others
const nerrsOnly = F({ networks: new Set(['nerrs']) });
ok(!hidden('nerr-reserves', nerrsOnly), 'NERRS network keeps estuary reserves');
ok(hidden('nep-programs', nerrsOnly), 'NERRS network hides the NEP layer');
ok(hidden('neon-sites', nerrsOnly), 'NERRS network hides NEON site polygons');
ok(hidden('coastal-fws-units', nerrsOnly), 'NERRS network hides the USFWS refuge mirror');
ok(!hidden('coastal-fws-units', F({ networks: new Set(['nwrs']) })), 'NWRS network keeps USFWS refuges');
ok(!hidden('ramsar-us', F({ networks: new Set(['ramsar']) })), 'RAMSAR network keeps Ramsar wetlands');
ok(!hidden('coastal-nps-units', F({ networks: new Set(['nps-im']) })), 'NPS-IM network keeps LRD NPS units');
ok(!hidden('coastal-nps-units', F({ networks: new Set(['nps-coastal']) })), 'NPS Coastal network keeps LRD NPS units too');
ok(!hidden('nep-programs', F({ networks: new Set(['nerrs', 'nep']) })), 'NERRS+NEP keeps the NEP layer');

// 5. NEON is not results-duplicating in cod-kmap: its facility points were
// removed (remove_neon_facility_points.py), so the polygons are the only
// NEON on the map and a type facet must not take them away.
ok(!RESULTS_DUPLICATING.has('neon-sites'), 'neon-sites is not results-duplicating');
const labOnly = F({ types: new Set(['university-marine-lab']) });
ok(!hidden('neon-sites', labOnly), 'type facet leaves NEON site polygons alone');
ok(!hidden('nerr-reserves', labOnly), 'type facet leaves NERR reserves alone');

// 6. the protected-area mirrors follow the type facet
ok(hidden('coastal-fws-units', labOnly), 'marine-lab type hides the federal protected-area mirror');
ok(hidden('coastal-state-protected', labOnly), 'marine-lab type hides the state protected-area mirror');
ok(hidden('coastal-ngo-private', labOnly), 'marine-lab type hides the private protected-area mirror');
const fedOnly = F({ types: new Set(['protected-area-federal']) });
ok(!hidden('coastal-fws-units', fedOnly), 'federal protected-area type keeps USFWS refuges');
ok(!hidden('coastal-wilderness', fedOnly), 'federal protected-area type keeps wilderness');
ok(hidden('coastal-state-protected', fedOnly), 'federal protected-area type hides state preserves');
ok(!hidden('coastal-state-protected', F({ types: new Set(['protected-area-state']) })),
   'state protected-area type keeps state preserves');

// 7. country/area/text search are not narrowing facets for overlays
ok(!facetsNarrowing(F({ countries: new Set(['US']), areas: new Set(['hydrology']), q: 'bay' })),
   'country/area/text selections do not hide overlays');

console.log(fail ? `\n${fail} FAILURE(S)` : '\nall assertions passed');
process.exit(fail ? 1 : 0);
