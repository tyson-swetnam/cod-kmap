#!/usr/bin/env python3
"""Load hand-curated agency-budget CSVs into funding_events.

Phase 2 of the funding research effort: 76 US federal facility units
(NMS sanctuaries, NPS coastal units, EPA NEPs, NERR reserves, NOAA OAR
labs, USGS science centers) don't appear as USAspending recipients
because they receive intramural appropriations from their parent
agency, not competitive grants. The amounts live in agency
Congressional Justification PDFs and annual reports.

This script reads CSVs in data/funding_research/agency_budgets/*.csv
and inserts them into funding_events. CSV format:

    facility_id,fiscal_year,amount_usd,funder_name,funder_type,
    program,relation,source_url,confidence,notes

Header is required. Columns:
    facility_id    — must match an existing facilities.facility_id
    fiscal_year    — 4-digit US fiscal year (Oct 1 of prev cal yr to Sep 30)
    amount_usd     — numeric, positive
    funder_name    — display name (auto-creates funder row if absent)
    funder_type    — federal | state | foundation | private (default federal)
    program        — agency program / line-item name (e.g. "NMS operating
                     allocation", "NERR Section 315 award + state match")
    relation       — appropriation | allocation | award | grant | match
                     | endowment | other (default 'appropriation')
    source_url     — direct URL to the budget book / annual report page
                     where this number was sourced
    confidence     — high | medium | low
    notes          — free-text qualifier

Idempotent via event_id = hash(funder|facility|"agency-budget"|fiscal_year).
Re-running an updated CSV REPLACES rows for the same (facility, fy)
that came from this loader. Rows from other sources (NSF, USAspending,
ProPublica) are untouched.

One run is one DuckLake snapshot (scripts/cod_db.py): every valid CSV
row is staged in a TEMP table and written with one MERGE into funders
and one into funding_events. A row whose amount/program/relation/
source_url/confidence/notes already match the lake is left alone (its
retrieved_at too), so re-running on loaded data changes nothing.
--dry-run opens the lake read-only and writes none. Publishing is
scripts/export_parquet.py's job.

Usage::

    python scripts/load_agency_budgets.py
    python scripts/load_agency_budgets.py --csv data/funding_research/agency_budgets/noaa_nerr.csv
    python scripts/load_agency_budgets.py --dry-run
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cod_db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DIR = ROOT / "data" / "funding_research" / "agency_budgets"

# Columns this loader owns on a matched funding_events row. retrieved_at
# moves only when one of them changes.
UPDATE_COLS = ("amount_usd", "program", "relation", "source_url",
               "confidence", "notes")


def _hash(s: str) -> str:
    return hashlib.blake2b(s.encode("utf-8"), digest_size=8).hexdigest()


def funder_id_for(name: str) -> str:
    return _hash((name or "").lower().strip())


def event_id_for(funder_id: str, facility_id: str,
                 fiscal_year: int) -> str:
    return _hash(f"{funder_id}|{facility_id}|agency-budget|{fiscal_year}")


def read_csv(path: Path, known_facilities: set[str], file_no: int,
             staged: list[tuple]) -> dict:
    """Validate one CSV and append its rows to ``staged``.

    Returns {path, rows_read, rows_valid, errors}. Each staged tuple is
    (_file, _seq, event_id, funder_id, funder_name, funder_type,
    facility_id, fiscal_year, amount_usd, program, relation, source_url,
    confidence, notes).
    """
    stats = {"path": str(path), "rows_read": 0, "rows_valid": 0, "errors": 0}
    if not path.exists():
        print(f"[error] CSV not found: {path}")
        stats["errors"] += 1
        return stats

    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(
            (line for line in fh if not line.lstrip().startswith("#")),
        )
        for ln, row in enumerate(reader, 2):  # header is row 1
            stats["rows_read"] += 1
            try:
                fid = (row.get("facility_id") or "").strip()
                fy = int((row.get("fiscal_year") or "0").strip() or 0)
                amt = float((row.get("amount_usd") or "0").strip() or 0)
                fname = (row.get("funder_name") or "").strip()
                ftype = (row.get("funder_type") or "federal").strip()
                program = (row.get("program") or "")[:200]
                relation = (row.get("relation") or "appropriation").strip()
                src_url = (row.get("source_url") or "").strip() or None
                confidence = (row.get("confidence") or "medium").strip()
                notes = (row.get("notes") or "")[:500]
            except (TypeError, ValueError) as e:
                print(f"[error] {path.name}:{ln} {e}")
                stats["errors"] += 1
                continue
            if not (fid and fy and amt and fname):
                print(f"[warn] {path.name}:{ln} missing required field — skip")
                stats["errors"] += 1
                continue
            if fid not in known_facilities:
                print(f"[warn] {path.name}:{ln} unknown facility_id={fid} — skip")
                stats["errors"] += 1
                continue
            funder_id = funder_id_for(fname)
            staged.append((file_no, len(staged), event_id_for(funder_id, fid, fy),
                           funder_id, fname, ftype or "federal", fid, fy, amt,
                           program, relation, src_url, confidence, notes))
            stats["rows_valid"] += 1
    return stats


STAGE_DDL = """
CREATE OR REPLACE TEMP TABLE _stage_agency_budgets (
    _file INTEGER, _seq INTEGER,
    event_id VARCHAR, funder_id VARCHAR, funder_name VARCHAR,
    funder_type VARCHAR, facility_id VARCHAR, fiscal_year INTEGER,
    amount_usd DOUBLE, program VARCHAR, relation VARCHAR,
    source_url VARCHAR, confidence VARCHAR, notes VARCHAR)
