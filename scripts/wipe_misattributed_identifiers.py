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

Keyed on the id AND the bad value, never on a name
--------------------------------------------------
The roster used to be keyed on the person's name and told the script
"wipe whatever ORCID / OpenAlex id this name holds". Once the bad value
was gone, that rule went on wiping whatever replaced it: Clark Alexander
and Tyler Smith were later given their correct, ORCID-matched ids
(A5052291224; 0000-0002-5256-6087, whose ORCID record is the UVI
Research Professor) and a dry run would have wiped both. Every entry is
now ``person_id -> (bad orcid, bad openalex_id)``: a value is cleared
only while the row still holds exactly that value, so a corrected id is
never touched. The bad values of the former name-keyed entries were
recovered from ``db/parquet/people.parquet`` at 2ad67de, the commit
before the 2026-07-26 wipe (6c9ebaa) that cleared them.

Shared blocklists (data/seed/)
------------------------------
``bad_openalex_pairs.csv`` lists every (person_id or canonical_id,
openalex_id) pair that must never be attached again, with the reason and
an evidence URL. ``bad_openalex_pairs()`` reads it; enrich_people_openalex,
enrich_registry_identifiers and validate_registry refuse those pairs, and
qa.py fails if one is present. Rejected ORCIDs live in
``orcid_resolution_log.csv`` as ``cleared-*`` rows, which
enrich_people_orcid.py honours. ``BAD_BY_ID`` must agree with both files;
``roster_drift()`` checks that, and a run refuses to start if it does not.

Effects, mirroring wipe_bad_openalex_attributions.py:
  * NULLs the offending column(s) on the person
  * DELETEs that person's authorship rows when openalex_id was wrong
    (those links are the misattribution)
  * DELETEs their person_areas rows, which were derived from them
  * leaves `publications` alone — a work wrongly linked to one person may
    be legitimately linked to another
  * records a provenance row per change

and for a person_registry row carrying a blocked openalex_id (an OpenAlex
author record that merges several humans; the ORCID, and so the row,
stays):
  * NULLs openalex_id and the bibliometrics copied from that author record
    (works/citation counts, h/i10, 2-yr citedness, coastal works/share,
    first_pub_year); re-points source_url at the ORCID record when it
    cited the blocked author; appends the reason to notes
  * DELETEs the rows proved only by that author id: coauthor_edges and
    registry_collaborations touching the row, coauthor_candidates and
    coauthor_unmatched_population first seen through it, and the db-only
    coauthor_pairs_raw rows harvested with it as the focal author
  * records a person_identity_source row ('manual-audit') per clear
  The tier is left for scripts/rank_person_registry.py to recompute.

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
import csv
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cod_db  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SEED = ROOT / "data" / "seed"
BAD_PAIRS_CSV = SEED / "bad_openalex_pairs.csv"
ORCID_LOG = SEED / "orcid_resolution_log.csv"

