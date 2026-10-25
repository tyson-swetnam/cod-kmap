#!/usr/bin/env python3
"""Remove the 81 NEON-as-facility-points records from the cod-kmap
catalogue. The NEON sites now live exclusively as the
``public/overlays/neon-sites.geojson`` polygon overlay; keeping them
in the facilities table would render them twice on the Map view (once
as observatory dots, once as polygons).

Drops:
  * facilities rows where parent_org='National Ecological Observatory Network'
  * the rows that point at them: locations, network_membership,
    area_links, facility_regions, facility_personnel, registry_facilities,
    dataset_facilities, facility_primary_groups
  * matching provenance rows (record_type='facility')

All deletes land in the lake as one snapshot (commit message
``remove_neon_facility_points: ...``), after an integrity check; nothing
lands if a remaining table still points at a NEON row (nothing cascades in
a lake). This script no longer writes parquet or
``public/facilities.geojson`` — publish with qa.py → export_parquet.py
afterwards. Point it at a scratch lake with ``$COD_LAKE_CATALOG`` or
``--catalog`` (``$COD_KMAP_DB``, the old .duckdb override, is ignored).

Idempotent — re-running on a clean lake drops 0 rows and writes no
snapshot. ``--dry-run`` reports the counts without writing (and keeps the
seed file).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import cod_db  # noqa: E402

R11_FAC_FILE = (ROOT / "data" / "raw" / "R11_coastal_ecosystems"
                / "facilities_neon_sites.json")

NEON_PARENT = "National Ecological Observatory Network"
CHILDREN = ("locations", "network_membership", "area_links", "facility_regions",
            "facility_personnel", "registry_facilities", "dataset_facilities",
            # Derived (compute_primary_groups.py); a recompute would drop
            # the same rows, and leaving them fails qa.py's integrity gate.
            "facility_primary_groups")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    cod_db.add_argparse_catalog(ap)
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would be removed; write nothing")
    args = ap.parse_args(argv)
    if os.environ.get("COD_KMAP_DB"):
        print("[neon-rm] $COD_KMAP_DB is ignored (the .duckdb store is retired); "
              "use $COD_LAKE_CATALOG or --catalog", file=sys.stderr)
    try:
        conn = cod_db.connect(read_only=args.dry_run, catalog=args.catalog,
                              data=args.lake_data)
    except cod_db.LakeNotFound as e:
        print(f"[err] {e}", file=sys.stderr)
        return 2
    print(f"[neon-rm] lake: {cod_db.catalog_path(args.catalog)}", file=sys.stderr)

    with conn:
        ids = [r[0] for r in cod_db.rows(
            conn, "SELECT facility_id FROM facilities WHERE parent_org = ? "
                  "ORDER BY facility_id", [NEON_PARENT])]
        print(f"[neon-rm] {len(ids)} NEON facility rows to remove",
              file=sys.stderr)
        if not ids:
            print("[neon-rm] nothing to do", file=sys.stderr)
            return 0
        # A failed statement aborts the transaction, so probe for each
        # table instead of try/except.
        children = [c for c in CHILDREN if cod_db.table_exists(conn, c)]
        in_ids = "IN (SELECT unnest(?::VARCHAR[]))"

        def counts() -> None:
            for child in children:
                n = cod_db.scalar(conn, f"SELECT COUNT(*) FROM {child} "
                                        f"WHERE facility_id {in_ids}", [ids])
                print(f"[neon-rm]   {child}: -{n}", file=sys.stderr)
            n = cod_db.scalar(conn, f"SELECT COUNT(*) FROM provenance "
                                    f"WHERE record_id {in_ids} AND record_type='facility'",
                              [ids])
            print(f"[neon-rm]   provenance: -{n}", file=sys.stderr)
            print(f"[neon-rm]   facilities: -{len(ids)}", file=sys.stderr)

        if args.dry_run:
            counts()
            print("[neon-rm] dry run: nothing written", file=sys.stderr)
            return 0

        with cod_db.transaction(conn, f"remove_neon_facility_points: drop "
                                      f"{len(ids)} NEON facility points"):
            counts()
            for child in children:
                conn.execute(f"DELETE FROM {child} WHERE facility_id {in_ids}", [ids])
            # provenance has no FK to facilities so we delete by record_id
            conn.execute(f"DELETE FROM provenance WHERE record_id {in_ids} "
                         f"AND record_type='facility'", [ids])
            conn.execute(f"DELETE FROM facilities WHERE facility_id {in_ids}", [ids])
            # Any other table that still points at a dropped facility
            # (declared FKs and cod_db.SOFT_REFERENCES) fails this and
            # rolls the whole run back.
            cod_db.assert_integrity(conn, ["facilities", "provenance"])
        print("[neon-rm] next: compute_primary_groups.py / compute_area_metrics.py "
              "→ qa.py → export_parquet.py", file=sys.stderr)

    # Drop the now-orphan R11 seed file so future ingest_r11.py runs
    # don't re-add the points.
    if R11_FAC_FILE.exists():
        try:
            R11_FAC_FILE.unlink()
            print(f"[neon-rm] removed seed file: "
                  f"{R11_FAC_FILE.relative_to(ROOT)}", file=sys.stderr)
        except OSError as exc:
            print(f"[neon-rm] could not remove seed file ({exc}); "
                  f"please delete by hand", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
