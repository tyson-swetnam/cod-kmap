// overlay-facets.js — which map overlays a filter selection should hide.
//
// Overlays are static context layers: they never pass through query()'s
// WHERE clause. So with the NEON network selected, the facility POINTS
// filtered correctly while every NERR reserve, NEP and sanctuary polygon
// stayed painted, which reads as unfiltered results. (Ported from the lto
// fork, cc10864, without its LTO-only sphere / ecosystem / life-zone
// facets: cod-kmap's filter state is just types, countries, areas,
// networks and q.)
//
// The decision is pure (ids + filter Sets in, boolean out) and lives here
// rather than in overlays.js so it can be exercised without MapLibre
// (scripts/test_overlay_facets.mjs). overlays.js owns the effects:
// show/hide, the muted sidebar row, and the user-override bookkeeping.

// Network slugs (schema/vocab/networks.csv) whose membership roll is what
// the layer draws. A layer hides when the network facet is active and
// selects none of its slugs. Evidence for each row:
//   * the federal estuary/marine layers carry the slug themselves, as the
//     `network_slug` property on every feature of the overlay GeoJSON;
//   * the coastal-terrestrial layers are 1:1 mirrors of facility points,
//     so they take the network those points belong to in
//     network_membership (matched on name: fws 196/196 nwrs, nps-units
//     141/141 nps-im, wilderness 67/67 nwps, usfs-special 90/90
//     usfs-rna-ef, ramsar 40/40 once the points' " (Ramsar)" suffix is
//     dropped). coastal-nps-units also keeps 'nps-coastal', the network the
//     legacy nps-coastal layer draws, since both layers show the same parks;
//   * neon-sites is NEON's own Field Sampling Boundaries.
// Deliberately untagged: epa-regions and neon-domains (pure administrative
// context — default-off, and nobody's membership roll even though the
// features carry a network_slug), and the state / NGO preserve layers,
// which belong to no network (they are governed by TYPE_TAGS instead).
export const NETWORK_TAGS = {
  'nerr-reserves':        ['nerrs'],
  'nep-programs':         ['nep'],
  'marine-sanctuaries':   ['nms'],
  'marine-monuments':     ['marine-monument'],
  'nps-coastal':          ['nps-coastal'],
  'coastal-nps-units':    ['nps-im', 'nps-coastal'],
  'coastal-fws-units':    ['nwrs'],
  'coastal-wilderness':   ['nwps'],
  'coastal-usfs-special': ['usfs-rna-ef'],
  'ramsar-us':            ['ramsar'],
  'neon-sites':           ['neon'],
};

// Facility types (schema/vocab/facility_types.csv) of the points a layer
// mirrors. The seven coastal-terrestrial layers were ingested by R11
// (scripts/coastal_research/ingest_r11.py) as one facility point per
// polygon, so a type facet that excludes those points must take the
// polygons with it. Every nwrs / nps-im / nwps / usfs-rna-ef / ramsar
// member is protected-area-federal; all 1,805 protected-area-state points
// have a same-named feature in the state layer, and all 970
// protected-area-private points one in the NGO layer. In lto this hole was closed by marking
// neon-sites results-duplicating; here the mirrored layers are tagged
// instead, so picking their own type (or network) keeps them on screen.
export const TYPE_TAGS = {
  'coastal-fws-units':       ['protected-area-federal'],
  'coastal-nps-units':       ['protected-area-federal'],
  'coastal-wilderness':      ['protected-area-federal'],
  'coastal-usfs-special':    ['protected-area-federal'],
  'ramsar-us':               ['protected-area-federal'],
  'coastal-state-protected': ['protected-area-state'],
  'coastal-ngo-private':     ['protected-area-private'],
};

// Overlays that hide under ANY narrowing facet, because each feature also
// exists as a filterable facility point and the points carry the answer.
// lto lists neon-sites here; cod-kmap does not. NEON was a facility
// point here too until scripts/coastal_research/remove_neon_facility_points.py
// dropped all 81 of them (and their network_membership rows) so that each
// site renders once, as the neon-sites polygon. public/facilities.geojson
// and facilities.parquet now hold no NEON rows and network_membership has
// no 'neon' members, so the polygons are the ONLY NEON on the map: hiding
// them under, say, a type facet would remove NEON entirely rather than
// de-duplicate it. They are governed by NETWORK_TAGS like any other layer.
// Kept (empty) so the mechanism and its test stay in step with lto.
export const RESULTS_DUPLICATING = new Set();

// Facets that narrow the facility result set. `countries`, `areas` and the
// free-text `q` are deliberately absent: an overlay is not a country or a
// research-area claim, and hiding on those would strip context the user
// still needs.
const NARROWING = ['networks', 'types'];

/** True when any facet that narrows the facility result set is active. */
export function facetsNarrowing(filters) {
  return NARROWING.some((k) => filters?.[k]?.size > 0);
}

/**
 * Should overlay `id` be hidden under this filter selection?
 * Pure — the caller applies overrides and effects.
 */
export function overlayHiddenByFilters(id, filters) {
  if (!facetsNarrowing(filters)) return false;
  if (RESULTS_DUPLICATING.has(id)) return true;

  const networks = filters?.networks;
  const nets = NETWORK_TAGS[id];
  if (nets && networks?.size && !nets.some((n) => networks.has(n))) return true;

  const types = filters?.types;
  const kinds = TYPE_TAGS[id];
  if (kinds && types?.size && !kinds.some((t) => types.has(t))) return true;

  return false;
}
