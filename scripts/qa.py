"""Data-quality assertions run after ingest.

Exits non-zero on any failure so CI workflows can gate deploys.

The store is a DuckLake (scripts/cod_db.py), opened read-only. DuckLake
cannot hold PRIMARY KEY / UNIQUE / FOREIGN KEY / CHECK constraints, so the
first database gate, check_integrity, re-enforces every constraint declared
in schema/schema.sql (duplicate keys, orphaned references, failed CHECKs,
NULLs in NOT NULL columns) against the lake. That gate is what used to be
the database's own job; a loader that writes a duplicate or an orphan now
fails here instead of at INSERT time. The same gate covers the derived
tables' keys (cod_db.DERIVED_CONSTRAINTS), the soft references schema.sql
does not declare (cod_db.SOFT_REFERENCES — the COD team, scholar, dataset
and registry back-references, which this file used to check by hand) and
column drift between the lake and schema.sql.

Two checks read source files rather than the database and run even on a
checkout with no data:

  * check_frontend_parses — every src/**/*.js must parse as an ES module
    (node vm.SourceTextModule; skipped with a warning when node is absent).
  * check_view_sql_types — no SQL template literal in src/views/*.js may
    return DECIMAL/HUGEINT to the browser.

Warnings (printed, not failures) flag known debt that needs a data change
to clear.

Invariants on the CSV/JSON-seeded tables (COD team, scholars, datasets,
registry) must be gated on the table being non-empty: refresh-data.yml
runs ingest.py, which leaves those tables empty in the CI lake.

Usage::

    python scripts/qa.py
    python scripts/qa.py --catalog /tmp/scratch/cat.sqlite   # or $COD_LAKE_CATALOG
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cod_db  # noqa: E402

# Territories that fall outside their country's main bounding box. Guam
# and the Northern Marianas sit at ~+145 EAST longitude and Palmyra at
# ~6 N, none of which a single US rectangle can express, so they were
# being reported as misplaced. Checked against these boxes before the
# country box is applied.
EXTRA_BBOXES = {
    "US": [
        (13.0, 21.0, 144.0, 146.5),      # Guam + Northern Mariana Islands
        (-15.0, 0.0, -172.0, -168.0),    # American Samoa
        (5.0, 7.0, -163.0, -161.0),      # Palmyra Atoll / Line Islands
        (18.0, 29.0, -178.5, -154.0),    # Hawaii + NW Hawaiian Islands
    ],
}

BBOX_BY_COUNTRY = {
    # (min_lat, max_lat, min_lng, max_lng) — generous continental boxes
    "US": (17.0, 72.0, -180.0, -64.0),
    "CA": (41.0, 84.0, -142.0, -52.0),
    "MX": (14.0, 33.0, -118.0, -86.0),
    "CU": (19.5, 23.5, -85.5, -74.0),
    "JM": (17.5, 18.7, -78.5, -76.0),
    "BS": (20.5, 27.5, -79.5, -72.5),
    "DO": (17.5, 20.0, -72.0, -68.0),
    "HT": (17.5, 20.0, -74.5, -71.5),
    "PR": (17.8, 18.6, -67.3, -65.2),
    "VI": (17.6, 18.5, -65.1, -64.5),
    "CO": (-4.3, 13.0, -81.8, -66.8),
    "BR": (-34.0, 5.3, -74.0, -28.6),
    "AR": (-55.2, -21.8, -73.6, -53.6),
    "CL": (-56.0, -17.5, -75.7, -66.4),
    "PE": (-18.4, -0.1, -81.4, -68.6),
    "EC": (-5.1, 1.7, -92.1, -75.2),
    "UY": (-35.0, -30.0, -58.5, -53.0),
    "VE": (0.6, 12.3, -73.4, -59.8),
    "PA": (7.2, 9.7, -83.0, -77.2),
    "CR": (8.0, 11.3, -86.0, -82.5),
    "GT": (13.7, 17.9, -92.3, -88.2),
    "BZ": (15.9, 18.5, -89.3, -87.3),
    "HN": (12.9, 16.6, -89.4, -83.1),
    "NI": (10.7, 15.1, -87.7, -82.6),
    "SV": (12.9, 14.5, -90.2, -87.6),
    "BB": (13.0, 13.4, -60.0, -59.3),
    "TT": (10.0, 11.5, -62.0, -60.4),
    "KY": (19.2, 19.9, -81.5, -79.7),
    "TC": (20.9, 22.0, -72.5, -71.0),
}


def assert_true(cond: bool, msg: str, failures: list[str]) -> None:
    if not cond:
        failures.append(msg)


# Vocabularies for the COD team / scholars / dataset-catalogue checks.
# Duplicated from the loader scripts on purpose: the point of a gate is to
# fail when the loader and the expectation drift apart, which a shared
# constant would hide.
COD_INSTITUTIONS = {
    "clemson", "yale", "unm", "battelle", "vcu", "pnnl", "unl", "obfs",
    "uga", "arizona", "delaware", "usc", "uidaho", "montana-state",
    "alabama", "florida", "coastal-carolina", "charleston",
    "other-university", "agency", "company", "various",
}
DATASET_CATEGORIES = {
    "observing-system", "monitoring-program", "data-portal", "remote-sensing",
    "synthesis-network", "model-output", "archive", "mapping-product",
}
ENDPOINT_TYPES = {
    "erddap", "thredds", "opendap", "ogc-wms", "ogc-wfs", "ogc-api",
    "rest-api", "s3", "ftp", "portal", "doi", "stac",
}
# How a dataset → facility edge was established. Kept in sync with
# METHOD_CONFIDENCE in scripts/link_dataset_facilities.py.
DATASET_FACILITY_METHODS = {"canonical-name", "acronym", "network-id"}
# Columns each new table must still have. Catches the drift that already
# bit the people tables, where schema.sql and init_people_tables.py
# disagreed about v_person_areas_enriched.
EXPECTED_COLUMNS = {
    "cod_wbs": {"wbs_code", "parent_code", "title", "lead_person_id", "sort_order"},
    "cod_team_members": {
        "member_id", "person_id", "display_name", "wbs_code", "role",
        "institution_slug", "is_pi", "is_copi", "is_leadership_committee", "status",
    },
    "community_scholars": {
        "scholar_id", "person_id", "name", "orcid", "openalex_id", "h_index",
        "coastal_works_count", "is_preeminent", "is_most_active", "is_rising",
        "rank_preeminent", "rank_most_active", "rank_rising", "source",
    },
    "coastal_datasets": {
        "dataset_id", "name", "provider", "category", "network_id",
        "parent_dataset_id", "homepage_url",
    },
    "dataset_endpoints": {"dataset_id", "endpoint_type", "url", "auth_required"},
    "dataset_facilities": {
        "dataset_id", "facility_id", "role", "method", "confidence",
        "evidence", "source", "source_url",
    },
    "person_registry": {
        "canonical_id", "display_name", "orcid", "openalex_id",
        "is_team", "is_site_personnel", "is_scholar",
        "person_id", "scholar_id", "tier", "tier_rank",
        "source_url", "confidence",
    },
    "person_identity_source": {
        "canonical_id", "field", "value", "method", "confidence",
    },
    "registry_collaborations": {
        "canonical_id_a", "canonical_id_b", "co_pub_count",
        "first_year", "last_year",
    },
    # Identifier-validation verdicts. Long form — one row per
    # (canonical_id, check_id, run_id) — so a re-validation appends and two
    # runs can be diffed rather than one silently overwriting the other.
    "person_validation": {
        "validation_id", "canonical_id", "check_id", "verdict",
        "method", "source_url", "retrieved_at", "confidence", "run_id",
    },
    # Provenanced co-authorship edges: exemplar_work_id is the work that
    # proves the edge, match_method the identifier rule that matched it.
    "coauthor_edges": {
        "edge_id", "canonical_id_a", "canonical_id_b", "co_pub_count",
        "exemplar_work_id", "match_method", "source_url", "retrieved_at",
        "confidence", "run_id",
    },
    # Co-authors not yet in the registry. A staging queue, not personnel
    # records: nothing here is a registry row until a curator promotes it.
    "coauthor_candidates": {
        "candidate_id", "display_name", "openalex_id",
        "seen_with_canonical_id", "seen_on_work_id", "n_registry_coauthors",
        "decision", "source_url", "retrieved_at", "confidence", "run_id",
    },
}


def table_rows(conn, table: str) -> int:
    """Row count, or -1 when the table isn't in this database.

    The weekly refresh workflow rebuilds a DB from ingest.py alone, where
    every table below is empty (their data lives in committed parquet that
    ingest never touches). Gating on this keeps that run green instead of
    failing on absent data it was never asked to produce.
    """
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    except duckdb.Error:
        return -1


def check_tables_present(conn, failures: list[str]) -> None:
    """If any of the new tables has rows, they must all exist.

    Every check below no-ops on an empty table so the ingest-only CI stays
    green, which means an accidentally *dropped* table looked exactly like an
    empty one and silenced its invariants. Once one of the group is populated
    we know this is a full database, so a missing sibling is a real fault.
    """
    counts = {t: table_rows(conn, t) for t in EXPECTED_COLUMNS}
    if not any(n > 0 for n in counts.values()):
        return
    absent = sorted(t for t, n in counts.items() if n < 0)
    assert_true(not absent,
                f"table(s) missing from a populated database: {absent}", failures)


def check_columns(conn, failures: list[str]) -> None:
    for table, expected in EXPECTED_COLUMNS.items():
        try:
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info('{table}')").fetchall()}
        except duckdb.Error:
            continue          # table absent — the row-count gates report it
        if not cols:
            continue
        missing = expected - cols
        assert_true(not missing, f"{table} missing column(s): {sorted(missing)}", failures)


def check_cod_team(conn, failures: list[str]) -> None:
    if table_rows(conn, "cod_team_members") <= 0:
        return

    pis = conn.execute(
        "SELECT COUNT(DISTINCT display_name) FROM cod_team_members WHERE is_pi").fetchone()[0]
    assert_true(pis == 1, f"expected exactly 1 COD PI, found {pis}", failures)

    copis = conn.execute(
        "SELECT COUNT(DISTINCT display_name) FROM cod_team_members WHERE is_copi").fetchone()[0]
    assert_true(copis >= 3, f"expected at least 3 COD Co-PIs, found {copis}", failures)

    slc = conn.execute(
        "SELECT COUNT(DISTINCT display_name) FROM cod_team_members "
        "WHERE is_leadership_committee").fetchone()[0]
    assert_true(slc >= 10,
                f"expected at least 10 Science Leadership Committee members, found {slc}",
                failures)

    # A named member must resolve to a people row, or the Team tab shows a
    # person with no profile links and the enrichment scripts skip them.
    # (Stricter than the person_id soft reference: an active row with NO
    # person_id fails here too.)
    orphans = conn.execute(
        "SELECT COUNT(*) FROM cod_team_members tm "
        "LEFT JOIN people p ON p.person_id = tm.person_id "
        "WHERE tm.status = 'active' AND (tm.person_id IS NULL OR p.person_id IS NULL)"
    ).fetchone()[0]
    assert_true(orphans == 0,
                f"{orphans} active COD team rows without a matching people row", failures)

    # Unfilled positions and group-staffed work must NOT carry a person: a
    # staffing pool stored as a human would be enriched as one.
    ghosts = conn.execute(
        "SELECT COUNT(*) FROM cod_team_members "
        "WHERE status <> 'active' AND person_id IS NOT NULL").fetchone()[0]
    assert_true(ghosts == 0,
                f"{ghosts} unfilled or group-staffed COD rows carry a person_id", failures)

    bad_status = conn.execute(
        "SELECT COUNT(*) FROM cod_team_members "
        "WHERE status NOT IN ('active','tbd','tbh','collective')").fetchone()[0]
    assert_true(bad_status == 0, f"{bad_status} COD team rows with an unknown status", failures)

    slugs = conn.execute(
        "SELECT DISTINCT institution_slug FROM cod_team_members "
        "WHERE institution_slug IS NOT NULL").fetchall()
    unknown = sorted({s[0] for s in slugs} - COD_INSTITUTIONS)
    assert_true(not unknown, f"COD team institution_slug not in vocab: {unknown}", failures)
    # cod_team_members.wbs_code -> cod_wbs and cod_wbs.parent_code -> cod_wbs
    # are cod_db.SOFT_REFERENCES, checked by check_integrity.


def check_scholars(conn, failures: list[str]) -> None:
    if table_rows(conn, "community_scholars") <= 0:
        return

    no_cohort = conn.execute(
        "SELECT COUNT(*) FROM community_scholars "
        "WHERE NOT (is_preeminent OR is_most_active OR is_rising)").fetchone()[0]
    assert_true(no_cohort == 0,
                f"{no_cohort} scholars carry no cohort flag", failures)

    # A rank must never outlive its flag. The converse is allowed: a curated
    # row has no measured metric to rank on, and inventing an order (the
    # roster once ranked alphabetically) renders in the UI as "#1" and reads
    # as a finding rather than an artifact. Unranked-but-flagged is honest.
    for flag, rank in (("is_preeminent", "rank_preeminent"),
                       ("is_most_active", "rank_most_active"),
                       ("is_rising", "rank_rising")):
        stray = conn.execute(
            f"SELECT COUNT(*) FROM community_scholars "
            f"WHERE {rank} IS NOT NULL AND NOT {flag}").fetchone()[0]
        assert_true(stray == 0,
                    f"{stray} scholars carry {rank} without {flag}", failures)
        dupes = conn.execute(
            f"SELECT COUNT(*) FROM (SELECT {rank} FROM community_scholars "
            f"WHERE {rank} IS NOT NULL GROUP BY {rank} HAVING COUNT(*) > 1)").fetchone()[0]
        assert_true(dupes == 0, f"{dupes} duplicate {rank} values", failures)
        # Every measured row in a cohort must be ranked, or the tab cannot
        # order it.
        unranked = conn.execute(
            f"SELECT COUNT(*) FROM community_scholars "
            f"WHERE {flag} AND {rank} IS NULL AND h_index IS NOT NULL").fetchone()[0]
        assert_true(unranked == 0,
                    f"{unranked} measured scholars in {flag} have no {rank}", failures)

    bad_orcid = conn.execute(
        r"SELECT COUNT(*) FROM community_scholars WHERE orcid IS NOT NULL "
        r"AND NOT regexp_matches(orcid, '^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$')").fetchone()[0]
    assert_true(bad_orcid == 0, f"{bad_orcid} scholars with a malformed ORCID", failures)

    bad_oa = conn.execute(
        r"SELECT COUNT(*) FROM community_scholars WHERE openalex_id IS NOT NULL "
        r"AND NOT regexp_matches(openalex_id, '^A[0-9]+$')").fetchone()[0]
    assert_true(bad_oa == 0, f"{bad_oa} scholars with a malformed OpenAlex id", failures)

    # person_id is only ever set by an ORCID / OpenAlex-id match, so a
    # dangling one means something linked by name. It is a
    # cod_db.SOFT_REFERENCES entry, checked by check_integrity.

    # Two researchers can legitimately share a name, and this repo's identity
    # rule is that a name is never an identity. So the invariant is a shared
    # name AND affiliation — which is a record entered twice, not a homonym.
    dup_names = conn.execute(
        "SELECT COUNT(*) FROM (SELECT lower(name), lower(COALESCE(affiliation,'')) "
        "FROM community_scholars GROUP BY 1, 2 HAVING COUNT(*) > 1)").fetchone()[0]
    assert_true(dup_names == 0,
                f"{dup_names} scholar name+affiliation pair(s) appear more than once",
                failures)

    # Cohort sizes are pinned only over the rows the harvest actually ranks.
    # Curated-only rows stay flagged on purpose — a hand-picked expert is not
    # dropped because a threshold disliked them — so counting them here would
    # fail the gate for doing the right thing.
    measured = conn.execute(
        "SELECT COUNT(*) FROM community_scholars WHERE h_index IS NOT NULL").fetchone()[0]
    if measured > 0:
        for flag, lo, hi in (("is_preeminent", 90, 110),
                             ("is_most_active", 90, 110),
                             ("is_rising", 40, 60)):
            n = conn.execute(
                f"SELECT COUNT(*) FROM community_scholars "
                f"WHERE {flag} AND h_index IS NOT NULL").fetchone()[0]
            assert_true(lo <= n <= hi,
                        f"harvested {flag} cohort is {n} measured rows, "
                        f"expected {lo}-{hi}", failures)


def check_person_registry(conn, failures: list[str]) -> None:
    """Invariants for the unified identity space.

    No-ops on an empty table, like every other block here, so the
    ingest-only CI rebuild stays green.
    """
    n = table_rows(conn, "person_registry")
    if n <= 0:
        return

    # The load-bearing rule: every registry row must carry SOME stable key,
    # so it can be re-resolved and de-duplicated on a later run rather than
    # silently forking into a second row the next time a source sees the
    # same person. This is the invariant that stops a name-only resolver
    # from ever writing here.
    #
    # A public identifier (ORCID / OpenAlex) satisfies it. So does the
    # project's own person_id, for COD-INTERNAL people only: `people` and
    # `cod_team_members` are curated by hand, person_id is stable across
    # runs, and build_person_registry.py de-duplicates on it via by_pid.
    # Requiring a public id of them dropped 95 real, named COD staff --
    # including the PI and Co-PI -- out of the registry and so off the
    # People view entirely. Rows keyed this way use a codp: canonical_id
    # and are upgraded to an orcid:/openalex: key if one is ever supplied.
    #
    # Harvested identities (community_scholars) still REQUIRE a public
    # identifier: for those the identifier is the whole basis of the claim.
    bare = conn.execute(
        "SELECT COUNT(*) FROM person_registry "
        "WHERE (orcid IS NULL OR orcid = '') "
        "  AND (openalex_id IS NULL OR openalex_id = '') "
        "  AND (person_id IS NULL OR person_id = '')").fetchone()[0]
    assert_true(bare == 0,
                f"{bare} person_registry row(s) carry no stable key "
                f"(no orcid, no openalex_id, no person_id)",
                failures)

    # A codp: row is only legitimate for a COD-internal source.
    bad_local = conn.execute(
        "SELECT COUNT(*) FROM person_registry "
        "WHERE canonical_id LIKE 'codp:%' "
        "  AND source NOT IN ('people', 'cod-team')").fetchone()[0]
    assert_true(bad_local == 0,
                f"{bad_local} codp: registry row(s) come from a non-internal "
                f"source (only 'people' and 'cod-team' may mint local ids)",
                failures)

    for col in ("source_url", "confidence"):
        missing = conn.execute(
            f"SELECT COUNT(*) FROM person_registry "
            f"WHERE {col} IS NULL OR {col} = ''").fetchone()[0]
        assert_true(missing == 0,
                    f"{missing} person_registry row(s) with no {col}", failures)

    bad_conf = conn.execute(
        "SELECT COUNT(*) FROM person_registry "
        "WHERE confidence NOT IN ('high','medium','low')").fetchone()[0]
    assert_true(bad_conf == 0,
                f"{bad_conf} person_registry row(s) with a confidence outside high/medium/low",
                failures)

    # canonical_id is derived from the identifier it is keyed on, so a row
    # claiming 'orcid:…' must actually carry that ORCID. A mismatch means
    # something rewrote the identifier without re-keying, which is how a
    # merge silently attaches to the wrong person.
    mismatched = conn.execute(
        "SELECT COUNT(*) FROM person_registry "
        "WHERE canonical_id LIKE 'orcid:%' "
        "  AND ('orcid:' || orcid) <> canonical_id").fetchone()[0]
    assert_true(mismatched == 0,
                f"{mismatched} person_registry row(s) whose canonical_id "
                f"disagrees with their orcid", failures)

    dup_orcid = conn.execute(
        "SELECT COUNT(*) FROM (SELECT orcid FROM person_registry "
        "WHERE orcid IS NOT NULL AND orcid <> '' "
        "GROUP BY orcid HAVING COUNT(*) > 1)").fetchone()[0]
    assert_true(dup_orcid == 0,
                f"{dup_orcid} ORCID(s) appear on more than one registry row", failures)

    dup_oa = conn.execute(
        "SELECT COUNT(*) FROM (SELECT openalex_id FROM person_registry "
        "WHERE openalex_id IS NOT NULL AND openalex_id <> '' "
        "GROUP BY openalex_id HAVING COUNT(*) > 1)").fetchone()[0]
    assert_true(dup_oa == 0,
                f"{dup_oa} OpenAlex id(s) appear on more than one registry row", failures)

    bad_tier = conn.execute(
        "SELECT COUNT(*) FROM person_registry "
        "WHERE tier NOT IN ('core','archive')").fetchone()[0]
    assert_true(bad_tier == 0,
                f"{bad_tier} person_registry row(s) with a tier outside core/archive",
                failures)

    # Every row must belong to at least one cohort, or it is an orphan the
    # UI has no place to show.
    orphan = conn.execute(
        "SELECT COUNT(*) FROM person_registry "
        "WHERE NOT is_team AND NOT is_site_personnel AND NOT is_scholar").fetchone()[0]
    assert_true(orphan == 0,
                f"{orphan} person_registry row(s) carry no cohort flag", failures)

    # Back-references (person_id -> people, scholar_id -> community_scholars)
    # are cod_db.SOFT_REFERENCES, checked by check_integrity.

    # Provenance: every registry row needs at least one recorded assertion.
    # Deliberately NOT gated on person_identity_source being non-empty — an
    # empty provenance table alongside a populated registry is the failure,
    # not a reason to skip the check. (Gating on it meant deleting every
    # provenance row silenced this invariant, which the injection test for
    # "unsourced row" caught.)
    if table_rows(conn, "person_identity_source") >= 0:
        unsourced = conn.execute(
            "SELECT COUNT(*) FROM person_registry r WHERE r.canonical_id NOT IN "
            "(SELECT canonical_id FROM person_identity_source)").fetchone()[0]
        assert_true(unsourced == 0,
                    f"{unsourced} person_registry row(s) with no person_identity_source entry",
                    failures)


def check_registry_collaborations(conn, failures: list[str]) -> None:
    """Edges must be undirected-normalised and reference real nodes."""
    n = table_rows(conn, "registry_collaborations")
    if n <= 0:
        return

    unordered = conn.execute(
        "SELECT COUNT(*) FROM registry_collaborations "
        "WHERE canonical_id_a >= canonical_id_b").fetchone()[0]
    assert_true(unordered == 0,
                f"{unordered} registry_collaborations row(s) not stored with a < b "
                f"(an undirected edge would be counted twice)", failures)

    nonpos = conn.execute(
        "SELECT COUNT(*) FROM registry_collaborations WHERE co_pub_count < 1").fetchone()[0]
    assert_true(nonpos == 0,
                f"{nonpos} registry_collaborations row(s) with co_pub_count < 1", failures)

    backwards = conn.execute(
        "SELECT COUNT(*) FROM registry_collaborations "
        "WHERE first_year IS NOT NULL AND last_year IS NOT NULL "
        "AND first_year > last_year").fetchone()[0]
    assert_true(backwards == 0,
                f"{backwards} registry_collaborations row(s) with first_year > last_year",
                failures)
    # Both endpoints -> person_registry: cod_db.SOFT_REFERENCES.


def check_person_validation(conn, failures: list[str]) -> None:
    """Verdicts must be from the closed vocabulary, provenanced, and non-vacuous."""
    n = table_rows(conn, "person_validation")
    if n <= 0:
        return

    bad_verdict = conn.execute(
        "SELECT COUNT(*) FROM person_validation "
        "WHERE verdict NOT IN ('pass','fail','not_applicable','unresolved')").fetchone()[0]
    assert_true(bad_verdict == 0,
                f"{bad_verdict} person_validation row(s) with a verdict outside "
                f"pass/fail/not_applicable/unresolved", failures)

    bad_conf = conn.execute(
        "SELECT COUNT(*) FROM person_validation "
        "WHERE confidence NOT IN ('high','medium','low')").fetchone()[0]
    assert_true(bad_conf == 0,
                f"{bad_conf} person_validation row(s) with a confidence outside "
                f"high/medium/low", failures)

    for col in ("source_url", "retrieved_at", "run_id", "method"):
        missing = conn.execute(
            f"SELECT COUNT(*) FROM person_validation "
            f"WHERE {col} IS NULL OR {col} = ''").fetchone()[0]
        assert_true(missing == 0,
                    f"{missing} person_validation row(s) with no {col}", failures)

    # A failure with no detail cannot be acted on.
    silent = conn.execute(
        "SELECT COUNT(*) FROM person_validation "
        "WHERE verdict = 'fail' AND (mismatch_detail IS NULL OR mismatch_detail = '')"
    ).fetchone()[0]
    assert_true(silent == 0,
                f"{silent} person_validation row(s) with verdict='fail' and no "
                f"mismatch_detail", failures)

    # The load-bearing rule: a codp:% row has no public identifier BY
    # CONSTRUCTION, so an ORCID/OpenAlex check on one is 'not_applicable'.
    # Recording it as 'fail' makes the registry look permanently broken and
    # invites someone to invent an identifier to clear the number.
    codp_failed = conn.execute(
        "SELECT COUNT(*) FROM person_validation "
        "WHERE canonical_id LIKE 'codp:%' "
        "  AND check_id IN ('orcid-resolves','openalex-author-resolves','ror-resolves') "
        "  AND verdict = 'fail'").fetchone()[0]
    assert_true(codp_failed == 0,
                f"{codp_failed} person_validation row(s) mark a codp: (COD-internal, "
                f"no public identifier by construction) row as 'fail' on a public-id "
                f"check; the correct verdict is 'not_applicable'", failures)

    # Conversely, a row WITH a public identifier must not get a free pass.
    wrong_na = conn.execute(
        "SELECT COUNT(*) FROM person_validation v "
        "JOIN person_registry r ON r.canonical_id = v.canonical_id "
        "WHERE v.check_id = 'orcid-resolves' AND v.verdict = 'not_applicable' "
        "  AND r.orcid IS NOT NULL").fetchone()[0]
    assert_true(wrong_na == 0,
                f"{wrong_na} person_validation row(s) mark the ORCID check "
                f"'not_applicable' for a registry row that HAS an orcid", failures)
    # canonical_id -> person_registry: cod_db.SOFT_REFERENCES.


def check_coauthor_edges(conn, failures: list[str]) -> None:
    """Every edge: canonical ordering, >=1 co-pub, a work that proves it, and
    an identifier-based match method."""
    n = table_rows(conn, "coauthor_edges")
    if n <= 0:
        return

    unordered = conn.execute(
        "SELECT COUNT(*) FROM coauthor_edges "
        "WHERE canonical_id_a >= canonical_id_b").fetchone()[0]
    assert_true(unordered == 0,
                f"{unordered} coauthor_edges row(s) not stored with a < b "
                f"(each undirected pair must appear once)", failures)

    nonpos = conn.execute(
        "SELECT COUNT(*) FROM coauthor_edges WHERE co_pub_count < 1").fetchone()[0]
    assert_true(nonpos == 0,
                f"{nonpos} coauthor_edges row(s) with co_pub_count < 1", failures)

    backwards = conn.execute(
        "SELECT COUNT(*) FROM coauthor_edges "
        "WHERE first_year IS NOT NULL AND last_year IS NOT NULL "
        "  AND first_year > last_year").fetchone()[0]
    assert_true(backwards == 0,
                f"{backwards} coauthor_edges row(s) with first_year > last_year",
                failures)

    # THE provenance requirement: an edge must name the work that proves it.
    no_evidence = conn.execute(
        "SELECT COUNT(*) FROM coauthor_edges "
        "WHERE exemplar_work_id IS NULL OR exemplar_work_id = ''").fetchone()[0]
    assert_true(no_evidence == 0,
                f"{no_evidence} coauthor_edges row(s) with no exemplar_work_id — "
                f"an edge with no provenance path back to a work is not admissible",
                failures)

    # Identifier equality only. A name-similarity match method would reopen the
    # failure mode that wipe_bad_openalex_attributions.py exists to undo.
    bad_method = conn.execute(
        "SELECT COUNT(*) FROM coauthor_edges WHERE match_method NOT IN "
        "('orcid-equality','openalex-author-id-equality','sameas-closure')"
    ).fetchone()[0]
    assert_true(bad_method == 0,
                f"{bad_method} coauthor_edges row(s) with a match_method outside "
                f"the identifier-equality set; name similarity is never admissible",
                failures)

    for col in ("source_url", "retrieved_at"):
        missing = conn.execute(
            f"SELECT COUNT(*) FROM coauthor_edges "
            f"WHERE {col} IS NULL OR {col} = ''").fetchone()[0]
        assert_true(missing == 0,
                    f"{missing} coauthor_edges row(s) with no {col}", failures)

    bad_conf = conn.execute(
        "SELECT COUNT(*) FROM coauthor_edges "
        "WHERE confidence NOT IN ('high','medium','low')").fetchone()[0]
    assert_true(bad_conf == 0,
                f"{bad_conf} coauthor_edges row(s) with a confidence outside "
                f"high/medium/low", failures)

    selfloop = conn.execute(
        "SELECT COUNT(*) FROM coauthor_edges "
        "WHERE canonical_id_a = canonical_id_b").fetchone()[0]
    assert_true(selfloop == 0,
                f"{selfloop} coauthor_edges self-loop(s)", failures)

    # Both endpoints -> person_registry: cod_db.SOFT_REFERENCES.
    if table_rows(conn, "person_registry") > 0:
        # A codp: person has no public identifier, so no identifier-equality
        # rule can match them to a co-author. An edge touching one means
        # something matched on a name.
        codp_edge = conn.execute(
            "SELECT COUNT(*) FROM coauthor_edges "
            "WHERE canonical_id_a LIKE 'codp:%' OR canonical_id_b LIKE 'codp:%'"
        ).fetchone()[0]
        assert_true(codp_edge == 0,
                    f"{codp_edge} coauthor_edges edge(s) touch a codp: row, which "
                    f"carries no public identifier — no identifier-equality rule "
                    f"could have produced these, so they were matched by name",
                    failures)


def check_coauthor_candidates(conn, failures: list[str]) -> None:
    """A candidate must carry a public identifier, and a rejection must say why."""
    n = table_rows(conn, "coauthor_candidates")
    if n <= 0:
        return

    bare = conn.execute(
        "SELECT COUNT(*) FROM coauthor_candidates "
        "WHERE orcid IS NULL AND openalex_id IS NULL").fetchone()[0]
    assert_true(bare == 0,
                f"{bare} coauthor_candidates row(s) carry neither orcid nor "
                f"openalex_id; a bare name is not an admissible proposal", failures)

    bad_decision = conn.execute(
        "SELECT COUNT(*) FROM coauthor_candidates WHERE decision NOT IN "
        "('pending','accepted','rejected','deferred')").fetchone()[0]
    assert_true(bad_decision == 0,
                f"{bad_decision} coauthor_candidates row(s) with a decision outside "
                f"pending/accepted/rejected/deferred", failures)

    # A dropped person must leave a reason behind, or the same candidate is
    # re-proposed next sweep and eventually accepted by attrition.
    silent = conn.execute(
        "SELECT COUNT(*) FROM coauthor_candidates "
        "WHERE decision IN ('rejected','deferred') "
        "  AND (ambiguity_note IS NULL OR ambiguity_note = '')").fetchone()[0]
    assert_true(silent == 0,
                f"{silent} coauthor_candidates row(s) rejected or deferred with no "
                f"ambiguity_note recording why", failures)

    bad_conf = conn.execute(
        "SELECT COUNT(*) FROM coauthor_candidates "
        "WHERE confidence NOT IN ('high','medium','low')").fetchone()[0]
    assert_true(bad_conf == 0,
                f"{bad_conf} coauthor_candidates row(s) with a confidence outside "
                f"high/medium/low", failures)

    for col in ("source_url", "retrieved_at", "seen_with_canonical_id",
                "seen_on_work_id"):
        missing = conn.execute(
            f"SELECT COUNT(*) FROM coauthor_candidates "
            f"WHERE {col} IS NULL OR {col} = ''").fetchone()[0]
        assert_true(missing == 0,
                    f"{missing} coauthor_candidates row(s) with no {col}", failures)

    # An accepted candidate must actually be in the registry, or the promotion
    # was recorded but never carried out.
    if table_rows(conn, "person_registry") > 0:
        unpromoted = conn.execute(
            "SELECT COUNT(*) FROM coauthor_candidates c "
            "WHERE c.decision = 'accepted' AND c.candidate_id NOT IN "
            "(SELECT canonical_id FROM person_registry)").fetchone()[0]
        assert_true(unpromoted == 0,
                    f"{unpromoted} coauthor_candidates row(s) marked 'accepted' with "
                    f"no matching person_registry row", failures)

        # A candidate that is still pending but already in the registry means
        # the queue is stale and a curator is reviewing something already done.
        already = conn.execute(
            "SELECT COUNT(*) FROM coauthor_candidates c "
            "WHERE c.decision = 'pending' AND c.candidate_id IN "
            "(SELECT canonical_id FROM person_registry)").fetchone()[0]
        assert_true(already == 0,
                    f"{already} coauthor_candidates row(s) still 'pending' although "
                    f"already present in person_registry", failures)


# ---------------------------------------------------------------------------


def check_datasets(conn, failures: list[str]) -> None:
    if table_rows(conn, "coastal_datasets") <= 0:
        return

    bad_cat = conn.execute(
        "SELECT DISTINCT category FROM coastal_datasets").fetchall()
    unknown = sorted({c[0] for c in bad_cat} - DATASET_CATEGORIES)
    assert_true(not unknown, f"dataset category not in vocab: {unknown}", failures)

    bad_slug = conn.execute(
        r"SELECT COUNT(*) FROM coastal_datasets "
        r"WHERE NOT regexp_matches(dataset_id, '^[a-z0-9][a-z0-9-]+$')").fetchone()[0]
    assert_true(bad_slug == 0, f"{bad_slug} datasets with a malformed dataset_id", failures)
    # parent_dataset_id -> coastal_datasets, network_id -> networks and
    # dataset_endpoints.dataset_id -> coastal_datasets are
    # cod_db.SOFT_REFERENCES, checked by check_integrity.

    if table_rows(conn, "dataset_endpoints") <= 0:
        failures.append("coastal_datasets has rows but dataset_endpoints is empty")
        return

    # A dataset with no endpoint is the one thing this catalogue exists to
    # prevent: the whole point is the access URL.
    no_endpoint = conn.execute(
        "SELECT COUNT(*) FROM coastal_datasets d LEFT JOIN dataset_endpoints e "
        "ON e.dataset_id = d.dataset_id WHERE e.dataset_id IS NULL").fetchone()[0]
    assert_true(no_endpoint == 0, f"{no_endpoint} datasets have no access endpoint", failures)

    types = conn.execute("SELECT DISTINCT endpoint_type FROM dataset_endpoints").fetchall()
    unknown_types = sorted({t[0] for t in types} - ENDPOINT_TYPES)
    assert_true(not unknown_types, f"endpoint_type not in vocab: {unknown_types}", failures)

    bad_url = conn.execute(
        "SELECT COUNT(*) FROM dataset_endpoints WHERE NOT regexp_matches(url, '^https?://')"
    ).fetchone()[0]
    assert_true(bad_url == 0, f"{bad_url} endpoints with a non-http(s) URL", failures)

    # The catalogue was assembled from two research passes, which found some
    # of the same resources under different slugs. Uniqueness on dataset_id
    # cannot catch that, so check the fields that identify a resource: two
    # rows sharing a landing page, or a name, are the same thing recorded
    # twice and should be merged rather than shown as separate entries.
    dup_url = conn.execute(
        "SELECT COUNT(*) FROM (SELECT homepage_url FROM coastal_datasets "
        "WHERE homepage_url IS NOT NULL GROUP BY 1 HAVING COUNT(*) > 1)").fetchone()[0]
    assert_true(dup_url == 0,
                f"{dup_url} homepage_url(s) shared by more than one dataset", failures)

    dup_name = conn.execute(
        "SELECT COUNT(*) FROM (SELECT lower(name) FROM coastal_datasets "
        "GROUP BY 1 HAVING COUNT(*) > 1)").fetchone()[0]
    assert_true(dup_name == 0,
                f"{dup_name} dataset name(s) appear more than once", failures)

    check_dataset_facilities(conn, failures)


def check_dataset_facilities(conn, failures: list[str]) -> None:
    """The dataset → stewarding-facility edge.

    Partial coverage is expected and is NOT a failure — 46% (33 of 72) of the
    catalogue names agencies that `facilities` does not carry as rows. What
    would be a failure is an edge to an entity that isn't there, an
    unlabelled method/confidence, or a link to a protected area (a state
    park does not steward an ERDDAP server, so such a match means the
    matcher regressed into fuzzy territory).
    """
    if table_rows(conn, "dataset_facilities") <= 0:
        return
    # dataset_id -> coastal_datasets and facility_id -> facilities are
    # cod_db.SOFT_REFERENCES, checked by check_integrity.

    bad_method = sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT method FROM dataset_facilities").fetchall()}
        - DATASET_FACILITY_METHODS)
    assert_true(not bad_method,
                f"dataset_facilities method not in vocab: {bad_method}", failures)

    bad_conf = sorted({r[0] for r in conn.execute(
        "SELECT DISTINCT confidence FROM dataset_facilities").fetchall()}
        - {"high", "medium", "low"})
    assert_true(not bad_conf,
                f"dataset_facilities confidence not in vocab: {bad_conf}", failures)

    unsourced = conn.execute(
        "SELECT COUNT(*) FROM dataset_facilities "
        "WHERE source IS NULL OR evidence IS NULL").fetchone()[0]
    assert_true(unsourced == 0,
                f"{unsourced} dataset_facilities row(s) without source + evidence",
                failures)

    place_edge = conn.execute(
        "SELECT COUNT(*) FROM dataset_facilities l JOIN facilities f "
        "ON f.facility_id = l.facility_id "
        "WHERE f.facility_type LIKE 'protected-area%'").fetchone()[0]
    assert_true(place_edge == 0,
                f"{place_edge} dataset(s) linked to a protected area rather than a "
                f"research organisation", failures)


def check_mvg_node_layout(conn, failures: list[str]) -> None:
    """Every knowledge-map node must point at a real person.

    Not a plain foreign key: personnel and leadership nodes carry a
    people.person_id, scholar-cohort nodes (442 today) a
    person_registry.canonical_id. A NULL source_id (an unfilled position)
    is allowed.
    """
    if table_rows(conn, "mvg_node_layout") <= 0:
        return
    has_registry = table_rows(conn, "person_registry") >= 0
    registry = ("OR m.source_id IN (SELECT canonical_id FROM person_registry)"
                if has_registry else "")
    dangling = conn.execute(
        f"""SELECT m.cohort, m.source_id FROM mvg_node_layout m
            WHERE m.source_id IS NOT NULL
              AND NOT (m.source_id IN (SELECT person_id FROM people) {registry})
            ORDER BY ALL""").fetchall()
    assert_true(not dangling,
                f"{len(dangling)} mvg_node_layout node(s) whose source_id is neither a "
                f"people.person_id nor a person_registry.canonical_id: {dangling[:5]}",
                failures)


CROSSWALK_CSV = Path(__file__).resolve().parent.parent / "data" / "vocab_crosswalk" / "openalex_to_area.csv"
RESEARCH_AREAS_CSV = Path(__file__).resolve().parent.parent / "schema" / "vocab" / "research_areas.csv"


def check_topic_crosswalk(conn, failures: list[str],
                          path: Path = CROSSWALK_CSV,
                          areas_csv: Path = RESEARCH_AREAS_CSV) -> None:
    """The OpenAlex -> research-area crosswalk must name real things.

    Its concept ids were once typed from memory: "Kelp" carried the id of
    "Global warming", "Ecosystem" the id of the general concept "Ecology",
    "Great Lakes" the id of "History", and 11,154 tags on 7,329 papers
    were scored through a wrong id. compute_person_areas.py and
    compute_area_metrics.py join on the id alone, so nothing downstream
    notices. Fails on:

      * a row that does not have exactly 5 fields (an unquoted label
        with a comma shifts every column after it);
      * an area_id that is not in schema/vocab/research_areas.csv;
      * an id that occurs in publication_topics under a different name.
        Gated on publication_topics being non-empty (ingest.py empties it
        in the CI lake).
    """
    import csv as _csv                                    # noqa: PLC0415
    import re as _re                                      # noqa: PLC0415
    if not path.exists() or not areas_csv.exists():
        failures.append(f"topic crosswalk or research-area vocab missing: {path}")
        return
    with areas_csv.open(newline="", encoding="utf-8") as fh:
        areas = {r["slug"] for r in _csv.DictReader(fh)}
    with path.open(newline="", encoding="utf-8") as fh:
        rows = [r for r in _csv.reader(fh)
                if r and not r[0].lstrip().startswith("#") and r[0] != "openalex_id"]

    bad_len = [r[0] for r in rows if len(r) != 5]
    assert_true(not bad_len,
                f"{len(bad_len)} topic-crosswalk row(s) without exactly 5 fields "
                f"(quote labels that contain commas): {bad_len[:5]}", failures)
    bad_area = sorted({r[2] if len(r) > 2 else "" for r in rows
                       if len(r) < 3 or r[2] not in areas})
    assert_true(not bad_area,
                f"topic crosswalk maps to unknown research areas: {bad_area[:8]}", failures)

    if table_rows(conn, "publication_topics") <= 0:
        return

    def norm(s: str) -> str:
        return _re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

    real = dict(conn.execute(
        "SELECT concept_id, any_value(concept_name) FROM publication_topics "
        "GROUP BY 1").fetchall())
    wrong = sorted({f"{r[0]} is '{real[r[0]]}', not '{r[1]}'" for r in rows
                    if len(r) > 1 and r[0] in real and norm(real[r[0]]) != norm(r[1])})
    assert_true(not wrong,
                f"{len(wrong)} topic-crosswalk id(s) carry the wrong label: {wrong[:5]}",
                failures)


def check_facility_ror(conn, failures: list[str]) -> None:
    """facilities.ror must be the bare 9-character ROR id, not a URL.

    link_registry_facilities.py joins it by equality to
    person_registry.affiliation_ror, which is stored bare, so a facility
    whose ror reads 'https://ror.org/0…' silently links to nobody.

    A hard failure. It depends on ingest.py and ingest_supplement.py
    normalising ROR ids to the bare form on load (WP1a), and on the
    7 URL-form rows in the committed db/parquet being rewritten by the
    regenerate step (WP8, "ROR normalisation" commit). A lake rebuilt from
    db/parquet before that commit fails here, by design.
    """
    bad = conn.execute(
        r"""SELECT facility_id, ror FROM facilities
            WHERE ror IS NOT NULL AND NOT regexp_matches(ror, '^0[a-z0-9]{8}$')
            ORDER BY 1""").fetchall()
    assert_true(not bad,
                f"{len(bad)} facilities.ror value(s) are not a bare ROR id "
                f"(^0[a-z0-9]{{8}}$): {bad[:3]}", failures)


# Verdicts scripts/check_url_health.py writes (status_label there). Duplicated
# on purpose, like the vocabularies above.
URL_HEALTH_STATUSES = {
    "ok", "redirect", "client-error", "server-error", "timeout",
    "template-skipped", "unchecked",
}
# The HTTP class each status with a real response must fall in.
URL_HEALTH_CODE_RANGE = {
    "ok": (200, 299), "redirect": (300, 399),
    "client-error": (400, 499), "server-error": (500, 599),
}


def check_url_health(conn, failures: list[str]) -> None:
    """url_health: status vocabulary, http_code range and class, sweep date.

    Gated on the table being non-empty: it is empty in a lake rebuilt from
    parquet until the first url-health.yml sweep, and ingest.py empties it.
    url uniqueness (its schema.sql primary key) is check_integrity's job.
    """
    if table_rows(conn, "url_health") <= 0:
        return

    statuses = {r[0] for r in conn.execute(
        "SELECT DISTINCT status FROM url_health").fetchall()}
    unknown = sorted(statuses - URL_HEALTH_STATUSES, key=str)
    assert_true(not unknown, f"url_health status not in vocab: {unknown}", failures)

    out_of_range = conn.execute(
        "SELECT count(*) FROM url_health "
        "WHERE http_code IS NOT NULL AND http_code NOT BETWEEN 100 AND 599").fetchone()[0]
    assert_true(out_of_range == 0,
                f"{out_of_range} url_health row(s) with http_code outside 100-599",
                failures)

    # A status that names an HTTP class needs a code in that class; ok /
    # redirect / client-error always come from a real response. server-error
    # may have no code (an SSL or redirect-loop failure is recorded that way).
    for status, (lo, hi) in URL_HEALTH_CODE_RANGE.items():
        need_code = "" if status == "server-error" else " OR http_code IS NULL"
        n = conn.execute(
            f"SELECT count(*) FROM url_health WHERE status = ? "
            f"AND ((http_code IS NOT NULL AND http_code NOT BETWEEN ? AND ?){need_code})",
            [status, lo, hi]).fetchone()[0]
        assert_true(n == 0, f"{n} url_health row(s) with status '{status}' but no "
                            f"http_code in {lo}-{hi}", failures)

    no_code = conn.execute(
        "SELECT count(*) FROM url_health WHERE http_code IS NOT NULL "
        "AND status IN ('timeout', 'template-skipped', 'unchecked')").fetchone()[0]
    assert_true(no_code == 0,
                f"{no_code} url_health row(s) marked timeout/template-skipped/unchecked "
                f"that carry an http_code", failures)

    bad_date = conn.execute(
        r"SELECT count(*) FROM url_health WHERE checked_at IS NULL "
        r"OR NOT regexp_matches(checked_at, '^\d{4}-\d{2}-\d{2}$') "
        r"OR TRY_CAST(checked_at AS DATE) IS NULL").fetchone()[0]
    assert_true(bad_date == 0,
                f"{bad_date} url_health row(s) without an ISO-8601 checked_at date",
                failures)


def check_published_redaction(catalog, failures: list[str]) -> None:
    """No contact details in the exported parquet.

    export_parquet.EXPORT_REDACT blanks people.email in both outputs
    (db/parquet and public/parquet are both committed to a public repo,
    and public/parquet is queryable from the SQL tab). Checks the files
    the exporter writes for THIS lake: the repo's for the default catalog,
    <catalog dir>/export/ for a scratch one.
    """
    root = cod_db.export_root(catalog)
    for sub in ("public/parquet", "db/parquet"):
        f = root / sub / "people.parquet"
        if not f.exists():
            continue
        n = duckdb.connect().execute(
            "SELECT count(email) FROM read_parquet(?)", [str(f)]).fetchone()[0]
        assert_true(n == 0, f"{n} email address(es) published in {f} "
                            f"(re-run scripts/export_parquet.py)", failures)


def check_integrity(conn, failures: list[str]) -> None:
    """Every PRIMARY KEY, UNIQUE, FOREIGN KEY, CHECK and NOT NULL in schema.sql.

    The lake stores no constraints (DuckLake rejects them), so this is the
    only place they are enforced. Generated from duckdb_constraints() of
    schema.sql run in an in-memory spec database — a constraint added to
    the schema is checked here with no change to this file — plus
    cod_db.DERIVED_CONSTRAINTS and cod_db.SOFT_REFERENCES.
    """
    violations = cod_db.integrity_violations(conn)
    for v in violations:
        ref = f" -> {v['ref_table']}" if v.get("ref_table") else ""
        what = {"derived": "derived-table constraint",
                "soft": "soft reference"}.get(v.get("origin"), "schema constraint")
        assert_true(False,
                    f"{what} violated: {v['table']} {v['kind']} "
                    f"{v['columns']}{ref}: {v['count']} row(s)/key(s), "
                    f"e.g. {v['sample'][:3]}", failures)
    print(f"[qa] integrity: {cod_db.constraint_count()} constraints checked "
          f"(schema.sql + derived tables + soft references) and columns compared "
          f"with schema.sql; {len(violations)} violated", file=sys.stderr)


def check_view_sql_types(failures: list[str]) -> None:
    """No view query may return DECIMAL or HUGEINT to the browser.

    duckdb-wasm and duckdb-python disagree about these two types, and the
    disagreement is silent:

      * DECIMAL  -> Arrow structured value in wasm, plain float in python.
                    ``Number(v)`` on it is NaN. Every ``Number(x) || 0``
                    guard in the views then yields 0.
      * HUGEINT  -> BigInt in wasm, int in python. ``Number()`` copes, but
                    any arithmetic mixing it with a JS number throws
                    "Cannot mix BigInt and other types".

    This is exactly how the Knowledge Map died with "invalid bounds": the
    region weight was DECIMAL(38,2) because an integer count was multiplied
    by a JS-side decimal literal, so ``a.weight = Number(a.weight) || 0``
    made every weight 0, ``areas.filter(a => a.weight > 0)`` returned
    nothing, allNodes was empty, and the Voronoi bounding box was NaN.

    Nothing caught it. The SQL executed fine, the file parsed fine, and
    duckdb-python returned a plain float, so the local run was clean while
    the browser showed a blank page. Casting at the SQL boundary is the fix
    that holds regardless of which client reads the parquet.
    """
    import glob                                           # noqa: PLC0415
    import re                                             # noqa: PLC0415

    root = Path(__file__).resolve().parent.parent
    site = root / "public" / "parquet"
    views = sorted((root / "src" / "views").glob("*.js"))
    if not views or not site.is_dir():
        return

    conn = duckdb.connect()
    for pq in site.glob("*.parquet"):
        conn.execute(
            f"CREATE OR REPLACE VIEW {pq.stem} AS "
            f"SELECT * FROM read_parquet('{pq.as_posix()}')")

    unsafe = ("DECIMAL", "HUGEINT", "UHUGEINT")
    for path in views:
        text = path.read_text()
        # Views interpolate numeric constants into SQL; substitute them so
        # the statement parses. Anything still interpolated is skipped.
        consts = dict(re.findall(r"const (W_[A-Z_]+)\s*=\s*([0-9.]+)", text))
        for m in re.finditer(r"`(\s*(?:WITH|SELECT)\b.*?)`", text, re.S):
            sql = m.group(1)
            for k, v in consts.items():
                sql = sql.replace("${" + k + "}", v)
            if "${" in sql or "?" in sql:
                continue
            try:
                rel = conn.sql(sql)
            except Exception:                             # noqa: BLE001
                # Type-checking is not this gate's job to enforce parseability;
                # check_frontend_parses and the view smoke tests cover that.
                continue
            for name, typ in zip(rel.columns, rel.types):
                if any(u in str(typ).upper() for u in unsafe):
                    assert_true(
                        False,
                        f"{path.name} returns column '{name}' as {typ}; "
                        f"duckdb-wasm hands DECIMAL/HUGEINT to JS as a "
                        f"non-number. Wrap it in CAST(... AS DOUBLE).",
                        failures)
    conn.close()


def check_frontend_parses(failures: list[str]) -> None:
    """Every src/*.js must be syntactically valid JavaScript.

    This exists because a SQL comment inside a JS template literal read
    ``-- OpenAlex metrics that `people` alone does not``. The backticks
    closed the template literal, the next token was a bare identifier, and
    the whole site died with "Uncaught SyntaxError: Unexpected identifier
    'people'" — a blank page behind "Loading data…". Every query in the
    file still ran correctly when extracted and executed against DuckDB,
    so SQL-level verification passed while the page was unloadable.

    A character-balance heuristic does NOT catch this: the stray backticks
    come in pairs, so the file counts as balanced. Only a parser catches it.

    The first version of this gate used quickjs. It DID detect this bug —
    that is worth stating plainly, because an earlier version of this
    docstring claimed otherwise. What it could not do is say where the bug
    was. quickjs has no ES-module parser, so the check blanked out
    import/export lines with a regex and wrapped the remainder in a function
    expression; a stray backtick then surfaced as ``SyntaxError: expecting
    '}'`` with no line number, pointing at the wrapper rather than at the
    offending comment. Diagnosing one cost a manual bisect. node reported
    the same defect as ``Unexpected identifier 'NaN'`` at network.js:411 on
    the first call.

    So this version is a diagnostics upgrade, not a detection one. It uses
    node's SourceTextModule, which parses the file as written, as an ES
    module, exactly as the browser does — no text substitution and no
    wrapper, so failures carry a real message and a real position.
    Construction parses without executing and without resolving imports, so
    bare specifiers ('maplibre-gl') that only an importmap resolves do not
    produce a false failure.

    Skipped, with a warning, if node is unavailable — the gate must still
    run in an environment that only has the Python data stack.
    """
    import json                                           # noqa: PLC0415
    import shutil                                         # noqa: PLC0415
    import subprocess                                     # noqa: PLC0415

    node = shutil.which("node")
    if not node:
        print("[qa] node not found — skipping the JS parse check. "
              "Install node to enable it.", file=sys.stderr)
        return

    src_dir = Path(__file__).resolve().parent.parent / "src"
    files = sorted(src_dir.rglob("*.js"))
    if not files:
        failures.append("no .js files found under src/ — is the checkout complete?")
        return

    script = """
const fs = require('fs');
const vm = require('vm');
const out = [];
for (const f of process.argv.slice(1)) {
  try {
    new vm.SourceTextModule(fs.readFileSync(f, 'utf8'), { identifier: f });
  } catch (e) {
    out.push({ file: f, error: String(e && e.message || e).slice(0, 200) });
  }
}
process.stdout.write(JSON.stringify(out));
"""
    proc = subprocess.run(                                # noqa: S603
        [node, "--experimental-vm-modules", "-e", script, "--",
         *[str(f) for f in files]],
        capture_output=True, text=True, timeout=120, check=False)
    if proc.returncode != 0 and not proc.stdout.strip():
        failures.append(f"JS parse check could not run: "
                        f"{proc.stderr.strip().splitlines()[-1][:160]}"
                        if proc.stderr.strip() else "JS parse check failed to run")
        return
    try:
        problems = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        failures.append("JS parse check produced unreadable output")
        return
    for p in problems:
        rel = Path(p["file"]).relative_to(src_dir.parent)
        assert_true(False, f"{rel} is not valid JavaScript: {p['error']}",
                    failures)


def check_facilities(conn, failures: list[str]) -> None:
    """Facility-level invariants: type, country, provenance, coordinates."""
    null_type = conn.execute(
        "SELECT COUNT(*) FROM facilities WHERE facility_type IS NULL OR country IS NULL"
    ).fetchone()[0]
    assert_true(null_type == 0, f"{null_type} facilities with null facility_type or country", failures)

    bad_enum = conn.execute(
        "SELECT COUNT(*) FROM facilities f LEFT JOIN facility_types t ON f.facility_type = t.slug WHERE t.slug IS NULL"
    ).fetchone()[0]
    assert_true(bad_enum == 0, f"{bad_enum} facilities reference unknown facility_type", failures)

    no_prov = conn.execute(
        """SELECT COUNT(*) FROM facilities f
           LEFT JOIN provenance p ON p.record_type='facility' AND p.record_id = f.facility_id
           WHERE p.record_id IS NULL"""
    ).fetchone()[0]
    assert_true(no_prov == 0, f"{no_prov} facilities without provenance rows", failures)

    # bbox checks
    for country, (min_lat, max_lat, min_lng, max_lng) in BBOX_BY_COUNTRY.items():
        boxes = [(min_lat, max_lat, min_lng, max_lng)] + EXTRA_BBOXES.get(country, [])
        # A facility is misplaced only if it falls outside EVERY box for
        # its country, so an offshore territory doesn't read as an error.
        outside = " AND ".join(
            "(hq_lat < ? OR hq_lat > ? OR hq_lng < ? OR hq_lng > ?)" for _ in boxes)
        params: list[float | str] = [country]
        for box in boxes:
            params.extend(box)
        count = conn.execute(
            f"""SELECT COUNT(*) FROM facilities
                WHERE country = ? AND hq_lat IS NOT NULL AND hq_lng IS NOT NULL
                AND ({outside})""",
            params,
        ).fetchone()[0]
        assert_true(count == 0, f"{count} {country} facilities outside the country bbox", failures)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="cod-kmap data-quality gate")
    cod_db.add_argparse_catalog(ap)
    args = ap.parse_args(argv)

    failures: list[str] = []
    warnings: list[str] = []
    try:
        conn = cod_db.connect(read_only=True, catalog=args.catalog, data=args.lake_data)
    except cod_db.LakeNotFound as e:
        print(f"QA FAILED:\n  - {e}")
        return 1
    with conn:
        check_integrity(conn, failures)

        # An empty facilities table is never a valid catalogue. Without this
        # every check below passes vacuously and export_parquet.py would then
        # overwrite the committed parquet with zero rows.
        n_fac = conn.execute("SELECT COUNT(*) FROM facilities").fetchone()[0]
        assert_true(n_fac > 0, "facilities is empty — refusing to pass an empty catalogue",
                    failures)

        # COD team / community scholars / dataset catalogue / registry. Each
        # block no-ops when its table is empty or absent, so the ingest-only
        # CI rebuild isn't failed by data it never produces. A lake whose
        # columns drifted from schema.sql (reported by check_integrity) can
        # make a hand-written check's SQL fail to bind; that is reported as
        # a failure, not a traceback.
        checks = [
            (check_facilities, ()),
            (check_facility_ror, ()),
            (check_tables_present, ()),
            (check_columns, ()),
            (check_cod_team, ()),
            (check_scholars, ()),
            (check_datasets, ()),
            (check_person_registry, ()),
            (check_registry_collaborations, ()),
            (check_person_validation, ()),
            (check_coauthor_edges, ()),
            (check_coauthor_candidates, ()),
            (check_mvg_node_layout, ()),
            # --- WP2 crosswalk checks ---
            (check_topic_crosswalk, ()),
            # --- WP3 identity hygiene checks ---
            # --- WP4 url health checks ---
            (check_url_health, ()),
        ]
        for check, extra in checks:
            try:
                check(conn, failures, *extra)
            except duckdb.Error as e:
                failures.append(f"{check.__name__} could not run: "
                                f"{str(e).splitlines()[0][:200]}")

    # Outside the DB block: these read files, not the lake, and must run
    # even on a checkout with no data.
    try:
        check_published_redaction(args.catalog, failures)
    except duckdb.Error as e:
        failures.append(f"check_published_redaction could not run: "
                        f"{str(e).splitlines()[0][:200]}")
    check_frontend_parses(failures)
    check_view_sql_types(failures)

    for w in warnings:
        print(f"[qa] WARNING: {w}")

    if failures:
        print("QA FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("QA passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
