"""
Coordinate Analyzer
-------------------
Given a (lat, lon), returns:
  1. Distance (m) to nearest of each linear-feature type
  2. Surface type the point lies on
  3. Population classification (Wilderness / Rural / Suburban / Urban)

US-focused (NLCD raster is L48 only) but OSM-derived signals work anywhere.
"""

import math
import time
from typing import Optional, List, Tuple, Dict

import requests
from shapely.geometry import Point, LineString, Polygon
from pyproj import Transformer

# ---------------------------------------------------------------------------
# Endpoints & headers
# ---------------------------------------------------------------------------

# Overpass mirrors — tried in rotation on retry. The main host occasionally
# rejects a UA that the mirrors accept (and vice-versa).
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
]

NLCD_WCS_URL = "https://www.mrlc.gov/geoserver/mrlc_display/NLCD_2021_Land_Cover_L48/ows"

# Cascade of free authoritative US road datasets — tried in order when OSM is
# sparse. Each is queried via ArcGIS REST; the closer-than-threshold result wins.
TIGER_ROADS_URL = ("https://tigerweb.geo.census.gov/arcgis/rest/services/"
                   "TIGERweb/Transportation/MapServer/18/query")
USFS_ROADS_URL  = ("https://apps.fs.usda.gov/arcx/rest/services/EDW/"
                   "EDW_RoadBasic_01/MapServer/0/query")
USGS_ROADS_URL  = ("https://carto.nationalmap.gov/arcgis/rest/services/"
                   "transportation/MapServer/2/query")

# TIGER MTFCC codes that count as drivable Roads. S17xx are walkways/stairs/etc.
_TIGER_ROAD_MTFCC = {
    "S1100",  # Primary Road
    "S1200",  # Secondary Road
    "S1400",  # Local Neighborhood Road, Rural Road, City Street
    "S1500",  # Vehicular Trail (4WD)
    "S1630",  # Ramp
    "S1640",  # Service Drive along limited-access highway
    "S1730",  # Alley
    "S1740",  # Private Road for service vehicles (logging, oil fields, etc.)
}

# Cascade of (threshold_m, source_label, query_url, mtfcc_filter). The cascade
# stops as soon as the current best road distance is <= the next threshold —
# saves repeated HTTP calls when OSM already returned a close match.
_ROAD_FALLBACK_CASCADE = [
    (500, "TIGER", TIGER_ROADS_URL, _TIGER_ROAD_MTFCC),
    (300, "USFS",  USFS_ROADS_URL,  None),
    (200, "USGS",  USGS_ROADS_URL,  None),
]

# Property-name candidates for the road's name across the different datasets.
_NAME_FIELD_CANDIDATES = (
    "FULLNAME", "NAME", "RD_NAME", "ROAD_NAME", "RTE_NM", "FULL_NAME",
    "STREETNAME", "STREET",
)

# Overpass rejects the default `python-requests/X` UA with 406. Don't set an
# Accept header — Overpass returns XML on error and a strict Accept turns
# every error into another 406.
HTTP_HEADERS = {
    "User-Agent": "geo_analyzer/1.1 (coordinate analysis tool)",
}

# ---------------------------------------------------------------------------
# Classification tables
# ---------------------------------------------------------------------------

# NLCD pixel value → surface label.
# 22 (Developed Low) and 23 (Developed Medium) are mostly lawn with sparse
# houses — NOT "Structure". Real buildings are caught by OSM polygons first;
# mapping these to Field reflects what you're actually standing on.
NLCD_SURFACE = {
    11: "Water",
    12: "Rock",      # Perennial Ice/Snow
    21: "Field",     # Developed Open Space (parks, lawns)
    22: "Field",     # Developed Low Intensity
    23: "Field",     # Developed Medium Intensity
    24: "Structure", # Developed High Intensity
    31: "Rock",      # Barren Land
    41: "Woods",     # Deciduous Forest
    42: "Woods",     # Evergreen Forest
    43: "Woods",     # Mixed Forest
    51: "Scrub",
    52: "Scrub",
    71: "Field",
    72: "Field",
    73: "Field",
    74: "Field",
    81: "Field",
    82: "Field",
    90: "Woods",     # Woody Wetlands
    95: "Drainage",  # Emergent Herbaceous Wetlands
}

