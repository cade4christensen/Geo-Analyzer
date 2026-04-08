"""
Coordinate Analyzer
-------------------
Given a latitude/longitude, returns:
  1. Distance (meters) to the nearest of each linear feature type
  2. What surface the coordinate landed on
  3. Population classification of the area
"""

import time
import requests
from typing import Optional
from shapely.geometry import Point, LineString, Polygon, shape
from pyproj import Transformer, Geod

# ---------------------------------------------------------------------------
# Constants / mappings
# ---------------------------------------------------------------------------

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
NLCD_WCS_URL = "https://www.mrlc.gov/geoserver/mrlc_display/NLCD_2021_Land_Cover_L48/ows"

GEOD = Geod(ellps="WGS84")

# NLCD pixel value → surface label
NLCD_SURFACE = {
    11: "Water",
    12: "Rock",          # Perennial Ice/Snow — rare lower 48
    21: "Field",         # Developed Open Space (parks, lawns)
    22: "Structure",     # Developed Low Intensity
    23: "Structure",     # Developed Medium Intensity
    24: "Structure",     # Developed High Intensity
    31: "Rock",          # Barren Land (rock, sand, clay)
    41: "Woods",         # Deciduous Forest
    42: "Woods",         # Evergreen Forest
    43: "Woods",         # Mixed Forest
    51: "Scrub",         # Dwarf Scrub (Alaska)
    52: "Scrub",         # Shrub/Scrub
    71: "Field",         # Grassland/Herbaceous
    72: "Field",         # Sedge/Herbaceous
    73: "Field",         # Lichens
    74: "Field",         # Moss
    81: "Field",         # Pasture/Hay
    82: "Field",         # Cultivated Crops
    90: "Woods",         # Woody Wetlands
    95: "Drainage",      # Emergent Herbaceous Wetlands
}

# NLCD → population hint (refined by building density below)
NLCD_POP_HINT = {
    21: "Rural",
    22: "Suburban",
    23: "Suburban",
    24: "Urban",
}

# Linear feature OSM tag filters → category label
LINEAR_FILTERS = {
    "Road":         '["highway"~"motorway|trunk|primary|secondary|tertiary|residential|service|unclassified|road"]',
    "Trail":        '["highway"~"path|footway|bridleway|track|steps"]',
    "Railroad":     '["railway"~"rail|light_rail|tram|subway|narrow_gauge|monorail"]',
    "Utility Line": '["power"~"line|minor_line"]',
    "Water Feature":'["waterway"~"river|stream|canal|drain|ditch"]',
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utm_transformer(lat: float, lon: float) -> Transformer:
    """Return a WGS84 → local UTM transformer for geodesic-accurate distances."""
    zone = int((lon + 180) / 6) + 1
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)


def _to_utm(transformer: Transformer, lon: float, lat: float):
    return transformer.transform(lon, lat)


def _query_overpass(ql: str, retries: int = 3) -> dict:
    for attempt in range(retries):
        try:
            r = requests.post(OVERPASS_URL, data={"data": ql}, timeout=45)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                raise RuntimeError(f"Overpass query failed: {e}")


def _way_to_linestring_utm(way: dict, transformer: Transformer) -> Optional[LineString]:
    """Convert an Overpass way (with geometry) to a UTM LineString."""
    geom = way.get("geometry", [])
    if len(geom) < 2:
        return None
    coords = [_to_utm(transformer, n["lon"], n["lat"]) for n in geom]
    return LineString(coords)


def _way_to_polygon_utm(way: dict, transformer: Transformer) -> Optional[Polygon]:
    geom = way.get("geometry", [])
    if len(geom) < 3:
        return None
    coords = [_to_utm(transformer, n["lon"], n["lat"]) for n in geom]
    return Polygon(coords)


# ---------------------------------------------------------------------------
# 1. Linear features — distance to nearest of each type
# ---------------------------------------------------------------------------

