"""Populate the `regions`, `region_area_links`, and `facility_regions`
tables from the canonical overlay GeoJSON files and the already-ingested
facilities.

Run as a standalone step (idempotent):

    python scripts/populate_regions.py                # the default lake (db/cod_kmap_catalog.sqlite)
    python scripts/populate_regions.py --catalog <path>

Or, automatically, at the end of `scripts/ingest.py` (same snapshot).

Design notes
------------

* Each polygon in the region overlays (REGION_OVERLAYS) becomes one row
  in `regions`. Its `region_id` is a stable hash of (network_id,
  lower(name)) so re-runs are idempotent. Two polygons that share a
  network and a name (one region drawn as two parts) share that id: the
  first one supplies the `regions` row, and a facility inside either part
  is linked to it.

* Only the seven region layers schema.sql names are regions. The other
  overlays in public/overlays/ (coastal-*, ramsar-us, neon-sites) are the
  footprints of protected areas and field sites the catalogue already
  holds as *facilities* (scripts/coastal_research/ingest_r11.py); making
  them regions as well would list each of them twice.

* `facility_regions` is derived: for every row in `facilities` with a
  non-null hq_lat/hq_lng, we do a point-in-polygon test against every
  region. A containment produces a (facility_id, region_id, 'within', 0.0)
  row. This makes "which facilities sit inside the Florida Keys NMS?" a
  plain SQL join.

* `region_area_links` is seeded from a small `KIND_AREAS` map — each
  region kind gets a set of plausible research areas that apply system-
  wide (e.g. every NMS sanctuary is tagged with "marine-ecosystems",
  "marine-policy-and-socio-economics"). A run resets it to the heuristic
  set, so per-region refinements belong in KIND_AREAS or a later step.

* The three tables are synchronised, not emptied and refilled: rows that
  no longer derive are deleted and only new or changed rows are written,
  so an unchanged run leaves `regions.created_at` alone and adds nothing
  to the lake beyond its snapshot. One standalone run is one lake
  snapshot (``populate_regions: ...``), checked by cod_db.assert_integrity
  before it commits. Publish with ``scripts/export_parquet.py``.

Dependencies
------------
  * shapely   (pip install shapely)   — for efficient PIP.
  * duckdb    (already a project dep).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import duckdb
from shapely.geometry import shape, Point
from shapely.strtree import STRtree

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cod_db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
OVERLAYS = ROOT / 'public' / 'overlays'

# The region layers (public/overlays/<name>.geojson). See the module
# docstring for why the protected-area overlays are not among them.
REGION_OVERLAYS = (
    'epa-regions',
    'marine-monuments',
    'marine-sanctuaries',
    'neon-domains',
    'nep-programs',
    'nerr-reserves',
    'nps-coastal',
)


# ── Region kind -> seed research-area slugs ─────────────────────────
# Conservative defaults: anything that applies system-wide to every
# feature of this kind. Area slugs must exist in research_areas.

KIND_AREAS = {
    'sanctuary': [
        'marine-ecosystems',
        'marine-policy-and-socio-economics',
        'fisheries-and-aquaculture',
    ],
    'monument': [
        'marine-ecosystems',
        'deep-sea',
        'marine-policy-and-socio-economics',
    ],
    'nerr-reserve': [
        'estuaries-and-wetlands',
        'salt-marshes',
        'tidal-wetlands',
        'long-term-ecological-research',
    ],
    'nep-program': [
        'estuaries-and-wetlands',
        'marine-policy-and-socio-economics',
        'coastal-processes',
    ],
    'nps-unit': [
        'coastal-terrestrial-ecosystems',
        'coastal-processes',
    ],
    'neon-domain': [
        'long-term-ecological-research',
        'coastal-terrestrial-ecosystems',
    ],
    'epa-region': [
        'marine-policy-and-socio-economics',
    ],
}

REGION_COLS = ('region_id', 'name', 'acronym', 'kind', 'network_id', 'url',
               'manager', 'designated', 'state', 'description',
               'source_file', 'source')
REGION_DDL = ('region_id VARCHAR, name VARCHAR, acronym VARCHAR, kind VARCHAR, '
              'network_id VARCHAR, url VARCHAR, manager VARCHAR, designated INTEGER, '
              'state VARCHAR, description VARCHAR, source_file VARCHAR, source VARCHAR')


# ── Helpers ─────────────────────────────────────────────────────────

def region_id(network_id: str, name: str) -> str:
    key = f'{network_id or ""}|{(name or "").strip().lower()}'
    return hashlib.sha1(key.encode('utf-8')).hexdigest()[:16]


def overlay_files() -> list[Path]:
    files = [OVERLAYS / f'{name}.geojson' for name in REGION_OVERLAYS]
    for p in files:
        if not p.exists():
            print(f'[warn] region overlay missing: {p.relative_to(ROOT)}', file=sys.stderr)
    return [p for p in files if p.exists()]


def load_region_rows(files: list[Path]) -> list[dict]:
    """One dict per polygon, ready for INSERT into `regions`. Plus a
    `geometry` key (shapely object) for PIP, stripped before insert."""
    rows: list[dict] = []
    for p in files:
        with p.open() as f:
            j = json.load(f)
        for feat in j.get('features', []):
            props = feat.get('properties') or {}
            name = props.get('name')
            if not name:
                continue
            network_slug = props.get('network_slug')
            rid = region_id(network_slug, name)
            try:
                geom = shape(feat['geometry'])
            except Exception as e:
                print(f'[warn] {p.name}: skipping bad geometry for {name!r}: {e}', file=sys.stderr)
                continue

            rows.append({
                'region_id':   rid,
                'name':        name,
                'acronym':     props.get('acronym'),
                'kind':        props.get('kind'),
                'network_id':  network_slug,
                'url':         props.get('url'),
                'manager':     props.get('manager'),
                'designated':  props.get('year_designated') or (
                    int(props['year']) if str(props.get('year') or '').isdigit() else None
                ),
                'state':       props.get('state') or props.get('states'),
                'description': props.get('description'),
                'source_file': p.name,
                'source':      props.get('source'),
                'geometry':    geom,
            })
    return rows


def _stage(conn, name: str, ddl: str, rows) -> None:
    conn.execute(f'CREATE OR REPLACE TEMP TABLE {name} ({ddl})')
    rows = list(rows)
    if rows:
        conn.executemany(f'INSERT INTO {name} VALUES ({", ".join("?" * len(rows[0]))})',
                         rows)


def _sync(conn, table: str, key: list[str], cols: tuple[str, ...], stage: str) -> dict:
    """Make `table` hold exactly the rows of `stage` (on `cols`).

    Deletes the rows whose key is no longer staged, then upserts only the
    staged rows that are new or differ, so an unchanged table is not
    rewritten. Columns outside `cols` (regions.created_at) keep their
    lake value on a match. `stage` must be unique on `key`."""
    on = ' AND '.join(f't.{k} = s.{k}' for k in key)
    n_before = cod_db.scalar(conn, f'SELECT count(*) FROM main.{table}')
    conn.execute(f'DELETE FROM main.{table} t WHERE NOT EXISTS '
                 f'(SELECT 1 FROM {stage} s WHERE {on})')
    deleted = n_before - cod_db.scalar(conn, f'SELECT count(*) FROM main.{table}')
    c = ', '.join(cols)
    st = cod_db.merge_upsert(conn, table, key,
                             f'SELECT {c} FROM {stage} EXCEPT SELECT {c} FROM main.{table}')
    return {'rows': cod_db.scalar(conn, f'SELECT count(*) FROM {stage}'),
            'inserted': st['inserted'], 'updated': st['matched'], 'deleted': deleted}


def region_links(conn, rows: list[dict]) -> list[tuple[str, str]]:
    valid_areas = {a for (a,) in cod_db.rows(conn, 'SELECT area_id FROM main.research_areas')}
    links: set[tuple[str, str]] = set()
    for r in rows:
        for area_id in KIND_AREAS.get(r['kind'] or '', []):
            if area_id in valid_areas:
                links.add((r['region_id'], area_id))
    return sorted(links)


def containment(conn, rows: list[dict],
                facility_ids: list[str] | None = None) -> list[tuple[str, str]]:
    """Point-in-polygon facilities (all, or `facility_ids`) against the
    region polygons; (facility_id, region_id) pairs. Uses shapely's
    STRtree to keep the inner loop O(log n)."""
    geoms = [r['geometry'] for r in rows]
    ids = [r['region_id'] for r in rows]
    if not geoms:
        return []
    tree = STRtree(geoms)

    sql = ('SELECT facility_id, hq_lat, hq_lng FROM main.facilities '
           'WHERE hq_lat IS NOT NULL AND hq_lng IS NOT NULL')
    params: list = []
    if facility_ids is not None:
        sql += ' AND facility_id IN (SELECT unnest(?::VARCHAR[]))'
        params = [list(facility_ids)]
    links: set[tuple[str, str]] = set()
    for fid, lat, lng in cod_db.rows(conn, sql, params):
        pt = Point(lng, lat)
        for idx in tree.query(pt):
            # shapely >=2 returns numpy integer indices; earlier versions
            # returned geometry objects. Coerce to a Python int either way.
            try:
                geom_idx = int(idx)
            except (TypeError, ValueError):
                geom_idx = geoms.index(idx)
            if geoms[geom_idx].contains(pt):
                links.add((fid, ids[geom_idx]))
    return sorted(links)


def write_regions(conn: duckdb.DuckDBPyConnection, rows: list[dict]) -> dict:
    """Synchronise the three region tables with `rows`. No transaction of
    its own: ingest.py calls it inside its snapshot, populate() inside
    one. Returns per-table counts."""
    first: dict[str, dict] = {}
    for r in rows:
        first.setdefault(r['region_id'], r)
    _stage(conn, '_pr_regions', REGION_DDL,
           ([r[c] for c in REGION_COLS] for r in first.values()))
    _stage(conn, '_pr_region_area_links', 'region_id VARCHAR, area_id VARCHAR',
           region_links(conn, rows))
    _stage(conn, '_pr_facility_regions',
           'facility_id VARCHAR, region_id VARCHAR, relation VARCHAR, distance_km DOUBLE',
           ((fid, rid, 'within', 0.0) for fid, rid in containment(conn, rows)))

    # Children first, so a region that disappears takes its links with it.
    stats = {
        'facility_regions': _sync(conn, 'facility_regions', ['facility_id', 'region_id'],
                                  ('facility_id', 'region_id', 'relation', 'distance_km'),
                                  '_pr_facility_regions'),
        'region_area_links': _sync(conn, 'region_area_links', ['region_id', 'area_id'],
                                   ('region_id', 'area_id'), '_pr_region_area_links'),
        'regions': _sync(conn, 'regions', ['region_id'], REGION_COLS, '_pr_regions'),
    }
    stats['regions']['polygons'] = len(rows)
    for t in ('_pr_regions', '_pr_region_area_links', '_pr_facility_regions'):
        conn.execute(f'DROP TABLE IF EXISTS {t}')
    return stats


# ── Main ────────────────────────────────────────────────────────────

def populate(conn: duckdb.DuckDBPyConnection | None = None,
             catalog: Path | None = None, lake_data: Path | None = None) -> dict:
    """Rebuild the three region tables as one snapshot. Uses `conn` when
    given, else opens the lake at `catalog`."""
    files = overlay_files()
    rows = load_region_rows(files)
    print(f'[info] loaded {len(rows)} region polygons from {len(files)} overlay files')

    own = conn is None
    if own:
        conn = cod_db.connect(catalog=catalog, data=lake_data)
    try:
        with cod_db.transaction(conn, f'populate_regions: {len(rows)} overlay polygons '
                                      f'from {len(files)} files'):
            stats = write_regions(conn, rows)
            cod_db.assert_integrity(conn, ['regions', 'region_area_links', 'facility_regions'])
    finally:
        if own:
            conn.close()
    dup = len(rows) - stats['regions']['rows']
    for table, st in stats.items():
        print(f'[ok] {table:18s} {st}')
    if dup:
        print(f'[ok] {dup} polygons share a region_id with an earlier one')
    return stats


def catalog_arg(args) -> Path | None:
    """--db is the pre-lake flag, kept as an alias of --catalog. The retired
    db/cod_kmap.duckdb file itself is not a catalog, so it is ignored."""
    if args.db is None or args.catalog is not None:
        return args.catalog
    if args.db.suffix == '.duckdb':
        print(f'[deprecated] --db {args.db} ignored (db/cod_kmap.duckdb is retired; '
              f'the store is a DuckLake); use --catalog', file=sys.stderr)
        return None
    return args.db


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    cod_db.add_argparse_catalog(parser)
    parser.add_argument('--db', type=Path, default=None,
                        help='deprecated alias of --catalog (a .duckdb path is ignored)')
    args = parser.parse_args(argv)
    try:
        populate(catalog=catalog_arg(args), lake_data=args.lake_data)
    except cod_db.LakeNotFound as e:
        print(f'[error] {e}', file=sys.stderr)
        return 2
    print('[next] python scripts/qa.py && python scripts/export_parquet.py')
    return 0


if __name__ == '__main__':
    sys.exit(main())
