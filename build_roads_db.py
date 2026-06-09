#!/usr/bin/env python3
"""
Fetch all WA DNR Active Roads from the state's public ArcGIS endpoint and
write them to a single pickle file shipped with the app. Replaces the
runtime cascade tier for WA_DNR with a local STRtree lookup.

Run periodically (monthly is plenty — WA DNR data doesn't change daily):
    python3 build_roads_db.py

Outputs: data/wa_dnr_roads.pkl  (~10-30 MB)

The ArcGIS endpoint caps responses at ~2000 features per query, so this
script subdivides Washington's bounding box recursively when it hits the
limit. Total fetch time is typically 5-20 minutes depending on network speed
and how busy the WA GIS server is.
"""
import os
import pickle
import sys
import time
from pathlib import Path
from typing import List, Tuple, Optional

import requests
from shapely.geometry import LineString

WADNR_URL = ("https://gis.dnr.wa.gov/site3/rest/services/"
             "Public_Transportation/WADNR_PUBLIC_ENG_Roads/MapServer/5/query")

# WA state bounding box (approximate, expanded slightly for safety)
WA_BBOX = (-124.9, 45.4, -116.8, 49.1)  # xmin, ymin, xmax, ymax (lon/lat)

MAX_RECORDS_GUESS = 2000  # WA DNR caps at 2000 per query
MAX_RECURSION_DEPTH = 7   # 4^7 = 16384 max tiles
TIMEOUT_S = 60

OUT_PATH = Path(__file__).parent / "data" / "wa_dnr_roads.pkl"

HEADERS = {
    "User-Agent": "geo_analyzer/1.1 (build script)",
}


def fetch_bbox(bbox: Tuple[float, float, float, float], depth: int = 0,
               retry: int = 0) -> List[dict]:
    """
    Fetch features in `bbox`. If the response says we hit the transfer limit,
    subdivide into 4 quadrants and recurse.
    """
    xmin, ymin, xmax, ymax = bbox
    indent = "  " * depth
    print(f"{indent}fetching [{xmin:.3f},{ymin:.3f} → {xmax:.3f},{ymax:.3f}] depth={depth}",
          flush=True)

    params = {
        "where": "1=1",
        "geometry": f"{xmin},{ymin},{xmax},{ymax}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "outSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "*",
        "returnGeometry": "true",
        "f": "geojson",
    }
    try:
        r = requests.get(WADNR_URL, params=params,
                         headers=HEADERS, timeout=TIMEOUT_S)
    except requests.exceptions.RequestException as e:
        if retry < 3:
            print(f"{indent}  retry {retry+1} after error: {e}", flush=True)
            time.sleep(2 ** retry)
            return fetch_bbox(bbox, depth, retry + 1)
        print(f"{indent}  FAILED after retries: {e}", flush=True)
        return []

    if r.status_code != 200:
        if retry < 3:
            time.sleep(2 ** retry)
            return fetch_bbox(bbox, depth, retry + 1)
        print(f"{indent}  HTTP {r.status_code}: skipping", flush=True)
        return []

    try:
        data = r.json()
    except Exception:
        return []

    features = data.get("features", []) or []
    exceeded = bool(data.get("exceededTransferLimit"))
    # Backup signal: if we returned exactly MAX_RECORDS_GUESS, server likely capped us
    if not exceeded and len(features) >= MAX_RECORDS_GUESS:
        exceeded = True

    print(f"{indent}  → {len(features)} features"
          f"{' (cap hit, subdividing)' if exceeded else ''}",
          flush=True)

    if exceeded and depth < MAX_RECURSION_DEPTH:
        xmid = (xmin + xmax) / 2
        ymid = (ymin + ymax) / 2
        sub_bboxes = [
            (xmin, ymin, xmid, ymid),
            (xmid, ymin, xmax, ymid),
            (xmin, ymid, xmid, ymax),
            (xmid, ymid, xmax, ymax),
        ]
        out: List[dict] = []
        for sb in sub_bboxes:
            out.extend(fetch_bbox(sb, depth + 1))
        return out

    return features


def feature_to_road(feat: dict) -> List[Tuple[LineString, dict]]:
    """Convert a GeoJSON feature into (LineString, metadata) tuples."""
    props = feat.get("properties") or {}
    geom = feat.get("geometry") or {}
    gtype = geom.get("type")

    if gtype == "LineString":
        coord_lists = [geom.get("coordinates") or []]
    elif gtype == "MultiLineString":
        coord_lists = geom.get("coordinates") or []
    else:
        return []

    name = (props.get("FULLNAME") or props.get("NAME") or
            props.get("RD_NM") or props.get("ROAD_NAME") or
            props.get("RD_NUM") or props.get("ROADNUM") or None)
    if name is not None:
        name = str(name).strip() or None

    out = []
    for coords in coord_lists:
        if len(coords) < 2:
            continue
        try:
            ls = LineString([(float(c[0]), float(c[1])) for c in coords])
        except Exception:
            continue
        out.append((ls, {"name": name, "source": "WA_DNR"}))
    return out


def main():
    print(f"Fetching WA DNR Active Roads → {OUT_PATH}")
    print(f"State bbox: {WA_BBOX}")
    start = time.time()

    raw = fetch_bbox(WA_BBOX)
    print(f"\nTotal raw features fetched: {len(raw)}")
    print(f"Fetch time: {(time.time() - start) / 60:.1f} min")

    print("Converting to LineStrings...")
    roads: List[Tuple[LineString, dict]] = []
    for feat in raw:
        roads.extend(feature_to_road(feat))
    print(f"Final road geometries: {len(roads)}")

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    print(f"Writing pickle → {OUT_PATH}")
    with open(OUT_PATH, "wb") as f:
        pickle.dump(roads, f, protocol=pickle.HIGHEST_PROTOCOL)

    size_mb = OUT_PATH.stat().st_size / (1024 * 1024)
    print(f"Done. File size: {size_mb:.1f} MB")
    print(f"Total elapsed: {(time.time() - start) / 60:.1f} min")


if __name__ == "__main__":
    main()
