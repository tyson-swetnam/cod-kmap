#!/usr/bin/env python3
"""Remove identifiers that point at the wrong human being.

Third instance of the failure this repo keeps having, after
wipe_bad_openalex_attributions.py and wipe_medicine_attributions.py.
This time the bad ids came in through the ORCID resolver rather than a
name-only OpenAlex search: scripts/enrich_people_orcid.py scored a
candidate's employer against the person's facility with
max(SequenceMatcher, token_overlap) and no requirement that the two
names share a distinctive word, so at the 0.45 default it accepted

    Electric Power Research Institute    -> NERR                (0.560)
    Oklahoma Medical Research Foundation -> LTER                (0.593)
    European Medicines Agency  -> Institute of Ocean Sciences   (0.513)
    Appalachian State University         -> Apalachicola NERR   (0.529)

and its name-only fallback picked arbitrarily among namesakes despite a
comment promising it fired only for a single candidate. Both defects are
fixed in that script; this one cleans up the rows they produced, plus
several wrong `openalex_id` values that predate them.

Every entry below was decided on evidence, not on a score:
  * the OpenAlex author record's display_name and last_known_institutions
  * the ORCID profile's own employment list and work titles
  * what the attributed publications are actually about, read from
    publication_topics

Diacritic and hyphenation differences ("Crespi Abril" vs "Crespi-Abril",
"Möller Jr." vs "Möller") were checked and are NOT treated as mismatches.

Effects, mirroring wipe_bad_openalex_attributions.py:
  * NULLs the offending column(s) on the person
  * DELETEs that person's authorship rows when openalex_id was wrong
    (those links are the misattribution)
  * DELETEs their person_areas rows, which were derived from them
  * leaves `publications` alone — a work wrongly linked to one person may
    be legitimately linked to another
  * records a provenance row per change

Re-runs are idempotent. A real run is one lake snapshot (scripts/cod_db.py),
rolled back whole if it would orphan a row; ``--dry-run`` opens the lake
read-only. Run scripts/export_parquet.py to publish.

Usage::
    python scripts/wipe_misattributed_identifiers.py --dry-run
    python scripts/wipe_misattributed_identifiers.py
"""
from __future__ import annotations

