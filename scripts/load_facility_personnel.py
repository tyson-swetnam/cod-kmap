#!/usr/bin/env python3
"""Load a facility_personnel seed CSV into people + facility_personnel.

Workflow:
  1. User fills in data/seed/facility_personnel_seed.csv (or any CSV with
     the same columns) by hand from institutional webpages.
  2. Run this script — each row becomes a people row (upserted on
     person_id = hash(lower(name) || orcid-or-email-or-idx)) plus a
     facility_personnel row tying the person to the facility.

Facility matching:
  - If `facility_acronym` is set, match facilities.acronym exactly. An
    acronym several facilities share resolves only to the one whose
    canonical_name appears in the row's `title`.
  - Else if `facility_name_like` is set, match canonical_name ILIKE %…%
    and require exactly one hit; otherwise skip the row with a warning.

Usage::

    python scripts/load_facility_personnel.py
    python scripts/load_facility_personnel.py --csv data/seed/facility_personnel_seed.csv
    python scripts/load_facility_personnel.py --catalog /tmp/scratch/cat.sqlite

Writes the DuckLake store of record (scripts/cod_db.py): the whole run is
one snapshot, rolled back if it breaks a schema.sql constraint. Publish
with scripts/export_parquet.py (``--export-parquet`` is a no-op now).

Idempotent: rows are staged in a TEMP table and MERGEd on the primary
keys, so re-running with the same CSV updates rows in place rather than
duplicating. Only rows the CSV would actually change are written (and
get a fresh ``updated_at`` / ``retrieved_at``), so a re-run on
already-loaded data commits nothing.

The CSV's ``email`` column feeds the person_id hash only: contact
details are not written to ``people`` (export_parquet.py redacts them
anyway).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from pathlib import Path
from typing import Iterable, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cod_db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CSV = ROOT / "data" / "seed" / "facility_personnel_seed.csv"

# Roles that default to is_key_personnel=true when the CSV cell is blank.
KEY_ROLE_DEFAULTS = {
    "Director",
    "Deputy Director",
    "Associate Director",
    "Chief Scientist",
    "Principal Investigator",
    "Head Administrator",
    "Program Manager",
    "President & Director",
    "Executive Director",
}

# Staging layouts (the leading _seq column is CSV order: last row wins).
PEOPLE_STAGE = [("_seq", "INTEGER"), ("person_id", "VARCHAR"), ("name", "VARCHAR"),
                ("name_family", "VARCHAR"), ("name_given", "VARCHAR"),
                ("orcid", "VARCHAR"), ("openalex_id", "VARCHAR"),
                ("homepage_url", "VARCHAR")]
ROLE_STAGE = [("_seq", "INTEGER"), ("person_id", "VARCHAR"), ("facility_id", "VARCHAR"),
              ("role", "VARCHAR"), ("title", "VARCHAR"), ("is_key_personnel", "BOOLEAN"),
              ("source", "VARCHAR"), ("source_url", "VARCHAR"),
              ("confidence", "VARCHAR"), ("notes", "VARCHAR")]
# people: name parts are overwritten, identifiers only filled in
# (the old upsert's SET x = COALESCE(excluded.x, people.x)).
PEOPLE_OVERWRITE = ["name", "name_family", "name_given"]
PEOPLE_COALESCE = ["orcid", "openalex_id", "homepage_url"]
ROLE_OVERWRITE = ["title", "is_key_personnel", "source", "source_url", "confidence"]


def person_id(name: str, orcid: str = "", email: str = "") -> str:
    """Stable hash so re-runs upsert instead of duplicating. ORCID is the
    strongest disambiguator; fall back to email, then name alone."""
    key = f"{name.strip().lower()}|{(orcid or '').strip()}|{(email or '').strip().lower()}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def split_name(full: str) -> tuple[str, str]:
    """Rough 'First Middle Last' split. Tolerates unusual names by
    returning ('', full) if no space."""
    parts = full.strip().split()
    if len(parts) < 2:
        return "", full.strip()
    return " ".join(parts[:-1]), parts[-1]


def resolve_facility(conn, acronym: str, name_like: str,
                     title: str = "") -> str | None:
    if acronym:
        hits = cod_db.rows(
            conn,
            "SELECT facility_id, canonical_name FROM facilities WHERE acronym = ? "
            "ORDER BY facility_id",
            [acronym.strip()],
        )
        if len(hits) == 1:
            return hits[0][0]
        if hits:
            # A shared acronym (29 facilities are "NERR", two "GRC") used to
            # resolve to whichever row the DuckDB file happened to return
            # first; the lake has no stable physical order. Pick the one
            # facility whose name the row's title spells out, else fall
            # through to name_like, else skip.
            named = [fid for fid, cname in hits
                     if cname and cname.lower() in (title or "").lower()]
            if len(named) == 1:
                return named[0]
            print(f"[warn] acronym {acronym!r} is shared by {len(hits)} facilities; "
                  f"trying name_like")
    if name_like:
        rows = cod_db.rows(
            conn,
            "SELECT facility_id, canonical_name FROM facilities "
            "WHERE canonical_name ILIKE ? ORDER BY canonical_name",
            [f"%{name_like.strip()}%"],
        )
        if len(rows) == 1:
            return rows[0][0]
        if len(rows) > 1:
            names = ", ".join(r[1] for r in rows)
            print(f"[warn] ambiguous name_like={name_like!r} matched: {names}")
    return None


def is_key_flag(raw: str | None, role: str, defaults: set[str]) -> bool:
    raw_key = (raw or "").strip().lower()
    if raw_key in ("true", "1", "yes", "y"):
        return True
    if raw_key in ("false", "0", "no", "n"):
        return False
    return role in defaults


# ---------------------------------------------------------------------------
# Staging + MERGE (shared with load_facility_personnel_by_id.py)
# ---------------------------------------------------------------------------

def stage_rows(conn, name: str, columns: Sequence[tuple[str, str]],
               rows: Iterable[Sequence]) -> str:
    """(Re)create TEMP table ``name`` and bulk-insert ``rows`` into it."""
    conn.execute(f"CREATE OR REPLACE TEMP TABLE {name} ("
                 + ", ".join(f"{cod_db.qident(c)} {t}" for c, t in columns) + ")")
    rows = list(rows)
    if rows:
        conn.executemany(f"INSERT INTO {name} VALUES ("
                         + ", ".join("?" for _ in columns) + ")", rows)
    return name


def changed_sql(src: str, table: str, keys: Sequence[str],
                overwrite: Sequence[str] = (), coalesce: Sequence[str] = (),
                extra: Sequence[str] = ()) -> str:
    """SELECT the rows of ``src`` that are new to ``table`` or would change
    one of its columns: an ``overwrite`` column that differs, a
    ``coalesce`` column whose non-NULL source value differs, or any
    ``extra`` predicate over ``s`` (source) and ``t`` (target)."""
    q = cod_db.qident
    on = " AND ".join(f"t.{q(k)} = s.{q(k)}" for k in keys)
    preds = [f"t.{q(keys[0])} IS NULL"]
    preds += [f"s.{q(c)} IS DISTINCT FROM t.{q(c)}" for c in overwrite]
    preds += [f"(s.{q(c)} IS NOT NULL AND s.{q(c)} IS DISTINCT FROM t.{q(c)})"
              for c in coalesce]
    preds += [f"({p})" for p in extra]
    return (f"SELECT s.* FROM {src} s LEFT JOIN {q(table)} t ON {on} "
            f"WHERE " + " OR ".join(preds))


# facility_personnel.notes is appended to, never replaced: a blank CSV cell
# keeps the note another script added (fix_epa_region_affiliations.py
# backfills one on every EPA region row), and a note already present is
# not appended twice. Without this every re-run erased those notes.
NOTES_CHANGED = ("s.notes IS NOT NULL AND "
                 "position(s.notes IN COALESCE(t.notes, '')) = 0")
NOTES_SET = {"notes": "CASE WHEN s.notes IS NULL OR "
                      "position(s.notes IN COALESCE(t.notes, '')) > 0 THEN t.notes "
                      "ELSE trim(COALESCE(t.notes, '') || ' ' || s.notes) END"}


def merge_people(conn, stage: str) -> dict:
    """MERGE staged people rows (PEOPLE_STAGE layout) into ``people``.

    Collapses repeated person_ids the way the old row-by-row upsert did:
    name parts from the last CSV row, each identifier from the last row
    that carried one. Unchanged people are not touched.
    """
    agg = ", ".join(
        [f"arg_max({c}, _seq) AS {c}" for c in PEOPLE_OVERWRITE]
        + [f"arg_max({c}, _seq) FILTER (WHERE {c} IS NOT NULL) AS {c}"
           for c in PEOPLE_COALESCE])
    conn.execute(f"CREATE OR REPLACE TEMP TABLE {stage}_agg AS "
                 f"SELECT person_id, {agg} FROM {stage} GROUP BY person_id")
    res = cod_db.merge_upsert(
        conn, "people", ["person_id"],
        f"SELECT *, CAST(now() AS TIMESTAMP) AS updated_at FROM ("
        + changed_sql(f"{stage}_agg", "people", ["person_id"],
                      PEOPLE_OVERWRITE, PEOPLE_COALESCE) + ")",
        coalesce=PEOPLE_COALESCE)
    res["staged_people"] = cod_db.scalar(conn, f"SELECT count(*) FROM {stage}_agg")
    conn.execute(f"DROP TABLE {stage}_agg")
    return res


def merge_roles(conn, stage: str) -> dict:
    """MERGE staged facility_personnel rows (ROLE_STAGE layout); the last
    CSV row per (person_id, facility_id, role) wins, ROLE_OVERWRITE columns
    are overwritten, notes appended (NOTES_SET) and retrieved_at is today —
    but only for rows that are new or actually change. start_date /
    end_date are never written by a seed CSV."""
    conn.execute(f"CREATE OR REPLACE TEMP TABLE {stage}_d AS "
                 f"SELECT DISTINCT ON (person_id, facility_id, role) * FROM {stage} "
                 f"ORDER BY person_id, facility_id, role, _seq DESC")
    keys = ["person_id", "facility_id", "role"]
    res = cod_db.merge_upsert(
        conn, "facility_personnel", keys,
        f"SELECT *, current_date AS retrieved_at FROM ("
        + changed_sql(f"{stage}_d", "facility_personnel", keys, ROLE_OVERWRITE,
                      extra=[NOTES_CHANGED]) + ")",
        update_cols=ROLE_OVERWRITE + ["retrieved_at"], extra_set=NOTES_SET)
    res["staged_roles"] = cod_db.scalar(conn, f"SELECT count(*) FROM {stage}_d")
    conn.execute(f"DROP TABLE {stage}_d")
    return res


def catalog_arg(args) -> Path | None:
    """--db is the pre-lake flag, kept as an alias of --catalog. The retired
    db/cod_kmap.duckdb file itself is not a catalog, so it is ignored."""
    if args.db is None or args.catalog is not None:
        return args.catalog
    if args.db.suffix == ".duckdb":
        print(f"[deprecated] --db {args.db} ignored (db/cod_kmap.duckdb is retired); "
              f"use --catalog", file=sys.stderr)
        return None
    return args.db


def add_db_alias(ap: argparse.ArgumentParser) -> None:
    cod_db.add_argparse_catalog(ap)
    ap.add_argument("--db", type=Path, default=None,
                    help="deprecated alias of --catalog (a .duckdb path is ignored)")


def main() -> int:
    ap = argparse.ArgumentParser()
    add_db_alias(ap)
    ap.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    ap.add_argument("--export-parquet", action="store_true",
                    help="deprecated no-op: run scripts/export_parquet.py to publish")
    args = ap.parse_args()
    if not args.csv.exists():
        print(f"[error] csv not found: {args.csv}", file=sys.stderr)
        return 2
    try:
        conn = cod_db.connect(catalog=catalog_arg(args), data=args.lake_data)
    except cod_db.LakeNotFound as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2

    ok = skipped = 0
    people_rows: list[tuple] = []
    role_rows: list[tuple] = []
    with args.csv.open() as fh, \
            cod_db.transaction(conn, f"load_facility_personnel: {args.csv.name}"):
        rdr = csv.DictReader(fh)
        for seq, row in enumerate(rdr):
            # Support '#'-prefixed comment rows.
            fac_acr  = (row.get("facility_acronym")  or "").strip()
            name_lk  = (row.get("facility_name_like") or "").strip()
            pname    = (row.get("person_name")       or "").strip()
            role     = (row.get("role")              or "").strip()
            if fac_acr.startswith("#") or not pname or not role:
                skipped += 1
                continue
            if None in row:
                # An unquoted comma shifts every later column (into orcid,
                # and so into the person_id hash): refuse the row.
                print(f"[skip] line {rdr.line_num}: more fields than the header; "
                      f"quote fields that contain commas")
                skipped += 1
                continue
            fid = resolve_facility(conn, fac_acr, name_lk, row.get("title") or "")
            if not fid:
                print(f"[skip] could not resolve facility for "
                      f"acronym={fac_acr!r}, name_like={name_lk!r}")
                skipped += 1
                continue
            orcid = (row.get("orcid") or "").strip()
            pid = person_id(pname, orcid, (row.get("email") or "").strip())
            given, family = split_name(pname)
            people_rows.append((seq, pid, pname, family, given, orcid or None,
                                (row.get("openalex_id") or "").strip() or None,
                                (row.get("homepage_url") or "").strip() or None))
            role_rows.append((seq, pid, fid, role,
                              (row.get("title") or "").strip() or None,
                              is_key_flag(row.get("is_key_personnel"), role,
                                          KEY_ROLE_DEFAULTS),
                              (row.get("source") or "manual").strip(),
                              (row.get("source_url") or "").strip() or None,
                              (row.get("confidence") or "medium").strip(),
                              (row.get("notes") or "").strip() or None))
            ok += 1

        stage_rows(conn, "stage_people", PEOPLE_STAGE, people_rows)
        stage_rows(conn, "stage_roles", ROLE_STAGE, role_rows)
        rp = merge_people(conn, "stage_people")
        rr = merge_roles(conn, "stage_roles")
        cod_db.assert_integrity(conn, ["people", "facility_personnel"])

    print(f"[load] ok={ok} skipped={skipped}")
    print(f"[merge] people: {rp['staged_people']} staged, {rp['inserted']} new, "
          f"{rp['matched']} updated; facility_personnel: {rr['staged_roles']} staged, "
          f"{rr['inserted']} new, {rr['matched']} updated")

    if args.export_parquet:
        print("[parquet] --export-parquet is a no-op now: the lake is the store of "
              "record; run scripts/export_parquet.py (the only parquet writer)")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
