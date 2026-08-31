#!/usr/bin/env python3
"""Rebuild db/cod_kmap.duckdb from the committed parquet files.

Why: DuckDB's on-disk storage format changes between releases (e.g. a
file written by duckdb 1.5.x is not readable by 1.3.x, triggering::

    duckdb.duckdb.SerializationException: Serialization Error:
    Failed to deserialize: field id mismatch, expected: 200, got: 103

Parquet is a stable, portable format — so we stop committing the
binary .duckdb file and commit the parquet folder instead. Anyone can
regenerate the DB locally with whatever DuckDB version they happen to
have installed.

Run from the repo root (idempotent)::

    python scripts/rebuild_db_from_parquet.py
    python scripts/rebuild_db_from_parquet.py --db db/cod_kmap.duckdb
    python scripts/rebuild_db_from_parquet.py --parquet db/parquet

The script:
  1. Deletes any existing .duckdb + .wal at the target path.
  2. Creates a fresh DB.
  3. Applies schema/schema.sql (CREATE OR REPLACE TABLE for every
     entity + VIEWs).
  4. For each committed parquet in db/parquet/, `INSERT INTO <table>
     SELECT * FROM read_parquet('<file>.parquet')`.
  5. Re-asserts views by reading them from schema.sql (they are
     CREATE OR REPLACE VIEW so step 3 already created them; this is a
     defensive second pass).

After a successful rebuild you should be able to run every other
script (enrich_people_openalex.py, load_facility_personnel.py, etc.)
without version-mismatch errors.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "db" / "cod_kmap.duckdb"
DEFAULT_PARQUET = ROOT / "db" / "parquet"
SCHEMA = ROOT / "schema" / "schema.sql"

# Ordered so foreign-key-dependent rows load after their targets.
# (DuckDB doesn't enforce FKs at INSERT time, but keeping a sensible
# order means a future enforcement won't blow up.)
LOAD_ORDER = [
    "facility_types",
    "facilities",
    "locations",
    "research_areas",
    "area_links",
    "networks",
    "network_membership",
    "regions",
    "region_area_links",
    "facility_regions",
    "funders",
    "funding_events",      # parent of funding_links view
    "cpi_index_us",        # usually zero rows; exported so schema views bind
    "provenance",
    # People-side
    "people",
    "facility_personnel",
    "publications",
    "authorship",
    "person_areas",
    "publication_topics",   # loads after publications; drives compute_person_areas.py
    "collaborations",
    # COD team / community scholars / dataset catalogue. All soft-ref
    # tables, so no FK_FILTERS entries and no ordering constraint beyond
    # loading cod_wbs before cod_team_members for readability.
    "cod_wbs",
    "cod_team_members",
    "community_scholars",
    "coastal_datasets",
    "dataset_endpoints",
    "dataset_facilities",   # loads after both its endpoints exist
    # Unified person identity. person_registry loads before its dependants
    # so the qa.py orphan-edge and unsourced-row invariants see a complete
    # picture. All soft-ref, so no FK_FILTERS entries.
    "person_registry",
    "person_identity_source",
    "registry_collaborations",
]

# Derived tables that have no DDL in schema/schema.sql — they are computed
# artifacts written straight to parquet by scripts/compute_*.py. src/db.js
# registers all of them in the browser, so leaving them out of the rebuild
# meant the local DB was missing tables the frontend can query (the Team
# tab's metrics join and the Stats dashboard both need
# person_area_metrics). Loaded with CREATE TABLE AS rather than INSERT
# because there is no pre-existing table to insert into.
DERIVED_TABLES = [
    "facility_primary_groups",
    "person_primary_groups",
    "research_areas_active",
    "person_area_metrics",
    "facility_area_funding",
    "funder_area_funding",
    "area_coverage_matrix",
    # registry_facilities has no schema.sql DDL — scripts/link_registry_facilities.py
    # creates it with CREATE TABLE IF NOT EXISTS and writes it straight to
    # parquet, same pattern as the compute_* outputs above.
    "registry_facilities",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--parquet", type=Path, default=DEFAULT_PARQUET)
    args = ap.parse_args()

    if not args.parquet.is_dir():
        print(f"[error] parquet dir not found: {args.parquet}", file=sys.stderr)
        return 2
    if not SCHEMA.exists():
        print(f"[error] schema.sql not found: {SCHEMA}", file=sys.stderr)
        return 2

    # Delete existing DB + WAL so we start clean. Without this, an old
    # binary left behind by a mismatched version will still error on
    # duckdb.connect() before we get to apply the new schema.
    for suffix in ("", ".wal"):
        p = args.db.with_suffix(args.db.suffix + suffix) if suffix else args.db
        if p.exists():
            try:
                p.unlink()
                print(f"[clean] removed {p}")
            except OSError as e:
                print(f"[warn] could not remove {p}: {e}", file=sys.stderr)

    args.db.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(args.db))
    print(f"[connect] duckdb {duckdb.__version__} -> {args.db}")

    print("[schema] applying schema.sql")
    conn.execute(SCHEMA.read_text())

    # Tables whose rows reference another table via foreign keys. If
    # the parent has missing rows (e.g. networks.parquet shipped without
    # the 'epa-region' row but network_membership.parquet has rows
    # pointing at it — happens when intermediate exports diverge),
    # straight INSERT crashes with a ConstraintException and aborts
    # the whole rebuild before later tables have a chance to load.
    # Defensively pre-filter on those rows so we DON'T leave the DB in
    # a half-loaded state. We still report how many rows were dropped
    # so the inconsistency is visible.
    FK_FILTERS = {
        # table       :  list of (column, parent_table, parent_column)
        "network_membership":  [("network_id",   "networks",       "network_id"),
                                ("facility_id",  "facilities",     "facility_id")],
        "area_links":          [("area_id",      "research_areas", "area_id"),
                                ("facility_id",  "facilities",     "facility_id")],
        "facility_regions":    [("region_id",    "regions",        "region_id"),
                                ("facility_id",  "facilities",     "facility_id")],
        "region_area_links":   [("region_id",    "regions",        "region_id"),
                                ("area_id",      "research_areas", "area_id")],
        "regions":             [("network_id",   "networks",       "network_id")],
        "funding_events":      [("facility_id",  "facilities",     "facility_id"),
                                ("funder_id",    "funders",        "funder_id")],
        "facility_personnel":  [("facility_id",  "facilities",     "facility_id"),
                                ("person_id",    "people",         "person_id")],
        "authorship":          [("publication_id","publications",  "publication_id"),
                                ("person_id",    "people",         "person_id")],
        "person_areas":        [("person_id",    "people",         "person_id"),
                                ("area_id",      "research_areas", "area_id")],
        "collaborations":      [("person_a_id",  "people",         "person_id"),
                                ("person_b_id",  "people",         "person_id")],
    }

    # Load each table from its parquet (skip ones without a file — they
    # may simply not exist yet in this tree).
    for table in LOAD_ORDER:
        f = args.parquet / f"{table}.parquet"
        if not f.exists():
            print(f"[skip]   {table:<22} (no {f.name})")
            continue
        # Build the SELECT, applying FK filters when available so a
        # bad row in the child can't blow up the load.
        filters = FK_FILTERS.get(table, [])
        where_clauses = []
        for col, parent, pcol in filters:
            where_clauses.append(
                f"({col} IS NULL OR {col} IN (SELECT {pcol} FROM {parent}))"
            )
        where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        # Count what's in the parquet vs what survives the filter so we
        # can warn on dropped rows.
        total = conn.execute(
            "SELECT COUNT(*) FROM read_parquet(?)", [str(f)]
        ).fetchone()[0]
        # BY NAME maps parquet columns -> table columns by name rather
        # than by position. Without this, a parquet written under a
        # slightly different schema version (e.g. person_areas with
        # source/evidence_count swapped) raises "Could not convert
        # string 'openalex_topics' to INT32" because INSERT SELECT *
        # pushes the string source value into the integer evidence_count
        # slot. BY NAME is immune to ordering drift.
        try:
            conn.execute(
                f"INSERT INTO {table} BY NAME "
                f"SELECT * FROM read_parquet(?) AS src{where_sql}",
                [str(f)],
            )
        except duckdb.Error as e:
            # Fallback: retry as a positional INSERT so genuine type
            # mismatches surface with their original error message.
            try:
                conn.execute(
                    f"INSERT INTO {table} "
                    f"SELECT * FROM read_parquet(?) AS src{where_sql}",
                    [str(f)],
                )
            except duckdb.Error as e2:
                print(f"[error]  {table:<22} INSERT failed: {e2}")
                print(f"         continuing with remaining tables…")
                continue
        cnt = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        dropped = total - cnt
        marker = f"  [DROPPED {dropped} fk-orphan rows]" if dropped else ""
        print(f"[load]   {table:<22} {cnt:>6} rows  <- {f.name}{marker}")

    # Derived / computed tables: materialise straight from parquet, since
    # they have no schema.sql definition to insert into.
    for table in DERIVED_TABLES:
        f = args.parquet / f"{table}.parquet"
        if not f.exists():
            print(f"[skip]   {table:<22} (no {f.name})")
            continue
        try:
            # Bind the path rather than interpolating it, matching the
            # LOAD_ORDER loop above.
            conn.execute(
                f"CREATE OR REPLACE TABLE {table} AS "
                f"SELECT * FROM read_parquet(?)", [str(f)]
            )
            cnt = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"[derive] {table:<22} {cnt:>6} rows  <- {f.name}")
        except duckdb.Error as e:
            print(f"[error]  {table:<22} CREATE failed: {e}")

    # Summary.
    print("\n[summary]")
    for table in LOAD_ORDER + DERIVED_TABLES:
        try:
            n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table:<24} {n:>6}")
        except duckdb.Error:
            pass

    conn.close()
    print(f"\n[done] rebuilt {args.db} from {args.parquet}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
