"""Data-quality assertions run after ingest.

Exits non-zero on any failure so CI workflows can gate deploys.
"""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb

DB_PATH = Path(__file__).resolve().parent.parent / "db" / "cod_kmap.duckdb"

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

    if table_rows(conn, "cod_wbs") > 0:
        dangling = conn.execute(
            "SELECT COUNT(*) FROM cod_team_members tm "
            "LEFT JOIN cod_wbs w ON w.wbs_code = tm.wbs_code WHERE w.wbs_code IS NULL"
        ).fetchone()[0]
        assert_true(dangling == 0,
                    f"{dangling} COD team rows reference an unknown wbs_code", failures)
        broken_parents = conn.execute(
            "SELECT COUNT(*) FROM cod_wbs c LEFT JOIN cod_wbs p ON p.wbs_code = c.parent_code "
            "WHERE c.parent_code IS NOT NULL AND p.wbs_code IS NULL").fetchone()[0]
        assert_true(broken_parents == 0,
                    f"{broken_parents} cod_wbs rows reference a missing parent_code", failures)


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
    # dangling one means something linked by name.
    orphans = conn.execute(
        "SELECT COUNT(*) FROM community_scholars s "
        "LEFT JOIN people p ON p.person_id = s.person_id "
        "WHERE s.person_id IS NOT NULL AND p.person_id IS NULL").fetchone()[0]
    assert_true(orphans == 0,
                f"{orphans} scholars link to a person_id that isn't in people", failures)

    dup_names = conn.execute(
        "SELECT COUNT(*) FROM (SELECT lower(name) FROM community_scholars "
        "GROUP BY 1 HAVING COUNT(*) > 1)").fetchone()[0]
    assert_true(dup_names == 0,
                f"{dup_names} scholar names appear more than once", failures)

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

    dangling_parent = conn.execute(
        "SELECT COUNT(*) FROM coastal_datasets d "
        "LEFT JOIN coastal_datasets p ON p.dataset_id = d.parent_dataset_id "
        "WHERE d.parent_dataset_id IS NOT NULL AND p.dataset_id IS NULL").fetchone()[0]
    assert_true(dangling_parent == 0,
                f"{dangling_parent} datasets reference a missing parent_dataset_id", failures)

    bad_network = conn.execute(
        "SELECT COUNT(*) FROM coastal_datasets d LEFT JOIN networks n "
        "ON n.network_id = d.network_id "
        "WHERE d.network_id IS NOT NULL AND n.network_id IS NULL").fetchone()[0]
    assert_true(bad_network == 0,
                f"{bad_network} datasets reference a network_id not in networks", failures)

    if table_rows(conn, "dataset_endpoints") <= 0:
        failures.append("coastal_datasets has rows but dataset_endpoints is empty")
        return

    # A dataset with no endpoint is the one thing this catalogue exists to
    # prevent: the whole point is the access URL.
    no_endpoint = conn.execute(
        "SELECT COUNT(*) FROM coastal_datasets d LEFT JOIN dataset_endpoints e "
        "ON e.dataset_id = d.dataset_id WHERE e.dataset_id IS NULL").fetchone()[0]
    assert_true(no_endpoint == 0, f"{no_endpoint} datasets have no access endpoint", failures)

    orphan_endpoints = conn.execute(
        "SELECT COUNT(*) FROM dataset_endpoints e LEFT JOIN coastal_datasets d "
        "ON d.dataset_id = e.dataset_id WHERE d.dataset_id IS NULL").fetchone()[0]
    assert_true(orphan_endpoints == 0,
                f"{orphan_endpoints} endpoints reference an unknown dataset", failures)

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


def main() -> int:
    failures: list[str] = []
    with duckdb.connect(str(DB_PATH)) as conn:
        conn.execute("SET search_path = main;")

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

        # COD team / community scholars / dataset catalogue. Each block
        # no-ops when its table is empty or absent, so the ingest-only CI
        # rebuild isn't failed by data it never produces.
        check_columns(conn, failures)
        check_cod_team(conn, failures)
        check_scholars(conn, failures)
        check_datasets(conn, failures)

    if failures:
        print("QA FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("QA passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