"""


def write_staged(conn, staged: list[tuple]) -> dict:
    """Two MERGEs from the staged rows; returns per-file write counts.

    funders: a new funder is inserted with country 'US' (the first CSV row
    naming it wins); an existing one only gets a missing type filled.
    funding_events: last CSV row for an event_id wins, as the old per-row
    UPDATE did; a matched row is rewritten only when a loader-owned
    column changed.
    """
    conn.execute(STAGE_DDL)
    conn.executemany(
        "INSERT INTO _stage_agency_budgets VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", staged)

    # Per-file counts, against the lake as it is before this run: a row is
    # "replaced" when its event already exists (in the lake, or earlier in
    # this run), "written" when it is new.
    per_file = {f: {"rows_written": 0, "rows_replaced": 0} for f, *_ in staged}
    for f, replaced, n in cod_db.rows(conn, """
            SELECT _file,
                   (row_number() OVER (PARTITION BY event_id ORDER BY _seq) > 1
                    OR EXISTS (SELECT 1 FROM funding_events e
                               WHERE e.event_id = s.event_id)) AS replaced,
                   1
            FROM _stage_agency_budgets s"""):
        per_file[f]["rows_replaced" if replaced else "rows_written"] += n

    conn.execute("""
        MERGE INTO funders t
        USING (SELECT DISTINCT ON (funder_id) funder_id, funder_name, funder_type
               FROM _stage_agency_budgets ORDER BY funder_id, _seq) s
          ON t.funder_id = s.funder_id
        WHEN MATCHED AND t.type IS NULL THEN UPDATE SET type = s.funder_type
        WHEN NOT MATCHED THEN INSERT (funder_id, name, type, country)
          VALUES (s.funder_id, s.funder_name, s.funder_type, 'US')""")

    changed = " OR ".join(f"t.{c} IS DISTINCT FROM s.{c}" for c in UPDATE_COLS)
    sets = ", ".join(f"{c} = s.{c}" for c in UPDATE_COLS)
    conn.execute(f"""
        MERGE INTO funding_events t
        USING (SELECT DISTINCT ON (event_id) *
               FROM _stage_agency_budgets ORDER BY event_id, _seq DESC) s
          ON t.event_id = s.event_id
        WHEN MATCHED AND ({changed})
          THEN UPDATE SET {sets}, retrieved_at = current_date
        WHEN NOT MATCHED THEN INSERT (
            event_id, funder_id, facility_id, amount_usd, amount_currency,
            fiscal_year, period_start, period_end, award_id, award_title,
            program, relation, source, source_url, retrieved_at,
            confidence, notes)
          VALUES (s.event_id, s.funder_id, s.facility_id, s.amount_usd, 'USD',
                  s.fiscal_year, NULL, NULL, NULL, NULL,
                  s.program, s.relation, 'agency budget research', s.source_url,
                  current_date, s.confidence, s.notes)""")
    conn.execute("DROP TABLE _stage_agency_budgets")
    return per_file


def main() -> int:
    ap = argparse.ArgumentParser()
    cod_db.add_argparse_catalog(ap)
    ap.add_argument("--db", dest="catalog", type=Path, help="deprecated alias of --catalog")
    ap.add_argument("--csv", type=Path, action="append",
                    help="Specific CSV file to load. Repeatable. Default "
                         "is to glob data/funding_research/agency_budgets/*.csv")
    ap.add_argument("--dir", type=Path, default=DEFAULT_DIR)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        conn = cod_db.connect(read_only=args.dry_run, catalog=args.catalog,
                              data=args.lake_data)
    except cod_db.LakeNotFound as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2

    if args.csv:
        files = list(args.csv)
    else:
        if not args.dir.exists():
            print(f"[info] no agency-budget CSVs yet at {args.dir}")
            return 0
        files = sorted(args.dir.glob("*.csv"))

    if not files:
        print("[info] no CSVs to load")
        return 0

    known = {r[0] for r in cod_db.rows(conn, "SELECT facility_id FROM facilities")}
    staged: list[tuple] = []
    per_csv = [read_csv(f, known, i, staged) for i, f in enumerate(files)]

    writes: dict = {}
    if staged and not args.dry_run:
        with cod_db.transaction(conn, f"load_agency_budgets: {len(staged)} agency-budget "
                                      f"funding_events rows from {len(files)} CSV(s)"):
            writes = write_staged(conn, staged)
            cod_db.assert_integrity(conn, ["funders", "funding_events"])

    grand = {"rows_read": 0, "rows_written": 0, "rows_replaced": 0, "errors": 0}
    for i, (f, s) in enumerate(zip(files, per_csv)):
        w = writes.get(i, {"rows_written": 0, "rows_replaced": 0})
        print(f"[load] {f.relative_to(ROOT) if f.is_relative_to(ROOT) else f}")
        print(f"  read={s['rows_read']}  wrote={w['rows_written']}  "
              f"replaced={w['rows_replaced']}  errors={s['errors']}")
        grand["rows_read"] += s["rows_read"]
        grand["errors"] += s["errors"]
        grand["rows_written"] += w["rows_written"]
        grand["rows_replaced"] += w["rows_replaced"]

    print(f"\n[done] read={grand['rows_read']}  wrote={grand['rows_written']}  "
          f"replaced={grand['rows_replaced']}  errors={grand['errors']}"
          + ("  (dry-run: nothing written)" if args.dry_run else ""))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