NLCD_POP_HINT = {
    21: "Rural",
    22: "Suburban",
    23: "Suburban",
    24: "Urban",
}

LINEAR_FILTERS = {
    "Road":          '["highway"~"motorway|trunk|primary|secondary|tertiary|residential|service|unclassified|road"]',
    "Trail":         '["highway"~"path|footway|bridleway|track|steps"]',
    "Railroad":      '["railway"~"rail|light_rail|tram|subway|narrow_gauge|monorail"]',
    "Utility Line":  '["power"~"line|minor_line"]',
    "Water Feature": '["waterway"~"river|stream|canal|drain|ditch"]',
}

# Distance tolerances (m) below which the point is considered "on" the
# nearest-feature output. OSM stores centerlines; thresholds approximate
# half the typical feature width.
LINEAR_ZERO_THRESHOLDS = {
    "Road":          15.0,
    "Trail":          3.0,
    "Railroad":       3.0,
    "Utility Line":   3.0,
    "Water Feature": 10.0,
}

# Per-class road thresholds for surface check — tighter than the zeroing
# thresholds above. A residential road's surface is ~5 m, not 15.
ROAD_SURFACE_THRESHOLD = {
    "motorway": 15.0, "trunk": 15.0, "primary": 15.0,
    "secondary": 8.0, "tertiary": 8.0,
    "residential": 5.0, "service": 5.0, "unclassified": 5.0, "road": 5.0,
}

# Building tag taxonomy used in population scoring.
_OCCUPIED = {
    "house", "residential", "apartments", "commercial", "retail", "industrial",
    "office", "school", "church", "hospital", "hotel", "warehouse", "civic",
    "public", "university", "college", "dormitory", "terrace",
    "semidetached_house", "detached",
}
_OUTBUILDING = {
    "garage", "shed", "barn", "farm_auxiliary", "hut", "cabin", "greenhouse",
    "stable", "sty", "roof", "carport", "hangar", "storage_tank", "bunker",
    "boathouse", "static_caravan",
}

# ---------------------------------------------------------------------------
# Projection helpers
# ---------------------------------------------------------------------------

def _validate_coord(lat: float, lon: float) -> None:
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        raise ValueError(
            f"Coordinate out of range: lat={lat}, lon={lon}. "
            "Expected lat in [-90, 90], lon in [-180, 180]."
        )


def _utm_transformer(lat: float, lon: float) -> Transformer:
    _validate_coord(lat, lon)
    zone = max(1, min(60, int((lon + 180) / 6) + 1))
    epsg = 32600 + zone if lat >= 0 else 32700 + zone
    return Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)


def _to_utm(transformer: Transformer, lon: float, lat: float) -> Tuple[float, float]:
    return transformer.transform(lon, lat)

# ---------------------------------------------------------------------------
# Overpass client
# ---------------------------------------------------------------------------

def _query_overpass(ql: str, retries: int = 3) -> dict:
    last_err = "no attempts made"
    for attempt in range(retries):
        url = OVERPASS_ENDPOINTS[attempt % len(OVERPASS_ENDPOINTS)]
        try:
            r = requests.post(url, data={"data": ql},
                              headers=HTTP_HEADERS, timeout=60)
        except requests.exceptions.RequestException as e:
            last_err = f"network error on {url}: {e}"
        else:
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError:
                    last_err = f"non-JSON response from {url}: {(r.text or '')[:200]}"
            elif r.status_code in (429, 503, 504):
                last_err = f"HTTP {r.status_code} (transient) from {url}"
            else:
                body = (r.text or "").replace("\n", " ")[:300]
                last_err = f"HTTP {r.status_code} from {url}: {body}"
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    raise RuntimeError(f"Overpass query failed: {last_err}")


def _way_to_linestring_utm(way: dict, transformer: Transformer) -> Optional[LineString]:
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


def _classify_linear_tag(tags: dict) -> Optional[str]:
    hw = tags.get("highway", "")
    rw = tags.get("railway", "")
    pw = tags.get("power", "")
    ww = tags.get("waterway", "")
    if hw in ROAD_SURFACE_THRESHOLD:
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
# US road-data fallback cascade (TIGER → USFS → USGS NTD)
# ---------------------------------------------------------------------------

