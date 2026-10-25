"""Export the lake's tables to Parquet: db/parquet (canonical) and public/ (site).

This is the ONLY writer of ``db/parquet/``, ``public/parquet/`` and
``public/facilities.geojson``. Everything upstream — ingest, loaders,
enrichers, the ``compute_*`` scripts, ``build_mvg_layout.py`` — writes into
the DuckLake store of record (``scripts/cod_db.py``); this script reads it
read-only and publishes.

Produces::

    db/parquet/<table>.parquet       every table in TABLES, unfiltered — the
                                     canonical committed copy that
                                     rebuild_lake_from_parquet.py loads
    public/parquet/<table>.parquet   what the static site serves: TABLES
                                     minus DB_ONLY, filtered by EXPORT_WHERE
    public/facilities.geojson        lightweight first-paint fallback

Both parquet sets are committed (gitignored but tracked: stage a NEW file
with ``git add -f``).

Scratch lakes do not publish. When the catalog is not the default
``db/cod_kmap_catalog.sqlite`` (``--catalog`` or ``$COD_LAKE_CATALOG``),
everything is written under ``<catalog dir>/export/`` with the same layout
(``export/db/parquet/``, ``export/public/parquet/``,
``export/public/facilities.geojson``), so an experiment cannot overwrite
the committed files. ``--out ROOT`` picks another root (``--out .`` publishes
from a non-default lake on purpose); ``--db-out``, ``--public-out`` and
``--geojson`` override one target each.

Rules, each fixing a way the old export could ship the wrong thing:

  * **A table in TABLES that the lake does not have is a hard failure.**
    The old export listed 14 tables and ~20 other scripts wrote their own
    parquet straight into both folders, so nothing checked that the
    published set was complete or current. Existence is checked for every
    table before any file is written, so a failed export writes nothing.
    ``ALLOW_MISSING`` is the explicit, commented exception list (empty).
  * **public/parquet is a filtered view of db/parquet, defined here.** The
    filters used to live in the producers (rank_person_registry.py,
    link_registry_facilities.py, ...), each with its own COPY. They are
    ``DB_ONLY`` and ``EXPORT_WHERE`` now, applied to the same lake rows.
  * **Contact details never ship** (``EXPORT_REDACT``), in either output:
    db/parquet is committed to a public repository too. The column is kept,
    all NULL, so both files keep the schema.sql shape.
  * **Row order is deterministic.** Every COPY is ``ORDER BY`` the table's
    key (schema.sql primary key, else the ``cod_db.DERIVED_CONSTRAINTS``
    one), then every other column (keyless tables sort by all columns),
    and the GeoJSON is ordered by facility id. Re-exporting unchanged data
    produces byte-identical files on any DuckDB version, so a diff in
    ``db/`` or ``public/`` is a real change.
  * Files are written to a temp name and renamed, so an interrupted run
    never leaves a truncated parquet behind.

COPY uses DuckDB's default parquet options (SNAPPY, format v1), which
DuckDB 1.1 (inside duckdb-wasm) and later read. Every column is written
OPTIONAL, as DuckDB 1.x wrote them (see ``copy_table``): DuckDB 2.0 would
otherwise mark a column REQUIRED whenever the lake's current physical
layout lets it prove there are no NULLs, so the bytes would change with
compaction even when the rows did not.

Usage::

    python scripts/export_parquet.py
    python scripts/export_parquet.py --catalog /tmp/x/cat.sqlite
                                     # -> /tmp/x/export/{db/parquet,public/...}
    python scripts/export_parquet.py --out /tmp/y      # -> /tmp/y/{db,public}/...
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cod_db  # noqa: E402

# Every table the project publishes, in db/parquet. The full inventory: a
# lake table not listed here is reported and not exported.
TABLES = [
    # Vocabularies.
    "facility_types",
    "research_areas",
    "networks",
    # Facilities and their facets.
    "facilities",
    "locations",
    "area_links",
    "network_membership",
    "provenance",
    # Funding. funding_links is a schema.sql VIEW over funding_events,
    # materialised here because the browser reads it as a table; the
    # rebuild skips the file (the view wins).
    "funders",
    "funding_events",
    "funding_links",
    # Usually zero rows; exported anyway so the parquet set binds every
    # schema view on its own (v_facility_funding_by_year_real joins it).
    "cpi_index_us",
    # Regions: one row per overlay polygon, region <-> research_area, and
    # which facilities sit inside which regions.
    "regions",
    "region_area_links",
    "facility_regions",
    # People.
    "people",
    "facility_personnel",
    "publications",
    "publication_topics",
    "authorship",
    "person_areas",
    "collaborations",
    # CSV/JSON-seeded layers: COD org chart, scholar roster, datasets.
    "cod_wbs",
    "cod_team_members",
    "community_scholars",
    "coastal_datasets",
    "dataset_endpoints",
    "dataset_facilities",
    # Unified person registry and co-authorship.
    "person_registry",
    "person_identity_source",
    "registry_collaborations",
    "registry_facilities",
    "person_validation",
    "coauthor_edges",
    "coauthor_candidates",
    # Derived tables — compute_primary_groups.py / compute_area_metrics.py /
    # build_mvg_layout.py write them INTO the lake (replace_table).
    "research_areas_active",
    "facility_primary_groups",
    "person_primary_groups",
    "person_area_metrics",
    "facility_area_funding",
    "funder_area_funding",
    "area_coverage_matrix",
    "mvg_node_layout",
    "mvg_area_polygons",
    "mvg_layout_metrics",
    "scholar_area_assignments",
    # db-only research tables (see DB_ONLY).
    "coauthor_pairs_raw",
    "coauthor_unmatched_population",
    "ror_resolution_cache",
    "split_identity_findings",
]

# Exported to db/parquet only. Working material of the registry pipeline —
# raw co-author dumps (3.4M rows), the unmatched co-author population, the
# ROR lookup cache and the split-identity audit — that the site never reads.
DB_ONLY = {
    "coauthor_pairs_raw",
    "coauthor_unmatched_population",
    "ror_resolution_cache",
    "split_identity_findings",
}

# Filters for public/parquet only (db/parquet ships every row). The browser
# only renders the core tier of person_registry (10k of 152k rows), so only
# core rows ship, and every row that points at a registry row ships only
# when what it points at ships. These reproduce what rank_person_registry.py
# and link_registry_facilities.py used to write to public/parquet exactly.
_CORE = "(SELECT canonical_id FROM person_registry WHERE tier = 'core')"
EXPORT_WHERE = {
    "person_registry": "WHERE tier = 'core'",
    "person_identity_source": f"WHERE canonical_id IN {_CORE}",
    "registry_facilities": f"WHERE canonical_id IN {_CORE}",
    "person_validation": f"WHERE canonical_id IN {_CORE}",
    # Edges only ship when both endpoints ship.
    "registry_collaborations":
        f"WHERE canonical_id_a IN {_CORE} AND canonical_id_b IN {_CORE}",
    "coauthor_edges":
        f"WHERE canonical_id_a IN {_CORE} AND canonical_id_b IN {_CORE}",
    # The review queue ships only the candidates seen with at least three
    # registry co-authors. An intentional change: the previous public file
    # (43,355 rows) was written by a one-off script whose rule matches no
    # predicate over the committed data; this rule gives 44,774 rows from
    # today's db/parquet. Nothing on the site reads the table.
    "coauthor_candidates": "WHERE n_registry_coauthors >= 3",
}

# Columns that stay in the lake but never ship, in EITHER output: both
# folders are committed to a public repository, and public/parquet is
# queryable from the SQL tab. The column is kept, all NULL, so the parquet
# keeps the schema.sql shape and a rebuilt lake has the column. The 46
# addresses survive in the committed seed CSVs.
EXPORT_REDACT = {
    "people": {"email": "CAST(NULL AS VARCHAR)"},
}

# Tables allowed to be absent from the lake (their existing parquet is left
# as is). Every entry needs a reason and should be temporary.
ALLOW_MISSING: dict[str, str] = {}


def order_by(con, table: str) -> str:
    """ORDER BY: the table's logical key first, then every remaining column."""
    cols = [d[0] for d in con.execute(
        f"SELECT * FROM {cod_db.qident(table)} LIMIT 0").description]
    pk = [c for c in (cod_db.logical_key(table) or []) if c in cols]
    keys = pk + [c for c in cols if c not in pk]
    return ", ".join(cod_db.qident(c) for c in keys)


