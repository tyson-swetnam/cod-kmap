#!/usr/bin/env python3
"""Validate person_registry identifiers and build the provenanced co-author graph.

Pipeline stages (each independently runnable via --stage):

  validate   For every core-tier registry row, resolve its OpenAlex author id
             (batched 50 per request), compare the returned ORCID and display
             name, and resolve its affiliation ROR against api.ror.org. Writes
             person_validation, long form, one row per (canonical_id, check_id,
             run_id) so re-validation appends and runs can be diffed.
  harvest    Page each researcher's coastal-topic works and emit co-author pairs.
  match      Resolve harvested co-authors against the registry BY IDENTIFIER
             ONLY, then rank the non-matching remainder as expansion candidates.
  rdf        Serialize the registry + candidates to Turtle, run OWL-RL sameAs
             closure, and report split identities the relational join misses.
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
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

import duckdb
import pandas as pd

DB = "db/cod_kmap.duckdb"
PARQUET_DIR = "db/parquet"
PUBLIC_DIR = "public/parquet"
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
        "       source_url "
        "FROM person_registry WHERE tier = 'core' ORDER BY tier_rank").fetchdf()
    print(f"[validate] {len(reg):,} core-tier rows", flush=True)

    ids = [short_id(v) for v in reg.openalex_id if isinstance(v, str) and v]
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
        if not oa_id:
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
    os.makedirs(PARQUET_DIR, exist_ok=True)
    out.to_parquet(f"{PARQUET_DIR}/person_validation.parquet", index=False)
    print(f"[validate] wrote {len(out):,} verdicts", flush=True)
    print(out.groupby(["check_id", "verdict"]).size().to_string(), flush=True)
    return out


# --------------------------------------------------------------------------- #
# stage: match
# --------------------------------------------------------------------------- #

def stage_match(con, run_id: str) -> None:
    """Resolve harvested co-authors against the registry BY IDENTIFIER ONLY.

    The unmatched population is materialised FIRST, with no registry linkage
    asserted, so the candidate queue is auditable independently of the matching.
    """
    today = date.today().isoformat()
    con.execute(f"CREATE OR REPLACE VIEW _pairs AS "
                f"SELECT * FROM '{PAIRS_DIR}/*.parquet'")

    ca = con.execute("""
        SELECT coauthor_openalex_id,
               any_value(coauthor_display_name) AS display_name,
               max(coauthor_orcid)              AS orcid,
               mode(coauthor_ror)               AS affiliation_ror,
               count(DISTINCT focal_openalex_id) AS n_registry_coauthors,
               count(DISTINCT work_id)          AS n_shared_works,
               min(publication_year)            AS first_year,
               max(publication_year)            AS last_year,
               min(focal_openalex_id)           AS seen_with_openalex_id,
               min(work_id)                     AS seen_on_work_id
        FROM _pairs WHERE coauthor_openalex_id IS NOT NULL
        GROUP BY coauthor_openalex_id""").fetchdf()
    print(f"[match] {len(ca):,} distinct co-authors (no matching applied)",
          flush=True)

    con.register("_ca", ca)
    resolved = con.execute("""
        WITH r AS (
          SELECT canonical_id, tier,
                 replace(coalesce(openalex_id,''),'https://openalex.org/','') AS oa,
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
    """).fetchdf()

    n_in = int(resolved.matched_canonical_id.notna().sum())
    print(f"[match] in_registry={n_in:,} "
          f"out_of_registry={len(resolved) - n_in:,}", flush=True)
    print(resolved.match_method.value_counts(dropna=False).to_string(), flush=True)

    # Edges: registry-to-registry only, each proved by a work.
    # Materialise the registry key map and PRE-FILTER the pair rows to those
    # whose BOTH endpoints are registry authors before joining. Joining the raw
    # 2.7M-row pair table against the registry twice and grouping in one shot
    # spilled 647 GiB of temp and died with OutOfMemoryError: the intermediate
    # is the full cross of every pair row against two key tables. Narrowing
    # first turns it into a small join over ~1% of the rows.
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _regkey AS
        SELECT canonical_id,
               replace(coalesce(openalex_id,''),'https://openalex.org/','') AS oa
        FROM person_registry
        WHERE openalex_id IS NOT NULL AND openalex_id <> ''""")
    con.execute("SET preserve_insertion_order = false")
    con.execute("""
        CREATE OR REPLACE TEMP TABLE _regpairs AS
        SELECT p.focal_openalex_id, p.coauthor_openalex_id,
               p.work_id, p.publication_year
        FROM _pairs p
        WHERE p.focal_openalex_id    IN (SELECT oa FROM _regkey)
          AND p.coauthor_openalex_id IN (SELECT oa FROM _regkey)""")
    n_rp = con.execute("SELECT count(*) FROM _regpairs").fetchone()[0]
    print(f"[match] {n_rp:,} pair rows have both endpoints in the registry",
          flush=True)
    edges = con.execute("""
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
        GROUP BY 1, 2""").fetchdf()
    edges["edge_id"] = [
        hashlib.sha1(f"{a}|{b}".encode()).hexdigest()[:24]
        for a, b in zip(edges.canonical_id_a, edges.canonical_id_b)]
    edges["weight"] = edges.co_pub_count.astype(float)
    edges["match_method"] = "openalex-author-id-equality"
    edges["evidence_work_ids"] = None
    edges["evidence_truncated"] = edges.co_pub_count > 1
    edges["exemplar_work_doi"] = None
    edges["exemplar_work_year"] = None
    edges["shared_areas"] = None
    edges["shared_facilities"] = None
    edges["same_institution"] = None
    edges["source"] = "openalex"
    edges["source_url"] = OA + "works/" + edges.exemplar_work_id
    edges["retrieved_at"] = today
    edges["confidence"] = "high"
    edges["run_id"] = run_id
    assert edges.exemplar_work_id.notna().all(), "every edge needs a proving work"

    cand = resolved[resolved.matched_canonical_id.isna()].copy()
    cand = cand[cand.seen_with_canonical_id.notna()]   # provenance is mandatory
    fac = set(con.execute(
        "SELECT replace(coalesce(ror,''),'https://ror.org/','') FROM facilities "
        "WHERE ror IS NOT NULL AND ror <> ''").fetchdf().iloc[:, 0])
    cand["ror_matches_facility"] = cand.affiliation_ror.isin(fac)
    cand["score"] = (cand.n_registry_coauthors * 3.0
                     + cand.n_shared_works * 0.5
                     + cand.ror_matches_facility.astype(int) * 10
                     + cand.orcid.notna().astype(int) * 2)
    cand["confidence"] = pd.cut(cand.score, [-1, 10, 40, 1e12],
                                labels=["low", "medium", "high"]).astype(str)
    cand["candidate_id"] = [
        hashlib.sha1(f"{o}|{run_id}".encode()).hexdigest()[:24]
        for o in cand.coauthor_openalex_id]
    cand = cand.rename(columns={"coauthor_openalex_id": "openalex_id"})
    for c in ("affiliation", "affiliation_country", "works_count",
              "coastal_works_count", "coastal_share", "h_index",
              "decided_by", "decided_at", "ambiguity_note", "notes"):
        cand[c] = None
    cand["decision"] = "pending"
    cand["source"] = "openalex"
    cand["source_url"] = OA + "authors/" + cand.openalex_id
    cand["retrieved_at"] = today
    cand["run_id"] = run_id

    os.makedirs(PARQUET_DIR, exist_ok=True)
    ca.to_parquet(f"{PARQUET_DIR}/coauthor_unmatched_population.parquet",
                  index=False)
    _write_conforming(con, edges, "coauthor_edges")
    _write_conforming(con, cand, "coauthor_candidates")
    print(f"[match] {len(edges):,} edges, {len(cand):,} candidates", flush=True)


