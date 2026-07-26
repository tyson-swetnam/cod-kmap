#!/usr/bin/env python3
"""Build the COD project team layer as a DuckLake, and sync it to people.

The COD org chart is hand-transcribed into two seed CSVs:

    data/seed/cod_wbs.csv            the Work Breakdown Structure tree
    data/seed/cod_team_members.csv   one row per (person, WBS element, role)

This script loads them into a **DuckLake** catalogue (db/cod_team.ducklake
plus its managed parquet under db/ducklake_data/), so each run lands as a
new snapshot and the roster's history is queryable:

    ATTACH 'ducklake:db/cod_team.ducklake' AS teamlake;
    SELECT * FROM teamlake.snapshots();
    SELECT * FROM teamlake.cod_team_members AT (VERSION => 1);

Both the catalogue and its data directory are gitignored, exactly like
db/cod_kmap.duckdb: the DuckLake is a local convenience, and the shared
canonical artifact is still plain parquet in db/parquet/ + public/parquet/
(which is also what DuckDB-Wasm reads in the browser). Everything is
regenerable from the two seed CSVs, so losing the lake costs nothing.

If the ducklake extension is unavailable — no network to
extensions.duckdb.org and no bundled wheel — the script says so and falls
back to plain tables in the main DB. The parquet output is byte-for-byte
the same either way, so a fallback run is not a degraded run.

Named members are also upserted into `people` so the existing Researcher
directory, enrichment scripts, and metrics pipeline pick them up. The
upsert COALESCEs every enrichable column, so a blank cell in the seed CSV
never wipes a value that scripts/enrich_people_*.py resolved earlier.

Usage::

    python scripts/build_cod_team_lake.py
    python scripts/build_cod_team_lake.py --no-ducklake     # skip the lake
    python scripts/build_cod_team_lake.py --dry-run
    python scripts/build_cod_team_lake.py --history         # show snapshots

Idempotent: re-running with unchanged seeds leaves identical tables (a new
DuckLake snapshot is still recorded, which is the point of the lake).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "db" / "cod_kmap.duckdb"
WBS_CSV = ROOT / "data" / "seed" / "cod_wbs.csv"
MEMBERS_CSV = ROOT / "data" / "seed" / "cod_team_members.csv"
CATALOG = ROOT / "db" / "cod_team.ducklake"
LAKE_DATA = ROOT / "db" / "ducklake_data"
PARQUET_OUT = [ROOT / "db" / "parquet", ROOT / "public" / "parquet"]

# Institution slugs from the org chart's colour legend, plus the extra
# affiliations named in member boxes. Validated so a typo in the seed CSV
# can't silently produce an uncoloured chip in the Team tab.
COD_INSTITUTIONS = {
    "clemson", "yale", "unm", "battelle", "vcu", "pnnl", "unl", "obfs",
    "uga", "arizona", "delaware", "usc", "uidaho", "montana-state",
    "alabama", "florida", "coastal-carolina", "charleston",
    "other-university", "agency", "company", "various",
}
MEMBER_STATUSES = {"active", "tbd", "tbh"}

WBS_COLS = ["wbs_code", "parent_code", "title", "lead_person_id", "sort_order", "notes"]
MEMBER_COLS = [
    "member_id", "person_id", "display_name", "wbs_code", "role", "institution",
    "institution_slug", "is_pi", "is_copi", "is_leadership_committee",
    "committees", "status", "sort_order", "source", "notes",
]
LAKE_TABLES = ["cod_wbs", "cod_team_members"]
# people changes too (the sync), so its parquet is refreshed alongside.
EXPORT_TABLES = LAKE_TABLES + ["people"]


def person_id(name: str, orcid: str = "", email: str = "") -> str:
    """Stable person hash. Identical formula to
    scripts/load_facility_personnel.py:54 so the same human seeded through
    either path collapses onto one people row."""
    key = f"{name.strip().lower()}|{(orcid or '').strip()}|{(email or '').strip().lower()}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def split_name(full: str) -> tuple[str, str]:
    parts = full.strip().split()
    if len(parts) < 2:
        return "", full.strip()
    return " ".join(parts[:-1]), parts[-1]


def as_bool(raw: str | None) -> bool:
    return (raw or "").strip().lower() in ("true", "1", "yes", "y")


def as_int(raw: str | None) -> int | None:
    raw = (raw or "").strip()
    try:
        return int(raw)
    except ValueError:
        return None


def read_seed(path: Path) -> list[dict]:
    """Read a seed CSV, skipping the leading `#` comment block and any
    `#`-prefixed rows (the seeds document their own provenance inline)."""
    if not path.exists():
        print(f"[error] seed not found: {path}", file=sys.stderr)
        raise SystemExit(2)
    lines = [ln for ln in path.read_text().splitlines() if not ln.lstrip().startswith("#")]
    return [row for row in csv.DictReader(lines)
            if any((v or "").strip() for v in row.values())]


def build_rows(wbs_raw: list[dict], members_raw: list[dict]) -> tuple[list, list, list[str]]:
    """Turn seed dicts into insert-ready tuples. Returns (wbs, members,
    fatal_problems)."""
    fatal: list[str] = []

    wbs_rows, wbs_codes = [], set()
    for r in wbs_raw:
        code = (r.get("wbs_code") or "").strip()
        if not code:
            fatal.append("cod_wbs: row with empty wbs_code")
            continue
        if code in wbs_codes:
            fatal.append(f"cod_wbs: duplicate wbs_code {code!r}")
            continue
        wbs_codes.add(code)
        wbs_rows.append([
            code,
            (r.get("parent_code") or "").strip() or None,
            (r.get("title") or "").strip(),
            None,  # lead_person_id: filled below from the members' roles
            as_int(r.get("sort_order")),
            (r.get("notes") or "").strip() or None,
        ])
    for row in wbs_rows:
        parent = row[1]
        if parent and parent not in wbs_codes:
            fatal.append(f"cod_wbs: {row[0]} parent_code {parent!r} not in the WBS")

    member_rows: list[list] = []
    seen_pk: set[tuple[str, str, str]] = set()
    tbd_seq: dict[str, int] = {}
    n_pi = n_copi = n_slc = 0
    # A person's identity is keyed on (name, email) so their two WBS rows
    # share one person_id.
    for r in members_raw:
        name = (r.get("display_name") or "").strip()
        wbs = (r.get("wbs_code") or "").strip()
        role = (r.get("role") or "").strip()
        status = (r.get("status") or "active").strip().lower()
        slug = (r.get("institution_slug") or "").strip() or None

        if not name or not wbs or not role:
            fatal.append(f"cod_team_members: incomplete row {name!r}/{wbs!r}/{role!r}")
            continue
        if status not in MEMBER_STATUSES:
            fatal.append(f"cod_team_members: {name}: status {status!r} not in {MEMBER_STATUSES}")
        if slug and slug not in COD_INSTITUTIONS:
            fatal.append(f"cod_team_members: {name}: institution_slug {slug!r} unknown")
        if wbs not in wbs_codes:
            fatal.append(f"cod_team_members: {name}: wbs_code {wbs!r} not in the WBS")

        orcid = (r.get("orcid") or "").strip()
        email = (r.get("email") or "").strip()
        if status == "active":
            pid = person_id(name, orcid, email)
            mid = pid
        else:
            # Unfilled position: no person, but still needs a stable key.
            tbd_seq[wbs] = tbd_seq.get(wbs, 0) + 1
            pid = None
            mid = f"{status}-{wbs}-{tbd_seq[wbs]}"

        pk = (mid, wbs, role)
        if pk in seen_pk:
            fatal.append(f"cod_team_members: duplicate (member,wbs,role) for {name} @ {wbs}/{role}")
            continue
        seen_pk.add(pk)

        is_pi, is_copi = as_bool(r.get("is_pi")), as_bool(r.get("is_copi"))
        is_slc = as_bool(r.get("is_leadership_committee"))
        n_pi += is_pi
        n_copi += is_copi
        n_slc += is_slc

        member_rows.append([
            mid, pid, name, wbs, role,
            (r.get("institution") or "").strip() or None,
            slug, is_pi, is_copi, is_slc,
            (r.get("committees") or "").strip() or None,
            status,
            as_int(r.get("sort_order")),
            (r.get("source") or "org-chart-2026").strip(),
            (r.get("notes") or "").strip() or None,
        ])

    # The chart has exactly one PI; the award has several Co-PIs. Catching
    # a violation here beats discovering two hero cards in the Team tab.
    pi_people = {row[2] for row in member_rows if row[7]}
    if len(pi_people) != 1:
        fatal.append(f"cod_team_members: expected exactly 1 PI, found {len(pi_people)}: {pi_people}")
    copi_people = {row[2] for row in member_rows if row[8]}
    if len(copi_people) < 3:
        fatal.append(f"cod_team_members: expected >=3 Co-PIs, found {len(copi_people)}")
    if n_slc < 10:
        fatal.append(f"cod_team_members: expected >=10 leadership-committee rows, found {n_slc}")

    # Backfill cod_wbs.lead_person_id from the first active member on each
    # element (chart reading order == seed sort_order).
    lead_by_wbs: dict[str, str] = {}
    for row in sorted((r for r in member_rows if r[1]), key=lambda r: (r[12] or 9999)):
        lead_by_wbs.setdefault(row[3], row[1])
    for row in wbs_rows:
        row[3] = lead_by_wbs.get(row[0])

    return wbs_rows, member_rows, fatal


def attach_lake(conn) -> str:
    """Try to attach a DuckLake catalogue; return the schema name to write
    to ('teamlake' on success, 'main' on fallback)."""
    try:
        from duckdb_extensions import import_extension  # noqa: PLC0415
        import_extension("ducklake")
        print("[lake] ducklake extension loaded from the bundled wheel")
    except Exception:  # noqa: BLE001 - the wheel is optional
        try:
            conn.execute("INSTALL ducklake;")
            print("[lake] ducklake extension installed from extensions.duckdb.org")
        except duckdb.Error as e:
            print(f"[warn] ducklake unavailable ({str(e).splitlines()[0]})")
            print("[warn] falling back to plain tables in the main DB "
                  "(identical parquet output; no snapshot history)")
            print("[hint] pip install duckdb-extensions duckdb-extension-ducklake")
            return "main"
    try:
        conn.execute("LOAD ducklake;")
        LAKE_DATA.mkdir(parents=True, exist_ok=True)
        conn.execute(
            f"ATTACH IF NOT EXISTS 'ducklake:{CATALOG}' AS teamlake "
            f"(DATA_PATH '{LAKE_DATA}/')"
        )
        print(f"[lake] attached {CATALOG.relative_to(ROOT)} "
              f"(data: {LAKE_DATA.relative_to(ROOT)}/)")
        return "teamlake"
    except duckdb.Error as e:
        print(f"[warn] could not attach DuckLake ({str(e).splitlines()[0]}); using main")
        return "main"


def write_tables(conn, target: str, wbs_rows: list, member_rows: list) -> None:
    """Create/replace the two tables in `target`, then mirror into main so
    qa.py, the parquet export, and rebuild all see the same rows whichever
    mode we ran in.

    Rows are staged in a temp table and moved with a single INSERT …
    SELECT rather than executemany: DuckLake records one snapshot per
    statement, so a per-row insert would bury each run's real change under
    a hundred single-row snapshots and make the history useless.
    """
    for table, cols, rows, ddl in (
        ("cod_wbs", WBS_COLS, wbs_rows,
         "wbs_code VARCHAR, parent_code VARCHAR, title VARCHAR, "
         "lead_person_id VARCHAR, sort_order INTEGER, notes VARCHAR"),
        ("cod_team_members", MEMBER_COLS, member_rows,
         "member_id VARCHAR, person_id VARCHAR, display_name VARCHAR, "
         "wbs_code VARCHAR, role VARCHAR, institution VARCHAR, "
         "institution_slug VARCHAR, is_pi BOOLEAN, is_copi BOOLEAN, "
         "is_leadership_committee BOOLEAN, committees VARCHAR, status VARCHAR, "
         "sort_order INTEGER, source VARCHAR, notes VARCHAR"),
    ):
        stage = f"_stage_{table}"
        conn.execute(f"CREATE OR REPLACE TEMP TABLE {stage} ({ddl})")
        conn.executemany(
            f"INSERT INTO {stage} VALUES ({', '.join('?' * len(cols))})", rows)
        conn.execute(f"CREATE OR REPLACE TABLE {target}.{table} ({ddl})")
        conn.execute(f"INSERT INTO {target}.{table} SELECT * FROM {stage}")
        conn.execute(f"DROP TABLE {stage}")
    print(f"[{target}] cod_wbs={len(wbs_rows)} cod_team_members={len(member_rows)}")

    if target != "main":
        # main.* already exist with PKs from schema.sql — replace contents,
        # not the tables, so the constraints stay in force.
        conn.execute("DELETE FROM main.cod_team_members;")
        conn.execute("DELETE FROM main.cod_wbs;")
        conn.execute(f"INSERT INTO main.cod_wbs SELECT * FROM {target}.cod_wbs;")
        conn.execute(
            f"INSERT INTO main.cod_team_members SELECT * FROM {target}.cod_team_members;")
        print("[main] mirrored from the lake")


def sync_people(conn, members_raw: list[dict], member_rows: list) -> int:
    """Upsert named members into people. COALESCE on every enrichable
    column so a blank seed cell never clobbers an enriched value."""
    # (name, email) -> person_id, from the rows we just built.
    pid_by_name: dict[str, str] = {}
    for row in member_rows:
        if row[1]:
            pid_by_name[row[2]] = row[1]

    seen: set[str] = set()
    n = 0
    for r in members_raw:
        name = (r.get("display_name") or "").strip()
        pid = pid_by_name.get(name)
        if not pid or pid in seen:
            continue
        seen.add(pid)
        given, family = split_name(name)
        conn.execute(
            """
            INSERT INTO people (
                person_id, name, name_family, name_given, email, orcid,
                openalex_id, google_scholar_id, homepage_url, status, notes,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, now())
            ON CONFLICT (person_id) DO UPDATE SET
                name              = excluded.name,
                name_family       = excluded.name_family,
                name_given        = excluded.name_given,
                email             = COALESCE(excluded.email, people.email),
                orcid             = COALESCE(excluded.orcid, people.orcid),
                openalex_id       = COALESCE(excluded.openalex_id, people.openalex_id),
                google_scholar_id = COALESCE(excluded.google_scholar_id,
                                             people.google_scholar_id),
                homepage_url      = COALESCE(excluded.homepage_url, people.homepage_url),
                status            = 'active',
                notes             = COALESCE(people.notes, excluded.notes),
                updated_at        = now()
            """,
            [pid, name, family, given,
             (r.get("email") or "").strip() or None,
             (r.get("orcid") or "").strip() or None,
             (r.get("openalex_id") or "").strip() or None,
             (r.get("google_scholar_id") or "").strip() or None,
             (r.get("homepage_url") or "").strip() or None,
             "COD team (org-chart-2026)"],
        )
        n += 1
    return n


def export_parquet(conn) -> None:
    for table in EXPORT_TABLES:
        for base in PARQUET_OUT:
            base.mkdir(parents=True, exist_ok=True)
            out = base / f"{table}.parquet"
            conn.execute(f"COPY (SELECT * FROM main.{table}) TO '{out}' (FORMAT PARQUET)")
        print(f"[parquet] {table} -> " + ", ".join(
            str((b / f'{table}.parquet').relative_to(ROOT)) for b in PARQUET_OUT))
    print("[note] parquet files are gitignored; stage them with `git add -f`")


def show_history(conn, target: str) -> None:
    if target != "teamlake":
        print("[history] no DuckLake attached")
        return
    try:
        rows = conn.execute(
            "SELECT snapshot_id, snapshot_time, changes FROM teamlake.snapshots() "
            "ORDER BY snapshot_id DESC LIMIT 10"
        ).fetchall()
        print(f"[history] {len(rows)} most recent snapshot(s):")
        for sid, stime, changes in rows:
            print(f"  #{sid}  {stime}  {changes}")
    except duckdb.Error as e:
        print(f"[warn] snapshots() unavailable: {str(e).splitlines()[0]}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--wbs-csv", type=Path, default=WBS_CSV)
    ap.add_argument("--members-csv", type=Path, default=MEMBERS_CSV)
    ap.add_argument("--no-ducklake", action="store_true",
                    help="write plain tables in the main DB, skip the lake")
    ap.add_argument("--dry-run", action="store_true", help="validate only, write nothing")
    ap.add_argument("--skip-export", action="store_true", help="don't refresh parquet")
    ap.add_argument("--history", action="store_true",
                    help="print the DuckLake snapshot log and exit")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"[error] {args.db} not found — run scripts/rebuild_db_from_parquet.py first",
              file=sys.stderr)
        return 2

    wbs_raw = read_seed(args.wbs_csv)
    members_raw = read_seed(args.members_csv)
    print(f"[read] {len(wbs_raw)} WBS elements, {len(members_raw)} member rows")

    wbs_rows, member_rows, fatal = build_rows(wbs_raw, members_raw)
    if fatal:
        print(f"\n[error] {len(fatal)} seed validation failure(s):", file=sys.stderr)
        for f in fatal:
            print(f"  - {f}", file=sys.stderr)
        return 1

    named = sum(1 for r in member_rows if r[1])
    open_slots = len(member_rows) - named
    people_ct = len({r[1] for r in member_rows if r[1]})
    print(f"[ok] validated: {people_ct} distinct people across {named} role rows, "
          f"{open_slots} unfilled position(s)")

    if args.dry_run:
        print("[dry-run] nothing written")
        return 0

    conn = duckdb.connect(str(args.db))
    conn.execute("SET search_path = main;")

    target = "main" if args.no_ducklake else attach_lake(conn)
    if args.history:
        show_history(conn, target)
        conn.close()
        return 0

    write_tables(conn, target, wbs_rows, member_rows)
    n_synced = sync_people(conn, members_raw, member_rows)
    print(f"[people] upserted {n_synced} COD team member(s)")

    if not args.skip_export:
        export_parquet(conn)
    show_history(conn, target)

    for label, sql in (
        ("PI", "SELECT display_name, institution FROM main.cod_team_members WHERE is_pi"),
        ("Co-PIs", "SELECT DISTINCT display_name, institution FROM main.cod_team_members "
                   "WHERE is_copi ORDER BY display_name"),
    ):
        rows = conn.execute(sql).fetchall()
        print(f"  {label}: " + "; ".join(f"{n} ({i})" for n, i in rows))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
