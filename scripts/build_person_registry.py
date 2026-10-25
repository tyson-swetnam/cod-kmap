#!/usr/bin/env python3
"""Resolve people, cod_team_members and community_scholars into one identity space.

WARNING -- THIS SCRIPT IS DESTRUCTIVE FOR THE ARCHIVE TIER.
It DELETEs person_registry and rebuilds it from three local tables only
(people, cod_team_members, community_scholars), yielding ~713 rows. The
~142,000 archive-tier identities harvested from OpenAlex by
scripts/harvest_coastal_authors.py are NOT reconstructed by any script in
the documented chain (docs/team_scholars_datasets_methods.md runs
build_person_registry -> compute_registry_collaborations ->
link_registry_facilities -> rank_person_registry -> qa; harvest is not in
it). Re-run harvest_coastal_authors.py explicitly, or restore the lake to
the snapshot before this run (``SELECT * FROM person_registry AT (VERSION
=> n)``; every run is one snapshot, see ``lake.snapshots()``), or rebuild
the lake from the committed db/parquet, if you need the archive tier back.

The rows that point at a dropped registry row (person_validation,
coauthor_edges, coauthor_candidates, registry_collaborations,
registry_facilities, scholar_area_assignments) are not touched here. The
run still commits — the downstream scripts in the chain recompute them —
but it prints what it orphaned, and qa.py fails until they are rebuilt.


The three human-facing layers grew independently. An audit on 2026-07-26
found they share 1 ORCID and 6 exact names across 843 rows, which makes
"who works with whom" unanswerable: the same researcher can be three rows
with three different keys and no edge between them.

This script folds all three into `person_registry`, one row per human,
keyed on a persistent identifier and carrying a role flag per cohort.

Merge rule — the load-bearing one
---------------------------------
Two rows merge ONLY on ORCID equality or openalex_id equality. Names are
never compared. This repo has had three separate wrong-person incidents
(scripts/wipe_bad_openalex_attributions.py, wipe_medicine_attributions.py,
wipe_misattributed_identifiers.py, the last of which cleared 1,460
authorship rows), and every one of them started with a name match. A row
with no persistent identifier is NOT written to the registry at all; it is
reported as unresolvable so it can be curated, because a registry row that
cannot be re-resolved forks into a duplicate on the next run.

canonical_id is derived from the identifier rather than hashed from
mutable fields: 'orcid:0000-…' when an ORCID is known, else
'openalex:A…'. That means re-running after an ORCID is discovered re-keys
the row deliberately and visibly, rather than silently creating a second
one. qa.py asserts the id and the ORCID agree.

Usage::

    python scripts/build_person_registry.py --dry-run
    python scripts/build_person_registry.py

The rebuild is one lake snapshot (scripts/cod_db.py); scripts/export_parquet.py
publishes it. --export-parquet is accepted for old callers and does nothing.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent))
import cod_db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
REGISTRY_TABLES = ("person_registry", "person_identity_source",
                   "registry_collaborations")

ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$")
OA_RE = re.compile(r"^A\d+$")


def clean_orcid(v) -> str | None:
    """Return a bare ORCID, or None. Rejects anything not ORCID-shaped.

    Two rows in `people` held biography prose in the orcid column until
    wipe_misattributed_identifiers.py moved it to notes; this guard means
    such a value can never key a registry row.
    """
    if not v:
        return None
    m = re.search(r"(\d{4}-\d{4}-\d{4}-\d{3}[\dX])", str(v))
    return m.group(1) if m else None


def clean_oa(v) -> str | None:
    if not v:
        return None
    s = str(v).rstrip("/").rsplit("/", 1)[-1].strip()
    return s if OA_RE.match(s) else None


def split_name(full: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", (full or "").strip()) if p]
    if len(parts) < 2:
        return "", (parts[0] if parts else "")
    return " ".join(parts[:-1]), parts[-1]


def ensure_tables(conn) -> None:
    """Create the registry tables if this lake predates them.

    schema.sql cannot be executed against the lake (DuckLake rejects its
    PRIMARY KEYs), so each missing table is created column-only from the
    schema.sql spec; cod_db.assert_integrity enforces the keys. Only absent
    tables are created — an existing populated table is never replaced.
    Call inside a transaction.
    """
    spec_tables = set(cod_db.spec()["tables"])
    if not all(t in spec_tables for t in REGISTRY_TABLES):
        print("[error] schema.sql has no person_registry DDL", file=sys.stderr)
        raise SystemExit(2)
    have = set(cod_db.lake_tables(conn))
    for t in REGISTRY_TABLES:
        if t not in have:
            conn.execute(cod_db.column_only_ddl(t))
            print(f"[schema] created {t} from schema/schema.sql")


def assert_written(conn, written: list[str]) -> None:
    """cod_db.assert_integrity, raising only for the tables this run wrote.

    A violation in a written table rolls the run back. A violation in a
    table that merely points at one of them (an edge or verdict whose
    registry row an earlier rebuild dropped) is printed, not raised: this
    run did not cause it and cannot repair it, the script that owns that
    table recomputes it, and qa.py fails on it until then, so nothing stale
    is exported unnoticed.
    """
    v = cod_db.integrity_violations(conn, written)
    own = [x for x in v if x["table"] in written]
    if own:
        raise cod_db.IntegrityError(own)
    dep = [x for x in v if x["table"] not in written]
    if dep:
        print("[warn] rows in other tables point at rows that no longer exist "
              "(qa.py fails until their scripts are re-run):\n"
              + "\n".join("       " + line for line in
                          cod_db.format_violations(dep).splitlines()),
              file=sys.stderr)


class Registry:
    """Accumulates rows, merging only on identifier equality."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.by_orcid: dict[str, dict] = {}
        self.by_oa: dict[str, dict] = {}
        self.prov: list[dict] = []
        self.unresolvable: list[tuple[str, str]] = []
        # Index on the project's own person_id, so a COD-internal person who
        # has no ORCID/OpenAlex id still resolves to exactly one identity
        # across `people` and `cod_team_members`.
        self.by_pid: dict[str, dict] = {}
        self.merges = 0

    def _record(self, row: dict, field: str, value, method: str,
                evidence: str, source_url: str, confidence: str) -> None:
        self.prov.append(dict(
            canonical_id=row["canonical_id"], field=field,
            value=None if value is None else str(value), method=method,
            evidence=evidence, source_url=source_url, confidence=confidence,
            retrieved_at=date.today().isoformat()))

    def add(self, *, name: str, orcid: str | None, openalex_id: str | None,
            cohort: str | None, source: str, source_url: str, confidence: str,
            extra: dict | None = None,
            local_id: str | None = None) -> dict | None:
        """Insert or merge one source row. Returns the registry row, or None
        when the row carries no usable identifier.

        `local_id` is the project's own person_id, supplied for COD-INTERNAL
        sources only (`people`, `cod_team_members`). Those rosters are curated
        by hand rather than harvested, so person_id is already a stable
        identity within this project — requiring an ORCID or OpenAlex id of
        them dropped real, named COD staff from the registry entirely,
        including the PI and Co-PI. External sources (community_scholars) do
        NOT pass local_id: for a harvested identity a persistent public
        identifier is the whole basis of the claim, and minting a local key
        for one would assert an identity we cannot substantiate."""
        orcid = clean_orcid(orcid)
        openalex_id = clean_oa(openalex_id)
        if not orcid and not openalex_id and not local_id:
            self.unresolvable.append((source, name))
            return None

        existing = (self.by_orcid.get(orcid) if orcid else None) \
            or (self.by_oa.get(openalex_id) if openalex_id else None) \
            or (self.by_pid.get(local_id) if local_id else None)

        if existing is not None:
            self.merges += 1
            matched_on = "orcid" if (orcid and orcid in self.by_orcid) else "openalex_id"
            self._record(existing, "merge", name, f"{matched_on}-equality",
                         f"{source} row '{name}' merged into "
                         f"{existing['canonical_id']} on {matched_on} equality",
                         source_url, "high")
            row = existing
            # Fill identifiers this source knows and the existing row doesn't.
            if orcid and not row.get("orcid"):
                row["orcid"] = orcid
                self.by_orcid[orcid] = row
                # Re-key: an ORCID outranks an OpenAlex id, which in turn
                # outranks a local codp: key, for canonical_id.
                if row["canonical_id"].startswith(("openalex:", "codp:")):
                    row["canonical_id"] = f"orcid:{orcid}"
                self._record(row, "orcid", orcid, "seed",
                             f"supplied by {source}", source_url, "high")
            if openalex_id and not row.get("openalex_id"):
                row["openalex_id"] = openalex_id
                self.by_oa[openalex_id] = row
                if row["canonical_id"].startswith("codp:"):
                    row["canonical_id"] = f"openalex:{openalex_id}"
                self._record(row, "openalex_id", openalex_id, "seed",
                             f"supplied by {source}", source_url, "high")
            if local_id and local_id not in self.by_pid:
                self.by_pid[local_id] = row
        else:
            given, family = split_name(name)
            if orcid:
                cid = f"orcid:{orcid}"
            elif openalex_id:
                cid = f"openalex:{openalex_id}"
            else:
                # COD-internal identity with no public identifier. The codp:
                # prefix marks it as project-local so downstream code can tell
                # it apart from a resolved public identity; it is replaced by
                # an orcid:/openalex: key above if one is ever supplied.
                cid = f"codp:{local_id}"
            row = dict(canonical_id=cid, display_name=name,
                       name_given=given, name_family=family,
                       orcid=orcid, openalex_id=openalex_id,
                       google_scholar_id=None, scopus_author_id=None,
                       wos_researcher_id=None, homepage_url=None,
                       affiliation=None, affiliation_ror=None,
                       affiliation_country=None,
                       is_team=False, is_site_personnel=False, is_scholar=False,
                       person_id=None, scholar_id=None,
                       works_count=None, cited_by_count=None, h_index=None,
                       i10_index=None, two_yr_mean_citedness=None,
                       coastal_works_count=None, coastal_share=None,
                       first_pub_year=None,
                       tier="archive", tier_rank=None, tier_score=None,
                       source=source, source_url=source_url,
                       confidence=confidence,
                       retrieved_at=date.today().isoformat(), notes=None)
            self.rows.append(row)
            if orcid:
                self.by_orcid[orcid] = row
            if openalex_id:
                self.by_oa[openalex_id] = row
            if local_id:
                self.by_pid[local_id] = row
            self._record(row, "canonical_id", cid, "seed",
                         f"created from {source} row '{name}'",
                         source_url, confidence)

        if cohort:
            row[f"is_{cohort}"] = True
        for k, v in (extra or {}).items():
            if v is not None and row.get(k) in (None, ""):
                row[k] = v
        return row


