#!/usr/bin/env python3
"""
Take the raw WA DNR pickle and produce a much smaller, deployable version:
  1. Simplify each LineString geometry with a ~5m tolerance (well below our
     cascade's 200m threshold, so accuracy impact is negligible).
  2. Strip metadata to just (name, source).
  3. Gzip-compress the output.

Input:  data/wa_dnr_roads.pkl       (raw, from build_roads_db.py)
Output: data/wa_dnr_roads.pkl.gz    (committed to the repo)
"""
import gzip
import pickle
import sys
import time
from pathlib import Path

from shapely.geometry import LineString

ROOT = Path(__file__).parent
IN  = ROOT / "data" / "wa_dnr_roads.pkl"
OUT = ROOT / "data" / "wa_dnr_roads.pkl.gz"

# Douglas-Peucker tolerance in degrees. 0.00005 ≈ ~5.5m at WA latitudes.
# Our cascade only checks "is the road within 200m", so 5m simplification
# is well inside the noise floor of that decision.
SIMPLIFY_TOLERANCE = 0.00005


def main():
    if not IN.exists():
        print(f"ERROR: {IN} not found — run build_roads_db.py first.", file=sys.stderr)
        sys.exit(1)

    print(f"Loading {IN} ({IN.stat().st_size / 1024 / 1024:.1f} MB)...")
    t0 = time.time()
    with open(IN, "rb") as f:
        roads = pickle.load(f)
    print(f"  loaded {len(roads):,} roads in {time.time()-t0:.1f}s")

    print(f"\nSimplifying geometries (tolerance ≈ 5.5m)...")
    t0 = time.time()
    out = []
    dropped_small = 0
    dropped_bad = 0
    total_points_before = 0
    total_points_after = 0
    for ls, meta in roads:
        try:
            total_points_before += len(ls.coords)
            simplified = ls.simplify(SIMPLIFY_TOLERANCE, preserve_topology=False)
            if simplified.geom_type != "LineString":
                dropped_bad += 1
                continue
            if len(simplified.coords) < 2:
                dropped_small += 1
                continue
            total_points_after += len(simplified.coords)
            out.append((simplified, {
                "name": meta.get("name"),
                "source": meta.get("source", "WA_DNR"),
            }))
        except Exception:
            dropped_bad += 1
    print(f"  simplified {len(out):,} roads in {time.time()-t0:.1f}s")
    print(f"  dropped {dropped_small} too-small, {dropped_bad} bad-geometry")
    print(f"  vertex reduction: {total_points_before:,} → {total_points_after:,} "
          f"({100 * (1 - total_points_after / max(1, total_points_before)):.1f}% fewer)")

    print(f"\nWriting gzipped pickle → {OUT}...")
    t0 = time.time()
    with gzip.open(OUT, "wb", compresslevel=9) as f:
        pickle.dump(out, f, protocol=pickle.HIGHEST_PROTOCOL)
    print(f"  wrote in {time.time()-t0:.1f}s")

    size_mb = OUT.stat().st_size / (1024 * 1024)
    print(f"\nFinal file size: {size_mb:.1f} MB")
    if size_mb > 95:
        print(f"WARNING: still over GitHub's ~100 MB limit. May need LFS or "
              f"a coarser SIMPLIFY_TOLERANCE.", file=sys.stderr)


if __name__ == "__main__":
    main()