# person_id -> (bad orcid or None, bad openalex_id or None, evidence).
# A value is cleared only while the row holds exactly that value.
BAD_BY_ID = {
    # --- 2026-07-26 roster (cleared in 6c9ebaa); formerly keyed on name ---
    "52a599a3f1287668": (
        "0000-0002-6722-0994", None,
        "Alex Parker (San Francisco Bay NERR): both ids were the planetary "
        "astronomer A. H. Parker (Southwest Research Institute / SETI); 99 "
        "attributed works on Pluto and Kuiper-belt objects."),
    "495ac472743b98ba": (
        "0009-0000-8271-0513", "A5011543287",
        "Andrew Thomson (Institute of Ocean Sciences): openalex_id was "
        "particle physicist 'M. Thomson' (Oxford/Regina), 188 works on LHC "
        "collisions; the ORCID's employer is the European Medicines Agency."),
    "51bd0564be322dfb": (
        "0009-0004-7452-2976", "A5042404214",
        "Anne M. Vogel (EPA Region 5): openalex_id was 'D. J. Ampleford' at "
        "Sandia National Laboratories, 104 works on laser-plasma physics; the "
        "ORCID profile has no employment record to verify."),
    "275d7e7beb3f0116": (
        "0000-0001-7629-6782", "A5023847957",
        "Bob Miller (Santa Barbara Coastal LTER): openalex_id was 'John A. "
        "Miller' (University of Georgia), 131 works on service-oriented "
        "architecture; the ORCID is 'Bobbette Miller', a rehabilitation "
        "researcher at the University of Oklahoma Health Sciences Center."),
    "5a40a871746aa17c": (
        None, "A5010805202",
        "Clark Alexander (Skidaway Institute of Oceanography): openalex_id was "
        "'J. A. Clark', the given name matched as a surname; 171 works. "
        "Disarmed: as a name-keyed entry it would have wiped his correct "
        "A5052291224, which carries his ORCID 0000-0002-3904-1091 (lto "
        "review 2026-10-23: correct, keep)."),
    "f778e750190e040d": (
        None, "A5065104630",
        "Ed Sherwood (Tampa Bay Estuary Program): openalex_id was 'Morgan "
        "Sherwood'. The stored ORCID is correct — seagrass remote sensing and "
        "harmful algal blooms — so only the OpenAlex id is listed."),
    "fd1a07d3b4b419fe": (
        "0000-0002-8133-0348", None,
        "Erik Smith (North Inlet-Winyah Bay NERR): the ORCID's employer is the "
        "Electric Power Research Institute and its works are climate/weather "
        "datasets. The openalex_id ('Erik M. Smith', University of South "
        "Carolina) is correct, so only the ORCID is listed."),
    "ad801d68677a4269": (
        None, "A5020224649",
        "James McClelland (Beaufort Lagoon LTER): openalex_id was 'James L. "
        "McClelland', the Stanford cognitive scientist (bilingualism, reading "
        "development). The stored ORCID 0000-0001-9619-8194 is correct ('J. W. "
        "McClelland', Arctic river biogeochemistry). Disarmed: as a name-keyed "
        "entry it would wipe any OpenAlex id he is given, including the "
        "ORCID-matched A5019713703 lto confirmed on 2026-10-24."),
    "6758e8afd668a3ba": (
        None, "A5033717261",
        "Jean Wiener: openalex_id was 'O. Schneider'; 182 works attributed."),
    "acc5daaf743dfc71": (
        "0000-0002-5297-4195", None,
        "Kim Miller (Apalachicola NERR): the ORCID is 'Kimberly Miller', a "
        "community-college educator (Gaston College / Stanly Community "
        "College) whose only work is on hybrid course design."),
    "b816e10d3c965252": (
        "0000-0001-5308-4680", None,
        "Mark Sanborn (EPA Region 1): the ORCID's works are cancer cell "
        "mechanics, hippocampal neurogenesis and mesenchymal stromal cells. "
        "(The openalex_id, 'Mark A. Sanborn' at Illinois College, was already "
        "gone when the roster was written.)"),
    "9181a33010fad584": (
        None, "A5068462564",
        "Mike De Luca: openalex_id was 'Raktim Sarma'; 99 works attributed."),
    "e5d3e47376a67746": (
        None, "A5001331214",
        "Paul Dest: openalex_id was 'Julien Perret'; 100 works attributed. "
        "He had been cleaned once by wipe_bad_openalex_attributions.py and was "
        "re-broken."),
    "adc2be4059e9dcb5": (
        None, "A5046725106",
        "Paul Orlando: openalex_id was 'C. Padilla Aranda'; 166 works."),
    "0433ba6616b2808a": (
        "0000-0002-8460-6573", None,
        "Tyler Smith (UVI Center for Marine and Environmental Studies): that "
        "ORCID's sole employer is Adventium Labs, a software research company "
        "(accepted at 0.667 on 'University of Minnesota'). Disarmed: as a "
        "name-keyed entry it would wipe his verified 0000-0002-5256-6087 "
        "(ORCID record: Tyler Smith, Research Professor, University of the "
        "Virgin Islands, 89 coral-reef works; checked 2026-10-25)."),
}

# Rows whose `orcid` column holds biography prose rather than an
# identifier. The text is preserved by moving it into `notes` rather than
# being discarded, since it is hand-curated and true — it is simply in
# the wrong column, where it would fail every downstream ORCID lookup.
PROSE_IN_ORCID = {
    "e0021e367b7acdbd": "Marty Downs",
    "88529d12a8486251": "Víctor Manuel Vidal Martínez",
}