def load(conn, table: str, cols: str) -> list[dict]:
    if not cod_db.table_exists(conn, table):
        return []
    cur = conn.execute(f"SELECT {cols} FROM {table}")
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser()
    cod_db.add_argparse_catalog(ap)
    ap.add_argument("--db", dest="catalog", type=Path,
                    help="deprecated alias of --catalog")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--export-parquet", action="store_true",
                    help="deprecated no-op: run scripts/export_parquet.py")
    args = ap.parse_args()

    # A dry run reads only; the registry tables are created (if absent)
    # inside the write transaction below, so it never writes a snapshot.
    try:
        conn = cod_db.connect(read_only=args.dry_run,
                              catalog=args.catalog, data=args.lake_data)
    except cod_db.LakeNotFound as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    reg = Registry()

    # ── people (facility staff / directory) ────────────────────────────
    people = load(conn, "people",
                  "person_id, name, orcid, openalex_id, google_scholar_id, "
                  "homepage_url")
    staffed = {r[0] for r in conn.execute(
        "SELECT DISTINCT person_id FROM facility_personnel").fetchall()}
    # `people` is the facility directory, but not every row actually staffs a
    # catalogued facility (242 of 280 do). is_site_personnel means "staffs a
    # site", so it is set from facility_personnel rather than from mere
    # presence in `people`; the rest still enter the registry via their
    # person_id back-reference and pick up a cohort flag from whichever other
    # layer claims them.
    for p in people:
        row = reg.add(name=p["name"], orcid=p["orcid"],
                      openalex_id=p["openalex_id"],
                      cohort="site_personnel" if p["person_id"] in staffed
                             else None,
                      source="people", source_url="cod-kmap:people",
                      confidence="high",
                      local_id=p["person_id"],
                      extra=dict(person_id=p["person_id"],
                                 google_scholar_id=p["google_scholar_id"],
                                 homepage_url=p["homepage_url"]))
        if row is not None:
            row["person_id"] = p["person_id"]

    # ── COD team (org chart) ───────────────────────────────────────────
    team = load(conn, "cod_team_members",
                "DISTINCT person_id, display_name, institution")
    team_pids = {t["person_id"] for t in team if t["person_id"]}
    people_by_pid = {p["person_id"]: p for p in people}
    for t in team:
        pid = t["person_id"]
        if not pid:
            continue                      # unfilled position — not a human
        src = people_by_pid.get(pid, {})
        reg.add(name=src.get("name") or t["display_name"],
                orcid=src.get("orcid"), openalex_id=src.get("openalex_id"),
                cohort="team", source="cod-team",
                source_url="cod-kmap:cod_team_members", confidence="high",
                local_id=pid,
                extra=dict(person_id=pid, affiliation=t.get("institution")))

    # ── community scholars (field-wide roster) ─────────────────────────
    scholars = load(conn, "community_scholars",
                    "scholar_id, person_id, name, orcid, openalex_id, "
                    "google_scholar_id, affiliation, affiliation_ror, "
                    "affiliation_country, homepage_url, works_count, "
                    "cited_by_count, h_index, i10_index, two_yr_mean_citedness, "
                    "coastal_works_count, first_pub_year, source, source_url, "
                    "confidence")
    for s in scholars:
        share = None
        if s["works_count"] and s["coastal_works_count"] is not None:
            share = round(s["coastal_works_count"] / s["works_count"], 4)
        row = reg.add(name=s["name"], orcid=s["orcid"],
                      openalex_id=s["openalex_id"], cohort="scholar",
                      source="community_scholars",
                      source_url=s.get("source_url") or "cod-kmap:community_scholars",
                      confidence=s.get("confidence") or "medium",
                      extra=dict(scholar_id=s["scholar_id"],
                                 google_scholar_id=s["google_scholar_id"],
                                 affiliation=s["affiliation"],
                                 affiliation_ror=s["affiliation_ror"],
                                 affiliation_country=s["affiliation_country"],
                                 homepage_url=s["homepage_url"],
                                 works_count=s["works_count"],
                                 cited_by_count=s["cited_by_count"],
                                 h_index=s["h_index"],
                                 i10_index=s["i10_index"],
                                 two_yr_mean_citedness=s["two_yr_mean_citedness"],
                                 coastal_works_count=s["coastal_works_count"],
                                 coastal_share=share,
                                 first_pub_year=s["first_pub_year"]))
        if row is not None and s["person_id"]:
            row["person_id"] = row.get("person_id") or s["person_id"]

    # ── report ─────────────────────────────────────────────────────────
    n = len(reg.rows)
    both = sum(1 for r in reg.rows
               if sum([r["is_team"], r["is_site_personnel"], r["is_scholar"]]) > 1)
    print(f"[registry] {len(people)} people + {len(team_pids)} team + "
          f"{len(scholars)} scholars -> {n} distinct identities")
    print(f"[registry] {reg.merges} merge(s) on identifier equality; "
          f"{both} identity/identities appear in more than one cohort")
    print(f"[registry] cohorts: team={sum(r['is_team'] for r in reg.rows)} "
          f"site_personnel={sum(r['is_site_personnel'] for r in reg.rows)} "
          f"scholar={sum(r['is_scholar'] for r in reg.rows)}")
    if reg.unresolvable:
        by_src: dict[str, int] = {}
        for src, _ in reg.unresolvable:
            by_src[src] = by_src.get(src, 0) + 1
        print(f"[registry] {len(reg.unresolvable)} row(s) had no persistent "
              f"identifier and were NOT written: {by_src}")
        print("           (run scripts/enrich_registry_identifiers.py, or "
              "curate an ORCID by hand, then re-run)")

    if args.dry_run:
        conn.close()
        return 0

    cols = list(reg.rows[0].keys()) if reg.rows else []
    pcols = ["canonical_id", "field", "value", "method", "evidence",
             "source_url", "confidence", "retrieved_at"]
    with cod_db.transaction(conn, f"build_person_registry: rebuild from people, "
                                  f"cod_team_members, community_scholars "
                                  f"({n} identities, {len(reg.prov)} sources)"):
        ensure_tables(conn)
        # Stage in temp tables typed like the targets, then one INSERT each:
        # row-by-row INSERTs into the lake are slow, and each would be its
        # own snapshot outside a transaction.
        cod_db.create_temp_like(conn, '_reg_stage', "SELECT * FROM person_registry")
        cod_db.create_temp_like(conn, '_src_stage', "SELECT * FROM person_identity_source")
        if reg.rows:
            conn.executemany(
                f"INSERT INTO _reg_stage ({','.join(cols)}) "
                f"VALUES ({','.join('?' * len(cols))})",
                [[r[c] for c in cols] for r in reg.rows])
        if reg.prov:
            conn.executemany(
                f"INSERT INTO _src_stage ({','.join(pcols)}) "
                f"VALUES ({','.join('?' * len(pcols))})",
                [[p[c] for c in pcols] for p in reg.prov])
        conn.execute("DELETE FROM person_registry")
        conn.execute("DELETE FROM person_identity_source")
        if reg.rows:
            conn.execute(f"INSERT INTO person_registry ({','.join(cols)}) "
                         f"SELECT {','.join(cols)} FROM _reg_stage")
        if reg.prov:
            conn.execute(f"INSERT INTO person_identity_source ({','.join(pcols)}) "
                         f"SELECT {','.join(pcols)} FROM _src_stage")
        assert_written(conn, ["person_registry", "person_identity_source"])
    print(f"[lake] person_registry {n} rows, "
          f"person_identity_source {len(reg.prov)} rows")

    if args.export_parquet:
        print("[note] --export-parquet is a no-op now; run "
              "scripts/export_parquet.py to publish the lake")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
