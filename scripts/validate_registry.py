#!/usr/bin/env python3
"""Validate person_registry identifiers and build the provenanced co-author graph.

Pipeline stages (each independently runnable via --stage):

  validate   For every core-tier registry row, resolve its OpenAlex author id
             (batched 50 per request), compare the returned ORCID and display
             name, and resolve its affiliation ROR against api.ror.org. Writes
             person_validation, long form, one row per (canonical_id, check_id,
             run_id) so re-validation appends and runs can be diffed.
             (It used to write db/parquet/person_validation.parquet only,
             which export_parquet.py then overwrote from the lake table.)
  harvest    Page each researcher's coastal-topic works and emit co-author pairs.
  match      Resolve harvested co-authors against the registry BY IDENTIFIER
             ONLY, then rank the non-matching remainder as expansion candidates.
             Replaces coauthor_edges / coauthor_candidates and the db-only
             coauthor_unmatched_population table.
  rdf        Serialize the registry + candidates to Turtle, run OWL-RL sameAs
             closure, and report split identities the relational join misses
             (db-only table split_identity_findings).
  shacl      Validate the RDF against schema/shapes/cod-shapes.ttl.

MEASURED CONSTRAINTS -- these drove the design; do not "optimise" past them.

  * The OpenAlex key has a HARD quota (X-RateLimit-Limit: 10000/day, one credit
    per request). Budget in REQUESTS, not seconds. --request-budget is the
    governor and the run stops cleanly when it is spent.
  * Throughput is server-capped at ~62 works/s and does NOT improve with
    concurrency (1/4/8 threads measured 60-87 works/s). Single-threaded on
    purpose; parallel workers only split bookkeeping.
  * Batch authors 50 per cursor: identical per-work cost, but one cursor covers
    50 authors instead of one (3.4s/200 works vs ~7s single-author).
  * api.ror.org serves ROR SCHEMA v2. `name` and `country.country_code` no
    longer exist: display name is the names[] entry typed 'ror_display',
    country is locations[].geonames_details.country_code. Parsing the v1 shape
    returns HTTP 200 with silently EMPTY values -- a failure that looks like
    success. parse_ror() below handles v2; do not "simplify" it.

IDENTITY RULES (project non-negotiables, enforced in code and in the schema):

  * A person is matched to a person by IDENTIFIER EQUALITY ONLY -- ORCID,
    OpenAlex author id, or owl:sameAs closure over those. NEVER by name. The
    coauthor_edges.match_method CHECK constraint has no 'name-similarity'
    member and one must never be added.
  * Every row written carries >=1 persistent identifier, a citable source_url,
    and a confidence in {high, medium, low}.
  * A (canonical_id or person_id, openalex_id) pair on the blocklist
    data/seed/bad_openalex_pairs.csv (an OpenAlex author record a review
    found to be another person, or several people merged) is refused: the
    validate stage does not fetch it and records a 'fail' verdict naming
    the blocklist, and the match stage does not key the registry row on
    it, so it proves no edge and resolves no co-author.
  * canonical_id values starting 'codp:' are COD-internal people with no public
    identifier BY CONSTRUCTION. Their public-identifier checks are
    'not_applicable', never 'fail'. Counting them as failures would pressure
    someone into inventing an identifier, which is the exact defect the
    registry build was fixed to avoid.

Usage:
    python scripts/validate_registry.py --stage validate
    python scripts/validate_registry.py --stage harvest --request-budget 2000
    python scripts/validate_registry.py --stage match
    python scripts/validate_registry.py --stage rdf --stage shacl

Every stage that writes does so into the DuckLake (scripts/cod_db.py), one
snapshot per stage; scripts/export_parquet.py publishes the tables (the
db-only ones to db/parquet only). The match and rdf stages read the
harvested co-author pairs from PAIRS_DIR (scripts/harvest_runner.py) when it
holds any parquet, else from the lake's db-only coauthor_pairs_raw table, so
both run offline on a lake rebuilt from the committed db/parquet.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cod_db  # noqa: E402
from wipe_misattributed_identifiers import (  # noqa: E402
    BAD_PAIRS_CSV, bad_openalex_pairs, is_blocked)

TOPIC_SET = "db/derived/coastal_topic_set.parquet"
STATE = "db/derived/harvest_state.json"
WORKS_DIR = "db/derived/harvest_works.parquet.d"
PAIRS_DIR = "db/derived/harvest_pairs.parquet.d"
ONTOLOGY = "schema/ontology/cod.owl"
SHAPES = "schema/shapes/cod-shapes.ttl"

OA = "https://api.openalex.org/"
ROR = "https://api.ror.org/organizations/"
UA = "cod-kmap-registry-validation"
CONF = ("high", "medium", "low")


# --------------------------------------------------------------------------- #
# lake helpers
# --------------------------------------------------------------------------- #

def _table_columns(con, table: str) -> list[str]:
    """Column names of a lake table, in order. Drains the result (DuckDB 2.0
    refuses .register() while an execute() result is half-read)."""
    cur = con.execute(f"SELECT * FROM {cod_db.qident(table)} LIMIT 0")
    names = [d[0] for d in cur.description]
    cur.fetchall()
    return names


def _stage_df(con, df: pd.DataFrame, table: str, stage: str) -> int:
    """Copy ``df`` into TEMP table ``stage``, typed like lake table ``table``.

    Column order and membership come from the lake, so a schema change
    surfaces here as a KeyError rather than as a silently mis-ordered
    INSERT, and the INSERT casts pandas dtypes (object, float-with-NaN) to
    the lake's types instead of letting them leak into it. Every earlier
    result on ``con`` is drained, so the register() below is safe.

    The stage gets explicit column DDL, not ``CREATE TEMP TABLE s AS SELECT
    * FROM t LIMIT 0``: in DuckDB 2.0.0 an empty-CTAS TEMP table filled from
    a registered frame of >= 122,880 rows inside a transaction reports the
    right count(*), but later scans of it (merge_upsert's copy) see none.
    """
    cols = _table_columns(con, table)
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise KeyError(f"{table}: pipeline produced no value for {missing}")
    types = dict(cod_db.rows(
        con, "SELECT column_name, data_type FROM duckdb_columns() "
             "WHERE database_name = ? AND schema_name = 'main' AND table_name = ?",
        [cod_db.LAKE, table]))
    con.execute(f"CREATE OR REPLACE TEMP TABLE {stage} ("
                + ", ".join(f"{cod_db.qident(c)} {types[c]}" for c in cols) + ")")
    if len(df):
        con.register("_df_in", df[cols])
        try:
            con.execute(f"INSERT INTO {stage} SELECT * FROM _df_in")
        finally:
            con.unregister("_df_in")
    return len(df)


def blocked_registry_keys(con) -> str:
    """Create TEMP table _blocked (canonical_id, oa): every registry row whose
    openalex_id is blocked for it (by canonical_id or person_id) in
    bad_openalex_pairs.csv. Returns its name, for anti-joins."""
    pairs = bad_openalex_pairs()
    con.execute("CREATE OR REPLACE TEMP TABLE _blocked_pairs "
                "(subject VARCHAR, oa VARCHAR)")
    if pairs:
        con.executemany("INSERT INTO _blocked_pairs VALUES (?, ?)", sorted(pairs))
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _blocked AS
        SELECT DISTINCT r.canonical_id,
               upper(replace(r.openalex_id, 'https://openalex.org/', '')) AS oa
        FROM person_registry r JOIN _blocked_pairs b
          ON (b.subject = r.canonical_id OR b.subject = r.person_id)
         AND b.oa = upper(replace(r.openalex_id, 'https://openalex.org/', ''))""")
    n = cod_db.scalar(con, "SELECT count(*) FROM _blocked")
    if n:
        print(f"[blocked] {n} registry row(s) carry a blocked openalex_id "
              f"({BAD_PAIRS_CSV.name}); they are not keyed on it", flush=True)
    return "_blocked"


