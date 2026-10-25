#!/usr/bin/env python3
"""Add one facilities_*.json file to the lake without touching the rest.

`scripts/ingest.py` upserts every data/raw/R*/ file (and re-derives the
region tables). For adding a handful of hand-researched records to a live
catalogue that is more than needed. This script takes the same agent-schema
JSON (see agents/README.md), checks it cannot collide with a facility
already in the lake, and inserts it with ingest.py's own `insert_records`,
so the two paths cannot drift.

The file must still live under data/raw/R*/facilities_*.json so that a full
ingest picks it up too.

Usage (the lake must exist: `scripts/rebuild_lake_from_parquet.py`)::

    python scripts/ingest_supplement.py data/raw/R4/facilities_conservancies.json
    python scripts/ingest_supplement.py <file> --dry-run   # checks only
    python scripts/ingest_supplement.py <file> --no-export # lake only

Steps:
  1. Validate facility_type / research_areas / networks against schema/vocab.
  2. Refuse a record that matches an existing facility from a different
     source file by URL, or by fuzzy name (>= 92) within 5 km — ingest.py's
     dedup rule. Re-running the same file is allowed and idempotent.
  3. Delete this file's previous locations / area_links / network_membership
     / facility_regions / provenance rows, then insert via
     ingest.insert_records (which normalises `ror` to the bare id).
  4. Point-in-polygon the new facilities against the regions already in the
     lake. populate_regions.populate() is not reused: it re-derives every
     region and every facility's containment, not just this file's.
  5. Check integrity and commit — steps 3-5 are one lake snapshot — then
     re-export db/parquet, public/parquet and public/facilities.geojson
     with export_parquet.main([]) (a scratch catalog exports to
     <catalog dir>/export/, never over the committed files).

`funders` must be empty: funding edges belong to the R9 / funding_events
scripts, which carry the amounts, years and award ids that a research
record does not.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import duckdb
from rapidfuzz import fuzz

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cod_db  # noqa: E402
import export_parquet  # noqa: E402
from ingest import (  # noqa: E402
    ROOT, VOCAB_DIR, Record, assign_ids, haversine_km, insert_records,
)
from populate_regions import containment, load_region_rows, overlay_files  # noqa: E402


def vocab(name: str) -> set[str]:
    with (VOCAB_DIR / name).open() as f:
        return {row["slug"] for row in csv.DictReader(f)}


def validate(records: list[Record]) -> list[str]:
    types = vocab("facility_types.csv")
    areas = vocab("research_areas.csv")
    nets = vocab("networks.csv")
    errs = []
    for r in records:
        d, name = r.raw, r.raw.get("canonical_name") or "?"
        if d.get("facility_type") not in types:
            errs.append(f"{name}: unknown facility_type {d.get('facility_type')!r}")
        for a in d.get("research_areas") or []:
            if a not in areas:
                errs.append(f"{name}: unknown research_area {a!r}")
        for n in d.get("networks") or []:
            if str(n).lower() not in nets:
                errs.append(f"{name}: unknown network {n!r}")
        if d.get("funders"):
            errs.append(f"{name}: funders must be empty (see module docstring)")
        hq = d.get("hq") or {}
        if hq.get("lat") is None or hq.get("lng") is None:
            errs.append(f"{name}: hq lat/lng required")
        prov = d.get("provenance") or {}
        if not prov.get("source_url") or not prov.get("confidence"):
            errs.append(f"{name}: provenance needs source_url + confidence")
    return errs


def collisions(conn: duckdb.DuckDBPyConnection, records: list[Record]) -> list[str]:
    ours = {r.fid for r in records}
    existing = cod_db.rows(
        conn,
        "SELECT facility_id, canonical_name, lower(trim(url)), hq_lat, hq_lng "
        "FROM main.facilities")
    errs = []
    for r in records:
        d = r.raw
        url = (d.get("url") or "").strip().lower() or None
        here = (d["hq"]["lat"], d["hq"]["lng"])
        for fid, name, eurl, lat, lng in existing:
            if fid in ours:
                continue  # our own earlier run
            if url and eurl == url:
                errs.append(f"{d['canonical_name']}: same URL as existing {name!r} ({fid})")
            elif (lat is not None and lng is not None
                  and fuzz.token_set_ratio(d["canonical_name"], name) >= 92
                  and haversine_km(here, (lat, lng)) < 5):
                errs.append(f"{d['canonical_name']}: fuzzy-matches existing {name!r} ({fid})")
    return errs


def link_regions(conn: duckdb.DuckDBPyConnection, records: list[Record]) -> int:
    """Containment edges for these facilities against the regions already
    in the lake (a polygon whose region is not there is skipped)."""
    known = {rid for (rid,) in cod_db.rows(conn, "SELECT region_id FROM main.regions")}
    rows = [r for r in load_region_rows(overlay_files()) if r["region_id"] in known]
    links = containment(conn, rows, [rec.fid for rec in records])
    conn.execute("CREATE OR REPLACE TEMP TABLE _sup_facility_regions "
                 "(facility_id VARCHAR, region_id VARCHAR)")
    if links:
        conn.executemany("INSERT INTO _sup_facility_regions VALUES (?, ?)", links)
    st = cod_db.merge_upsert(
        conn, "facility_regions", ["facility_id", "region_id"],
        "SELECT facility_id, region_id, 'within' AS relation, 0.0::DOUBLE AS distance_km "
        "FROM _sup_facility_regions", insert_only=True)
    conn.execute("DROP TABLE _sup_facility_regions")
    return st["unique_keys"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("file", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-export", action="store_true",
                    help="write the lake only; run scripts/export_parquet.py later")
    cod_db.add_argparse_catalog(ap)
    args = ap.parse_args(argv)

    path = args.file.resolve()
    agent = path.parent.name
    if not (path.parent.parent == ROOT / "data" / "raw" and agent.startswith("R")
            and path.name.startswith("facilities_")):
        print(f"[error] {args.file} must be data/raw/R*/facilities_*.json", file=sys.stderr)
        return 2

    records = [Record(agent=agent, raw=rec) for rec in json.loads(path.read_text())]
    assign_ids(records)
    if len({r.fid for r in records}) != len(records):
        print("[error] duplicate canonical_name|acronym within the file", file=sys.stderr)
        return 1

    errs = validate(records)
    try:
        conn = cod_db.connect(read_only=args.dry_run, catalog=args.catalog,
                              data=args.lake_data)
    except cod_db.LakeNotFound as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    rel = path.relative_to(ROOT)
    try:
        errs += collisions(conn, records)
        if errs:
            for e in errs:
                print(f"[error] {e}", file=sys.stderr)
            return 1
        if args.dry_run:
            print(f"[ok] {len(records)} records pass validation (dry run)")
            return 0

        fids = [r.fid for r in records]
        with cod_db.transaction(conn, f"ingest_supplement: {len(records)} facilities "
                                      f"from {rel}"):
            for table in ("locations", "area_links", "network_membership",
                          "facility_regions"):
                conn.execute(f"DELETE FROM main.{table} WHERE facility_id IN "
                             f"(SELECT unnest(?::VARCHAR[]))", [fids])
            conn.execute(
                "DELETE FROM main.provenance WHERE record_type = 'facility' "
                "AND record_id IN (SELECT unnest(?::VARCHAR[]))", [fids])
            insert_records(conn, records)
            n_links = link_regions(conn, records)
            cod_db.assert_integrity(conn, [
                "facilities", "locations", "area_links", "network_membership",
                "facility_regions", "provenance", "funders", "funding_events"])
        snap = cod_db.latest_snapshot(conn)
    finally:
        conn.close()
    print(f"[ok] inserted {len(records)} facilities from {rel} "
          f"(snapshot {snap.get('snapshot_id')})")
    print(f"[ok] facility_regions: {n_links} containment edges")

    if args.no_export:
        print("[next] python scripts/qa.py && python scripts/export_parquet.py")
        return 0
    export_argv = []
    if args.catalog is not None:
        export_argv += ["--catalog", str(args.catalog)]
    if args.lake_data is not None:
        export_argv += ["--lake-data", str(args.lake_data)]
    return export_parquet.main(export_argv)


if __name__ == "__main__":
    sys.exit(main())
