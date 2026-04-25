# Roadmap — Site-suitability ranking for new coastal observatories

The "where should the next observatory go" question requires three new
data layers we don't have yet. This document captures the plan so the
research-area dashboard can graduate from "where are existing
facilities?" to "where would new ones be most representative?"

The user (you) selected the *full ingestion* path, so this roadmap is
real work — estimated 5-7 working days of ingestion + scoring + UI.

## 1. The three new data layers

### 1a. Geographic / climatic strata (the "what's the world look like" layer)

| Source | Scale | Format | License | Why |
|--------|------:|--------|---------|-----|
| MEOW (Marine Ecoregions of the World, Spalding et al. 2007) | 232 marine ecoregions | shapefile | CC-BY | Defines the "should each ecoregion have at least one observatory?" baseline. |
| Köppen-Geiger climate zones (Beck et al. 2018) | 30 climate classes | 1-km raster | CC-BY | Coastal climate stratification: tropical wet, Mediterranean, polar tundra, etc. |
| GADM coastal admin boundaries | 38k+ polygons world-wide | shapefile | free for non-comm | Per-state / per-province / per-EEZ coverage gap detection. |
| EEZ boundaries (Marine Regions Flanders v12) | 281 EEZs | shapefile | CC-BY | Country-level marine sovereignty for funding-attribution. |

Schema additions:

```sql
CREATE TABLE meow_ecoregions (
  ecoregion_id    INTEGER PRIMARY KEY,
  ecoregion_name  VARCHAR,
  province_name   VARCHAR,
  realm_name      VARCHAR,
  geometry        GEOMETRY  -- WKB polygon
);
CREATE TABLE koppen_zones (
  facility_id     VARCHAR REFERENCES facilities(facility_id),
  koppen_class    VARCHAR(3),    -- 'Cfa', 'Dfb', etc.
  koppen_name     VARCHAR
);
CREATE TABLE eez_zones (
  eez_id          INTEGER PRIMARY KEY,
  sovereign       VARCHAR,
  geoname         VARCHAR,
  area_km2        DOUBLE,
  geometry        GEOMETRY
);
CREATE TABLE facility_strata (
  facility_id     VARCHAR REFERENCES facilities(facility_id) PRIMARY KEY,
  meow_ecoregion  INTEGER,
  koppen_class    VARCHAR(3),
  eez_id          INTEGER,
  gadm_admin1     VARCHAR        -- state/province
);
```

Ingestion:

- `scripts/ingest_meow.py` — load MEOW shapefile via DuckDB spatial
  extension, upsert into `meow_ecoregions`.
- `scripts/ingest_koppen_geiger.py` — sample the Köppen raster at
  every facility's `(hq_lat, hq_lng)` using `rasterio`.
- `scripts/ingest_eez.py` — same as MEOW but for EEZs.
- `scripts/spatial_overlay_facilities.py` — point-in-polygon every
  facility against MEOW + EEZ + GADM-admin1 → populate `facility_strata`.

### 1b. Human-influence proxy (the "is this place still wild?" layer)

| Source | Scale | Format | License |
|--------|------:|--------|---------|
| GHSL Population (JRC, 2023 release) | 100-m raster, global | GeoTIFF | CC-BY |
| WCMC Cumulative Human Impact (Halpern 2019) | 1-km raster, global oceans | GeoTIFF | CC-BY |
| WCMC Marine Protected Area boundaries | 17k MPAs | shapefile | CC-BY |
| Distance-to-nearest-major-port (computed from World Port Index 2019) | derived | GeoTIFF | computed |

Schema additions:

```sql
CREATE TABLE facility_human_influence (
  facility_id            VARCHAR REFERENCES facilities(facility_id) PRIMARY KEY,
  pop_density_per_km2    DOUBLE,        -- GHSL 100m within 5km of HQ
  cumulative_impact      DOUBLE,        -- WCMC 0-15 score
  inside_mpa             BOOLEAN,       -- HQ inside any WDPA polygon
  distance_to_port_km    DOUBLE,        -- nearest WPI port
  influence_score_0_to_1 DOUBLE         -- composite normalized (0=pristine, 1=heavy)
);
```

Ingestion:

- `scripts/ingest_human_pressures.py` — sample GHSL + WCMC rasters at
  each facility, compute composite score.

### 1c. Natural-diversity proxy (the "is this place biologically rich?" layer)