def pairs_source(con) -> str:
    """The harvested co-author pairs: the harvest_runner.py checkpoint
    parquet when present, else the lake's db-only coauthor_pairs_raw table
    (the committed copy of the same pairs, loaded from db/parquet)."""
    if glob.glob(f"{PAIRS_DIR}/*.parquet"):
        print(f"[pairs] reading {PAIRS_DIR}/*.parquet", flush=True)
        return f"read_parquet('{PAIRS_DIR}/*.parquet')"
    if cod_db.table_exists(con, "coauthor_pairs_raw"):
        print(f"[pairs] {PAIRS_DIR} is empty; reading the lake's "
              f"coauthor_pairs_raw table", flush=True)
        return f"{cod_db.LAKE}.main.coauthor_pairs_raw"
    sys.exit(f"no harvested co-author pairs: {PAIRS_DIR} holds no parquet and "
             "the lake has no coauthor_pairs_raw table (run "
             "scripts/harvest_runner.py, or rebuild the lake from db/parquet)")


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _oa_key() -> str:
    k = os.environ.get("OPENALEX_API_KEY")
    if not k:
        sys.exit("OPENALEX_API_KEY is not set. OpenAlex rejects keyless calls "
                 "(409/429); never pass mailto= instead.")
    return k


def http_json(url: str, retries: int = 5, timeout: int = 90):
    """GET JSON with exponential backoff on 429/5xx. Returns None on 404."""
    delay = 1.0
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as f:
                return json.loads(f.read())
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
                continue
            raise
    return None


def oa_get(path: str, **params):
    params["api_key"] = _oa_key()
    return http_json(OA + path + "?" + urllib.parse.urlencode(params))


def quota_remaining() -> int | None:
    """Live quota from the response headers, so a run can stop before exhausting
    a key that is shared with every other job in the project."""
    url = (OA + "works?" + urllib.parse.urlencode(
        {"filter": "openalex_id:W2741809807", "select": "id",
         "per-page": "1", "api_key": _oa_key()}))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as f:
            v = f.headers.get("X-RateLimit-Remaining")
            return int(v) if v is not None else None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #

def short_id(v):
    """'https://openalex.org/A123' -> 'A123'. Passes bare ids through."""
    return None if v is None else str(v).rsplit("/", 1)[-1]


def bare_orcid(v):
    """ORCID URL or bare -> bare 0000-0000-0000-000X, else None.

    Returns None rather than a best-effort string: cod-shapes.ttl requires bare
    16-digit form and a malformed value must not enter the graph at all.
    """
    if not v or not isinstance(v, str):
        return None
    s = v.rsplit("/", 1)[-1].strip().upper()
    parts = s.split("-")
    if len(parts) == 4 and all(len(p) == 4 for p in parts):
        return s
    return None


def parse_ror(j: dict) -> tuple[str, str, str]:
    """(display_name, country_code, status) from a ROR **v2** payload.

    v1's `name` and `country.country_code` are gone. Parsing the v1 shape
    yields empty strings with HTTP 200 -- looks like success, silently loses
    every organisation name.
    """
    disp = ""
    for n in (j.get("names") or []):
        if "ror_display" in (n.get("types") or []):
            disp = n.get("value") or ""
            break
    if not disp:
        for n in (j.get("names") or []):
            if "label" in (n.get("types") or []):
                disp = n.get("value") or ""
                break
    country = ""
    for loc in (j.get("locations") or []):
        gd = loc.get("geonames_details") or {}
        if gd.get("country_code"):
            country = gd["country_code"]
            break
    return disp, country, (j.get("status") or "")