import argparse
import contextlib
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cod_db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# name -> (wipe_orcid, wipe_openalex, evidence)
BAD = {
    "Alex Parker": (
        True, True,
        "Both ids are the planetary astronomer A. H. Parker (Southwest "
        "Research Institute / SETI); 99 attributed works are on Pluto and "
        "Kuiper-belt objects. The San Francisco Bay NERR person is a "
        "different Alex Parker."),
    "Andrew Thomson": (
        True, True,
        "openalex_id is particle physicist 'M. Thomson' (Oxford/Regina); "
        "188 works on LHC collisions. Stored ORCID's employer is the "
        "European Medicines Agency. Neither is the Institute of Ocean "
        "Sciences person."),
    "Anne M. Vogel": (
        True, True,
        "openalex_id resolves to 'D. J. Ampleford' at Sandia National "
        "Laboratories — a different name entirely — with 104 works on "
        "laser-plasma physics. ORCID profile has no employment record."),
    "Bob Miller": (
        True, True,
        "openalex_id is 'John A. Miller' (University of Georgia), 131 "
        "works on service-oriented architecture and web services. Stored "
        "ORCID is 'Bobbette Miller', a rehabilitation researcher at the "
        "University of Oklahoma Health Sciences Center. The Santa Barbara "
        "Coastal LTER person is Robert J. Miller."),
    "Clark Alexander": (
        False, True,
        "openalex_id resolves to 'J. A. Clark' — the given name was matched "
        "as a surname. 171 works attributed."),
    "Ed Sherwood": (
        False, True,
        "openalex_id is 'Morgan Sherwood'. The stored ORCID is correct — "
        "its works are seagrass remote sensing and harmful algal blooms at "
        "the Tampa Bay Estuary Program — so only the OpenAlex id is wiped."),
    "Erik Smith": (
        True, False,
        "Stored ORCID's employer is the Electric Power Research Institute "
        "and its works are climate/weather datasets. The openalex_id is "
        "correct — 'Erik M. Smith' at the University of South Carolina, "
        "which runs North Inlet-Winyah Bay NERR — so only the ORCID goes."),
    "James McClelland": (
        False, True,
        "openalex_id is 'James L. McClelland', the Stanford cognitive "
        "scientist; the attributed works are on bilingualism and reading "
        "development. The stored ORCID is correct — 'J. W. McClelland', "
        "Arctic river biogeochemistry, Beaufort Lagoon LTER."),
    "Jean Wiener": (
        False, True,
        "openalex_id resolves to 'O. Schneider'. 182 works attributed."),
    "Kim Miller": (
        True, False,
        "Stored ORCID is 'Kimberly Miller', a community-college educator "
        "(Gaston College / Stanly Community College) whose only work is on "
        "hybrid course design. Not the Apalachicola NERR person."),
    "Mark Sanborn": (
        True, True,
        "Stored ORCID's works are cancer cell mechanics, hippocampal "
        "neurogenesis and mesenchymal stromal cells. openalex_id is 'Mark "
        "A. Sanborn' at Illinois College, publishing on mosquito-borne "
        "disease. Neither is the EPA Region 1 person."),
    "Mike De Luca": (
        False, True,
        "openalex_id resolves to 'Raktim Sarma'. 99 works attributed."),
    "Paul Dest": (
        False, True,
        "openalex_id resolves to 'Julien Perret'. 100 works attributed. "
        "This person was already cleaned once by "
        "wipe_bad_openalex_attributions.py and was re-broken."),
    "Paul Orlando": (
        False, True,
        "openalex_id resolves to 'C. Padilla Aranda'. 166 works attributed."),
    "Tyler Smith": (
        True, False,
        "Stored ORCID's sole employer is Adventium Labs, a software "
        "research company. Not the University of the Virgin Islands "
        "Center for Marine and Environmental Studies person."),
}

# Rows whose `orcid` column holds biography prose rather than an
# identifier. The text is preserved by moving it into `notes` rather than
# being discarded, since it is hand-curated and true — it is simply in
# the wrong column, where it would fail every downstream ORCID lookup.
PROSE_IN_ORCID = ["Marty Downs", "Víctor Manuel Vidal Martínez"]


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


