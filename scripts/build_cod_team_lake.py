#!/usr/bin/env python3
"""Load the COD project team layer into the lake, and sync it to people.

The COD org chart is hand-transcribed into two seed CSVs:

    data/seed/cod_wbs.csv            the Work Breakdown Structure tree
    data/seed/cod_team_members.csv   one row per (person, WBS element, role)

This script validates them and writes the ``cod_wbs`` and
``cod_team_members`` tables of the project's DuckLake store of record
(``scripts/cod_db.py``), in one transaction, so each run that changes the
roster lands as ONE snapshot with the commit message
``build_cod_team_lake: …``. The roster's history is therefore the lake's
own history, queryable from ``import cod_db; con = cod_db.connect(read_only=True)``::

    SELECT snapshot_id, snapshot_time, author, commit_message
      FROM lake.snapshots()
     WHERE commit_message LIKE 'build_cod_team_lake:%' ORDER BY 1 DESC;
    SELECT * FROM cod_team_members AT (VERSION => 12);
    SELECT change_type, member_id, wbs_code, role
      FROM ducklake_table_changes('lake', 'main', 'cod_team_members', 12, 12);

``--history`` prints that log with the per-table row changes of each
snapshot. This replaces the separate ``db/cod_team.ducklake`` +
``db/ducklake_data/`` catalogue the layer used to keep, and its
plain-table fallback: there is one lake now, and no second copy to mirror.

A run whose seeds match the lake changes nothing, so it commits nothing:
each table is rewritten only when its staged rows differ from the lake
(``DELETE`` + one ``INSERT … SELECT``, which keeps the column definitions'
NOT NULL / DEFAULT; DuckLake snapshots per *statement*, so rows are never
inserted one at a time), and only people rows the sync would actually
change are merged.

Named members (status ``active``) are also merged into ``people`` so the
Researcher directory, enrichment scripts and metrics pipeline pick them up.
The merge never erases an identifier: ``orcid``, ``openalex_id``,
``google_scholar_id`` and ``homepage_url`` keep the lake's value when the
seed cell is blank, and ``notes`` keeps the lake's value whenever it has
one. ``email`` is never written: it still feeds the ``person_id`` hash
(``sha1(f"{name.lower()}|{orcid}|{email.lower()}")[:16]``, identical to
scripts/load_facility_personnel.py), but contact details stay in the seed.

Nothing here writes parquet. Publish with ``python scripts/export_parquet.py``.

Usage::

    python scripts/build_cod_team_lake.py
    python scripts/build_cod_team_lake.py --dry-run     # validate the seeds only
    python scripts/build_cod_team_lake.py --history     # roster snapshots
    python scripts/build_cod_team_lake.py --catalog /tmp/x/cat.sqlite   # scratch lake
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
WBS_CSV = ROOT / "data" / "seed" / "cod_wbs.csv"
MEMBERS_CSV = ROOT / "data" / "seed" / "cod_team_members.csv"
SCRIPT = "build_cod_team_lake"

# Institution slugs from the org chart's colour legend, plus the extra
# affiliations named in member boxes. Validated so a typo in the seed CSV
# can't silently produce an uncoloured chip in the Team tab.
COD_INSTITUTIONS = {
    "clemson", "yale", "unm", "battelle", "vcu", "pnnl", "unl", "obfs",
    "uga", "arizona", "delaware", "usc", "uidaho", "montana-state",
    "alabama", "florida", "coastal-carolina", "charleston",
    "other-university", "agency", "company", "various",
}
# active = a named individual; tbd/tbh = an unfilled position; collective =
# work staffed by a group rather than a person (the chart's "NEON Staff"
# box). Only 'active' rows are synced into `people`, so a staffing pool
# cannot end up stored as a human.
MEMBER_STATUSES = {"active", "tbd", "tbh", "collective"}

WBS_COLS = ["wbs_code", "parent_code", "title", "lead_person_id", "sort_order", "notes"]
MEMBER_COLS = [
    "member_id", "person_id", "display_name", "wbs_code", "role", "institution",
    "institution_slug", "is_pi", "is_copi", "is_leadership_committee",
    "committees", "status", "sort_order", "source", "notes",
]
TEAM_TABLES = [("cod_wbs", WBS_COLS), ("cod_team_members", MEMBER_COLS)]

# The people columns the sync stages, and how each merges into an existing
# row — the semantics of the pre-lake per-row upsert on person_id:
#   overwrite  the seed wins            (name, name_family, name_given, status)
#   coalesce   the seed wins unless it is blank  (the enrichable identifiers)
#   fill       the lake wins unless it is blank  (notes)
# email is deliberately absent (see the module docstring), and updated_at
# is set to now() on every row the merge changes.
PEOPLE_COLS = [
    "person_id", "name", "name_family", "name_given", "orcid", "openalex_id",
    "google_scholar_id", "homepage_url", "status", "notes",
]
PEOPLE_COALESCE = ["orcid", "openalex_id", "google_scholar_id", "homepage_url"]
PEOPLE_FILL = ["notes"]
PEOPLE_NOTE = "COD team (org-chart-2026)"

# Tables this script writes; --history reports changes to these.
WRITES = ["cod_wbs", "cod_team_members", "people"]


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
            # Unfilled position or a staffing pool: no person row, but still
            # needs a stable key.
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


def members_raw_aligned(member_rows: list, members_raw: list[dict]) -> list[dict]:
    """Re-pair each built row with the seed row it came from.

    build_rows() skips malformed seed rows, so the two lists can differ in
    length and cannot be zipped blind. Matching on (display_name, wbs_code,
    role) is exact: that triple is the seed's own primary key.
    """
    index = {((r.get("display_name") or "").strip(),
              (r.get("wbs_code") or "").strip(),
              (r.get("role") or "").strip()): r for r in members_raw}
    return [index.get((row[2], row[3], row[4]), {}) for row in member_rows]


def people_rows(members_raw: list[dict], member_rows: list) -> list[list]:
    """One staged people row per distinct named member (first role row wins).

    Walks the built rows and their originating seed row together. Resolving
    by display_name instead would let two different people who share a name
    collapse onto whichever row happened to be last, silently writing one
    person's identifiers onto the other.
    """
    seen: set[str] = set()
    out: list[list] = []
    for row, r in zip(member_rows, members_raw_aligned(member_rows, members_raw)):
        pid, name = row[1], row[2]
        if not pid or pid in seen:
            continue
        seen.add(pid)
        given, family = split_name(name)
        out.append([
            pid, name, family, given,
            (r.get("orcid") or "").strip() or None,
            (r.get("openalex_id") or "").strip() or None,
            (r.get("google_scholar_id") or "").strip() or None,
            (r.get("homepage_url") or "").strip() or None,
            "active",
            PEOPLE_NOTE,
        ])
    return out


def _cols(cols: list[str], prefix: str = "") -> str:
    return ", ".join(prefix + cod_db.qident(c) for c in cols)


def _stage(con, stage: str, like: str, cols: list[str], rows: list) -> None:
    """A TEMP table with ``like``'s column types for ``cols``, holding ``rows``.

    executemany into TEMP is fine: the temp catalog is not the lake, so it
    makes no snapshots. The lake is then written with one statement.
    """
    con.execute(f"CREATE OR REPLACE TEMP TABLE {stage} AS "
                f"SELECT {_cols(cols)} FROM {cod_db.qident(like)} LIMIT 0")
    if rows:
        con.executemany(f"INSERT INTO {stage} VALUES ({', '.join('?' * len(cols))})", rows)


def _n_differing(con, stage: str, table: str, cols: list[str]) -> int:
    """Rows in either the staged set or the lake table but not both (bag diff)."""
    c, t = _cols(cols), cod_db.qident(table)
    return cod_db.scalar(
        con,
        f"SELECT (SELECT count(*) FROM (SELECT {c} FROM {stage} EXCEPT ALL "
        f"                              SELECT {c} FROM {t})) "
        f"     + (SELECT count(*) FROM (SELECT {c} FROM {t} EXCEPT ALL "
        f"                              SELECT {c} FROM {stage}))")


def write_team_tables(con, wbs_rows: list, member_rows: list) -> dict[str, int]:
    """Replace cod_wbs and cod_team_members with the seed rows, if they differ.

    DELETE + one INSERT … SELECT per table, not CREATE OR REPLACE: the
    lake's column definitions (NOT NULL, DEFAULT) survive. A table whose
    rows already match is not touched, so an unchanged seed makes no
    snapshot. Returns the number of differing rows per table (0 = untouched).
    """
    out: dict[str, int] = {}
    for (table, cols), rows in zip(TEAM_TABLES, (wbs_rows, member_rows)):
        stage = f"_stage_{table}"
        _stage(con, stage, table, cols, rows)
        n = _n_differing(con, stage, table, cols)
        if n:
            con.execute(f"DELETE FROM {cod_db.qident(table)}")
            con.execute(f"INSERT INTO {cod_db.qident(table)} ({_cols(cols)}) "
                        f"SELECT {_cols(cols)} FROM {stage}")
        con.execute(f"DROP TABLE {stage}")
        out[table] = n
        print(f"[lake] {table}: {len(rows)} rows "
              + (f"(rewritten; {n} row(s) differed)" if n else "(unchanged)"))
    return out


def sync_people(con, rows: list[list]) -> dict:
    """Merge the named members into people (see PEOPLE_COLS for the rules).

    Only rows the merge would change are merged, so an unchanged roster
    leaves people (and its updated_at stamps) alone and commits nothing.
    """
    _stage(con, "_stage_people", "people", PEOPLE_COLS, rows)

    def merged(c: str) -> str:
        q = cod_db.qident(c)
        if c in PEOPLE_FILL:
            return f"COALESCE(t.{q}, s.{q})"
        if c in PEOPLE_COALESCE:
            return f"COALESCE(s.{q}, t.{q})"
        return f"s.{q}"

    changed = " OR ".join(f"{merged(c)} IS DISTINCT FROM t.{cod_db.qident(c)}"
                          for c in PEOPLE_COLS if c != "person_id")
    con.execute(
        "CREATE OR REPLACE TEMP TABLE _stage_people_delta AS "
        "SELECT s.* FROM _stage_people s "
        "LEFT JOIN people t ON t.person_id = s.person_id "
        f"WHERE t.person_id IS NULL OR {changed}")
    n_delta = cod_db.scalar(con, "SELECT count(*) FROM _stage_people_delta")
    res = {"staged": len(rows), "matched": 0, "inserted": 0,
           "unchanged": len(rows) - n_delta}
    if n_delta:
        r = cod_db.merge_upsert(con, "people", ["person_id"], "_stage_people_delta",
                                coalesce=PEOPLE_COALESCE, fill=PEOPLE_FILL,
                                extra_set={"updated_at": "now()"})
        res.update(matched=r["matched"], inserted=r["inserted"])
    con.execute("DROP TABLE _stage_people_delta")
    con.execute("DROP TABLE _stage_people")
    print(f"[lake] people: {res['staged']} COD team member(s): {res['inserted']} inserted, "
          f"{res['matched']} updated, {res['unchanged']} unchanged")
    return res


def show_history(con, limit: int) -> None:
    """This script's snapshots, newest first, with per-table row changes."""
    snaps = cod_db.rows(
        con,
        f"SELECT snapshot_id, CAST(snapshot_time AS VARCHAR), author, commit_message "
        f"FROM {cod_db.LAKE}.snapshots() WHERE commit_message LIKE ? "
        f"ORDER BY snapshot_id DESC LIMIT ?", [f"{SCRIPT}:%", limit])
    if not snaps:
        print(f"[history] no {SCRIPT} snapshots in this lake yet")
        return
    print(f"[history] {len(snaps)} most recent {SCRIPT} snapshot(s):")
    for sid, stime, author, msg in snaps:
        print(f"  #{sid}  {stime}  {author}  {msg}")
        parts = []
        for table in WRITES:
            counts = cod_db.rows(
                con,
                f"SELECT change_type, count(*) FROM ducklake_table_changes("
                f"'{cod_db.LAKE}', 'main', '{table}', {int(sid)}, {int(sid)}) "
                f"GROUP BY 1 ORDER BY 1")
            if counts:
                parts.append(f"{table}: " + ", ".join(f"{n} {ct}" for ct, n in counts))
        print("      " + ("; ".join(parts) or "no row changes"))
    print("[history] rows as of a snapshot:  "
          "SELECT * FROM cod_team_members AT (VERSION => n)")


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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    cod_db.add_argparse_catalog(ap)
    ap.add_argument("--db", type=Path, default=None,
                    help="deprecated alias of --catalog (a .duckdb path is ignored)")
    ap.add_argument("--wbs-csv", type=Path, default=WBS_CSV)
    ap.add_argument("--members-csv", type=Path, default=MEMBERS_CSV)
    ap.add_argument("--no-ducklake", dest="legacy_no_lake", action="store_true",
                    help="deprecated no-op: the team layer always lives in the main lake")
    ap.add_argument("--skip-export", action="store_true",
                    help="deprecated no-op: this script no longer writes parquet")
    ap.add_argument("--dry-run", action="store_true", help="validate only, write nothing")
    ap.add_argument("--history", nargs="?", type=int, const=10, default=None, metavar="N",
                    help=f"print the last N (default 10) {SCRIPT} snapshots and exit")
    args = ap.parse_args(argv)

    if args.legacy_no_lake:
        print("[deprecated] --no-ducklake ignored: the COD team tables live in the "
              "main lake (scripts/cod_db.py); there is no separate catalogue to skip",
              file=sys.stderr)
    if args.skip_export:
        print("[deprecated] --skip-export ignored: this script never writes parquet "
              "(run scripts/export_parquet.py)", file=sys.stderr)
    catalog = catalog_arg(args)

    if args.history is not None:
        try:
            con = cod_db.connect(read_only=True, catalog=catalog, data=args.lake_data)
        except cod_db.LakeNotFound as e:
            print(f"[error] {e}", file=sys.stderr)
            return 2
        show_history(con, args.history)
        con.close()
        return 0

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
    open_slots = sum(1 for r in member_rows if r[11] in ("tbd", "tbh"))
    collective = sum(1 for r in member_rows if r[11] == "collective")
    person_rows = people_rows(members_raw, member_rows)
    print(f"[ok] validated: {len(person_rows)} distinct people across {named} role rows, "
          f"{open_slots} unfilled position(s), {collective} group-staffed")

    if args.dry_run:
        print("[dry-run] nothing written")
        return 0

    try:
        con = cod_db.connect(catalog=catalog, data=args.lake_data)
    except cod_db.LakeNotFound as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2

    before = cod_db.latest_snapshot(con).get("snapshot_id")
    message = (f"{SCRIPT}: {len(wbs_rows)} WBS rows, {len(member_rows)} member rows, "
               f"{len(person_rows)} people from data/seed/cod_*.csv")
    try:
        with cod_db.transaction(con, message):
            write_team_tables(con, wbs_rows, member_rows)
            sync_people(con, person_rows)
            cod_db.assert_integrity(con, WRITES)
    except cod_db.IntegrityError as e:
        print(f"[error] integrity check failed; nothing committed:\n{e}", file=sys.stderr)
        con.close()
        return 1

    after = cod_db.latest_snapshot(con)
    if after.get("snapshot_id") != before:
        print(f"[lake] committed snapshot #{after['snapshot_id']}: {after['commit_message']}")
        print("[next] python scripts/qa.py && python scripts/export_parquet.py   "
              "# publish to db/parquet + public/parquet")
    else:
        print("[lake] seeds match the lake; nothing changed, no snapshot written")

    for label, sql in (
        ("PI", "SELECT display_name, institution FROM cod_team_members WHERE is_pi"),
        ("Co-PIs", "SELECT DISTINCT display_name, institution FROM cod_team_members "
                   "WHERE is_copi ORDER BY display_name"),
    ):
        rows = cod_db.rows(con, sql)
        print(f"  {label}: " + "; ".join(f"{n} ({i})" for n, i in rows))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