# --------------------------------------------------------------------------- #
# stage: validate
# --------------------------------------------------------------------------- #

VERDICT_MAP = {
    "resolved": "pass", "agree": "pass", "exact": "pass",
    "ror_confirmed": "pass", "family_match": "pass",
    "not_applicable": "not_applicable", "both_absent": "not_applicable",
    "ror_absent": "not_applicable",
    "disagree": "fail", "ror_stale": "fail",
    "openalex_absent": "unresolved", "partial": "unresolved",
}


def stage_validate(con, run_id: str, batch_size: int = 50) -> pd.DataFrame:
    reg = con.execute(
        "SELECT canonical_id, display_name, orcid, openalex_id, affiliation, "
        "       affiliation_ror, is_team, is_site_personnel, is_scholar, "
        "       source_url, person_id "
        "FROM person_registry WHERE tier = 'core' ORDER BY tier_rank").fetchdf()
    print(f"[validate] {len(reg):,} core-tier rows", flush=True)

    # A blocked author is never fetched for the row it is blocked on.
    pairs = bad_openalex_pairs()
    blocked = {r.canonical_id for r in reg.itertuples(index=False)
               if isinstance(r.openalex_id, str)
               and is_blocked(pairs, r.openalex_id, r.canonical_id,
                              r.person_id if isinstance(r.person_id, str) else None)}
    if blocked:
        print(f"[validate] {len(blocked)} row(s) carry a blocked openalex_id; "
              f"not fetched, recorded as fail", flush=True)
    ids = [short_id(r.openalex_id) for r in reg.itertuples(index=False)
           if isinstance(r.openalex_id, str) and r.openalex_id
           and r.canonical_id not in blocked]
    got: dict[str, dict] = {}
    batches = [ids[i:i + batch_size] for i in range(0, len(ids), batch_size)]
    for bi, batch in enumerate(batches):
        d = oa_get("authors", filter="openalex_id:" + "|".join(batch),
                   select="id,orcid,display_name,works_count,cited_by_count,"
                          "last_known_institutions",
                   **{"per-page": str(batch_size)})
        for a in (d.get("results") or []):
            got[short_id(a.get("id"))] = a
        if bi % 25 == 0:
            print(f"[validate] author batch {bi + 1}/{len(batches)}", flush=True)

    # Resolve each DISTINCT ror once -- far fewer RORs than researchers.
    rors = sorted({short_id(v) for v in reg.affiliation_ror
                   if isinstance(v, str) and v})
    ror_cache: dict[str, tuple[str, str, str]] = {}
    for i, rid in enumerate(rors):
        j = http_json(ROR + rid)
        ror_cache[rid] = parse_ror(j) if j else ("", "", "not_found")
        if i % 500 == 0:
            print(f"[validate] ror {i + 1}/{len(rors)}", flush=True)

    today = date.today().isoformat()
    rows = []
    for r in reg.itertuples(index=False):
        oa_id = short_id(r.openalex_id) if isinstance(r.openalex_id, str) else None
        rec = got.get(oa_id) if oa_id else None
        is_local = str(r.canonical_id).startswith("codp:")
        oa_url = f"{OA}authors/{oa_id}" if oa_id else None
        reg_url = r.source_url if isinstance(r.source_url, str) else None

        def emit(check_id, verdict, id_type, id_value, source, url, evidence,
                 mismatch=None):
            # Every row needs a citable source. Fall back through what was
            # actually consulted; a codp: row's source is the curated repo file.
            src = url or oa_url or reg_url or (
                "https://github.com/tyson-swetnam/cod-kmap/blob/main/"
                "db/parquet/people.parquet")
            conf = ("low" if is_local or verdict in ("unresolved",)
                    else "high" if verdict == "pass"
                    else "medium")
            rows.append(dict(
                validation_id=hashlib.sha1(
                    f"{r.canonical_id}|{check_id}|{run_id}".encode()
                ).hexdigest()[:24],
                canonical_id=r.canonical_id, check_id=check_id,
                subject_id_type=id_type, subject_id_value=id_value,
                verdict=verdict, http_status=None, evidence=evidence,
                mismatch_detail=mismatch, method="identifier-equality",
                source=source, source_url=src, retrieved_at=today,
                confidence=conf, run_id=run_id, notes=None))

        # 1. OpenAlex author id resolves.
        if r.canonical_id in blocked:
            emit("openalex-author-resolves", "fail", "openalex_id", oa_id,
                 "registry", oa_url,
                 f"openalex_id is blocked for this row in {BAD_PAIRS_CSV.name} "
                 f"(another person, or several merged); not fetched",
                 "run scripts/wipe_misattributed_identifiers.py")
        elif not oa_id:
            emit("openalex-author-resolves", "not_applicable", "none", None,
                 "registry", None, "no OpenAlex author id on the registry row")
        elif rec is None:
            emit("openalex-author-resolves", "fail", "openalex_id", oa_id,
                 "openalex", oa_url, "id returned no author record (stale or merged)")
        else:
            emit("openalex-author-resolves", "pass", "openalex_id", oa_id,
                 "openalex", oa_url, f"resolved to {rec.get('display_name')}")

        # 2. ORCID agreement. The id is the identity; a name mismatch never
        #    invalidates it, and a missing OpenAlex record means we could not
        #    check (unresolved) rather than that there was nothing to check.
        stored = bare_orcid(r.orcid)
        remote = bare_orcid(rec.get("orcid")) if rec else None
        if not stored and not remote:
            emit("orcid-resolves", "not_applicable", "none", None, "registry",
                 None, "no ORCID on either side")
        elif stored and not rec:
            emit("orcid-resolves", "unresolved", "orcid", stored, "registry",
                 f"https://orcid.org/{stored}",
                 "registry row has an ORCID but no OpenAlex record to compare")
        elif stored and remote and stored != remote:
            emit("orcid-resolves", "fail", "orcid", stored, "openalex", oa_url,
                 "stored ORCID disagrees with OpenAlex",
                 f"stored {stored} vs OpenAlex {remote}")
        elif stored and remote:
            emit("orcid-resolves", "pass", "orcid", stored, "openalex", oa_url,
                 "stored ORCID agrees with OpenAlex")
        else:
            emit("orcid-resolves", "unresolved", "orcid", stored or remote,
                 "openalex", oa_url, "ORCID present on only one side")

        # 3. ROR resolves and agrees.
        stored_ror = short_id(r.affiliation_ror) if isinstance(r.affiliation_ror, str) else None
        remote_ror = None
        if rec:
            insts = rec.get("last_known_institutions") or []
            remote_ror = short_id(insts[0].get("ror")) if insts and insts[0].get("ror") else None
        if not stored_ror:
            emit("ror-resolves", "not_applicable", "none", None, "registry",
                 None, "no affiliation ROR on the registry row")
        else:
            name, country, status = ror_cache.get(stored_ror, ("", "", "not_found"))
            ror_url = ROR + stored_ror
            if status == "not_found":
                emit("ror-resolves", "fail", "ror", stored_ror, "ror", ror_url,
                     "ROR id does not resolve")
            elif remote_ror and remote_ror != stored_ror:
                emit("ror-resolves", "fail", "ror", stored_ror, "ror", ror_url,
                     f"resolves to {name} ({status}) but OpenAlex reports a "
                     f"different institution",
                     f"stored {stored_ror} ({name}) vs OpenAlex {remote_ror}")
            else:
                # An inactive/withdrawn org still resolves and is historically
                # correct -- pass, but the status is carried for remapping.
                emit("ror-resolves", "pass", "ror", stored_ror, "ror", ror_url,
                     f"resolves to {name} [{country}] status={status or 'unknown'}")

        # 4. Name agreement -- reported, never used to break an identity.
        if not rec:
            emit("name-agreement", "not_applicable", "none", None, "registry",
                 None, "no OpenAlex record to compare a name against")
        else:
            a = (r.display_name or "").strip().casefold()
            b = (rec.get("display_name") or "").strip().casefold()
            if a and b and a == b:
                emit("name-agreement", "pass", "none", None, "openalex", oa_url,
                     "display names match exactly")
            elif a and b and a.split()[-1] == b.split()[-1]:
                emit("name-agreement", "pass", "none", None, "openalex", oa_url,
                     f"family name matches ('{r.display_name}' vs "
                     f"'{rec.get('display_name')}')")
            else:
                emit("name-agreement", "unresolved", "none", None, "openalex",
                     oa_url, f"names differ: '{r.display_name}' vs "
                             f"'{rec.get('display_name')}'")

    out = pd.DataFrame(rows)
    assert out.validation_id.is_unique, "validation_id must be unique (PK)"
    assert out.source_url.notna().all(), "every verdict needs a citable source"
    assert set(out.confidence) <= set(CONF), f"confidence must be in {CONF}"
    # Append this run (validation_id is per run_id); re-running the same
    # run_id updates its rows in place rather than duplicating them.
    with cod_db.transaction(con, f"validate_registry: validate {run_id} "
                                 f"({len(out):,} verdicts)"):
        _stage_df(con, out, "person_validation", "_validation_stage")
        st = cod_db.merge_upsert(con, "person_validation", ["validation_id"],
                                 "_validation_stage")
        cod_db.assert_integrity(con, ["person_validation"])
    print(f"[validate] wrote {len(out):,} verdicts "
          f"({st['inserted']:,} new, {st['matched']:,} replaced)", flush=True)
    print(out.groupby(["check_id", "verdict"]).size().to_string(), flush=True)
    return out