def _extract_road_name(props: dict) -> Optional[str]:
    """Pick the most-name-looking value across the various dataset schemas."""
    for f in _NAME_FIELD_CANDIDATES:
        v = props.get(f)
        if v and str(v).strip() and str(v).strip().lower() not in ("none", "null"):
            return str(v).strip()
    return None


def _arcgis_query_nearest_road(
    url: str, lat: float, lon: float, transformer: Transformer,
    point_utm: Point, radius_m: int, mtfcc_filter: Optional[set] = None,
) -> Optional[Tuple[float, Optional[str]]]:
    """
    Generic ArcGIS REST roads query — works for TIGER, USFS, USGS NTD.
    Returns (distance_m, name) of the nearest segment in the bbox, or None.
    """
    dlat = radius_m / 111320.0
    dlon = radius_m / (111320.0 * max(math.cos(math.radians(lat)), 1e-3))
    bbox = f"{lon - dlon},{lat - dlat},{lon + dlon},{lat + dlat}"

    params = {
        "where":          "1=1",
        "geometry":       bbox,
        "geometryType":   "esriGeometryEnvelope",
        "inSR":           "4326",
        "outSR":          "4326",
        "spatialRel":     "esriSpatialRelIntersects",
        "outFields":      "*",
        "returnGeometry": "true",
        "f":              "geojson",
    }
    try:
        r = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=20)
        if r.status_code != 200:
            return None
        data = r.json()
    except Exception:
        return None

    nearest_dist: Optional[float] = None
    nearest_name: Optional[str] = None

    for feat in data.get("features", []):
        props = feat.get("properties") or {}
        if mtfcc_filter and props.get("MTFCC", "") not in mtfcc_filter:
            continue

        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        if gtype == "LineString":
            coord_lists = [geom.get("coordinates") or []]
        elif gtype == "MultiLineString":
            coord_lists = geom.get("coordinates") or []
        else:
            continue

        for coords in coord_lists:
            if len(coords) < 2:
                continue
            try:
                utm_coords = [_to_utm(transformer, c[0], c[1]) for c in coords]
                ls = LineString(utm_coords)
                d = point_utm.distance(ls)
            except Exception:
                continue
            if nearest_dist is None or d < nearest_dist:
                nearest_dist = d
                nearest_name = _extract_road_name(props)

    if nearest_dist is None:
        return None
    return nearest_dist, nearest_name


def _cascade_nearest_road(
    lat: float, lon: float, transformer: Transformer, point_utm: Point,
    radius_m: int, starting_best: Optional[Tuple[float, Optional[str], str]] = None,
) -> Optional[Tuple[float, Optional[str], str]]:
    """
    Walk the TIGER→USFS→USGS cascade. Stops early as soon as `starting_best`
    (or the current winner) is already within the next dataset's threshold —
    avoids unnecessary HTTP calls when OSM already returned a tight match.
    Returns (distance_m, name, source) or `starting_best` if nothing improves.
    """
    best = starting_best  # (dist, name, source) or None

    for threshold, label, url, mtfcc in _ROAD_FALLBACK_CASCADE:
        # Skip this and remaining sources if we're already inside threshold.
        if best is not None and best[0] <= threshold:
            break
        result = _arcgis_query_nearest_road(
            url, lat, lon, transformer, point_utm, radius_m, mtfcc
        )
        if result is None:
            continue
        d, name = result
        if best is None or d < best[0]:
            best = (d, name, label)

    return best


# ---------------------------------------------------------------------------
# 1. Nearest linear features
# ---------------------------------------------------------------------------

