# R10 — COMPASS synthesis-networks ingest

## Scope
Import the network-of-networks dataset from Myers-Pigg et al., *Advancing the
understanding of coastal disturbances with a network-of-networks approach*
(Ecosphere), released at https://github.com/COMPASS-DOE/synthesis-networks.

The dataset covers 52 observation, experiment, and monitoring networks spanning
terrestrial, freshwater, marine, atmospheric, and coastal ecosystems, and a
hexagon-level attribute table used for the paper's Figure 2. Only the
coastal-relevant subset is folded into the cod-kmap facilities graph; the
remainder is kept verbatim under `data/raw/synthesis-networks/` for
provenance and for downstream analyses that need the broader context.

## Sources
- https://github.com/COMPASS-DOE/synthesis-networks (MIT)
  - `data/Networks_table_updated.csv` — 52 networks × 15 attributes
  - `data/hexagons_Ecoregion_TableToExcel.xlsx` — per-hexagon ecoregion, land
    cover, hazard counts, network-site counts within 50 km
  - `scripts/networks_figure2.Rmd` — R Markdown that reproduces the paper's
    Figure 2 from the hexagon table

## Inputs
- Wave 1 `schema/vocab/networks.csv` (to detect duplicates against existing
  entries such as IOOS, NERRS, LTER, NEON, OOI, GOOS, NAML)

## Outputs
- `data/raw/synthesis-networks/Networks_table_updated.csv` (verbatim)
- `data/raw/synthesis-networks/hexagons_Ecoregion_TableToExcel.xlsx` (verbatim)
- `data/raw/synthesis-networks/UPSTREAM_README.md` (attribution)
- `data/raw/synthesis-networks/UPSTREAM_LICENSE` (MIT)
- `scripts/synthesis-networks/networks_figure2.Rmd` (analysis script, verbatim)
- Future: `data/raw/R10/facilities_synthesis_networks.json` — one facility
  record per network that (a) is flagged `Coastal.Ecosystem = 1` and
  (b) does not already appear in `schema/vocab/networks.csv`.

## Method
1. Preserve upstream artifacts verbatim for citation integrity.
2. On ingest, join `Networks_table_updated.csv` against the existing vocabulary
   by acronym and name. Skip any row whose network acronym is already present
   (IOOS, NEON, LTER, NERRS/NERRN, GOOS, NAML, OOI — verified 2026-04-23).
3. For the remaining rows with `Coastal.Ecosystem = 1` (e.g. National Marine
   Sanctuary, National Estuary Program, Coastal Zone Management, Louisiana
   CRMS, Marine GEO, Sentinel Site Program, NCCOS, NACP, NCCA, OCB,
   Coastal Rainforest Margins Research Network, Coastal Carbon RCN), emit a
   facility record with:
   - `facility_type: "network"`
   - `country: "US"` (most are US-scoped) or multi-country per the `Geographic`
     column — emit one record per country when the network is explicitly
     multi-national
   - `funders: [{ name: <MainFundingAgency>, relation: "parent-agency" }]`
   - `research_areas`: derived from the ecosystem flags (map
     Terrestrial/Freshwater/Marine/Atmospheric/Coastal to GCMD slugs in D3)
   - `provenance.source_url: "https://github.com/COMPASS-DOE/synthesis-networks"`
   - `provenance.confidence: "medium"` (no HQ geocoding in upstream)
4. Leave HQ `lat/lng` null; D2's geocoder will resolve where feasible.
5. The hexagon table is out of scope for the facilities graph; it remains a
   raw artifact for any future ecoregion-joined analytics (Figure 2
   reproductions, hazard-vs-network-density overlays).

## Known landmarks (must appear after step 3)
- National Marine Sanctuary (NMS) — NOAA
- National Estuary Program (NEP) — EPA
- Coastal Zone Management (CZM) — NOAA
- Louisiana Coastwide Reference Monitoring System (CRMS) — DOI USGS
- Marine GEO / Tennenbaum Marine Observatories Network — Smithsonian
- Sentinel Site Program — NOAA
- National Centers for Coastal Ocean Science (NCCOS) — NOAA
- Coastal Carbon Research Coordination Network (CCRCN) — NSF RCN

## Attribution
Any derivative product that uses this dataset must cite:

> Myers-Pigg, A. N. et al. *Advancing the understanding of coastal disturbances
> with a network-of-networks approach.* Ecosphere.

and link to https://github.com/COMPASS-DOE/synthesis-networks.