# --------------------------------------------------------------------------- #
# stage: match
# --------------------------------------------------------------------------- #

def stage_match(con, run_id: str) -> None:
    """Resolve harvested co-authors against the registry BY IDENTIFIER ONLY.

    The unmatched population is materialised FIRST, with no registry linkage
    asserted, so the candidate queue is auditable independently of the matching.

    Everything is set-based SQL over TEMP tables, written to the lake in one
    transaction. (The previous version pulled each step into pandas and
    re-registered the frame on the connection; DuckDB 2.0 refuses a
    register() while any execute() result is half-read, and at 3.4M pair
    rows the round trip was the slow part anyway.)
    """
    today = date.today().isoformat()
    # TEMP: a plain CREATE VIEW would land in the lake (the connection USEs
    # it) as a persistent view over a local checkpoint path.
    con.execute(f"CREATE OR REPLACE TEMP VIEW _pairs AS "
                f"SELECT * FROM {pairs_source(con)}")

    # Typed like db/parquet's coauthor_unmatched_population: counts BIGINT,
    # years DOUBLE (the pair files carry publication_year as DOUBLE).
    #
    # affiliation_ror is the co-author's most frequent ROR across the pairs,
    # ties to the smallest id. It was mode(coauthor_ror), which returns an
    # arbitrary member of a tie: 11,408 co-authors have one, and no tie-break
    # rule reproduces the committed picks, so a re-run reshuffled ~5,800
    # RORs (and, through ror_matches_facility, some candidate confidences).
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _ca AS
        WITH ror_n AS (
          SELECT coauthor_openalex_id, coauthor_ror, count(*) AS n
          FROM _pairs
          WHERE coauthor_openalex_id IS NOT NULL AND coauthor_ror IS NOT NULL
          GROUP BY ALL),
        top_ror AS (
          SELECT coauthor_openalex_id,
                 first(coauthor_ror ORDER BY n DESC, coauthor_ror) AS affiliation_ror
          FROM ror_n GROUP BY coauthor_openalex_id),
        agg AS (
          SELECT coauthor_openalex_id,
                 any_value(coauthor_display_name) AS display_name,
                 max(coauthor_orcid)              AS orcid,
                 count(DISTINCT focal_openalex_id) AS n_registry_coauthors,
                 count(DISTINCT work_id)          AS n_shared_works,
                 CAST(min(publication_year) AS DOUBLE) AS first_year,
                 CAST(max(publication_year) AS DOUBLE) AS last_year,
                 min(focal_openalex_id)           AS seen_with_openalex_id,
                 min(work_id)                     AS seen_on_work_id
          FROM _pairs WHERE coauthor_openalex_id IS NOT NULL
          GROUP BY coauthor_openalex_id)
        SELECT a.coauthor_openalex_id, a.display_name, a.orcid, t.affiliation_ror,
               a.n_registry_coauthors, a.n_shared_works, a.first_year, a.last_year,
               a.seen_with_openalex_id, a.seen_on_work_id
        FROM agg a LEFT JOIN top_ror t USING (coauthor_openalex_id)""")
    n_ca = cod_db.scalar(con, "SELECT count(*) FROM _ca")
    print(f"[match] {n_ca:,} distinct co-authors (no matching applied)",
          flush=True)

    # A registry row is never keyed on an openalex_id blocked for it.
    blocked = blocked_registry_keys(con)
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _resolved AS
        WITH r AS (
          SELECT canonical_id, tier,
                 CASE WHEN canonical_id IN (SELECT canonical_id FROM {blocked})
                      THEN ''
                      ELSE replace(coalesce(openalex_id,''),'https://openalex.org/','')
                 END AS oa,
                 lower(replace(coalesce(orcid,''),'https://orcid.org/','')) AS orc
          FROM person_registry)
        SELECT c.*,
               coalesce(a.canonical_id, b.canonical_id) AS matched_canonical_id,
               CASE WHEN a.canonical_id IS NOT NULL
                      THEN 'openalex-author-id-equality'
                    WHEN b.canonical_id IS NOT NULL THEN 'orcid-equality'
               END AS match_method,
               f.canonical_id AS seen_with_canonical_id
        FROM _ca c
        LEFT JOIN r a ON a.oa = c.coauthor_openalex_id AND a.oa <> ''
        LEFT JOIN r b ON b.orc = lower(c.orcid) AND b.orc <> ''
        LEFT JOIN r f ON f.oa = c.seen_with_openalex_id AND f.oa <> ''
    """)
    n_res, n_in = cod_db.rows(con, "SELECT count(*), count(matched_canonical_id) "
                                   "FROM _resolved")[0]
    print(f"[match] in_registry={n_in:,} "
          f"out_of_registry={n_res - n_in:,}", flush=True)
    for method, n in cod_db.rows(con, "SELECT match_method, count(*) FROM _resolved "
                                      "GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {method or 'NaN':<30} {n:>9,}", flush=True)

    # Edges: registry-to-registry only, each proved by a work.
    # Materialise the registry key map and PRE-FILTER the pair rows to those
    # whose BOTH endpoints are registry authors before joining. Joining the raw
    # 2.7M-row pair table against the registry twice and grouping in one shot
    # spilled 647 GiB of temp and died with OutOfMemoryError: the intermediate
    # is the full cross of every pair row against two key tables. Narrowing
    # first turns it into a small join over ~1% of the rows.
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _regkey AS
        SELECT canonical_id,
               replace(coalesce(openalex_id,''),'https://openalex.org/','') AS oa
        FROM person_registry
        WHERE openalex_id IS NOT NULL AND openalex_id <> ''
          AND canonical_id NOT IN (SELECT canonical_id FROM {blocked})""")
    con.execute("SET preserve_insertion_order = false")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _regpairs AS
        SELECT p.focal_openalex_id, p.coauthor_openalex_id,
               p.work_id, p.publication_year
        FROM _pairs p
        WHERE p.focal_openalex_id    IN (SELECT oa FROM _regkey)
          AND p.coauthor_openalex_id IN (SELECT oa FROM _regkey)""")
    n_rp = cod_db.scalar(con, "SELECT count(*) FROM _regpairs")
    print(f"[match] {n_rp:,} pair rows have both endpoints in the registry",
          flush=True)
    # edge_id = sha1(a|b)[:24] and the provenance columns, as the pandas
    # version computed them; DuckDB's sha1() is hashlib's lowercase hex.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _edges AS
        WITH e AS (
          SELECT least(ra.canonical_id, rb.canonical_id)    AS canonical_id_a,
                 greatest(ra.canonical_id, rb.canonical_id) AS canonical_id_b,
                 count(DISTINCT p.work_id) AS co_pub_count,
                 min(p.publication_year)   AS first_year,
                 max(p.publication_year)   AS last_year,
                 min(p.work_id)            AS exemplar_work_id
          FROM _regpairs p
          JOIN _regkey ra ON ra.oa = p.focal_openalex_id
          JOIN _regkey rb ON rb.oa = p.coauthor_openalex_id
          WHERE ra.canonical_id <> rb.canonical_id
          GROUP BY 1, 2)
        SELECT substr(sha1(canonical_id_a || '|' || canonical_id_b), 1, 24) AS edge_id,
               canonical_id_a, canonical_id_b, co_pub_count, first_year, last_year,
               CAST(co_pub_count AS DOUBLE)       AS weight,
               exemplar_work_id,
               CAST(NULL AS VARCHAR)              AS exemplar_work_doi,
               CAST(NULL AS INTEGER)              AS exemplar_work_year,
               CAST(NULL AS VARCHAR)              AS evidence_work_ids,
               co_pub_count > 1                   AS evidence_truncated,
               'openalex-author-id-equality'      AS match_method,
               CAST(NULL AS VARCHAR)              AS shared_areas,
               CAST(NULL AS VARCHAR)              AS shared_facilities,
               CAST(NULL AS BOOLEAN)              AS same_institution,
               'openalex'                         AS source,
               'https://api.openalex.org/works/' || exemplar_work_id AS source_url,
               CAST(? AS VARCHAR)                 AS retrieved_at,
               'high'                             AS confidence,
               CAST(? AS VARCHAR)                 AS run_id
        FROM e""", [today, run_id])
    if cod_db.scalar(con, "SELECT count(*) FROM _edges WHERE exemplar_work_id IS NULL"):
        raise AssertionError("every edge needs a proving work")

    # Candidates: out-of-registry co-authors with a registry member to cite
    # (provenance is mandatory). The score and the confidence bands are the
    # pandas version's: pd.cut(score, [-1, 10, 40, 1e12]) is right-closed.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _cand AS
        WITH fac AS (
          SELECT DISTINCT replace(coalesce(ror,''),'https://ror.org/','') AS ror
          FROM facilities WHERE ror IS NOT NULL AND ror <> ''),
        c AS (
          SELECT r.*,
                 coalesce(r.affiliation_ror IN (SELECT ror FROM fac), false)
                   AS ror_matches_facility
          FROM _resolved r
          WHERE r.matched_canonical_id IS NULL
            AND r.seen_with_canonical_id IS NOT NULL),
        s AS (
          SELECT *, n_registry_coauthors * 3.0
                  + n_shared_works * 0.5
                  + CAST(ror_matches_facility AS INTEGER) * 10
                  + CAST(orcid IS NOT NULL AS INTEGER) * 2 AS score
          FROM c)
        SELECT substr(sha1(coauthor_openalex_id || '|' || ?), 1, 24) AS candidate_id,
               display_name, orcid, coauthor_openalex_id AS openalex_id,
               CAST(NULL AS VARCHAR) AS affiliation, affiliation_ror,
               CAST(NULL AS VARCHAR) AS affiliation_country,
               seen_with_canonical_id, seen_on_work_id,
               n_registry_coauthors, n_shared_works,
               CAST(NULL AS INTEGER) AS works_count,
               CAST(NULL AS INTEGER) AS coastal_works_count,
               CAST(NULL AS DOUBLE)  AS coastal_share,
               CAST(NULL AS INTEGER) AS h_index,
               'pending' AS decision,
               CAST(NULL AS VARCHAR) AS decided_by,
               CAST(NULL AS VARCHAR) AS decided_at,
               CAST(NULL AS VARCHAR) AS ambiguity_note,
               'openalex' AS source,
               'https://api.openalex.org/authors/' || coauthor_openalex_id AS source_url,
               CAST(? AS VARCHAR) AS retrieved_at,
               CASE WHEN score <= 10 THEN 'low'
                    WHEN score <= 40 THEN 'medium'
                    ELSE 'high' END AS confidence,
               CAST(? AS VARCHAR) AS run_id,
               CAST(NULL AS VARCHAR) AS notes
        FROM s""", [run_id, today, run_id])

    n_e = cod_db.scalar(con, "SELECT count(*) FROM _edges")
    n_c = cod_db.scalar(con, "SELECT count(*) FROM _cand")
    with cod_db.transaction(con, f"validate_registry: match {run_id} "
                                 f"({n_e:,} edges, {n_c:,} candidates, "
                                 f"{n_ca:,} co-authors)"):
        n_pop = cod_db.replace_table(con, "coauthor_unmatched_population",
                                     "SELECT * FROM _ca")
        print(f"[match] coauthor_unmatched_population: {n_pop:,} row(s) (db-only)",
              flush=True)
        _write_conforming(con, "_edges", "coauthor_edges")
        _write_conforming(con, "_cand", "coauthor_candidates")
        cod_db.assert_integrity(con, ["coauthor_edges", "coauthor_candidates",
                                      "coauthor_unmatched_population"])
    print(f"[match] {n_e:,} edges, {n_c:,} candidates", flush=True)