def find_nearest_linear_features(lat: float, lon: float, radius_m: int = 2000) -> dict:
    """
    Returns a dict:
      {
        "Road":         {"distance_m": float, "name": Optional[str]},
        "Trail":        ...,
        "Railroad":     ...,
        "Utility Line": ...,
        "Water Feature":{...},
      }
    distance_m is None if no feature found within radius_m.
    """
    transformer = _utm_transformer(lat, lon)
    px, py = _to_utm(transformer, lon, lat)
    point_utm = Point(px, py)

    # Build one combined Overpass query for all feature types
    parts = []
    for label, tag_filter in LINEAR_FILTERS.items():
        parts.append(f'  way{tag_filter}(around:{radius_m},{lat},{lon});')
    # Also include waterway relations (e.g. large rivers)
    parts.append(f'  relation["waterway"~"river|stream"](around:{radius_m},{lat},{lon});')

    ql = f"[out:json][timeout:40];\n(\n{''.join(parts)}\n);\nout geom;"
    data = _query_overpass(ql)

    # Index ways by their tags
    results = {label: {"distance_m": None, "name": None} for label in LINEAR_FILTERS}

    for element in data.get("elements", []):
        if element["type"] not in ("way", "relation"):
            continue
        tags = element.get("tags", {})
        label = _classify_linear_tag(tags)
        if label is None:
            continue

        ls = _way_to_linestring_utm(element, transformer)
        if ls is None:
            continue

        dist = point_utm.distance(ls)
        current = results[label]["distance_m"]
        if current is None or dist < current:
            results[label]["distance_m"] = round(dist, 1)
            results[label]["name"] = tags.get("name") or tags.get("ref")

    return results


def _classify_linear_tag(tags: dict) -> Optional[str]:
    hw = tags.get("highway", "")
    rw = tags.get("railway", "")
    pw = tags.get("power", "")
    ww = tags.get("waterway", "")

    if hw in ("motorway", "trunk", "primary", "secondary", "tertiary",
              "residential", "service", "unclassified", "road"):
        return "Road"
    if hw in ("path", "footway", "bridleway", "track", "steps"):
        return "Trail"
    if rw in ("rail", "light_rail", "tram", "subway", "narrow_gauge", "monorail"):
        return "Railroad"
    if pw in ("line", "minor_line"):
        return "Utility Line"
    if ww in ("river", "stream", "canal", "drain", "ditch"):
        return "Water Feature"
    return None


# ---------------------------------------------------------------------------
# 2. Surface at point — what did the coordinate land on?
# ---------------------------------------------------------------------------

def get_surface_at_point(lat: float, lon: float) -> str:
    """
    Returns one of:
      Structure | Road | Linear | Drainage | Water |
      Brush | Scrub | Woods | Field | Rock | Unknown
    """
    # --- Step 1: check OSM for high-priority features at the exact point ---
    osm_surface = _osm_surface_at_point(lat, lon)
    if osm_surface:
        return osm_surface

    # --- Step 2: fall back to NLCD raster ---
    nlcd_val = _get_nlcd_value(lat, lon)
    if nlcd_val is not None:
        return NLCD_SURFACE.get(nlcd_val, "Unknown")

    return "Unknown"