def main() -> int:
    ap = argparse.ArgumentParser()
    cod_db.add_argparse_catalog(ap)
    ap.add_argument("--db", type=Path, default=None,
                    help="deprecated alias of --catalog (a .duckdb path is ignored)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    try:
        conn = cod_db.connect(read_only=args.dry_run, catalog=catalog_arg(args),
                              data=args.lake_data)
    except cod_db.LakeNotFound as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2

    # A dry run opens the lake read-only and writes nothing; a real run is
    # one snapshot (rolled back whole if the integrity check fails, and
    # empty — no snapshot — when there is nothing left to wipe).
    tx = (contextlib.nullcontext() if args.dry_run else cod_db.transaction(
        conn, f"wipe_misattributed_identifiers: {len(BAD)} roster + "
              f"{len(PROSE_IN_ORCID)} prose-in-orcid rows"))
    with tx:
        n_orcid, n_oa, n_auth, n_areas, n_prose = _wipe(conn, args.dry_run)
        if not args.dry_run:
            cod_db.assert_integrity(
                conn, ["people", "authorship", "person_areas", "provenance"])

    print(f"[totals] orcid_wiped={n_orcid} openalex_wiped={n_oa} "
          f"authorship_deleted={n_auth} person_areas_deleted={n_areas} "
          f"prose_moved={n_prose}")

    if not args.dry_run:
        # parquet is export_parquet.py's to write.
        print("[next] run scripts/export_parquet.py to publish")
    conn.close()
    return 0


def _person_by_name(conn, cols: str, name: str):
    """First people row with this exact name (ORDER BY person_id so a
    shared name resolves the same way every run), or None."""
    r = cod_db.rows(conn, f"SELECT {cols} FROM people WHERE name = ? "
                          f"ORDER BY person_id LIMIT 1", [name])
    return r[0] if r else None


def _wipe(conn, dry_run: bool) -> tuple[int, int, int, int, int]:
    """Apply BAD and PROSE_IN_ORCID; with dry_run, only print."""
    now = datetime.now(timezone.utc).isoformat()
    n_orcid = n_oa = n_auth = n_areas = n_prose = 0
    prov: list[tuple] = []

    for name, (wipe_orcid, wipe_oa, why) in BAD.items():
        row = _person_by_name(conn, "person_id, orcid, openalex_id", name)
        if not row:
            print(f"  [skip] {name}: not in people")
            continue
        pid, orcid, oaid = row
        acts = []
        if wipe_orcid and orcid:
            acts.append(f"orcid={orcid}")
        if wipe_oa and oaid:
            acts.append(f"openalex_id={oaid}")
        if not acts:
            continue
        npub = cod_db.scalar(
            conn, "SELECT count(*) FROM authorship WHERE person_id = ?", [pid])
        print(f"  {name}: wiping {', '.join(acts)}"
              + (f" and {npub} authorship rows" if wipe_oa and npub else ""))
        if dry_run:
            continue

        if wipe_orcid and orcid:
            cod_db.scalar(conn, "UPDATE people SET orcid = NULL, updated_at = now() "
                                "WHERE person_id = ?", [pid])
            n_orcid += 1
        if wipe_oa and oaid:
            cod_db.scalar(conn, "UPDATE people SET openalex_id = NULL, "
                                "research_interests = NULL, updated_at = now() "
                                "WHERE person_id = ?", [pid])
            n_oa += 1
            # DELETE returns a one-row (count) result.
            n_auth += cod_db.scalar(
                conn, "DELETE FROM authorship WHERE person_id = ?", [pid])
            n_areas += cod_db.scalar(
                conn, "DELETE FROM person_areas WHERE person_id = ?", [pid])

        prov.append(("person", pid,
                     "https://api.openalex.org/authors + https://pub.orcid.org",
                     now, "high",
                     f"wipe_misattributed_identifiers.py: cleared "
                     f"{', '.join(a.split('=')[0] for a in acts)}. {why}"))

    if prov:
        # Staged, then one INSERT: a row-at-a-time insert into the lake
        # writes one tiny data file per statement.
        conn.execute("CREATE OR REPLACE TEMP TABLE stage_provenance ("
                     "record_type VARCHAR, record_id VARCHAR, source_url VARCHAR, "
                     "retrieved_at DATE, confidence VARCHAR, agent VARCHAR)")
        conn.executemany("INSERT INTO stage_provenance VALUES (?,?,?,?,?,?)", prov)
        conn.execute("INSERT INTO provenance (record_type, record_id, source_url, "
                     "retrieved_at, confidence, agent) "
                     "SELECT * FROM stage_provenance")
        conn.execute("DROP TABLE stage_provenance")

    for name in PROSE_IN_ORCID:
        row = _person_by_name(conn, "person_id, orcid, notes", name)
        if not row:
            continue
        pid, orcid, notes = row
        if not orcid or len(orcid) == 19:
            continue
        print(f"  {name}: moving prose out of orcid into notes")
        if dry_run:
            continue
        merged = "; ".join(t for t in [notes, orcid] if t)
        cod_db.scalar(conn, "UPDATE people SET orcid = NULL, notes = ?, "
                            "updated_at = now() WHERE person_id = ?", [merged, pid])
        n_prose += 1

    return n_orcid, n_oa, n_auth, n_areas, n_prose


if __name__ == "__main__":
    sys.exit(main())
