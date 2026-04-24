-- cod-kmap DuckDB schema (D1 deliverable)
-- Idempotent: safe to re-read against an existing DB.
-- Load the spatial extension externally (INSTALL spatial; LOAD spatial;)
-- before calling ST_Point on the locations table.

-- All tables live in the default `main` schema. DuckDB does not allow
-- a custom schema with the same name as the database file, and the
-- browser-side Wasm client expects tables to be discoverable without a
-- schema prefix.

-------------------------------------------------------------------------------
-- Vocabularies (seeded from schema/vocab/*.csv by ingest.py)
-------------------------------------------------------------------------------

CREATE OR REPLACE TABLE facility_types (
    slug         VARCHAR PRIMARY KEY,
    label        VARCHAR NOT NULL,
    description  VARCHAR
);

CREATE OR REPLACE TABLE research_areas (
    area_id      VARCHAR PRIMARY KEY,   -- slug
    label        VARCHAR NOT NULL,
    gcmd_uri     VARCHAR,
    parent_id    VARCHAR                -- soft reference to research_areas.area_id; no FK to allow unordered bulk load
);

CREATE OR REPLACE TABLE networks (
    network_id   VARCHAR PRIMARY KEY,   -- slug
    label        VARCHAR NOT NULL,
    level        VARCHAR,               -- e.g. us-national / international
    url          VARCHAR
);

-------------------------------------------------------------------------------
-- Core entities
-------------------------------------------------------------------------------

CREATE OR REPLACE TABLE facilities (
    facility_id     VARCHAR PRIMARY KEY,             -- hash(name||acronym)
    canonical_name  VARCHAR NOT NULL,
    acronym         VARCHAR,
    parent_org      VARCHAR,
    facility_type   VARCHAR NOT NULL REFERENCES facility_types(slug),
    country         VARCHAR NOT NULL,                -- ISO 3166-1 alpha-2
    region          VARCHAR,
    hq_address      VARCHAR,
    hq_lat          DOUBLE,
    hq_lng          DOUBLE,
    url             VARCHAR,
    contact         VARCHAR,
    established     INTEGER,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE OR REPLACE TABLE locations (
    location_id     VARCHAR PRIMARY KEY,             -- hash(facility_id||label)
    facility_id     VARCHAR NOT NULL REFERENCES facilities(facility_id),
    label           VARCHAR,
    address         VARCHAR,
    lat             DOUBLE,
    lng             DOUBLE,
    role            VARCHAR                          -- headquarters|field-station|observatory|vessel|mooring-array|buoy|lab|office|virtual
);

CREATE OR REPLACE TABLE funders (
    funder_id       VARCHAR PRIMARY KEY,             -- hash(lower(name))
    name            VARCHAR NOT NULL,
    type            VARCHAR,                         -- federal|state|foundation|international|industry|private
    country         VARCHAR,
    url             VARCHAR,
    notes           VARCHAR
);

CREATE OR REPLACE TABLE funding_links (
    funder_id       VARCHAR NOT NULL REFERENCES funders(funder_id),
    facility_id     VARCHAR NOT NULL REFERENCES facilities(facility_id),
    amount_usd      DOUBLE,
    fiscal_year     INTEGER,
    award_id        VARCHAR,
    relation        VARCHAR,                         -- parent-agency|grant|endowment|contract|cooperative-agreement|state-appropriation|private-donor|membership-fee
    source_url      VARCHAR
);

CREATE OR REPLACE TABLE area_links (
    facility_id     VARCHAR NOT NULL REFERENCES facilities(facility_id),
    area_id         VARCHAR NOT NULL REFERENCES research_areas(area_id),
    PRIMARY KEY (facility_id, area_id)
);

CREATE OR REPLACE TABLE network_membership (
    facility_id     VARCHAR NOT NULL REFERENCES facilities(facility_id),
    network_id      VARCHAR NOT NULL REFERENCES networks(network_id),
    role            VARCHAR,
    PRIMARY KEY (facility_id, network_id)
);

-------------------------------------------------------------------------------
-- Regions (overlay polygons as first-class records)
-------------------------------------------------------------------------------
--
-- Every polygon shown under "Map overlays" (NMS sanctuaries, marine national
-- monuments, NERR reserves, NPS coastal units, NEP programs, NEON ecological
-- domains, EPA regions) becomes a row here, with the same rich metadata the
-- per-point facilities already carry (name / acronym / url / manager / etc.)
-- plus a foreign key to the `networks` vocabulary so region ↔ facility
-- joins are cheap.

CREATE OR REPLACE TABLE regions (
    region_id       VARCHAR PRIMARY KEY,             -- hash(network_id||lower(name))
    name            VARCHAR NOT NULL,
    acronym         VARCHAR,
    kind            VARCHAR,                          -- sanctuary | monument | nerr-reserve | nep-program | nps-unit | neon-domain | epa-region
    network_id      VARCHAR REFERENCES networks(network_id),
    url             VARCHAR,
    manager         VARCHAR,
    designated      INTEGER,
    state           VARCHAR,
    description     VARCHAR,
    source_file     VARCHAR,                          -- which public/overlays/*.geojson produced this row
    source          VARCHAR,                          -- upstream attribution (e.g. COMPASS-DOE/synthesis-networks)
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Region ↔ research_area (editable; empty by default — seed via
-- scripts/populate_regions.py based on kind heuristics, or by hand).
CREATE OR REPLACE TABLE region_area_links (
    region_id       VARCHAR NOT NULL REFERENCES regions(region_id),
    area_id         VARCHAR NOT NULL REFERENCES research_areas(area_id),
    PRIMARY KEY (region_id, area_id)
);

-- Facility ↔ region (derived: point-in-polygon against each overlay).
-- `relation` is 'within' when the facility's HQ coordinate lies inside
-- the polygon; future work may add 'nearby' or 'adjacent' links.
CREATE OR REPLACE TABLE facility_regions (
    facility_id     VARCHAR NOT NULL REFERENCES facilities(facility_id),
    region_id       VARCHAR NOT NULL REFERENCES regions(region_id),
    relation        VARCHAR,                          -- within | nearby | adjacent
    distance_km     DOUBLE,                           -- 0.0 when within
    PRIMARY KEY (facility_id, region_id)
);

-------------------------------------------------------------------------------
-- Provenance & ingest bookkeeping
-------------------------------------------------------------------------------

CREATE OR REPLACE TABLE provenance (
    record_type     VARCHAR NOT NULL,                -- 'facility' | 'funding_link' | ...
    record_id       VARCHAR NOT NULL,
    source_url      VARCHAR,
    retrieved_at    DATE,
    confidence      VARCHAR,                         -- high|medium|low
    agent           VARCHAR                          -- R1..R9, D2, etc.
);

CREATE OR REPLACE TABLE ingest_runs (
    run_id          VARCHAR PRIMARY KEY,
    started_at      TIMESTAMP,
    finished_at     TIMESTAMP,
    git_sha         VARCHAR,
    facility_count  INTEGER,
    status          VARCHAR                          -- success|failed|partial
);

-------------------------------------------------------------------------------
-- Helper views for the web UI (F3 consumes these)
-------------------------------------------------------------------------------

CREATE OR REPLACE VIEW v_facility_map AS
SELECT
    f.facility_id           AS id,
    f.canonical_name        AS name,
    f.acronym               AS acronym,
    f.facility_type         AS type,
    f.country               AS country,
    f.region                AS region,
    f.hq_lat                AS lat,
    f.hq_lng                AS lng,
    f.url                   AS url,
    f.parent_org            AS parent_org
FROM facilities f
WHERE f.hq_lat IS NOT NULL AND f.hq_lng IS NOT NULL;

CREATE OR REPLACE VIEW v_facility_enriched AS
SELECT
    f.facility_id,
    f.canonical_name,
    f.acronym,
    f.facility_type,
    f.country,
    f.hq_lat,
    f.hq_lng,
    f.url,
    list(DISTINCT ra.label)        AS research_areas,
    list(DISTINCT n.label)         AS networks,
    list(DISTINCT fu.name)         AS funders,
    list(DISTINCT r.name)          AS regions
FROM facilities f
LEFT JOIN area_links al        ON al.facility_id = f.facility_id
LEFT JOIN research_areas ra    ON ra.area_id    = al.area_id
LEFT JOIN network_membership nm ON nm.facility_id = f.facility_id
LEFT JOIN networks n           ON n.network_id   = nm.network_id
LEFT JOIN funding_links fl     ON fl.facility_id = f.facility_id
LEFT JOIN funders fu           ON fu.funder_id   = fl.funder_id
LEFT JOIN facility_regions fr  ON fr.facility_id = f.facility_id
LEFT JOIN regions r            ON r.region_id    = fr.region_id
GROUP BY f.facility_id, f.canonical_name, f.acronym, f.facility_type,
         f.country, f.hq_lat, f.hq_lng, f.url;

-- Region enriched — every polygon with its network + any facilities within.
CREATE OR REPLACE VIEW v_region_enriched AS
SELECT
    r.region_id,
    r.name,
    r.acronym,
    r.kind,
    r.network_id,
    n.label         AS network_label,
    r.url,
    r.manager,
    r.designated,
    r.state,
    r.description,
    list(DISTINCT ra.label)         AS research_areas,
    list(DISTINCT f.canonical_name) AS facilities,
    count(DISTINCT fr.facility_id)  AS facility_count
FROM regions r
LEFT JOIN networks n             ON n.network_id  = r.network_id
LEFT JOIN region_area_links ral  ON ral.region_id = r.region_id
LEFT JOIN research_areas ra      ON ra.area_id    = ral.area_id
LEFT JOIN facility_regions fr    ON fr.region_id  = r.region_id
LEFT JOIN facilities f           ON f.facility_id = fr.facility_id
GROUP BY r.region_id, r.name, r.acronym, r.kind, r.network_id, n.label,
         r.url, r.manager, r.designated, r.state, r.description;