def _osm_surface_at_point(lat: float, lon: float, radius_m: int = 30) -> Optional[str]:
    """
    Query OSM for features within ~30 m of the point.
    Priority: building > road > linear > drainage/water > landuse/natural polygon.
    """
    ql = f"""
[out:json][timeout:30];
(
  way["building"](around:{radius_m},{lat},{lon});
  way["highway"~"motorway|trunk|primary|secondary|tertiary|residential|service|unclassified|road"](around:5,{lat},{lon});
  way["railway"](around:5,{lat},{lon});
  way["power"~"line|minor_line"](around:5,{lat},{lon});
  way["waterway"~"river|stream|canal|drain|ditch"](around:10,{lat},{lon});
  way["natural"="water"](around:{radius_m},{lat},{lon});
  relation["natural"="water"](around:{radius_m},{lat},{lon});
  way["landuse"~"reservoir|basin"](around:{radius_m},{lat},{lon});
  way["natural"~"wood|scrub|grassland|bare_rock|scree"](around:{radius_m},{lat},{lon});
  way["landuse"~"forest|grass|meadow|farmland|farmyard"](around:{radius_m},{lat},{lon});
);
out geom;
"""
    try:
        data = _query_overpass(ql)
    except Exception:
        return None

    transformer = _utm_transformer(lat, lon)
    px, py = _to_utm(transformer, lon, lat)
    point_utm = Point(px, py)

    buildings, roads, linears, drainages, waters, landuses = [], [], [], [], [], []

    for el in data.get("elements", []):
        tags = el.get("tags", {})
        geom_pts = el.get("geometry", [])

        if el["type"] == "way":
            if tags.get("building"):
                poly = _way_to_polygon_utm(el, transformer)
                if poly:
                    buildings.append(poly)
            elif tags.get("highway"):
                ls = _way_to_linestring_utm(el, transformer)
                if ls:
                    roads.append(ls)
            elif tags.get("railway") or tags.get("power"):
                ls = _way_to_linestring_utm(el, transformer)
                if ls:
                    linears.append(ls)
            elif tags.get("waterway"):
                ls = _way_to_linestring_utm(el, transformer)
                if ls:
                    drainages.append(ls)
            elif tags.get("natural") in ("water",) or tags.get("landuse") in ("reservoir", "basin"):
                poly = _way_to_polygon_utm(el, transformer)
                if poly:
                    waters.append(poly)
            elif tags.get("natural") or tags.get("landuse"):
                poly = _way_to_polygon_utm(el, transformer)
                if poly:
                    landuses.append((poly, tags))

    # Check containment / proximity in priority order
    if any(b.contains(point_utm) or b.distance(point_utm) < 5 for b in buildings):
        return "Structure"
    if any(r.distance(point_utm) < 8 for r in roads):
        return "Road"
    if any(l.distance(point_utm) < 8 for l in linears):
        return "Linear"
    if any(d.distance(point_utm) < 8 for d in drainages):
        return "Drainage"
    if any(w.contains(point_utm) for w in waters):
        return "Water"

    # Check landuse/natural polygons containing the point
    for poly, tags in landuses:
        if poly.contains(point_utm):
            nat = tags.get("natural", "")
            lu = tags.get("landuse", "")
            if nat == "wood" or lu in ("forest",):
                return "Woods"
            if nat == "scrub":
                return "Scrub"
            if nat in ("grassland", "heath"):
                return "Field"
            if nat in ("bare_rock", "scree"):
                return "Rock"
            if lu in ("grass", "meadow", "farmland", "farmyard", "orchard", "vineyard"):
                return "Field"

    return None


def _get_nlcd_value(lat: float, lon: float) -> Optional[int]:
    """
    Query the MRLC NLCD 2021 WCS for the land cover class at (lat, lon).
    Returns the NLCD integer class, or None on failure.
    """
    try:
        import rasterio
        from rasterio.io import MemoryFile
    except ImportError:
        return None

    try:
        # Convert WGS84 → Albers Equal Area (EPSG:5070, native NLCD projection)
        transformer = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
        x, y = transformer.transform(lon, lat)

        # Request a 3×3 pixel window (90 m × 90 m, NLCD pixel = 30 m)
        buf = 50
        bbox = f"{x - buf},{y - buf},{x + buf},{y + buf}"

        params = {
            "service":       "WCS",
            "version":       "1.0.0",
            "request":       "GetCoverage",
            "coverage":      "NLCD_2021_Land_Cover_L48",
            "bbox":          bbox,
            "crs":           "EPSG:5070",
            "response_crs":  "EPSG:5070",
            "width":         "3",
            "height":        "3",
            "format":        "GeoTIFF",
        }

        r = requests.get(NLCD_WCS_URL, params=params, timeout=30)
        r.raise_for_status()

        # Verify we got an image, not an XML error
        if b"<ServiceException" in r.content[:500] or b"<?xml" in r.content[:50]:
            return None

        with MemoryFile(r.content) as memfile:
            with memfile.open() as dataset:
                # Sample the center pixel
                row, col = dataset.index(x, y)
                data = dataset.read(1)
                nrows, ncols = data.shape
                row = max(0, min(row, nrows - 1))
                col = max(0, min(col, ncols - 1))
                val = int(data[row, col])
                return val if val != 0 else None

    except Exception:
        return None


# ---------------------------------------------------------------------------
# 3. Population classification
# ---------------------------------------------------------------------------

