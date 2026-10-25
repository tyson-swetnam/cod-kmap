#!/usr/bin/env python3
"""Load a facility_personnel CSV that uses facility_id directly.

Adapter for round-2 personnel CSVs whose subagents emit `facility_id`
(stronger key) instead of `facility_acronym` / `facility_name_like`
(the legacy loader's fields). Same upsert semantics as
`load_facility_personnel.py` — idempotent on (person_id, facility_id,
role), and it shares that script's staging + MERGE helpers.

CSV header (in order, exactly):

    facility_id,role,name,title,is_key_personnel,start_date,end_date,
    source,source_url,confidence,notes,orcid,openalex_id,homepage_url,email

Usage::

    python scripts/load_facility_personnel_by_id.py --csv path.csv
    python scripts/load_facility_personnel_by_id.py --csv-glob 'data/seed/*.csv'

Writes the DuckLake store of record (scripts/cod_db.py): every CSV of the
run is staged in TEMP tables and merged in one snapshot, rolled back whole
if a row breaks a schema.sql constraint. Rows the CSVs would not change
are not written, so a re-run commits nothing. The `email` column feeds the
person_id hash only; it is not stored. Publish with export_parquet.py.
"""
from __future__ import annotations

import argparse
import csv
import sys
from glob import glob
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cod_db  # noqa: E402
from load_facility_personnel import (PEOPLE_STAGE, ROLE_STAGE, add_db_alias,  # noqa: E402
                                     catalog_arg, is_key_flag, merge_people,
                                     merge_roles, person_id, split_name, stage_rows)

ROOT = Path(__file__).resolve().parent.parent

KEY_ROLE_DEFAULTS = {
    "Director", "Executive Director", "Deputy Director", "Associate Director",
    "Chief Scientist", "Principal Investigator", "Co-Principal Investigator",
    "Reserve Manager", "Program Manager", "Head Administrator",
    "President & Director", "Research Coordinator", "Research Director",
    "Scientific Director", "Education Coordinator", "Stewardship Coordinator",
    "Manager",
}


def load_one(path: Path, facility_ids: set[str], seq0: int,
             people_rows: list, role_rows: list) -> dict:
    """Parse one CSV into the staging lists. No database access: facility
    ids are checked against ``facility_ids`` (read once per run)."""
    stats = {"path": str(path), "ok": 0, "skipped": 0, "errors": 0}
    if not path.exists():
        print(f"[error] {path} not found")
        stats["errors"] += 1
        return stats
    with path.open(newline="", encoding="utf-8") as fh:
        for ln, row in enumerate(csv.DictReader(fh), 2):
            try:
                if None in row:
                    # An unquoted comma shifts every later column: the
                    # notes text of two round-2 rows landed in `orcid` (and
                    # so in their person_id hash). Refuse the row.
                    print(f"[skip] {path.name}:{ln} more fields than the header; "
                          f"quote fields that contain commas")
                    stats["skipped"] += 1
                    continue
                fid = (row.get("facility_id") or "").strip()
                name = (row.get("name") or "").strip()
                role = (row.get("role") or "").strip()
                if not (fid and name and role):
                    stats["skipped"] += 1
                    continue
                # Validate facility exists.
                if fid not in facility_ids:
                    print(f"[skip] {path.name}:{ln} unknown facility_id={fid}")
                    stats["skipped"] += 1
                    continue
                orcid = (row.get("orcid") or "").strip()
                pid = person_id(name, orcid, (row.get("email") or "").strip())
                given, family = split_name(name)
                seq = seq0 + ln
                people_rows.append((seq, pid, name, family, given, orcid or None,
                                    (row.get("openalex_id") or "").strip() or None,
                                    (row.get("homepage_url") or "").strip() or None))
                role_rows.append((seq, pid, fid, role,
                                  (row.get("title") or "").strip() or None,
                                  is_key_flag(row.get("is_key_personnel"), role,
                                              KEY_ROLE_DEFAULTS),
                                  (row.get("source") or "").strip() or "facility-webpage",
                                  (row.get("source_url") or "").strip() or None,
                                  (row.get("confidence") or "").strip() or "medium",
                                  (row.get("notes") or "").strip() or None))
                stats["ok"] += 1
            except Exception as e:  # noqa: BLE001 — a bad row skips, the run goes on
                print(f"[error] {path.name}:{ln} {e}")
                stats["errors"] += 1
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    add_db_alias(ap)
    ap.add_argument("--csv", type=Path, action="append")
    ap.add_argument("--csv-glob", type=str, default=None)
    args = ap.parse_args()

    paths: list[Path] = []
    if args.csv:
        paths.extend(args.csv)
    if args.csv_glob:
        paths.extend(Path(p) for p in sorted(glob(args.csv_glob)))
    if not paths:
        print("[error] need --csv or --csv-glob")
        return 2

    try:
        conn = cod_db.connect(catalog=catalog_arg(args), data=args.lake_data)
    except cod_db.LakeNotFound as e:
        print(f"[error] {e}")
        return 2
    grand = {"ok": 0, "skipped": 0, "errors": 0}
    people_rows: list[tuple] = []
    role_rows: list[tuple] = []
    names = ", ".join(p.name for p in paths[:3]) + (f" +{len(paths) - 3}" if len(paths) > 3 else "")
    try:
        with cod_db.transaction(conn, f"load_facility_personnel_by_id: {names}"):
            facility_ids = {r[0] for r in cod_db.rows(conn, "SELECT facility_id FROM facilities")}
            for i, p in enumerate(paths):
                # seq orders rows across files: a later file's row wins, as
                # it did when each row was upserted in turn.
                s = load_one(p, facility_ids, i * 1_000_000, people_rows, role_rows)
                for k in grand:
                    grand[k] += s[k]
                print(f"  {p.name}: ok={s['ok']} skipped={s['skipped']} errors={s['errors']}")
            stage_rows(conn, "stage_people", PEOPLE_STAGE, people_rows)
            stage_rows(conn, "stage_roles", ROLE_STAGE, role_rows)
            rp = merge_people(conn, "stage_people")
            rr = merge_roles(conn, "stage_roles")
            cod_db.assert_integrity(conn, ["people", "facility_personnel"])
    except (duckdb.Error, cod_db.IntegrityError) as e:
        print(f"[error] rolled back, nothing written: {e}", file=sys.stderr)
        conn.close()
        return 1
    print(f"[done] ok={grand['ok']} skipped={grand['skipped']} errors={grand['errors']}")
    print(f"[merge] people: {rp['staged_people']} staged, {rp['inserted']} new, "
          f"{rp['matched']} updated; facility_personnel: {rr['staged_roles']} staged, "
          f"{rr['inserted']} new, {rr['matched']} updated")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