def _write_conforming(con, df: pd.DataFrame, table: str) -> None:
    """Project df onto the committed table schema, then write parquet.

    Column order and membership come from the DB, so a schema change surfaces
    here as a KeyError rather than as a silently mis-ordered INSERT.
    """
    cols = [r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()]
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise KeyError(f"{table}: pipeline produced no value for {missing}")
    df[cols].to_parquet(f"{PARQUET_DIR}/{table}.parquet", index=False)


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

    # Split identities: shared ORCID, different OpenAlex author id.
    split = con.execute("""
        WITH r AS (
          SELECT canonical_id, display_name,
                 replace(coalesce(openalex_id,''),'https://openalex.org/','') AS oa,
                 lower(replace(coalesce(orcid,''),'https://orcid.org/','')) AS orc
          FROM person_registry WHERE tier='core'),
        c AS (
          SELECT coauthor_openalex_id AS oa2, lower(coauthor_orcid) AS orc2
          FROM (SELECT * FROM '""" + PAIRS_DIR + """/*.parquet')
          WHERE coauthor_orcid IS NOT NULL GROUP BY 1,2)
        SELECT r.canonical_id, r.display_name, r.oa AS registry_openalex_id,
               c.oa2 AS duplicate_openalex_id, r.orc AS shared_orcid
        FROM r JOIN c ON c.orc2 = r.orc
        WHERE r.orc <> '' AND r.oa <> '' AND c.oa2 <> r.oa""").fetchdf()
    if len(split):
        split["finding"] = "duplicate-openalex-author-record"
        split["resolved_by"] = "owl:sameAs closure over cod:orcidId"
        split["would_be_missed_by"] = "openalex-author-id-equality join"
        split["source_url"] = OA + "authors/" + split.duplicate_openalex_id
        split["confidence"] = "high"
        split["retrieved_at"] = today
        split.to_parquet(f"{PARQUET_DIR}/split_identity_findings.parquet",
                         index=False)
    print(f"[rdf] {len(split):,} split identities "
          f"(shared ORCID, different OpenAlex id)", flush=True)
    return out_ttl


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
    ap.add_argument("--db", default=DB)
    ap.add_argument("--request-budget", type=int, default=2000,
                    help="hard cap on OpenAlex requests this run (quota is "
                         "10,000/day, one credit per request, shared)")
    ap.add_argument("--quota-floor", type=int, default=800,
                    help="stop if the key's remaining quota falls below this")
    ap.add_argument("--run-id", default=None)
    a = ap.parse_args()

    run_id = a.run_id or "val-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    con = duckdb.connect(a.db)
    print(f"[run] {run_id} db={a.db} stages={a.stage}", flush=True)

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