def find_nearest_linear_features(lat: float, lon: float, radius_m: int = 2000) -> dict:
    """
    Returns {label: {"distance_m": float|None, "name": str|None, "source": str|None}}
    for each linear-feature class. None means no feature within radius_m.
    Sources: "OSM" (default), "TIGER" (US Census roads, fallback when OSM is sparse).
    """
    transformer = _utm_transformer(lat, lon)
    px, py = _to_utm(transformer, lon, lat)
    point_utm = Point(px, py)

    parts = [f'  way{tag_filter}(around:{radius_m},{lat},{lon});'
             for tag_filter in LINEAR_FILTERS.values()]
    parts.append(f'  relation["waterway"~"river|stream"](around:{radius_m},{lat},{lon});')
    ql = f"[out:json][timeout:50];\n(\n{''.join(parts)}\n);\nout geom;"

    data = _query_overpass(ql)
    results = {label: {"distance_m": None, "name": None, "source": None}
               for label in LINEAR_FILTERS}

    for el in data.get("elements", []):
        if el.get("type") not in ("way", "relation"):
            continue
        tags = el.get("tags", {})
        label = _classify_linear_tag(tags)
        if label is None:
            continue
        ls = _way_to_linestring_utm(el, transformer)
        if ls is None:
            continue
        dist = point_utm.distance(ls)
        current = results[label]["distance_m"]
        if current is None or dist < current:
            results[label]["distance_m"] = round(dist, 1)
            results[label]["name"] = tags.get("name") or tags.get("ref")
            results[label]["source"] = "OSM"

    # Cascading Road fallback when OSM coverage is sparse: TIGER → USFS → USGS NTD.
    # Each only fires if the current best distance is still above its threshold.
    # US-only data; outside the US these all silently no-op.
    osm_dist = results["Road"]["distance_m"]
    if osm_dist is None or osm_dist > 500:
        starting = (osm_dist, results["Road"]["name"], "OSM") if osm_dist is not None else None
        best = _cascade_nearest_road(lat, lon, transformer, point_utm, radius_m, starting)
        if best is not None:
            d, name, source = best
            results["Road"]["distance_m"] = round(d, 1)
            results["Road"]["name"] = name
            results["Road"]["source"] = source

    return results

# ---------------------------------------------------------------------------
# 2. Surface at point
# ---------------------------------------------------------------------------

def get_surface_at_point(lat: float, lon: float) -> str:
    """
    Returns one of:
      Structure | Road | Linear | Drainage | Water |
      Woods | Scrub | Field | Rock | Unknown
    Prefers precise OSM features; falls back to NLCD raster for natural
    land cover when OSM has nothing local.
    """
    osm_surface = _osm_surface_at_point(lat, lon)
    if osm_surface:
        return osm_surface
    nlcd_val = _get_nlcd_value(lat, lon)
    if nlcd_val is not None:
        return NLCD_SURFACE.get(nlcd_val, "Unknown")
    return "Unknown"


