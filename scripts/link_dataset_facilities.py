#!/usr/bin/env python3
"""Link catalogued datasets to the facilities that steward them.

`coastal_datasets` had no edge to any other entity except `networks`, so
"who produced this dataset" was unanswerable from the graph — the steward
was only ever a free-text `provider` string. This derives that edge
offline from fields already in the database. **No network access.**

Three deterministic passes, all string equality or substring containment
against `facilities`. Nothing fuzzy, nothing scored, no name-similarity
threshold: this repo has cleaned up name-only resolvers three times
(`wipe_bad_openalex_attributions.py`) and a missing link is much cheaper
than a wrong one.

1. **canonical-name** (`high`) — a facility's full `canonical_name`
   appears verbatim inside `provider` or `program`. Only names ≥12 chars
   are eligible, so 'NERR' or 'LTER' can never match this way. Where two
   catalogued facilities' names nest ('Monterey Bay Aquarium' inside
   'Monterey Bay Aquarium Research Institute') only the longer wins.
2. **acronym** (`medium`) — a capitalised token in `provider` equals a
   facility `acronym` of ≥3 chars. Acronyms held by more than one
   facility (LTER, LTREB, NEP, NERR — 33 sites share four acronyms) are
   excluded outright; an ambiguous acronym is not evidence.
3. **network-id** (`high`) — `coastal_datasets.network_id` resolves via
   `networks.label` to a facility of type 'network' with that acronym.
   This is an ID join through the vocabulary, not a text match.

`role` records what the matched text said the organisation does, read
from the words immediately before the match ('hosted by', 'operated by',
'archived at', 'via'). Absent a cue the role is 'steward', which is what
`provider` means by default. A dataset may have several rows: SCCOOS data
is stewarded by both the SCCOOS regional association and Scripps, and
that is the correct answer, not a conflict to resolve.

Expected yield on the 72-record catalogue is 39 datasets (54%) and 48
edges. The 33 unlinked datasets are overwhelmingly agencies the facility
catalogue does not carry as rows (NASA centres, EPA, USGS mission areas,
EU/UN bodies) — they are absent, not unmatched, and forcing them in would
mean inventing facility records.

Usage::

    python scripts/link_dataset_facilities.py --dry-run
    python scripts/link_dataset_facilities.py --export-parquet
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "db" / "cod_kmap.duckdb"
PARQUET_OUT = [ROOT / "db" / "parquet", ROOT / "public" / "parquet"]

# Facility types that can steward a dataset. The other 3,309 rows in
# `facilities` are protected areas — a state park does not publish an
# ERDDAP server, and letting 'Bay' or 'Point' names into a substring test
# is how a text matcher gets embarrassing.
RESEARCH_TYPES = {
    "federal", "network", "nonprofit", "international-federal",
    "university-marine-lab", "international-university", "state",
    "international-nonprofit", "foundation", "observatory",
    "university", "consortium", "international-network", "tribal", "private",
}

# Shortest canonical_name allowed to match as a substring. Below this,
# short institutional names collide with ordinary prose.
MIN_NAME_LEN = 12
MIN_ACRONYM_LEN = 3

# Words that qualify what the matched organisation does. Checked against
# the ~16 characters immediately before the match, longest cue first.
ROLE_CUES = [
    (re.compile(r"hosted (?:by|at)\s*$", re.I), "host"),
    (re.compile(r"operated by\s*$", re.I),      "operator"),
    (re.compile(r"archived at\s*$", re.I),      "archive"),
    (re.compile(r"via\s*$", re.I),              "distributor"),
]
DEFAULT_ROLE = "steward"

# Method precedence when two passes find the same (dataset, facility):
# an ID join and a full-name match outrank a bare acronym.
METHOD_RANK = {"canonical-name": 0, "network-id": 1, "acronym": 2}
METHOD_CONFIDENCE = {"canonical-name": "high", "network-id": "high",
                     "acronym": "medium"}

ACRONYM_TOKEN = re.compile(r"\b[A-Z][A-Za-z0-9\-.]{2,}\b")


def ensure_link_table(conn) -> None:
    """Mirror of the DDL in schema/schema.sql, for a DB predating it."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dataset_facilities (
            dataset_id   VARCHAR NOT NULL,
            facility_id  VARCHAR NOT NULL,
            role         VARCHAR,
            method       VARCHAR NOT NULL,
            confidence   VARCHAR NOT NULL,
            evidence     VARCHAR,
            source       VARCHAR,
            source_url   VARCHAR,
            retrieved_at DATE,
            PRIMARY KEY (dataset_id, facility_id, role)
        )""")


def load_facilities(conn):
    """Research organisations, plus the two lookup structures the passes need."""
    rows = conn.execute(
        "SELECT facility_id, canonical_name, acronym, facility_type "
        "FROM facilities WHERE facility_type IN "
        f"({', '.join('?' * len(RESEARCH_TYPES))})",
        sorted(RESEARCH_TYPES),
    ).fetchall()

    # An acronym shared by several facilities identifies none of them.
    counts = Counter(a for _, _, a, _ in rows if a)
    ambiguous = {a for a, n in counts.items() if n > 1}
    by_acronym: dict[str, list[tuple]] = defaultdict(list)
    for r in rows:
        ac = r[2]
        if ac and len(ac) >= MIN_ACRONYM_LEN and ac not in ambiguous:
            by_acronym[ac.upper()].append(r)

    named = [r for r in rows if len(r[1]) >= MIN_NAME_LEN]
    # 'X ⊂ Y' → prefer Y. Precomputed once so the per-dataset loop is cheap.
    superseded: dict[str, str] = {}
    for a in named:
        for b in named:
            if a[0] != b[0] and len(a[1]) < len(b[1]) and a[1].lower() in b[1].lower():
                superseded[a[0]] = b[0]

    if ambiguous:
        print(f"[facilities] {len(rows)} research organisation(s); "
              f"{len(named)} name-matchable; acronym(s) too ambiguous to use: "
              f"{sorted(ambiguous)}")
    return named, by_acronym, superseded


def role_for(text_lower: str, pos: int) -> str:
    prefix = text_lower[max(0, pos - 16):pos]
    for pat, role in ROLE_CUES:
        if pat.search(prefix):
            return role
    return DEFAULT_ROLE


def name_matches(text, named, superseded):
    """(facility_id, matched_name, role) for every canonical_name in `text`."""
    if not text:
        return []
    low = text.lower()
    found = {}
    for fid, name, _ac, _ft in named:
        pos = low.find(name.lower())
        if pos >= 0:
            found[fid] = (name, pos)
    for fid in list(found):                     # drop the nested shorter name
        sup = superseded.get(fid)
        if sup and sup in found:
            del found[fid]
    return [(fid, name, role_for(low, pos)) for fid, (name, pos) in found.items()]


def derive(conn):
    named, by_acronym, superseded = load_facilities(conn)
    net_label = dict(conn.execute("SELECT network_id, label FROM networks").fetchall())
    datasets = conn.execute(
        "SELECT dataset_id, provider, program, network_id, source_url "
        "FROM coastal_datasets").fetchall()

    edges: dict[tuple[str, str], dict] = {}

    def put(dataset_id, facility_id, role, method, evidence, source_url):
        key = (dataset_id, facility_id)
        prior = edges.get(key)
        if prior and METHOD_RANK[prior["method"]] <= METHOD_RANK[method]:
            return
        edges[key] = {
            "dataset_id": dataset_id, "facility_id": facility_id, "role": role,
            "method": method, "confidence": METHOD_CONFIDENCE[method],
            "evidence": evidence[:200], "source_url": source_url,
        }

    for dataset_id, provider, program, network_id, source_url in datasets:
        for field, text in (("provider", provider), ("program", program)):
            for fid, name, role in name_matches(text, named, superseded):
                put(dataset_id, fid, role, "canonical-name",
                    f"{field} contains '{name}'", source_url)

        # Read the role from the cue words preceding the acronym, the same
        # way the canonical-name pass does. Hardcoding DEFAULT_ROLE here
        # meant "archived at NCEI" was recorded as stewardship, which is a
        # different relationship and the docstring already promised cue
        # reading applied to every pass.
        prov_lower = (provider or "").lower()
        for m in ACRONYM_TOKEN.finditer(provider or ""):
            token = m.group(0)
            for fid, name, _ac, _ft in by_acronym.get(token.upper(), []):
                put(dataset_id, fid, role_for(prov_lower, m.start()), "acronym",
                    f"provider token '{token}' = acronym of '{name}'", source_url)

        label = net_label.get(network_id) if network_id else None
        if label:
            for fid, name, _ac, ftype in by_acronym.get(label.upper(), []):
                if ftype == "network":
                    put(dataset_id, fid, "network", "network-id",
                        f"network_id '{network_id}' = network facility '{name}'",
                        source_url)
    return list(edges.values())


def report(conn) -> None:
    n, ds, fac = conn.execute(
        "SELECT COUNT(*), COUNT(DISTINCT dataset_id), COUNT(DISTINCT facility_id) "
        "FROM dataset_facilities").fetchone()
    total = conn.execute("SELECT COUNT(*) FROM coastal_datasets").fetchone()[0]
    pct = (100.0 * ds / total) if total else 0.0
    print(f"[link] {n} dataset↔facility edge(s): {ds}/{total} datasets "
          f"({pct:.1f}%) across {fac} facilities")
    for method, conf, k in conn.execute(
        "SELECT method, confidence, COUNT(*) FROM dataset_facilities "
        "GROUP BY 1, 2 ORDER BY 3 DESC"
    ).fetchall():
        print(f"  {method:15s} {conf:7s} {k}")
    for role, k in conn.execute(
        "SELECT role, COUNT(*) FROM dataset_facilities GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall():
        print(f"  role {role:12s} {k}")
    unlinked = conn.execute(
        "SELECT COUNT(*) FROM coastal_datasets d LEFT JOIN dataset_facilities l "
        "ON l.dataset_id = d.dataset_id WHERE l.dataset_id IS NULL").fetchone()[0]
    print(f"[unlinked] {unlinked} dataset(s) have no facility in the catalogue — "
          f"expected; see the module docstring")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--export-parquet", action="store_true")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"[error] {args.db} not found — run "
              f"scripts/rebuild_db_from_parquet.py first", file=sys.stderr)
        return 2

    conn = duckdb.connect(str(args.db))
    conn.execute("SET search_path = main;")
    if conn.execute("SELECT COUNT(*) FROM coastal_datasets").fetchone()[0] == 0:
        print("[error] coastal_datasets is empty — run "
              "scripts/load_coastal_datasets.py first", file=sys.stderr)
        return 2

    ensure_link_table(conn)
    rows = derive(conn)
    if not rows:
        print("[error] derived zero edges — refusing to empty dataset_facilities",
              file=sys.stderr)
        return 1

    if args.dry_run:
        print(f"[dry-run] {len(rows)} edge(s) derived, nothing written")
        for r in sorted(rows, key=lambda r: (r["dataset_id"], r["facility_id"])):
            print(f"  {r['dataset_id']:34s} {r['role']:11s} {r['confidence']:6s} "
                  f"{r['evidence']}")
        conn.close()
        return 0

    today = date.today().isoformat()
    conn.execute("DELETE FROM dataset_facilities")
    conn.executemany(
        "INSERT INTO dataset_facilities (dataset_id, facility_id, role, method, "
        "confidence, evidence, source, source_url, retrieved_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 'derived:link_dataset_facilities', ?, ?)",
        [[r["dataset_id"], r["facility_id"], r["role"], r["method"],
          r["confidence"], r["evidence"], r["source_url"], today] for r in rows],
    )
    report(conn)

    if args.export_parquet:
        for base in PARQUET_OUT:
            base.mkdir(parents=True, exist_ok=True)
            out = base / "dataset_facilities.parquet"
            conn.execute(f"COPY dataset_facilities TO '{out}' (FORMAT PARQUET)")
        # Unlike registry_facilities there is no tier filter: both endpoints
        # (coastal_datasets, facilities) ship whole, so every edge resolves
        # in the browser.
        print("[parquet] dataset_facilities -> db/parquet, public/parquet")
        print("[note] new parquet files are gitignored; stage them with `git add -f`")
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