def classify_population(lat: float, lon: float) -> str:
    """
    Returns one of: Wilderness | Rural | Suburban | Urban

    Method:
      - Count buildings within 500 m (primary signal — not affected by segment length)
      - Clip road geometries to the 500 m buffer before summing length, so a single
        long highway segment doesn't inflate the density score
      - Use NLCD as a tiebreaker only when OSM data is ambiguous
    """
    nlcd_val = _get_nlcd_value(lat, lon)
    nlcd_hint = NLCD_POP_HINT.get(nlcd_val)

    # If NLCD says high-density developed, trust it immediately
    if nlcd_hint == "Urban":
        return "Urban"

    # Query buildings and roads within 500 m
    ql = f"""
[out:json][timeout:30];
(
  way["building"](around:500,{lat},{lon});
  way["highway"~"motorway|trunk|primary|secondary|tertiary|residential|service|unclassified"](around:500,{lat},{lon});
);
out geom;
"""
    time.sleep(1)  # avoid Overpass rate limiting after prior queries
    try:
        data = _query_overpass(ql)
    except Exception:
        if nlcd_hint:
            return nlcd_hint
        return "Wilderness"

    transformer = _utm_transformer(lat, lon)
    px, py = _to_utm(transformer, lon, lat)
    point_utm = Point(px, py)
    buffer_500 = point_utm.buffer(500)

    building_count = 0
    clipped_road_length_m = 0.0

    for el in data.get("elements", []):
        tags = el.get("tags", {})
        if tags.get("building"):
            building_count += 1
        elif tags.get("highway"):
            ls = _way_to_linestring_utm(el, transformer)
            if ls:
                # Clip to the 500 m buffer so long through-roads don't inflate density
                clipped = ls.intersection(buffer_500)
                clipped_road_length_m += clipped.length

    # --- Primary signal: building count ---
    if building_count >= 50:
        return "Urban"
    if building_count >= 10:
        return "Suburban"
    if building_count >= 2:
        return "Rural"

    # --- Secondary signal: clipped road density ---
    # Clipped thresholds within a 500 m radius buffer:
    #   Urban:    many roads crossing  → 5 000+ m
    #   Suburban: several roads        → 1 500–5 000 m
    #   Rural:    1–2 roads passing by → 200–1 500 m
    #   Wilderness: no real roads      → < 200 m
    if clipped_road_length_m > 5000:
        return "Urban"
    if clipped_road_length_m > 1500:
        return "Suburban"
    if clipped_road_length_m > 200:
        return "Rural"

    # --- Tiebreaker: NLCD developed class ---
    if nlcd_hint:
        return nlcd_hint

    return "Wilderness"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze(lat: float, lon: float) -> dict:
    """
    Full analysis for a coordinate. Returns:
    {
      "coordinate": {"lat": float, "lon": float},
      "linear_features": {
          "Road":         {"distance_m": float|None, "name": str|None},
          "Trail":        ...,
          "Railroad":     ...,
          "Utility Line": ...,
          "Water Feature":{...},
      },
      "surface":     str,   # what the point landed on
      "population":  str,   # Wilderness/Rural/Suburban/Urban
    }
    """
    print(f"Analyzing ({lat}, {lon})...")

    print("  Querying linear features...")
    linear = find_nearest_linear_features(lat, lon)

    print("  Identifying surface at point...")
    surface = get_surface_at_point(lat, lon)

    # If the point landed on a linear feature, set that feature's distance to 0
    _zero_distance_for_surface(surface, linear)

    print("  Classifying population...")
    population = classify_population(lat, lon)

    return {
        "coordinate":      {"lat": lat, "lon": lon},
        "linear_features": linear,
        "surface":         surface,
        "population":      population,
    }


def _zero_distance_for_surface(surface: str, linear: dict):
    """
    When the point is on a linear feature, its distance to that feature is 0.
    Maps surface labels to their corresponding linear feature key(s).
    """
    surface_to_feature = {
        "Road":     ["Road"],
        "Drainage": ["Water Feature"],
        "Water":    ["Water Feature"],
    }
    # For "Linear" (railroad or utility line), zero whichever has the smaller distance
    if surface == "Linear":
        candidates = ["Railroad", "Utility Line"]
        best = min(
            (k for k in candidates if linear.get(k, {}).get("distance_m") is not None),
            key=lambda k: linear[k]["distance_m"],
            default=None,
        )
        if best:
            linear[best]["distance_m"] = 0.0
        return

    for feature in surface_to_feature.get(surface, []):
        if linear.get(feature) is not None:
            linear[feature]["distance_m"] = 0.0