# person_registry columns copied from the OpenAlex author record
# (enrich_registry_identifiers.author_fields / harvest_coastal_authors). A
# merged author record's counts describe several people, so they go with
# the id.
REGISTRY_OA_METRICS = ("works_count", "cited_by_count", "h_index", "i10_index",
                       "two_yr_mean_citedness", "coastal_works_count",
                       "coastal_share", "first_pub_year")


# ---------------------------------------------------------------------------
# Shared blocklist readers (imported by the enrich / registry scripts).
# ---------------------------------------------------------------------------

def norm_openalex(v: str | None) -> str:
    """'https://openalex.org/a5…' or 'A5…' -> 'A5…' (upper-case), or ''."""
    return (v or "").strip().rstrip("/").rsplit("/", 1)[-1].upper()


def load_bad_openalex_rows(path: Path = BAD_PAIRS_CSV) -> list[dict]:
    """The rows of bad_openalex_pairs.csv; each names a person_id OR a
    person_registry canonical_id, and the openalex_id never to attach."""
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        rows = []
        for r in csv.DictReader(l for l in fh if not l.startswith("#")):
            r = {k: (v or "").strip() for k, v in r.items()}
            r["openalex_id"] = norm_openalex(r.get("openalex_id"))
            if r["openalex_id"] and (r.get("person_id") or r.get("canonical_id")):
                rows.append(r)
        return rows


def bad_openalex_pairs(path: Path = BAD_PAIRS_CSV) -> set[tuple[str, str]]:
    """(subject id, OPENALEX_ID) pairs that must never be (re-)attached.

    The subject is a people.person_id or a person_registry.canonical_id;
    the two id spaces cannot collide ('orcid:' / 'openalex:' / 'codp:'
    prefixes vs 16 hex digits), so one set serves both. The id is
    upper-cased; test with is_blocked().
    """
    out: set[tuple[str, str]] = set()
    for r in load_bad_openalex_rows(path):
        for subject in (r.get("person_id"), r.get("canonical_id")):
            if subject:
                out.add((subject, r["openalex_id"]))
    return out


def is_blocked(pairs: set[tuple[str, str]], openalex_id: str | None,
               *subject_ids: str | None) -> bool:
    """True when `openalex_id` is blocked for any of `subject_ids`."""
    oa = norm_openalex(openalex_id)
    return bool(oa) and any((s, oa) in pairs for s in subject_ids if s)