def _osm_surface_at_point(lat: float, lon: float, radius_m: int = 30) -> Optional[str]:
    ql = f"""
[out:json][timeout:30];
(
  way["building"](around:{radius_m},{lat},{lon});
  way["highway"~"motorway|trunk|primary|secondary|tertiary|residential|service|unclassified|road"](around:20,{lat},{lon});
  way["railway"](around:8,{lat},{lon});
  way["power"~"line|minor_line"](around:8,{lat},{lon});
  way["waterway"~"river|stream|canal|drain|ditch"](around:15,{lat},{lon});
  way["natural"="water"](around:{radius_m},{lat},{lon});
  relation["natural"="water"](around:{radius_m},{lat},{lon});
  way["landuse"~"reservoir|basin"](around:{radius_m},{lat},{lon});
  way["natural"="wetland"](around:{radius_m},{lat},{lon});
  way["natural"~"wood|scrub|heath|grassland|bare_rock|scree|sand|beach"](around:{radius_m},{lat},{lon});
  way["landuse"~"forest|grass|meadow|farmland|farmyard|orchard|vineyard|cemetery|recreation_ground"](around:{radius_m},{lat},{lon});
  way["leisure"="park"](around:{radius_m},{lat},{lon});
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

    buildings:  List[Polygon] = []
    roads:      List[Tuple[LineString, str]] = []
    linears:    List[LineString] = []
    drainages:  List[LineString] = []
    wetlands:   List[Polygon] = []
    waters:     List[Polygon] = []
    landuses:   List[Tuple[Polygon, dict]] = []

    for el in data.get("elements", []):
        if el.get("type") != "way":
            continue
        tags = el.get("tags", {})

        if tags.get("building"):
            poly = _way_to_polygon_utm(el, transformer)
            if poly:
                buildings.append(poly)
        elif tags.get("highway"):
            ls = _way_to_linestring_utm(el, transformer)
            if ls:
                roads.append((ls, tags.get("highway", "")))
        elif tags.get("railway") or tags.get("power"):
            ls = _way_to_linestring_utm(el, transformer)
            if ls:
                linears.append(ls)
        elif tags.get("waterway"):
            ls = _way_to_linestring_utm(el, transformer)
            if ls:
                drainages.append(ls)
        elif tags.get("natural") == "wetland":
            poly = _way_to_polygon_utm(el, transformer)
            if poly:
                wetlands.append(poly)
        elif tags.get("natural") == "water" or tags.get("landuse") in ("reservoir", "basin"):
            poly = _way_to_polygon_utm(el, transformer)
            if poly:
                waters.append(poly)
        elif tags.get("natural") or tags.get("landuse") or tags.get("leisure"):
            poly = _way_to_polygon_utm(el, transformer)
            if poly:
                landuses.append((poly, tags))

    # Priority order: built features first, then water, then natural cover.
    if any(b.contains(point_utm) for b in buildings):
        return "Structure"

    if any(ls.distance(point_utm) < ROAD_SURFACE_THRESHOLD.get(cls, 5.0)
           for ls, cls in roads):
        return "Road"

    if any(l.distance(point_utm) < 5 for l in linears):
        return "Linear"

    if any(d.distance(point_utm) < 10 for d in drainages):
        return "Drainage"

    if any(w.contains(point_utm) for w in wetlands):
        return "Drainage"

    if any(w.contains(point_utm) or w.distance(point_utm) < 2 for w in waters):
        return "Water"

    for poly, tags in landuses:
        if not poly.contains(point_utm):
            continue
        nat  = tags.get("natural", "")
        lu   = tags.get("landuse", "")
        leis = tags.get("leisure", "")
        if nat == "wood" or lu == "forest":
            return "Woods"
        if nat == "scrub":
            return "Scrub"
        if nat in ("grassland", "heath"):
            return "Field"
        if nat in ("bare_rock", "scree"):
            return "Rock"
        if nat in ("sand", "beach"):
            return "Rock"
        if lu in ("grass", "meadow", "farmland", "farmyard",
                  "orchard", "vineyard", "cemetery", "recreation_ground"):
            return "Field"
        if leis == "park":
            return "Field"

    return None


def _get_nlcd_value(lat: float, lon: float) -> Optional[int]:
    """
    Sample NLCD 2021 land cover at (lat, lon). Returns the modal class in a
    3x3 pixel window so a thin road pixel doesn't dominate a single-pixel read.
    """
    try:
        import rasterio
        from rasterio.io import MemoryFile
    except ImportError:
        return None

    try:
        transformer = Transformer.from_crs("EPSG:4326", "EPSG:5070", always_xy=True)
        x, y = transformer.transform(lon, lat)

        buf = 50  # m, gives 100x100 bbox around point — comfortably 3+ pixels wide
        bbox = f"{x - buf},{y - buf},{x + buf},{y + buf}"

        params = {
            "service":      "WCS",
            "version":      "1.0.0",
            "request":      "GetCoverage",
            "coverage":     "NLCD_2021_Land_Cover_L48",
            "bbox":         bbox,
            "crs":          "EPSG:5070",
            "response_crs": "EPSG:5070",
            "width":        "3",
            "height":       "3",
            "format":       "GeoTIFF",
        }
        r = requests.get(
            NLCD_WCS_URL,
            params=params,
            headers={"User-Agent": HTTP_HEADERS["User-Agent"]},
            timeout=30,
        )
        r.raise_for_status()

        # Detect XML error replies that masquerade as a 200 OK.
        if b"<ServiceException" in r.content[:500] or b"<?xml" in r.content[:50]:
            return None

        with MemoryFile(r.content) as memfile:
            with memfile.open() as ds:
                row, col = ds.index(x, y)
                data = ds.read(1)
                nrows, ncols = data.shape
                row = max(0, min(row, nrows - 1))
                col = max(0, min(col, ncols - 1))

                r0, r1 = max(0, row - 1), min(nrows, row + 2)
                c0, c1 = max(0, col - 1), min(ncols, col + 2)
                vals = [int(v) for v in data[r0:r1, c0:c1].ravel() if v != 0]
                if not vals:
                    return None

                counts: Dict[int, int] = {}
                for v in vals:
                    counts[v] = counts.get(v, 0) + 1
                top_count = max(counts.values())
                winners = [v for v, c in counts.items() if c == top_count]
                centre = int(data[row, col])
                return centre if centre in winners else winners[0]
    except Exception:
        return None

# ---------------------------------------------------------------------------
# 3. Population
# ---------------------------------------------------------------------------

def _count_buildings_and_roads(lat: float, lon: float, radius_m: int):
    """
    Query Overpass for buildings + roads within `radius_m`. Returns
    (building_score, building_count, clipped_road_length_m) where:
      - building_score weights ambiguous "yes" tags at 0.3 (confidence-adjusted)
      - building_count is the raw count of non-outbuilding structures
      - clipped_road_length_m is highway length intersected with the radius
    Returns (None, None, None) on failure.
    """
    ql = f"""