def copy_table(con, table: str, where: str, dest: Path,
               redact: dict[str, str] | None = None) -> int:
    """COPY one table, sorted, with every parquet column OPTIONAL.

    DuckDB 2.0 writes a column as REQUIRED whenever the planner's
    statistics say it holds no NULLs. Those statistics depend on the
    lake's physical state (inlined rows, delete files, compaction), so the
    same rows exported before and after ``lake_maintenance.py`` came out
    with different REQUIRED flags — different bytes, and a binary diff in
    git for an unchanged table. Routing the rows through the nullable side
    of a LEFT JOIN drops that statistic, so every column is OPTIONAL, as
    every DuckDB 1.x export (all the committed files) was. The LIMIT drops
    the one all-NULL row the join yields for an empty table; unlike a
    WHERE, it does not give the planner its not-NULL statistic back.
    """
    tmp = dest.with_name(f".{dest.name}.tmp")
    repl = (" REPLACE (" + ", ".join(f"{e} AS {cod_db.qident(c)}"
                                     for c, e in redact.items()) + ")"
            if redact else "")
    src = f"(SELECT *{repl} FROM {cod_db.qident(table)} {where})"
    con.execute(
        f"COPY (SELECT _t.* FROM (SELECT 1 AS __cod_one) _d LEFT JOIN {src} _t ON true "
        f"ORDER BY {order_by(con, table)} "
        f"LIMIT (SELECT count(*) FROM {src})) TO '{tmp.as_posix()}' (FORMAT PARQUET)")
    os.replace(tmp, dest)
    return cod_db.scalar(con, "SELECT count(*) FROM read_parquet(?)", [str(dest)])