| Source | Scale | Format | License |
|--------|------:|--------|---------|
| GBIF marine occurrence records | global | API + downloads | CC-BY |
| OBIS (Ocean Biogeographic Information System) | global oceans | API | CC0 |
| Reef Life Survey (RLS) species lists | global reefs | CSV | CC-BY |
| MARSPEC marine spatial environmental indices | 1-km raster | GeoTIFF | CC-BY |

Per-facility metrics (computed within a 50-km buffer):

```sql
CREATE TABLE facility_diversity (
  facility_id          VARCHAR REFERENCES facilities(facility_id) PRIMARY KEY,
  gbif_n_species       INTEGER,    -- distinct species records w/in 50km
  gbif_n_records       INTEGER,
  obis_n_species       INTEGER,
  marspec_temp_range   DOUBLE,     -- annual SST range
  marspec_salinity_var DOUBLE,
  diversity_score_0_to_1 DOUBLE
);
```

Ingestion:

- `scripts/ingest_gbif_richness.py` — for each facility, query GBIF
  Occurrences API with a 50-km bounding box, count distinct species.
- `scripts/ingest_obis.py` — same for OBIS.
- `scripts/ingest_marspec.py` — sample raster.

## 2. The suitability ranking algorithm

Once 1a-1c are loaded, `scripts/rank_candidate_sites.py` produces a
per-research-area "top 25 candidate new sites" CSV:

```
candidate_lat, candidate_lng,         -- center of the candidate cell
meow_ecoregion, koppen_class, eez,    -- strata
n_existing_facilities_in_area_within_500km,
n_existing_facilities_in_strata,      -- coverage emptiness
diversity_score, influence_score,     -- environmental quality
representativeness_z,                 -- how distinct this stratum is
final_score                           -- combined
```

Algorithm:

1. Tile the global coastline into 50-km hex cells (use H3 resolution 4).
2. For each cell, look up the strata + diversity + influence.
3. Compute "stratum emptiness" per research area: `1 - n_existing / max_n_per_stratum`.
4. Final score (per research-area-per-cell):
   ```
   score = (stratum_emptiness × 0.4) +
           (diversity_score × 0.3) +
           ((1 - influence_score) × 0.2) +
           (representativeness_z × 0.1)
   ```
5. Drop cells within 100 km of an existing same-area facility.
6. Top 25 per research area = candidate ranking.

## 3. Dashboard hookup

After candidate sites land, the per-area dashboard (`src/views/stats.js`)
gains a new card:

```
┌─────────────────────────────────────────────────────────┐
│ Top 5 candidate sites for a new <area> observatory      │
├──────────────┬──────────┬─────────┬─────────┬──────────┤
│ Location      │ MEOW      │ Diversity│ Pristine │ Score   │
├──────────────┼──────────┼─────────┼─────────┼──────────┤
│ Pribilof Is.  │ Aleutian │ 0.91     │ 0.86     │ 0.87    │
│ St. Paul, AK  │ Islands  │          │          │          │
│ ...           │          │          │          │          │
└─────────────────────────────────────────────────────────┘
```

Each row links to the geographic Map tab pre-zoomed to the cell with a
proposed-site marker.

## 4. Effort breakdown

| Phase | Days |
|-------|-----:|
| 1a. Geo/climatic strata ingestion (MEOW, Köppen, EEZ, GADM) | 1.5 |
| 1b. Human-influence raster sampling (GHSL, WCMC) | 1.0 |
| 1c. Diversity ingestion (GBIF, OBIS) — async, tons of API calls | 2.0 |
| 2. Hex-cell tiling + stratum-emptiness scoring | 1.0 |
| 3. Suitability ranking + CSV export | 0.5 |
| 4. Dashboard "candidate sites" card + map drill-in | 0.5 |
| **Total** | **6.5** |

## 5. Dependencies

- `duckdb` ≥ 1.5 with the `spatial` extension loaded
- Python: `rasterio`, `geopandas`, `h3` (Uber H3 hex tiling),
  `shapely`, `requests` (for GBIF/OBIS APIs)
- Optional: `pyproj` for coordinate transforms (Köppen raster is in
  WGS84 lat/lng; MEOW + EEZ use the same)

## 6. Why this isn't shipping in V1

It's substantial, requires several large file downloads (GHSL is
~6 GB at 100-m, MEOW is small but Köppen is ~200 MB, MPA shapefile is
~1 GB), and the ranking algorithm benefits from a real pass with
domain experts (Tyson + collaborators) before being exposed in the
UI as "go build an observatory here." The dashboard already gives
operators a strong "what coverage exists today?" view; the
suitability layer is a logical next step but not a prerequisite.