def _write_conforming(con, stage: str, table: str) -> None:
    """Replace lake table ``table`` with the rows of TEMP table ``stage``.

    Column order and membership come from the lake table, so a schema change
    surfaces here as a KeyError rather than as a silently mis-ordered INSERT.

    This used to write db/parquet/<table>.parquet and stop, leaving the DB
    table as it was; export_parquet.py owns that file now and rewrites it
    from the lake, so the lake is where the rows have to land. A schema.sql
    table, so DELETE + INSERT (not replace_table) keeps its NOT NULL and
    DEFAULT definitions. Call inside cod_db.transaction().
    """
    cols = _table_columns(con, table)
    have = set(_table_columns(con, stage))
    missing = [c for c in cols if c not in have]
    if missing:
        raise KeyError(f"{table}: pipeline produced no value for {missing}")
    sel = ", ".join(cod_db.qident(c) for c in cols)
    con.execute(f"DELETE FROM {cod_db.qident(table)}")
    con.execute(f"INSERT INTO {cod_db.qident(table)} ({sel}) SELECT {sel} FROM {stage}")
    n = cod_db.scalar(con, f"SELECT count(*) FROM {cod_db.qident(table)}")
    print(f"[match] {table}: {n:,} row(s) loaded", flush=True)


# --------------------------------------------------------------------------- #
# stage: rdf  (OWL-RL sameAs closure)
# --------------------------------------------------------------------------- #

