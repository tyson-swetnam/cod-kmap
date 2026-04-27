# Site suitability — a roadmap for "where should the next observatory go?"

cod-kmap currently answers the question *where do existing coastal
research facilities sit?*. To answer the harder question — *where
would a new observatory be most representative of the coastal critical
zone?* — three additional data layers and a ranking algorithm are
needed.

This page describes the layers and the ranking method as a roadmap.
The site-suitability tab in the application becomes available once
all three layers are loaded.

## Three data layers

### Geographic and climatic strata

What the world looks like, by ocean ecoregion and climate zone.

| Source | Scale | License |
|--------|------:|---------|
| **MEOW** (Marine Ecoregions of the World, Spalding et al. 2007) | 232 marine ecoregions | CC-BY |
| **Köppen-Geiger** climate zones (Beck et al. 2018) | 30 classes, 1-km raster | CC-BY |
| **GADM** coastal admin boundaries | 38,000+ polygons world-wide | free for non-commercial |
| **EEZ** boundaries (Marine Regions Flanders v12) | 281 EEZs | CC-BY |

The corresponding cod-kmap tables hold per-facility membership in
every stratum:

```
facility_strata
  facility_id    →  facilities.facility_id
  meow_ecoregion →  meow_ecoregions.ecoregion_id
  koppen_class   →  e.g. 'Cfa', 'Dfb'
  eez_id         →  eez_zones.eez_id
  gadm_admin1    →  state or province
```

### Human-influence proxies

Is this place still wild?

| Source | Scale | License |
|--------|------:|---------|
| **GHSL Population** (JRC, 2023) | 100-m raster, global | CC-BY |
| **Cumulative Human Impact** (Halpern 2019) | 1-km raster, global oceans | CC-BY |
| **WCMC Marine Protected Areas** | 17,000+ MPAs | CC-BY |
| **Distance to nearest major port** | derived from the World Port Index | computed |

```
facility_human_influence
  pop_density_per_km2     -- GHSL within 5 km of HQ
  cumulative_impact       -- WCMC 0-15 score
  inside_mpa              -- HQ inside any WDPA polygon
  distance_to_port_km     -- nearest WPI port
  influence_score_0_to_1  -- composite (0 = pristine, 1 = heavy pressure)
```

### Biological diversity proxies

Is this place biologically rich?

| Source | Scale | License |
|--------|------:|---------|
| **GBIF** marine occurrence records | global | CC-BY |
| **OBIS** Ocean Biogeographic Information System | global oceans | CC0 |
| **MARSPEC** marine spatial environmental indices | 1-km raster | CC-BY |
| **Reef Life Survey** species lists | global reefs | CC-BY |

Per-facility metrics, computed within a 50-km buffer:

```
facility_diversity
  gbif_n_species, gbif_n_records
  obis_n_species
  marspec_temp_range, marspec_salinity_var
  diversity_score_0_to_1
```

## The ranking algorithm

Once all three layers are loaded, the candidate-site ranker tiles the
coastline into 50-km hexagonal cells (H3 resolution 4) and scores
each cell against each research area:

```
score = (stratum_emptiness × 0.4)
      + (diversity_score    × 0.3)
      + ((1 - influence)    × 0.2)
      + (representativeness × 0.1)
```

- **Stratum emptiness** captures how under-represented a stratum is
  for a given research area: `1 − (existing_facilities / max_facilities_per_stratum)`.
- **Diversity** rewards biologically rich cells.
- **Inverse influence** rewards relatively pristine cells.
- **Representativeness** rewards cells that are statistically
  distinctive within their region.

Cells within 100 km of an existing same-area facility are excluded so
the ranker doesn't recommend "another observatory next to this
observatory." The top 25 cells per research area form the published
candidate list.

## Where it shows up in the UI

A new card in the per-area dashboard:

| Location | Ecoregion | Diversity | Pristine | Score |
|---|---|---|---|---|
| Pribilof Islands, AK | Aleutian Islands | 0.91 | 0.86 | 0.87 |
| (next four) | … | … | … | … |

Each row links into the Map tab, pre-zoomed to the candidate cell
with a proposed-site marker.

## Effort and dependencies

The three ingestion passes plus the ranking algorithm and dashboard
add up to roughly six and a half working days:

| Phase | Days |
|---|---:|
| Geographic and climatic strata (MEOW, Köppen, EEZ, GADM) | 1.5 |
| Human-influence raster sampling (GHSL, WCMC) | 1.0 |
| Diversity ingestion (GBIF, OBIS) — many API calls, async | 2.0 |
| Hex-cell tiling and stratum-emptiness scoring | 1.0 |
| Suitability ranking and CSV export | 0.5 |
| Dashboard card and map drill-in | 0.5 |
| **Total** | **6.5** |

Software dependencies:

- DuckDB ≥ 1.5 with the spatial extension.
- Python: `rasterio`, `geopandas`, `h3`, `shapely`, `requests`.

The largest downloads are GHSL Population at 100 m (~6 GB) and the
WDPA shapefile (~1 GB). Köppen-Geiger is ~200 MB; MEOW and EEZ are
small.

## Why the suitability layer isn't shipping in the first release

It's substantial, requires several large file downloads, and the
ranking should be reviewed by domain experts before being exposed
in the UI as "go build an observatory here." The current dashboard
already gives operators a strong "what coverage exists today?" view;
the suitability layer is a logical next step rather than a
prerequisite.
