#!/usr/bin/env python3
"""Filter a raw GeoJSON polygon layer down to coastal-relevant features.

A feature is considered "coastal" when *either*:

  1. its representative point falls inside a coastal state polygon, AND
  2. its representative point is within ``--max-coast-km`` kilometres of
     the nearest coastline vertex.

For (1) we use a tiny embedded list of coastal-state bounding boxes plus
state polygons fetched on demand from the US Census TIGER cartographic
boundaries layer. For (2) we use the GSHHG-derived shoreline that ships
with Natural Earth at 1:50m (we fetch it lazily on first run).

In addition to filtering, this script:

  * dissolves multi-part polygons that share an ORG/UNIT name into a
    single MultiPolygon record (FWS especially has parcel-level rows);
  * simplifies geometry to ~``--simplify-deg`` degrees tolerance for
    web overlay use (default 0.0008 ≈ 80 m — small enough to look right
    at z=10, big enough to keep overlay files under a few MB);
  * preserves the raw ArcGIS attributes under ``properties`` and adds a
    canonical name, manager, designation type, area_acres, state, and
    source_url for downstream ingest.

Usage:
    python scripts/coastal_research/filter_coastal.py \
        --input network_synth_spatial_analysis/coastal_protected/fws_approved.geojson \
        --layer fws \
        --output public/overlays/coastal-nwrs.geojson \
        --max-coast-km 50 --simplify-deg 0.0008
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.request
from collections import defaultdict
from typing import Iterable

from shapely.geometry import shape, mapping, Point, MultiPolygon, Polygon
from shapely.ops import unary_union

# ---------------------------------------------------------------------------
# Coastal state set (Lower 48 ocean + AK + HI + US territories).
# Great-Lakes-only states are intentionally excluded per scoping.
# ---------------------------------------------------------------------------

COASTAL_STATES = {
    # Lower 48 ocean / Gulf
    "WA", "OR", "CA", "TX", "LA", "MS", "AL", "FL", "GA", "SC", "NC",
    "VA", "MD", "DE", "NJ", "NY", "CT", "RI", "MA", "NH", "ME",
    # Alaska + Hawaii
    "AK", "HI",
    # US territories
    "PR", "VI", "GU", "MP", "AS",
}

# ---------------------------------------------------------------------------
# Per-layer attribute mapping.  Each entry tells us how to pull a name,
# acronym, manager and dissolve key out of the raw ArcGIS attributes.
# ---------------------------------------------------------------------------

LAYER_SCHEMAS = {
    "fws": {
        "name_field": "ORGNAME",
        "label_field": "LABELNAME",
        "type_field": "RSL_TYPE",
        "area_acres": "GISACRES",
        "literal": "LIT",
        "manager": "U.S. Fish and Wildlife Service",
        "kind_map": {
            "NWR": "national-wildlife-refuge",
            "WMD": "waterfowl-management-district",
            "COORD": "coordination-area",
            "NM": "national-monument-fws",
        },
        "source_url": (
            "https://services.arcgis.com/QVENGdaPbd4LUkLV/arcgis/rest/"
            "services/FWSApproved_Authoritative/FeatureServer/0"
        ),
    },
    "nps": {
        "name_field": "UNIT_NAME",
        "label_field": "UNIT_NAME",
        "type_field": "UNIT_TYPE",
        "area_acres": None,
        "literal": "UNIT_CODE",
        "manager": "National Park Service",
        "kind_map": None,  # use UNIT_TYPE verbatim
        "source_url": (
            "https://services1.arcgis.com/fBc8EJBxQRMcHlei/arcgis/rest/"
            "services/NPS_Land_Resources_Division_Boundary_and_Tract_Data_Service/"
            "FeatureServer/2"
        ),
    },
    "usfs_special": {
        "name_field": "AREANAME",
        "label_field": "AREANAME",
        "type_field": "AREATYPE",
        "area_acres": "GIS_ACRES",
        "literal": "SPECINTMGTAREAID",
        "manager": "U.S. Forest Service",
        "kind_map": None,
        "source_url": (
            "https://apps.fs.usda.gov/arcx/rest/services/EDW/"
            "EDW_SpecialInterestManagementArea_01/MapServer/0"
        ),
    },
    "usfs_wilderness": {
        "name_field": "WILDERNESSNAME",
        "label_field": "WILDERNESSNAME",
        "type_field": None,
        "area_acres": "GIS_ACRES",
        "literal": "WID",
        "manager": "Multiple (USFS / NPS / FWS / BLM)",
        "kind_map": None,
        "source_url": (
            "https://apps.fs.usda.gov/arcx/rest/services/EDW/"
            "EDW_Wilderness_02/MapServer/0"
        ),
    },
}


# ---------------------------------------------------------------------------
# Coastline cache (Natural Earth 1:50m coastline)
# ---------------------------------------------------------------------------

NE_COASTLINE_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/ne_50m_coastline.geojson"
)
NE_LAND_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/"
    "geojson/ne_50m_admin_1_states_provinces.geojson"
)


def _cache_path(name: str) -> str:
    base = os.path.join("data", "raw", "R11_coastal_ecosystems", "_cache")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, name)


def _fetch(url: str, dst: str) -> None:
    if os.path.exists(dst) and os.path.getsize(dst) > 1000:
        return
    print(f"[coast] fetching {url}", file=sys.stderr)
    req = urllib.request.Request(url, headers={"User-Agent": "cod-kmap/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        with open(dst, "wb") as f:
            f.write(r.read())


def load_coastline_points() -> list[tuple[float, float]]:
    """Return a flat list of (lon, lat) vertices on the world coastline."""
    p = _cache_path("ne_50m_coastline.geojson")
    _fetch(NE_COASTLINE_URL, p)
    with open(p) as f:
        d = json.load(f)
    pts: list[tuple[float, float]] = []
    for ft in d["features"]:
        g = ft["geometry"]
        if g["type"] == "LineString":
            pts.extend((float(x), float(y)) for x, y in g["coordinates"])
        elif g["type"] == "MultiLineString":
            for line in g["coordinates"]:
                pts.extend((float(x), float(y)) for x, y in line)
    return pts


def load_state_polys() -> dict[str, list]:
    """Return {state_code: [shapely Polygon, ...]} for US states + territories.

    Restricts to records where ``adm0_a3=='USA'``. The Natural Earth file uses
    ``postal`` or ``iso_3166_2`` for the state abbreviation.
    """
    p = _cache_path("ne_50m_admin_1_states_provinces.geojson")
    _fetch(NE_LAND_URL, p)
    with open(p) as f:
        d = json.load(f)
    polys: dict[str, list] = defaultdict(list)
    for ft in d["features"]:
        props = ft["properties"]
        if props.get("adm0_a3") not in {"USA", "PRI", "VIR", "GUM", "MNP", "ASM"}:
            continue
        code = props.get("postal") or (props.get("iso_3166_2") or "").split("-")[-1]
        if not code:
            continue
        try:
            geom = shape(ft["geometry"])
        except Exception:
            continue
        if geom.geom_type == "Polygon":
            polys[code].append(geom)
        elif geom.geom_type == "MultiPolygon":
            polys[code].extend(geom.geoms)
    return polys


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lon1, lat1 = a; lon2, lat2 = b
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lat2 - lat1)
    dn = math.radians(lon2 - lon1)
    h = math.sin(dl / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dn / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


def state_for_point(pt: Point, state_polys: dict[str, list]) -> str | None:
    for code, polys in state_polys.items():
        for p in polys:
            if p.contains(pt):
                return code
    # Fallback: nearest centroid distance among coastal states
    best = (None, float("inf"))
    for code, polys in state_polys.items():
        for p in polys:
            d = pt.distance(p.centroid)
            if d < best[1]:
                best = (code, d)
    return best[0]


def min_coast_km(pt: Point, coast_pts: list[tuple[float, float]]) -> float:
    """Approximate min distance via a coarse longitude window prefilter."""
    lon, lat = pt.x, pt.y
    window = 3.0  # degrees
    nearest = float("inf")
    for cl, ct in coast_pts:
        if abs(cl - lon) > window:
            continue
        d = haversine_km((lon, lat), (cl, ct))
        if d < nearest:
            nearest = d
            if nearest < 1.0:
                return nearest
    return nearest


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def slug(s: str) -> str:
    return "".join(c.lower() if c.isalnum() else "-" for c in (s or "")).strip("-")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--layer", required=True, choices=list(LAYER_SCHEMAS))
    ap.add_argument("--output", required=True)
    ap.add_argument("--max-coast-km", type=float, default=50.0)
    ap.add_argument("--simplify-deg", type=float, default=0.0008)
    args = ap.parse_args()

    schema = LAYER_SCHEMAS[args.layer]

    print(f"[filter] loading coastline + state polygons", file=sys.stderr)
    coast_pts = load_coastline_points()
    state_polys = load_state_polys()
    print(f"[filter] {len(coast_pts)} coastline vertices, "
          f"{sum(len(v) for v in state_polys.values())} state polygons "
          f"({len(state_polys)} codes)", file=sys.stderr)

    with open(args.input) as f:
        raw = json.load(f)
    feats_in = raw.get("features") or []
    print(f"[filter] {len(feats_in)} input features", file=sys.stderr)

    # 1) Group by canonical name, attach state + coast distance
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    skipped = {"no_state": 0, "non_coastal_state": 0, "too_far_inland": 0,
               "no_geometry": 0, "no_name": 0}
    name_field = schema["name_field"]
    for ft in feats_in:
        if not ft.get("geometry"):
            skipped["no_geometry"] += 1
            continue
        try:
            geom = shape(ft["geometry"])
        except Exception:
            skipped["no_geometry"] += 1
            continue
        if geom.is_empty:
            skipped["no_geometry"] += 1
            continue
        nm = (ft["properties"].get(name_field) or "").strip()
        if not nm:
            skipped["no_name"] += 1
            continue
        try:
            rep = geom.representative_point()
        except Exception:
            rep = geom.centroid
        st = state_for_point(rep, state_polys)
        if not st:
            skipped["no_state"] += 1
            continue
        if st not in COASTAL_STATES:
            skipped["non_coastal_state"] += 1
            continue
        d_km = min_coast_km(rep, coast_pts)
        if d_km > args.max_coast_km:
            skipped["too_far_inland"] += 1
            continue
        groups[(nm, st)].append({"geom": geom, "props": ft["properties"], "d_km": d_km, "rep": rep})

    print(f"[filter] kept {len(groups)} groups; skipped: {skipped}", file=sys.stderr)

    # 2) Dissolve per group, simplify, build output features
    out_feats = []
    for (nm, st), parts in sorted(groups.items()):
        try:
            merged = unary_union([p["geom"] for p in parts])
        except Exception:
            merged = MultiPolygon([p["geom"] for p in parts if p["geom"].geom_type in ("Polygon",)])
        merged = merged.simplify(args.simplify_deg, preserve_topology=True)
        if merged.is_empty:
            continue
        # representative props from largest part
        biggest = max(parts, key=lambda p: p["geom"].area)
        bp = biggest["props"]
        ttype = bp.get(schema["type_field"]) if schema["type_field"] else None
        kind = (schema["kind_map"] or {}).get(ttype, ttype)
        acres_field = schema.get("area_acres")
        acres = None
        if acres_field:
            try:
                acres = sum(float(p["props"].get(acres_field) or 0) for p in parts)
            except Exception:
                acres = None
        out_feats.append({
            "type": "Feature",
            "geometry": mapping(merged),
            "properties": {
                "name": nm,
                "acronym": bp.get(schema["literal"]) if schema.get("literal") else None,
                "kind": kind,
                "manager": schema["manager"],
                "state": st,
                "area_acres": round(acres, 1) if acres else None,
                "min_coast_km": round(biggest["d_km"], 2),
                "source": schema["source_url"],
                "raw_attributes": bp,
            },
        })

    fc = {
        "type": "FeatureCollection",
        "features": out_feats,
        "metadata": {
            "input": args.input,
            "layer": args.layer,
            "max_coast_km": args.max_coast_km,
            "simplify_deg": args.simplify_deg,
            "feature_count": len(out_feats),
            "retrieved_at": time.strftime("%Y-%m-%d"),
            "source_service": schema["source_url"],
            "manager": schema["manager"],
        },
    }
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(fc, f)
    print(f"[filter] wrote {len(out_feats)} features to {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