def load_orcid_rejections(path: Path = ORCID_LOG) -> dict[tuple[str, str], str]:
    """(person_id, ORCID upper-cased) -> decision for every 'cleared-*' row of
    the ORCID resolution log (enrich_people_orcid.load_rejections)."""
    out: dict[tuple[str, str], str] = {}
    if not path.exists():
        return out
    with path.open(newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            dec = (r.get("decision") or "").strip()
            pid = (r.get("person_id") or "").strip()
            oid = (r.get("orcid") or "").strip().upper()
            if dec.startswith("cleared-") and pid and oid:
                out[(pid, oid)] = dec
    return out


def roster_drift(pairs_csv: Path = BAD_PAIRS_CSV, orcid_log: Path = ORCID_LOG) -> list[str]:
    """Where BAD_BY_ID disagrees with the shared seed files (empty = agrees).

    Every BAD_BY_ID openalex_id must be a person_id row of
    bad_openalex_pairs.csv and vice versa, and every BAD_BY_ID ORCID must
    have a cleared-* row in the ORCID log, so the enrich scripts block
    exactly what this roster clears.
    """
    out = []
    csv_people = {(r["person_id"], r["openalex_id"])
                  for r in load_bad_openalex_rows(pairs_csv) if r.get("person_id")}
    roster = {(pid, oa.upper()) for pid, (_, oa, _) in BAD_BY_ID.items() if oa}
    out += [f"BAD_BY_ID {pid} {oa} missing from {pairs_csv.name}"
            for pid, oa in sorted(roster - csv_people)]
    out += [f"{pairs_csv.name} person row {pid} {oa} missing from BAD_BY_ID"
            for pid, oa in sorted(csv_people - roster)]
    rejected = load_orcid_rejections(orcid_log)
    out += [f"BAD_BY_ID {pid} ORCID {orcid} has no cleared-* row in {orcid_log.name}"
            for pid, (orcid, _, _) in sorted(BAD_BY_ID.items())
            if orcid and (pid, orcid.upper()) not in rejected]
    return out


# ---------------------------------------------------------------------------

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

    drift = roster_drift()
    if drift:
        print("[error] BAD_BY_ID and the seed blocklists disagree; fix them first:\n  "
              + "\n  ".join(drift), file=sys.stderr)
        return 2

    try:
        conn = cod_db.connect(read_only=args.dry_run, catalog=catalog_arg(args),
                              data=args.lake_data)
    except cod_db.LakeNotFound as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2

    registry_rows = [r for r in load_bad_openalex_rows() if r.get("canonical_id")]
    # A dry run opens the lake read-only and writes nothing; a real run is
    # one snapshot (rolled back whole if the integrity check fails, and
    # empty — no snapshot — when there is nothing left to wipe).
    tx = (contextlib.nullcontext() if args.dry_run else cod_db.transaction(
        conn, f"wipe_misattributed_identifiers: {len(BAD_BY_ID)} people roster + "
              f"{len(registry_rows)} registry blocklist + "
              f"{len(PROSE_IN_ORCID)} prose-in-orcid rows"))
    with tx:
        n_orcid, n_oa, n_auth, n_areas, n_prose = _wipe(conn, args.dry_run)
        reg = _wipe_registry(conn, registry_rows, args.dry_run)
        if not args.dry_run:
            cod_db.assert_integrity(
                conn, ["people", "authorship", "person_areas", "provenance",
                       "person_registry", "person_identity_source",
                       "coauthor_edges", "registry_collaborations",
                       "coauthor_candidates", "coauthor_unmatched_population",
                       "coauthor_pairs_raw"])

    print(f"[totals] orcid_wiped={n_orcid} openalex_wiped={n_oa} "
          f"authorship_deleted={n_auth} person_areas_deleted={n_areas} "
          f"prose_moved={n_prose}")
    print("[totals] registry " + " ".join(f"{k}={v}" for k, v in reg.items()))

    if not args.dry_run:
        # parquet is export_parquet.py's to write.
        print("[next] run scripts/rank_person_registry.py if registry rows were "
              "cleared, then scripts/export_parquet.py to publish")
    conn.close()
    return 0


def _wipe(conn, dry_run: bool) -> tuple[int, int, int, int, int]:
    """Apply BAD_BY_ID and PROSE_IN_ORCID to people; with dry_run, only print."""
    now = datetime.now(timezone.utc).isoformat()
    n_orcid = n_oa = n_auth = n_areas = n_prose = 0
    prov: list[tuple] = []

    for pid, (bad_orcid, bad_oa, why) in BAD_BY_ID.items():
        r = cod_db.rows(conn, "SELECT name, orcid, openalex_id FROM people "
                              "WHERE person_id = ?", [pid])
        if not r:
            print(f"  [skip] {pid}: not in people")
            continue
        name, orcid, oaid = r[0]
        wipe_orcid = bool(bad_orcid) and (orcid or "").strip().upper() == bad_orcid.upper()
        wipe_oa = bool(bad_oa) and norm_openalex(oaid) == bad_oa.upper()
        acts = ([f"orcid={orcid}"] if wipe_orcid else []) + \
               ([f"openalex_id={oaid}"] if wipe_oa else [])
        if not acts:
            continue
        npub = cod_db.scalar(
            conn, "SELECT count(*) FROM authorship WHERE person_id = ?", [pid])
        print(f"  {name} ({pid}): wiping {', '.join(acts)}"
              + (f" and {npub} authorship rows" if wipe_oa and npub else ""))
        if dry_run:
            continue

        if wipe_orcid:
            cod_db.scalar(conn, "UPDATE people SET orcid = NULL, updated_at = now() "
                                "WHERE person_id = ?", [pid])
            n_orcid += 1
        if wipe_oa:
            cod_db.scalar(conn, "UPDATE people SET openalex_id = NULL, "
                                "research_interests = NULL, "
                                "research_interests_source_url = NULL, "
                                "updated_at = now() WHERE person_id = ?", [pid])
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

    for pid, name in PROSE_IN_ORCID.items():
        r = cod_db.rows(conn, "SELECT orcid, notes FROM people WHERE person_id = ?", [pid])
        if not r:
            continue
        orcid, notes = r[0]
        if not orcid or len(orcid) == 19:
            continue
        print(f"  {name} ({pid}): moving prose out of orcid into notes")
        if dry_run:
            continue
        merged = "; ".join(t for t in [notes, orcid] if t)
        cod_db.scalar(conn, "UPDATE people SET orcid = NULL, notes = ?, "
                            "updated_at = now() WHERE person_id = ?", [merged, pid])
        n_prose += 1

    return n_orcid, n_oa, n_auth, n_areas, n_prose


def _wipe_registry(conn, blocked: list[dict], dry_run: bool) -> dict[str, int]:
    """Clear blocked openalex_ids from person_registry and the rows that rest
    on them; with dry_run, only count and print."""
    out = dict.fromkeys(("rows", "coauthor_edges", "registry_collaborations",
                         "coauthor_candidates", "coauthor_unmatched_population",
                         "coauthor_pairs_raw"), 0)
    if not cod_db.table_exists(conn, "person_registry"):
        return out
    today = datetime.now(timezone.utc).date().isoformat()
    has = {t: cod_db.table_exists(conn, t) for t in out if t != "rows"}

    for b in blocked:
        cid, oa = b["canonical_id"], b["openalex_id"]
        r = cod_db.rows(conn, "SELECT display_name, orcid, openalex_id, source_url "
                              "FROM person_registry WHERE canonical_id = ?", [cid])
        if not r or norm_openalex(r[0][2]) != oa:
            continue
        name, orcid, stored_oa, src = r[0]
        # What rests on this author id. Counted before anything is deleted.
        deps = {
            "coauthor_edges": ("canonical_id_a = ? OR canonical_id_b = ?", [cid, cid]),
            "registry_collaborations": ("canonical_id_a = ? OR canonical_id_b = ?", [cid, cid]),
            "coauthor_candidates": ("seen_with_canonical_id = ?", [cid]),
            "coauthor_unmatched_population": ("seen_with_openalex_id = ?", [stored_oa]),
            "coauthor_pairs_raw": ("focal_openalex_id = ?", [stored_oa]),
        }
        counts = {t: cod_db.scalar(conn, f"SELECT count(*) FROM {t} WHERE {w}", p)
                  for t, (w, p) in deps.items() if has[t]}
        print(f"  [registry] {name} ({cid}): clearing openalex_id={stored_oa}; "
              + ", ".join(f"{t} -{n}" for t, n in counts.items()))
        out["rows"] += 1
        for t, n in counts.items():
            out[t] += n
        if dry_run:
            continue

        new_src = (f"https://orcid.org/{orcid}"
                   if orcid and norm_openalex(src) == oa else src)
        note = (f"openalex_id {stored_oa} cleared {today} by "
                f"wipe_misattributed_identifiers.py (data/seed/bad_openalex_pairs.csv): "
                f"the author record merges several people; its bibliometrics "
                f"were cleared with it.")
        cod_db.scalar(conn, f"""
            UPDATE person_registry
            SET openalex_id = NULL,
                {", ".join(f"{c} = NULL" for c in REGISTRY_OA_METRICS)},
                source_url = ?,
                notes = CASE WHEN notes IS NULL OR notes = '' THEN ?
                             ELSE notes || '; ' || ? END
            WHERE canonical_id = ?""", [new_src, note, note, cid])
        for t, (w, p) in deps.items():
            if has[t]:
                cod_db.scalar(conn, f"DELETE FROM {t} WHERE {w}", p)
        cod_db.create_temp_like(conn, "_wipe_pis",
                                "SELECT * FROM person_identity_source")
        conn.execute(
            "INSERT INTO _wipe_pis (canonical_id, field, value, method, evidence, "
            "source_url, confidence, retrieved_at) VALUES (?,?,?,?,?,?,?,?)",
            [cid, "openalex_id-cleared", stored_oa, "manual-audit",
             f"blocked by data/seed/bad_openalex_pairs.csv: {b.get('reason', '')}",
             b.get("evidence_url") or None, "high", today])
        cod_db.merge_upsert(conn, "person_identity_source",
                            ["canonical_id", "field", "value", "method", "source_url"],
                            "_wipe_pis")
        conn.execute("DROP TABLE _wipe_pis")
    return out


if __name__ == "__main__":
    sys.exit(main())