def stage_rdf(con, run_id: str, out_ttl: str = "db/derived/cod-registry.ttl"):
    """Serialize to Turtle and run OWL-RL closure.

    What the closure is FOR: cod:orcidId / cod:openAlexAuthorId / cod:rorId are
    owl:InverseFunctionalProperty, so sharing one derives owl:sameAs. Agreement
    between this and the identifier join is NOT independent validation -- both
    key on the same equality. The closure earns its place on SPLIT IDENTITIES:
    a co-author sharing an ORCID with a registry person while carrying a
    DIFFERENT OpenAlex author id (duplicate OpenAlex author records for one
    human). An OpenAlex-id join resolves none of those; transitive sameAs
    resolves all of them.
    """
    try:
        from rdflib import Graph, Literal, Namespace, URIRef
        from rdflib.namespace import FOAF, OWL, RDF, XSD
        import owlrl
    except ImportError:
        sys.exit("stage rdf needs rdflib and owlrl: pip install rdflib owlrl")

    COD = Namespace("https://tyson-swetnam.github.io/cod-kmap/ns/cod#")
    CODID = Namespace("https://tyson-swetnam.github.io/cod-kmap/id/")
    today = date.today().isoformat()

    reg = con.execute(
        "SELECT r.canonical_id, r.display_name, r.orcid, r.openalex_id, "
        "       r.affiliation_ror, v.confidence, v.source_url "
        "FROM person_registry r "
        "LEFT JOIN (SELECT canonical_id, any_value(confidence) confidence, "
        "                  any_value(source_url) source_url "
        "           FROM person_validation GROUP BY canonical_id) v "
        "  USING (canonical_id) "
        "WHERE r.tier = 'core'").fetchdf()

    g = Graph()
    g.bind("cod", COD); g.bind("codid", CODID); g.bind("foaf", FOAF)

    def p_iri(cid):
        return CODID[f"person/{str(cid).replace(':', '_')}"]

    def c_iri(oa):
        return CODID[f"coauthor/{oa}"]

    for r in reg.itertuples(index=False):
        s = p_iri(r.canonical_id)
        g.add((s, RDF.type, COD.RegistryPerson))
        g.add((s, COD.canonicalId, Literal(r.canonical_id)))
        if isinstance(r.display_name, str):
            g.add((s, FOAF.name, Literal(r.display_name)))
        b = bare_orcid(r.orcid)
        if b:
            g.add((s, COD.orcidId, Literal(b)))
        if isinstance(r.openalex_id, str) and r.openalex_id:
            g.add((s, COD.openAlexAuthorId, Literal(short_id(r.openalex_id))))
        if isinstance(r.affiliation_ror, str) and r.affiliation_ror:
            g.add((s, COD.rorId, Literal(short_id(r.affiliation_ror))))
        conf = r.confidence if r.confidence in CONF else "low"
        g.add((s, COD.confidence, Literal(conf)))
        if isinstance(r.source_url, str) and r.source_url:
            g.add((s, COD.sourceUrl, URIRef(r.source_url)))
        # A source_url without a retrieval date is not reproducible provenance,
        # and cod-shapes.ttl rejects it.
        g.add((s, COD.retrievedAt, Literal(today, datatype=XSD.date)))

    cand = con.execute(
        "SELECT openalex_id, display_name, orcid, affiliation_ror, confidence, "
        "       source_url FROM coauthor_candidates "
        "WHERE confidence IN ('high','medium')").fetchdf()
    for r in cand.itertuples(index=False):
        s = c_iri(r.openalex_id)
        g.add((s, RDF.type, COD.CoauthorCandidate))
        g.add((s, COD.openAlexAuthorId, Literal(r.openalex_id)))
        if isinstance(r.display_name, str):
            g.add((s, FOAF.name, Literal(r.display_name)))
        b = bare_orcid(r.orcid)
        if b:
            g.add((s, COD.orcidId, Literal(b)))
        if isinstance(r.affiliation_ror, str) and r.affiliation_ror:
            g.add((s, COD.rorId, Literal(short_id(r.affiliation_ror))))
        g.add((s, COD.confidence,
               Literal(r.confidence if r.confidence in CONF else "low")))
        if isinstance(r.source_url, str) and r.source_url:
            g.add((s, COD.sourceUrl, URIRef(r.source_url)))
        g.add((s, COD.retrievedAt, Literal(today, datatype=XSD.date)))

    os.makedirs(os.path.dirname(out_ttl), exist_ok=True)
    g.serialize(out_ttl, format="turtle")
    print(f"[rdf] {len(g):,} triples -> {out_ttl}", flush=True)

    # Closure on the identity subgraph only. The full graph (names, RORs,
    # provenance literals) makes OWL-RL quadratic and it timed out at ~95k
    # triples; identity properties are all the closure needs.
    gi = Graph()
    for s, p, o in g:
        if p in (COD.orcidId, COD.openAlexAuthorId, COD.canonicalId, RDF.type):
            gi.add((s, p, o))
    gi.parse(ONTOLOGY, format="turtle")
    t0 = time.time()
    owlrl.DeductiveClosure(owlrl.OWLRL_Semantics, axiomatic_triples=False,
                           datatype_axioms=False).expand(gi)
    bridges = [(s, o) for s, _, o in gi.triples((None, OWL.sameAs, None))
               if s != o and "/person/" in str(s) and "/coauthor/" in str(o)]
    print(f"[rdf] closure {time.time() - t0:.1f}s, "
          f"{len(bridges):,} person<->coauthor bridges", flush=True)

    n = split_identities(con, run_id, today)
    print(f"[rdf] {n:,} split identities "
          f"(shared ORCID, different OpenAlex id)", flush=True)
    return out_ttl


