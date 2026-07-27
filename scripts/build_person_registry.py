#!/usr/bin/env python3
"""Resolve people, cod_team_members and community_scholars into one identity space.

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
    python scripts/build_person_registry.py --export-parquet
"""
from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "db" / "cod_kmap.duckdb"
PARQUET_OUT = [ROOT / "db" / "parquet", ROOT / "public" / "parquet"]
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


def name_slug(full: str) -> str:
    """Stable, ASCII, lowercase slug of a person's name.

    Used only for the site-scoped canonical_id, so it must be deterministic
    across rebuilds and across machines: NFKD-fold accents rather than
    depending on locale, keep only [a-z0-9-], and never hash. A hash would
    be shorter but would make the id opaque and untraceable back to the
    person, which is the opposite of what a registry key is for.
    """
    s = unicodedata.normalize("NFKD", (full or "").strip().lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s or "unnamed"


def split_name(full: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", (full or "").strip()) if p]
    if len(parts) < 2:
        return "", (parts[0] if parts else "")
    return " ".join(parts[:-1]), parts[-1]


def ensure_tables(conn) -> None:
    """Create the registry tables if this DB predates them."""
    ddl = (ROOT / "schema" / "schema.sql").read_text()
    marker = "CREATE OR REPLACE TABLE person_registry"
    if marker not in ddl:
        print("[error] schema.sql has no person_registry DDL", file=sys.stderr)
        raise SystemExit(2)
    have = {r[0] for r in conn.execute(
        "SELECT table_name FROM information_schema.tables").fetchall()}
    if all(t in have for t in REGISTRY_TABLES):
        return
    # CREATE OR REPLACE would wipe an existing populated table, so only run
    # the DDL when at least one of the three is genuinely absent.
    conn.execute(ddl[ddl.index(marker):])
    print("[schema] created registry tables from schema/schema.sql")


class Registry:
    """Accumulates rows, merging only on identifier equality."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.by_orcid: dict[str, dict] = {}
        self.by_oa: dict[str, dict] = {}
        self.by_site: dict[str, dict] = {}
        self.by_person: dict[str, dict] = {}
        self.prov: list[dict] = []
        self.unresolvable: list[tuple[str, str]] = []
        self.merges = 0

    def _record(self, row: dict, field: str, value, method: str,
                evidence: str, source_url: str, confidence: str) -> None:
        self.prov.append(dict(
            canonical_id=row["canonical_id"], field=field,
            value=None if value is None else str(value), method=method,
            evidence=evidence, source_url=source_url, confidence=confidence,
            retrieved_at=date.today().isoformat()))

    def _drop_site_key(self, row: dict) -> None:
        """Retire a row's site-scoped index entry when it gains a real id.

        Without this the promoted row stays reachable under its old
        `site:` key, so a later source with no identifier would merge into
        a row whose canonical_id no longer matches that key — a silent
        inconsistency between the index and the data it indexes.
        """
        old = row.get("canonical_id", "")
        if old.startswith("site:") and self.by_site.get(old) is row:
            del self.by_site[old]

    def add(self, *, name: str, orcid: str | None, openalex_id: str | None,
            cohort: str | None, source: str, source_url: str, confidence: str,
            extra: dict | None = None,
            site_scope: str | None = None) -> dict | None:
        """Insert or merge one source row.

        Returns the registry row, or None when the row has no identifier and
        no ``site_scope`` to fall back on.

        ``site_scope`` is a catalogued facility_id. Passing it admits a
        person who has neither an ORCID nor an OpenAlex id, keyed
        ``site:<facility_id>:<name-slug>``. It is deliberately an explicit
        opt-in rather than an automatic fallback: it must only be used for
        people whose presence is attested by a facility_personnel row with a
        citable source, never to rescue an unresolved scholar. See the
        identity_class note in schema/schema.sql.
        """
        orcid = clean_orcid(orcid)
        openalex_id = clean_oa(openalex_id)
        if not orcid and not openalex_id and not site_scope:
            self.unresolvable.append((source, name))
            return None

        site_key = (f"site:{site_scope}:{name_slug(name)}"
                    if site_scope and not orcid and not openalex_id else None)
        person_id = (extra or {}).get("person_id")
        # Three lookup routes, in descending order of strength.
        #
        # by_person_id is what makes a site-scoped row promotable. A
        # site-scoped row is in neither by_orcid nor by_oa (it has no
        # identifier to index), and by_site is consulted only when the
        # INCOMING row also has no identifier — so without this third route
        # a later source carrying an ORCID for the same human matched
        # nothing and created a SECOND row, splitting their cohort flags
        # across two identities. That is the exact failure the registry
        # exists to prevent.
        #
        # people.person_id is a safe join key here precisely because it is
        # NOT a name: it is the directory's own primary key, and the team
        # and scholar ingests both carry it as a back-reference. Matching on
        # it is identifier equality, not name matching.
        existing = (self.by_orcid.get(orcid) if orcid else None) \
            or (self.by_oa.get(openalex_id) if openalex_id else None) \
            or (self.by_person.get(person_id) if person_id else None) \
            or (self.by_site.get(site_key) if site_key else None)

        if existing is not None:
            self.merges += 1
            if orcid and orcid in self.by_orcid:
                matched_on = "orcid"
            elif openalex_id and openalex_id in self.by_oa:
                matched_on = "openalex_id"
            else:
                matched_on = "site-scoped id"
            self._record(existing, "merge", name, f"{matched_on}-equality",
                         f"{source} row '{name}' merged into "
                         f"{existing['canonical_id']} on {matched_on} equality",
                         source_url, "high")
            row = existing
            # Fill identifiers this source knows and the existing row doesn't.
            if orcid and not row.get("orcid"):
                row["orcid"] = orcid
                self.by_orcid[orcid] = row
                # Re-key: an ORCID outranks both an OpenAlex id and a
                # site-scoped id. Promoting a site-scoped row also upgrades
                # its identity_class, so "we later found out who this is"
                # is recorded rather than silently assumed.
                if row["canonical_id"].startswith(("openalex:", "site:")):
                    self._drop_site_key(row)
                    row["canonical_id"] = f"orcid:{orcid}"
                    row["identity_class"] = "persistent"
                self._record(row, "orcid", orcid, "seed",
                             f"supplied by {source}", source_url, "high")
            if openalex_id and not row.get("openalex_id"):
                row["openalex_id"] = openalex_id
                self.by_oa[openalex_id] = row
                # Same promotion for a site-scoped row gaining an OpenAlex
                # id — but never demote an orcid: key.
                if row["canonical_id"].startswith("site:"):
                    self._drop_site_key(row)
                    row["canonical_id"] = f"openalex:{openalex_id}"
                    row["identity_class"] = "persistent"
                self._record(row, "openalex_id", openalex_id, "seed",
                             f"supplied by {source}", source_url, "high")
        else:
            given, family = split_name(name)
            if orcid:
                cid, klass = f"orcid:{orcid}", "persistent"
            elif openalex_id:
                cid, klass = f"openalex:{openalex_id}", "persistent"
            else:
                cid = f"site:{site_scope}:{name_slug(name)}"
                klass = "site-scoped"
            row = dict(canonical_id=cid, display_name=name,
                       identity_class=klass,
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
            if site_key:
                self.by_site[site_key] = row
            if person_id:
                self.by_person[person_id] = row
            self._record(row, "canonical_id", cid, "seed",
                         f"created from {source} row '{name}'",
                         source_url, confidence)

        if cohort:
            row[f"is_{cohort}"] = True
        for k, v in (extra or {}).items():
            if v is not None and row.get(k) in (None, ""):
                row[k] = v
        # Index by person_id however the row was reached, including on the
        # merge path where a row created from an identifier-only source
        # first learns its directory person_id here. Without this a row
        # merged on ORCID would stay invisible to a later person_id lookup.
        if person_id and self.by_person.get(person_id) is not row:
            self.by_person[person_id] = row
        return row


def load(conn, table: str, cols: str) -> list[dict]:
    try:
        cur = conn.execute(f"SELECT {cols} FROM {table}")
    except duckdb.Error:
        return []
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--export-parquet", action="store_true")
    ap.add_argument("--merge", action="store_true",
                    help="Rewrite only the rows this script derives from the "
                         "source tables, leaving harvested rows in place. Use "
                         "this on any registry that harvest_coastal_authors.py "
                         "has written to — a full rebuild drops those rows.")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"[error] db not found: {args.db}", file=sys.stderr)
        return 2

    conn = duckdb.connect(str(args.db))
    ensure_tables(conn)
    reg = Registry()

    # ── people (facility staff / directory) ────────────────────────────
    people = load(conn, "people",
                  "person_id, name, orcid, openalex_id, google_scholar_id, "
                  "homepage_url")
    staffed = {r[0] for r in conn.execute(
        "SELECT DISTINCT person_id FROM facility_personnel").fetchall()}
    # Primary catalogued facility per person, ranked the same way the map's
    # person_primary_facility is, so the site-scoped id and the map position
    # are anchored to the SAME facility and cannot drift apart.
    primary_site = {r[0]: r[1] for r in conn.execute("""
        SELECT person_id, facility_id FROM (
          SELECT person_id, facility_id,
                 ROW_NUMBER() OVER (PARTITION BY person_id
                   ORDER BY is_key_personnel DESC, role, facility_id) rk
          FROM facility_personnel) WHERE rk = 1""").fetchall()}
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
                      # Site-scoped identity, but ONLY for someone who
                      # actually staffs a catalogued facility. A directory
                      # row with no facility and no identifier still yields
                      # None: there is nothing to anchor an id to.
                      site_scope=primary_site.get(p["person_id"]),
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

    # DESTRUCTIVE BY DESIGN, AND EASY TO REGRET. This script rebuilds the
    # registry from the three SOURCE tables only (people, cod_team_members,
    # community_scholars). The ~151k rows added by
    # scripts/harvest_coastal_authors.py exist in NEITHER, so a plain re-run
    # silently drops them: 152,008 rows in, 687 out. Recovery is
    # `git checkout db/parquet/person_registry.parquet && python
    # scripts/rebuild_db_from_parquet.py`, because the committed parquet is
    # the durable artifact -- but only if you notice.
    #
    # --merge adds and updates the source-derived rows while leaving
    # harvested ones alone. Use it on a registry that has been harvested
    # into; use the full rebuild only to recreate one from scratch.
    if args.merge:
        existing = conn.execute("SELECT COUNT(*) FROM person_registry").fetchone()[0]
        cids = [r["canonical_id"] for r in reg.rows]
        conn.execute(
            "DELETE FROM person_registry WHERE canonical_id IN "
            f"({','.join('?' * len(cids))})", cids) if cids else None
        conn.execute(
            "DELETE FROM person_identity_source WHERE canonical_id IN "
            f"({','.join('?' * len(cids))})", cids) if cids else None
        print(f"[registry] merge mode: {existing:,} existing row(s) preserved "
              f"except the {len(cids):,} being rewritten")
    else:
        n_before = conn.execute("SELECT COUNT(*) FROM person_registry").fetchone()[0]
        if n_before > len(reg.rows) * 2:
            print(f"[registry] WARNING: replacing {n_before:,} rows with "
                  f"{len(reg.rows):,}. If this registry was harvested into, "
                  f"you want --merge; the harvested rows are about to be "
                  f"dropped.")
        conn.execute("DELETE FROM person_registry")
        conn.execute("DELETE FROM person_identity_source")
    cols = list(reg.rows[0].keys()) if reg.rows else []
    if reg.rows:
        conn.executemany(
            f"INSERT INTO person_registry ({','.join(cols)}) "
            f"VALUES ({','.join('?' * len(cols))})",
            [[r[c] for c in cols] for r in reg.rows])
    if reg.prov:
        pcols = ["canonical_id", "field", "value", "method", "evidence",
                 "source_url", "confidence", "retrieved_at"]
        conn.executemany(
            f"INSERT INTO person_identity_source ({','.join(pcols)}) "
            f"VALUES ({','.join('?' * len(pcols))})",
            [[p[c] for c in pcols] for p in reg.prov])

    # AFTER the inserts, never before: a row promoted from openalex:/site:
    # to orcid: leaves provenance under its OLD canonical_id, which now
    # matches no registry row. The targeted delete above cannot catch those
    # (it only knows the NEW ids). Sweeping before the inserts would delete
    # provenance for rows that are about to come back.
    orphans = conn.execute(
        "DELETE FROM person_identity_source WHERE canonical_id NOT IN "
        "(SELECT canonical_id FROM person_registry) RETURNING 1").fetchall()
    if orphans:
        print(f"[registry] swept {len(orphans):,} orphaned provenance row(s) "
              f"left behind by re-keyed identities")

    total = conn.execute("SELECT COUNT(*) FROM person_registry").fetchone()[0]
    prov_total = conn.execute(
        "SELECT COUNT(*) FROM person_identity_source").fetchone()[0]
    print(f"[db] person_registry {n} rows written "
          f"({total:,} total), person_identity_source {len(reg.prov)} written "
          f"({prov_total:,} total)")

    if args.export_parquet:
        for base in PARQUET_OUT:
            base.mkdir(parents=True, exist_ok=True)
            for t in REGISTRY_TABLES:
                out = base / f"{t}.parquet"
                conn.execute(f"COPY {t} TO '{out}' (FORMAT PARQUET)")
            print(f"[parquet] wrote {base}/person_registry.parquet (+2)")
        print("[note] parquet is gitignored; stage with `git add -f`")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