def copy_file(src: Path, dest: Path) -> None:
    tmp = dest.with_name(f".{dest.name}.tmp")
    shutil.copyfile(src, tmp)
    os.replace(tmp, dest)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    cod_db.add_argparse_catalog(ap)
    ap.add_argument("--out", type=Path, default=None,
                    help="output root holding db/parquet/, public/parquet/ and "
                         "public/facilities.geojson (default: the repo for the "
                         "default catalog, <catalog dir>/export otherwise)")
    ap.add_argument("--db-out", type=Path, default=None,
                    help="canonical parquet dir (default: <root>/db/parquet)")
    ap.add_argument("--public-out", type=Path, default=None,
                    help="site parquet dir (default: <root>/public/parquet)")
    ap.add_argument("--geojson", type=Path, default=None,
                    help="GeoJSON fallback (default: <root>/public/facilities.geojson)")
    args = ap.parse_args(argv)

    root = (args.out or cod_db.export_root(args.catalog)).resolve()
    if args.out is None and not cod_db.is_default_catalog(args.catalog):
        print(f"[note] non-default catalog: writing under {root} rather than the repo "
              "(pass --out to override)", file=sys.stderr)
    db_out = (args.db_out or root / "db" / "parquet").resolve()
    pub_out = (args.public_out or root / "public" / "parquet").resolve()
    geojson = (args.geojson or root / "public" / "facilities.geojson").resolve()

    unknown = sorted((DB_ONLY | set(EXPORT_WHERE) | set(EXPORT_REDACT)) - set(TABLES))
    if unknown:
        print(f"[error] DB_ONLY/EXPORT_WHERE/EXPORT_REDACT name tables not in TABLES: "
              f"{unknown}", file=sys.stderr)
        return 1

    try:
        conn = cod_db.connect(read_only=True, catalog=args.catalog, data=args.lake_data)
    except cod_db.LakeNotFound as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2

    with conn:
        # 1. Everything must be there before anything is written.
        missing = [t for t in TABLES if not cod_db.table_exists(conn, t)]
        fatal = [t for t in missing if t not in ALLOW_MISSING]
        if fatal:
            print(f"[error] in TABLES but not in the lake: {', '.join(fatal)}\n"
                  "        Run the script that produces them (or rebuild the lake); "
                  "nothing was written.", file=sys.stderr)
            return 1

        db_out.mkdir(parents=True, exist_ok=True)
        pub_out.mkdir(parents=True, exist_ok=True)

        # 2. db/parquet: every row, contact details redacted.
        n_db: dict[str, int] = {}
        for t in TABLES:
            if t in missing:
                continue
            n_db[t] = copy_table(conn, t, "", db_out / f"{t}.parquet",
                                 EXPORT_REDACT.get(t))

        # 3. public/parquet: the site subset. An unfiltered table is the same
        #    bytes as its db/parquet file, so it is copied, not re-exported.
        n_pub: dict[str, int] = {}
        for t in TABLES:
            if t in missing or t in DB_ONLY:
                continue
            dest = pub_out / f"{t}.parquet"
            if t in EXPORT_WHERE:
                n_pub[t] = copy_table(conn, t, EXPORT_WHERE[t], dest,
                                      EXPORT_REDACT.get(t))
            else:
                copy_file(db_out / f"{t}.parquet", dest)
                n_pub[t] = n_db[t]

        # 4. Lightweight GeoJSON with just the facility-map view, by id.
        rows = conn.execute(
            """SELECT id, name, acronym, type, country, lat, lng, url, parent_org
               FROM v_facility_map
               ORDER BY id"""
        ).fetchall()
        not_exported = sorted(set(cod_db.lake_tables(conn)) - set(TABLES))
        snap = cod_db.latest_snapshot(conn)

    features = []
    for fid, name, acronym, ftype, country, lat, lng, url, parent in rows:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [lng, lat]},
            "properties": {
                "id": fid,
                "name": name,
                "acronym": acronym,
                "type": ftype,
                "country": country,
                "url": url,
                "parent_org": parent,
            },
        })
    geojson.parent.mkdir(parents=True, exist_ok=True)
    tmp = geojson.with_name(f".{geojson.name}.tmp")
    tmp.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, indent=0)
    )
    os.replace(tmp, geojson)

    for t, n in n_db.items():
        pub = ("-" if t in DB_ONLY
               else f"{n_pub[t]:>9}" + ("  (filtered)" if t in EXPORT_WHERE else ""))
        print(f"[export] {t:<30} db {n:>9}   public {pub}")
    print(f"[ok] exported {len(n_db)} tables to {db_out} and {len(n_pub)} to {pub_out} "
          f"from lake snapshot {snap.get('snapshot_id')}; {len(features)} features "
          f"-> {geojson}")
    if missing:
        print(f"[allowed-missing] not in the lake, existing parquet left as is: "
              f"{', '.join(missing)}")
    if not_exported:
        print(f"[info] lake tables not in TABLES (not published): {', '.join(not_exported)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