def split_identities(con, run_id: str, today: str) -> int:
    """Split identities: shared ORCID, different OpenAlex author id.

    Replaces the db-only split_identity_findings table in one snapshot, in
    the column shape db/parquet holds (and cod_db.DERIVED_CONSTRAINTS keys
    on: canonical_id, coauthor_oa). The old code wrote a parquet file with
    different column names (registry_openalex_id / duplicate_openalex_id)
    that matched neither the committed file nor its key; the committed
    176 rows came from the same ORCID-equality join with this wording.
    """
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE _split AS
        WITH r AS (
          SELECT canonical_id, display_name,
                 replace(coalesce(openalex_id,''),'https://openalex.org/','') AS oa,
                 lower(replace(coalesce(orcid,''),'https://orcid.org/','')) AS orc
          FROM person_registry WHERE tier='core'),
        c AS (
          SELECT coauthor_openalex_id AS oa2, lower(coauthor_orcid) AS orc2
          FROM {pairs_source(con)}
          WHERE coauthor_orcid IS NOT NULL GROUP BY 1,2)
        SELECT r.canonical_id, r.display_name, r.oa AS reg_oa,
               r.orc AS shared_orcid, c.oa2 AS coauthor_oa,
               'duplicate-openalex-author-record' AS finding,
               'registry person and co-author node share ORCID ' || r.orc
                 || ' but carry different OpenAlex author ids (' || r.oa
                 || ' vs ' || c.oa2 || ')' AS evidence,
               'owl:sameAs closure over cod:orcidId (InverseFunctionalProperty) '
                 || '— unifies the two differing OpenAlex author ids into one '
                 || 'identity' AS resolved_by,
               'openalex-author-id-equality join (resolves 0 of these by '
                 || 'construction: the two OpenAlex ids differ)' AS would_be_missed_by,
               '{OA}authors/' || c.oa2 AS source_url,
               'high' AS confidence,
               CAST(? AS VARCHAR) AS retrieved_at,
               'SQL ORCID-equality join over harvested co-author pairs' AS enumerated_by
        FROM r JOIN c ON c.orc2 = r.orc
        WHERE r.orc <> '' AND r.oa <> '' AND c.oa2 <> r.oa""", [today])
    n = cod_db.scalar(con, "SELECT count(*) FROM _split")
    with cod_db.transaction(con, f"validate_registry: rdf {run_id} "
                                 f"({n:,} split identities)"):
        cod_db.replace_table(con, "split_identity_findings", "SELECT * FROM _split")
        cod_db.assert_integrity(con, ["split_identity_findings"])
    return n


def stage_shacl(ttl: str = "db/derived/cod-registry.ttl") -> bool:
    try:
        from rdflib import Graph
        import pyshacl
    except ImportError:
        sys.exit("stage shacl needs pyshacl: pip install pyshacl rdflib")
    data = Graph().parse(ttl, format="turtle")
    shapes = Graph().parse(SHAPES, format="turtle")
    conforms, _, text = pyshacl.validate(data, shacl_graph=shapes,
                                         inference="none", advanced=True)
    n = text.count("Constraint Violation")
    print(f"[shacl] conforms={conforms} violations={n}", flush=True)
    os.makedirs("db/derived", exist_ok=True)
    with open("db/derived/shacl_report.md", "w") as f:
        f.write(
            "# SHACL conformance\n\n"
            f"- graph: `{ttl}`\n"
            f"- shapes: `{SHAPES}`\n"
            f"- conforms: **{conforms}**\n"
            f"- violations: **{n}**\n\n"
            "```\n" + text[:20000] + "\n```\n")
    return bool(conforms)


# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", action="append", required=True,
                    choices=["validate", "harvest", "match", "rdf", "shacl"])
    cod_db.add_argparse_catalog(ap)
    ap.add_argument("--db", dest="catalog", type=Path,
                    help="deprecated alias of --catalog")
    ap.add_argument("--request-budget", type=int, default=2000,
                    help="hard cap on OpenAlex requests this run (quota is "
                         "10,000/day, one credit per request, shared)")
    ap.add_argument("--quota-floor", type=int, default=800,
                    help="stop if the key's remaining quota falls below this")
    ap.add_argument("--run-id", default=None)
    a = ap.parse_args()

    run_id = a.run_id or "val-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    writes = bool({"validate", "match", "rdf"} & set(a.stage))
    try:
        con = cod_db.connect(read_only=not writes,
                             catalog=a.catalog, data=a.lake_data)
    except cod_db.LakeNotFound as e:
        print(f"[error] {e}", file=sys.stderr)
        return 2
    print(f"[run] {run_id} lake={cod_db.catalog_path(a.catalog)} "
          f"stages={a.stage}", flush=True)

    if {"validate", "harvest"} & set(a.stage):
        q = quota_remaining()
        if q is not None:
            print(f"[quota] {q:,} requests remaining", flush=True)
            if q < a.quota_floor:
                sys.exit(f"quota {q} is below floor {a.quota_floor}; "
                         "wait for the daily reset")

    if "validate" in a.stage:
        stage_validate(con, run_id)
    if "harvest" in a.stage:
        print("[harvest] see scripts/harvest_runner.py -- resumable, "
              "batches 50 authors per cursor, honours --request-budget",
              flush=True)
    if "match" in a.stage:
        stage_match(con, run_id)
    if "rdf" in a.stage:
        stage_rdf(con, run_id)
    if "shacl" in a.stage:
        if not stage_shacl():
            print("[shacl] graph does not conform -- see "
                  "db/derived/shacl_report.md", flush=True)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