[out:json][timeout:30];
(
  way["building"](around:{radius_m},{lat},{lon});
  way["highway"~"motorway|trunk|primary|secondary|tertiary|residential|unclassified"](around:{radius_m},{lat},{lon});
);
out geom;
"""
    try:
        data = _query_overpass(ql)
    except Exception:
        return None, None, None

    transformer = _utm_transformer(lat, lon)
    px, py = _to_utm(transformer, lon, lat)
    point_utm = Point(px, py)
    buffer_poly = point_utm.buffer(radius_m)

    building_score = 0.0
    building_count = 0
    clipped_road_length_m = 0.0
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        bval = tags.get("building", "")
        if bval:
            if bval in _OUTBUILDING:
                continue
            building_count += 1
            if bval in _OCCUPIED:
                building_score += 1.0
            else:
                building_score += 0.3
        elif tags.get("highway"):
            ls = _way_to_linestring_utm(el, transformer)
            if ls:
                clipped_road_length_m += ls.intersection(buffer_poly).length

    return building_score, building_count, clipped_road_length_m


def _classify_from_signals(building_score: float, clipped_road_length_m: float,
                           nlcd_hint: Optional[str],
                           building_count: int = 0) -> str:
    """Pure tiered classification from already-computed 500m signals."""
    # Urban requires very dense built environment. A single highway running
    # through a suburb shouldn't promote — use raw building count for the
    # density check rather than the road length.
    if building_count >= 250:
        return "Urban"
    if building_score >= 70:
        return "Urban"
    if building_score >= 3 and clipped_road_length_m > 1500:
        return "Suburban"
    if building_score >= 1 and clipped_road_length_m > 2500:
        return "Suburban"
    if building_score >= 0.4 and clipped_road_length_m > 4000:
        return "Suburban"
    if building_score >= 5 and nlcd_hint in ("Suburban", "Urban"):
        return "Suburban"
    if building_score >= 0.4:
        return "Rural"

    # Road-only fallback when buildings are absent.
    if clipped_road_length_m > 6000:
        return "Suburban" if nlcd_hint else "Rural"
    if clipped_road_length_m > 2500 and nlcd_hint:
        return "Suburban" if nlcd_hint != "Rural" else "Rural"
    if clipped_road_length_m > 1000 and nlcd_hint:
        return "Rural"

    return nlcd_hint or "Wilderness"


def _nlcd_hint_with_offset_fallback(lat: float, lon: float) -> Optional[str]:
    """
    Returns the NLCD-derived population hint, sampling offset points if the
    centre reads as water/wetland/unknown. Lets a point inside a lake adopt
    the land-cover hint of the surrounding shore.
    """
    nlcd_val = _get_nlcd_value(lat, lon)
    hint = NLCD_POP_HINT.get(nlcd_val)
    if hint or nlcd_val not in (None, 11, 12, 90, 95):
        return hint

    # Centre is water/wetland with no developed hint — look around 400m out.
    offset_m = 400
    dlat = offset_m / 111320.0
    dlon = offset_m / (111320.0 * max(math.cos(math.radians(lat)), 1e-3))

    counts: Dict[int, int] = {}
    for olat, olon in [(lat + dlat, lon), (lat - dlat, lon),
                       (lat, lon + dlon), (lat, lon - dlon)]:
        v = _get_nlcd_value(olat, olon)
        if v is not None and v != 11:  # skip more water samples
            counts[v] = counts.get(v, 0) + 1

    if not counts:
        return None
    dominant = max(counts.items(), key=lambda kv: kv[1])[0]
    return NLCD_POP_HINT.get(dominant)


LAST_POP_DEBUG: Dict[str, object] = {}


def classify_population(lat: float, lon: float) -> str:
    """Returns Wilderness | Rural | Suburban | Urban."""
    LAST_POP_DEBUG.clear()

    nlcd_hint = _nlcd_hint_with_offset_fallback(lat, lon)
    LAST_POP_DEBUG["nlcd_hint"] = nlcd_hint

    # Trust NLCD at the top end — NLCD 24 is reliable for high-density urban.
    if nlcd_hint == "Urban":
        LAST_POP_DEBUG["path"] = "nlcd_hint=Urban shortcut"
        return "Urban"

    # Primary signal: 500m radius around the point.
    time.sleep(1)  # gentle rate-limit
    b500_score, b500_count, r500 = _count_buildings_and_roads(lat, lon, 500)
    LAST_POP_DEBUG["b500_score"] = b500_score
    LAST_POP_DEBUG["b500_count"] = b500_count
    LAST_POP_DEBUG["r500"] = r500
    if b500_score is None:
        LAST_POP_DEBUG["path"] = "500m query failed"
        return nlcd_hint or "Wilderness"

    # Strong NLCD trust: if offset-sampled NLCD says Suburban, the surrounding
    # land cover is suburban. Only require minimal OSM evidence.
    if nlcd_hint == "Suburban" and (b500_score >= 0.4 or r500 > 500):
        if b500_count >= 250 or b500_score >= 70:
            LAST_POP_DEBUG["path"] = "NLCD-Suburban + high OSM → Urban"
            return "Urban"
        LAST_POP_DEBUG["path"] = "NLCD-Suburban + any OSM → Suburban"
        return "Suburban"

    result = _classify_from_signals(b500_score, r500, nlcd_hint, b500_count)
    LAST_POP_DEBUG["primary_result"] = result

    # Wider 1000m context — same OSM-based fallback as before.
    if result in ("Rural", "Wilderness"):
        time.sleep(1)
        b1000_score, b1000_count, r1000 = _count_buildings_and_roads(lat, lon, 1000)
        LAST_POP_DEBUG["b1000_score"] = b1000_score
        LAST_POP_DEBUG["b1000_count"] = b1000_count
        LAST_POP_DEBUG["r1000"] = r1000
        if b1000_count is not None:
            if b1000_count >= 30 and r1000 > 3500:
                LAST_POP_DEBUG["path"] = "wider: 30+ buildings + 3500m road → Suburban"
                return "Suburban"
            if b1000_count >= 60:
                LAST_POP_DEBUG["path"] = "wider: 60+ buildings → Suburban"
                return "Suburban"
            if nlcd_hint in ("Suburban", "Urban") and (
                b1000_count >= 5 or (r1000 or 0) > 2000
            ):
                LAST_POP_DEBUG["path"] = "wider: NLCD-developed + any OSM → Suburban"
                return "Suburban"
            if result == "Wilderness" and b1000_count >= 3:
                LAST_POP_DEBUG["path"] = "wider: Wilderness + 3+ buildings → Rural"
                return "Rural"

    LAST_POP_DEBUG["path"] = f"final fallthrough → {result}"
    return result

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze(lat: float, lon: float) -> dict:
    """
    Full analysis for a coordinate. Returns:
    {
      "coordinate": {"lat": float, "lon": float},
      "linear_features": {
          "Road":          {"distance_m": float|None, "name": str|None},
          "Trail":         ...,
          "Railroad":      ...,
          "Utility Line":  ...,
          "Water Feature": ...,
      },
      "surface":    str,   # what the point landed on
      "population": str,   # Wilderness/Rural/Suburban/Urban
    }
    """
    _validate_coord(lat, lon)
    print(f"Analyzing ({lat}, {lon})...")

    print("  Querying linear features...")
    linear = find_nearest_linear_features(lat, lon)

    print("  Identifying surface at point...")
    surface = get_surface_at_point(lat, lon)

    _zero_distances_by_threshold(linear)

    print("  Classifying population...")
    population = classify_population(lat, lon)

    return {
        "coordinate":      {"lat": lat, "lon": lon},
        "linear_features": linear,
        "surface":         surface,
        "_pop_debug":      dict(LAST_POP_DEBUG),
        "population":      population,
    }


def _zero_distances_by_threshold(linear: dict) -> None:
    """Zero out distances for features the point is physically on."""
    for feature, threshold in LINEAR_ZERO_THRESHOLDS.items():
        info = linear.get(feature, {})
        dist = info.get("distance_m")
        if dist is not None and dist <= threshold:
            linear[feature]["distance_m"] = 0.0
