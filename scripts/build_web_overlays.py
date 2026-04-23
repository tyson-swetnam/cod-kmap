"""Bundle polygon overlay layers from network_synth_spatial_analysis/ into
web-ready GeoJSON for the map UI.

Outputs land in web/public/overlays/:

  nerr-reserves.geojson       one polygon per NERR reserve (latest year file)
  marine-sanctuaries.geojson  every NMS boundary polygon
  marine-monuments.geojson    five Marine National Monuments
  nps-coastal.geojson         National Park Service marine-protected-area parks
  nep-programs.geojson        28 National Estuary Program boundaries
  neon-domains.geojson        22 NEON ecological domains
  epa-regions.geojson         10 EPA administrative regions

Each feature's properties are normalised to a small set: {name, network,
acronym, source} so the MapLibre style can key colour/label by a single
field. Idempotent: re-running overwrites existing files.

Source: network_synth_spatial_analysis/ (COMPASS-DOE synthesis-networks
spatial companion, MIT).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from shapely.geometry import shape, mapping

ROOT = Path(__file__).resolve().parent.parent
SPATIAL = ROOT / "network_synth_spatial_analysis"
OUT = ROOT / "web" / "public" / "overlays"

SOURCE = "COMPASS-DOE/synthesis-networks"

# Simplification tolerances (degrees). Picked so the polygons still read well
# at continental zoom; detail returns when the user zooms in because MapLibre
# upsamples vector edges smoothly. Use a tighter tolerance for smaller
# features (NEP, NERR) than for broad administrative polygons (EPA regions).
SIMPLIFY_FINE = 0.002    # ~200 m
SIMPLIFY_MEDIUM = 0.01   # ~1 km
SIMPLIFY_COARSE = 0.03   # ~3 km

# Coordinate precision in the output files (decimal places).
PRECISION = 4            # ~11 m


def _round_coords(obj, ndigits):
    if isinstance(obj, (list, tuple)):
        if obj and isinstance(obj[0], (int, float)):
            return [round(float(x), ndigits) for x in obj]
        return [_round_coords(x, ndigits) for x in obj]
    return obj


def simplify_geom(geom: dict, tolerance: float) -> dict:
    """Simplify a GeoJSON geometry and return a coordinate-rounded version."""
    try:
        g = shape(geom)
        if not g.is_valid:
            g = g.buffer(0)
        g = g.simplify(tolerance, preserve_topology=True)
        out = mapping(g)
    except Exception:
        out = geom
    out["coordinates"] = _round_coords(out.get("coordinates"), PRECISION)
    return out


def write_fc(path: Path, features: list[dict], meta: dict | None = None) -> None:
    fc = {"type": "FeatureCollection", "features": features}
    if meta:
        fc["meta"] = meta
    path.write_text(json.dumps(fc))
    kb = path.stat().st_size // 1024
    print(f"  {path.relative_to(ROOT)} ({len(features)} features, {kb} KB)")


def latest_nerr_boundary_per_reserve() -> list[Path]:
    """Return one GIS_Process/<acronym>/Boundaries/Reserve_Boundaries file per
    NERR reserve, preferring the highest-year filename."""
    base = SPATIAL / "SH_ALL_RB" / "GIS_Process"
    chosen: dict[str, Path] = {}
    for f in base.glob("*/Boundaries/Reserve_Boundaries/*_RB_*.geojson"):
        m = re.match(r"([A-Z]+)_RB_(\d{4})\.geojson", f.name)
        if not m:
            continue
        acronym, year = m.group(1), int(m.group(2))
        current = chosen.get(acronym)
        if current is None or int(re.search(r"_RB_(\d{4})", current.name).group(1)) < year:
            chosen[acronym] = f
    return sorted(chosen.values())


NERR_NAMES = {
    "ACE": "ACE Basin",
    "APA": "Apalachicola",
    "CBM": "Chesapeake Bay Maryland",
    "CBV": "Chesapeake Bay Virginia",
    "DEL": "Delaware",
    "ELK": "Elkhorn Slough",
    "GND": "Grand Bay",
    "GRB": "Great Bay",
    "GTM": "Guana Tolomato Matanzas",
    "HUD": "Hudson River",
    "JAC": "Jacques Cousteau",
    "JOB": "Jobos Bay",
    "KAC": "Kachemak Bay",
    "LKS": "Lake Superior",
    "MAR": "Mission-Aransas",
    "NAR": "Narragansett Bay",
    "NIW": "North Inlet–Winyah Bay",
    "NOC": "North Carolina",
    "OWC": "Old Woman Creek",
    "PDB": "Padilla Bay",
    "RKB": "Rookery Bay",
    "SAP": "Sapelo Island",
    "SFB": "San Francisco Bay",
    "SOS": "South Slough",
    "TJR": "Tijuana River",
    "WEL": "Wells",
    "WKB": "Weeks Bay",
    "WQB": "Waquoit Bay",
}


def bundle_nerrs() -> None:
    features = []
    for path in latest_nerr_boundary_per_reserve():
        acronym = re.match(r"([A-Z]+)_", path.name).group(1)
        with path.open() as fh:
            data = json.load(fh)
        for f in data["features"]:
            props = {
                "name": f"{NERR_NAMES.get(acronym, acronym)} NERR",
                "acronym": acronym,
                "network": "NERRS",
                "source": SOURCE,
            }
            features.append({
                "type": "Feature",
                "properties": props,
                "geometry": simplify_geom(f["geometry"], SIMPLIFY_FINE),
            })
    write_fc(OUT / "nerr-reserves.geojson", features)


def bundle_sanctuaries() -> None:
    features = []
    nms_dir = SPATIAL / "Land_Cover" / "NMS_boundaries"
    # Take only one PMNM copy to avoid duplicates between Albers vs WGS84.
    skip = {"PMNM_py_Albers.geojson"}
    for path in sorted(nms_dir.rglob("*.geojson")):
        if path.name in skip:
            continue
        with path.open() as fh:
            data = json.load(fh)
        for f in data["features"]:
            p = f["properties"]
            name = (
                p.get("SANCTUARY") or p.get("Sanctuary") or p.get("Name")
                or p.get("AREA_NAME") or path.parent.name.upper()
            )
            features.append({
                "type": "Feature",
                "properties": {
                    "name": f"{name} National Marine Sanctuary" if "Sanctuary" not in str(name)
                            and "NMS" not in str(name) else str(name),
                    "network": "NMS",
                    "source": SOURCE,
                },
                "geometry": simplify_geom(f["geometry"], SIMPLIFY_MEDIUM),
            })
    write_fc(OUT / "marine-sanctuaries.geojson", features)


def bundle_monuments() -> None:
    # Two candidate files exist. MarineMonuments/Monuments.geojson has 5
    # large-ocean monuments with labels; MPAI_MarineMonuments is a superset
    # without names. Use the labelled one and merge names from the other.
    with (SPATIAL / "MarineMonuments" / "Monuments.geojson").open() as fh:
        data = json.load(fh)
    features = []
    for f in data["features"]:
        p = f["properties"]
        features.append({
            "type": "Feature",
            "properties": {
                "name": p.get("Site_Name", "Marine National Monument"),
                "state": p.get("State"),
                "network": "Marine-Monument",
                "source": SOURCE,
            },
            "geometry": simplify_geom(f["geometry"], SIMPLIFY_COARSE),
        })
    write_fc(OUT / "marine-monuments.geojson", features)


def bundle_nps_coastal() -> None:
    # NPS.geojson has 44 coastal NPS units marked as MPA members.
    with (SPATIAL / "MPAI_MarineNationalParks" / "NPS.geojson").open() as fh:
        data = json.load(fh)
    features = []
    for f in data["features"]:
        p = f["properties"]
        features.append({
            "type": "Feature",
            "properties": {
                "name": p.get("Site_Name") or "NPS Unit",
                "state": p.get("State"),
                "management": p.get("NS_Full"),
                "protection_level": p.get("Prot_Lvl"),
                "network": "NPS-Coastal",
                "source": SOURCE,
            },
            "geometry": simplify_geom(f["geometry"], SIMPLIFY_MEDIUM),
        })
    write_fc(OUT / "nps-coastal.geojson", features)


def bundle_nep() -> None:
    path = SPATIAL / "NEP_BoundariesFY19" / "NEP_Boundaries2019.geojson"
    with path.open() as fh:
        data = json.load(fh)
    features = []
    for f in data["features"]:
        p = f["properties"]
        features.append({
            "type": "Feature",
            "properties": {
                "name": (p.get("NEP_NAME") or "").strip(),
                "short": p.get("NEP_SHORT"),
                "year": p.get("YEAR_DESIG"),
                "epa_region": p.get("EPA_REGION"),
                "area_sqmi": p.get("AREA_SQMI"),
                "network": "NEP",
                "source": SOURCE,
            },
            "geometry": simplify_geom(f["geometry"], SIMPLIFY_FINE),
        })
    write_fc(OUT / "nep-programs.geojson", features)


def bundle_neon_domains() -> None:
    path = SPATIAL / "Land_Cover" / "NEON_domains" / "NEONDomains_0" / "NEON_Domains.geojson"
    with path.open() as fh:
        data = json.load(fh)
    features = []
    for f in data["features"]:
        p = f["properties"]
        features.append({
            "type": "Feature",
            "properties": {
                "name": (p.get("DomainName") or "").strip(),
                "domain_id": p.get("DomainID"),
                "network": "NEON",
                "source": SOURCE,
            },
            "geometry": simplify_geom(f["geometry"], SIMPLIFY_COARSE),
        })
    write_fc(OUT / "neon-domains.geojson", features)


def bundle_epa_regions() -> None:
    path = SPATIAL / "EPA_Locations" / "EPA_Regions__Region_Boundaries.geojson"
    with path.open() as fh:
        data = json.load(fh)
    features = []
    for f in data["features"]:
        p = f["properties"]
        features.append({
            "type": "Feature",
            "properties": {
                "name": f"EPA Region {p.get('EPAREGION')}",
                "region": p.get("EPAREGION"),
                "network": "EPA-Region",
                "source": SOURCE,
            },
            "geometry": simplify_geom(f["geometry"], SIMPLIFY_COARSE),
        })
    write_fc(OUT / "epa-regions.geojson", features)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    print("Writing map overlays to", OUT.relative_to(ROOT))
    bundle_nerrs()
    bundle_sanctuaries()
    bundle_monuments()
    bundle_nps_coastal()
    bundle_nep()
    bundle_neon_domains()
    bundle_epa_regions()

    manifest = {
        "nerr-reserves":      {"label": "NERR reserves",           "color": "#0d9488", "category": "coastal"},
        "nep-programs":       {"label": "National Estuary Program","color": "#7c3aed", "category": "coastal"},
        "marine-sanctuaries": {"label": "Marine Sanctuaries",      "color": "#0369a1", "category": "marine"},
        "marine-monuments":   {"label": "Marine Monuments",        "color": "#1e40af", "category": "marine"},
        "nps-coastal":        {"label": "NPS Coastal Units",       "color": "#16a34a", "category": "marine"},
        "neon-domains":       {"label": "NEON Ecological Domains", "color": "#d4a017", "category": "context"},
        "epa-regions":        {"label": "EPA Regions",             "color": "#64748b", "category": "context"},
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"  {(OUT / 'manifest.json').relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
