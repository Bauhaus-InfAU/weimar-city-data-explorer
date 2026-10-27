#!/usr/bin/env python3
"""
download_dgm.py — fetch Thuringia DGM (digital terrain model) tiles for the
Weimar Area of Interest.

Source: GDI-Th ATOM download service
  service feed : https://geoportal.geoportal-th.de/dienste/atom_th_hoehendaten_dgm
  dataset feed : ?type=dataset&id=14418d25-fcd7-4a3f-99a9-e3059a2772af

The dataset feed lists ~17 000 tiles (1x1 km, EPSG:25832) in three vintages:
  dgm_2010-2013 : 2 m raster, whole state
  dgm_2014-2019 : 1 m raster, whole state   <-- default here (complete + high res)
  dgm_2020-2025 : 1 m raster, still being built

Each tile is a .zip holding an ASCII `.xyz` grid ("E N Z" per line, 1 m spacing,
1 000 000 points) plus a `.meta` header (acquisition date, CRS, accuracy).
Height CRS is EPSG:5783 (DHHN2016 normal heights, GCG2011 quasigeoid).
Licence: Datenlizenz Deutschland – Namensnennung 2.0, © GDI-Th.

No third-party deps — standard library only. Safe to re-run: existing files are
skipped. Writes a manifest CSV next to the tiles.

Usage:
  python tools/download_dgm.py                 # AOI Weimar, vintage 2014-2019
  python tools/download_dgm.py --vintage dgm_2010-2013
  python tools/download_dgm.py --dry-run       # list tiles, download nothing
  python tools/download_dgm.py --bbox W S E N  # custom lon/lat box
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
import urllib.request
from pathlib import Path

# Weimar AOI (lon/lat, EPSG:4326) — from DATA.md "Область интереса (AOI) — Weimar"
AOI_DEFAULT = (11.2365, 50.91673, 11.40122, 51.03776)  # west, south, east, north

DATASET_FEED = (
    "https://geoportal.geoportal-th.de/dienste/atom_th_hoehendaten_dgm"
    "?type=dataset&id=14418d25-fcd7-4a3f-99a9-e3059a2772af"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = REPO_ROOT / "data" / "raw" / "dgm_th"

# <link rel="section" href="...zip" ... title="663_5650_1x1km"
#        bbox="minLat minLon maxLat maxLon"/>
LINK_RE = re.compile(
    r'<link\s+rel="section"\s+href="([^"]+\.zip)"'
    r'[^>]*?title="([^"]*)"'
    r'[^>]*?bbox="([^"]+)"',
    re.IGNORECASE,
)


def fetch(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "weimar-digital-twin/dgm"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def intersects(tile_bbox, aoi) -> bool:
    """tile_bbox = (minLat, minLon, maxLat, maxLon); aoi = (W, S, E, N)."""
    min_lat, min_lon, max_lat, max_lon = tile_bbox
    w, s, e, n = aoi
    return not (max_lon < w or min_lon > e or max_lat < s or min_lat > n)


def parse_feed(xml: str, vintage: str, aoi):
    """Yield (url, name, tile_bbox) for tiles of the chosen vintage inside the AOI."""
    seen = set()
    for href, title, bbox_str in LINK_RE.findall(xml):
        if f"/{vintage}/" not in href or href in seen:
            continue
        seen.add(href)
        parts = [float(x) for x in bbox_str.split()]
        if len(parts) != 4:
            continue
        tile_bbox = tuple(parts)  # minLat, minLon, maxLat, maxLon
        if intersects(tile_bbox, aoi):
            yield href, title, tile_bbox


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vintage", default="dgm_2014-2019",
                    choices=["dgm_2010-2013", "dgm_2014-2019", "dgm_2020-2025"])
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"),
                    default=AOI_DEFAULT, help="lon/lat AOI (default: Weimar)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pause", type=float, default=0.3, help="seconds between downloads")
    args = ap.parse_args()

    aoi = tuple(args.bbox)
    out_dir = OUT_ROOT / args.vintage
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"AOI (W,S,E,N) : {aoi}")
    print(f"vintage       : {args.vintage}")
    print(f"target dir    : {out_dir}")
    print("fetching dataset feed (~13 MB)…", flush=True)
    feed = fetch(DATASET_FEED).decode("utf-8", "replace")

    tiles = sorted(parse_feed(feed, args.vintage, aoi))
    print(f"tiles intersecting AOI: {len(tiles)}")
    if not tiles:
        print("nothing to do", file=sys.stderr)
        return 1

    manifest = out_dir / "_manifest.csv"
    rows = []
    total_bytes = 0
    downloaded = skipped = failed = 0

    for i, (url, title, tb) in enumerate(tiles, 1):
        fname = url.rsplit("/", 1)[-1]
        dest = out_dir / fname
        status = ""
        size = dest.stat().st_size if dest.exists() else 0

        if args.dry_run:
            status = "dry-run"
        elif dest.exists() and size > 0:
            status = "skip (exists)"
            skipped += 1
        else:
            try:
                data = fetch(url)
                dest.write_bytes(data)
                size = len(data)
                status = "ok"
                downloaded += 1
                time.sleep(args.pause)
            except Exception as exc:  # noqa: BLE001
                status = f"FAIL {exc}"
                failed += 1

        total_bytes += size
        rows.append({
            "tile": title, "file": fname,
            "min_lat": tb[0], "min_lon": tb[1], "max_lat": tb[2], "max_lon": tb[3],
            "bytes": size, "status": status, "url": url,
        })
        print(f"[{i:>3}/{len(tiles)}] {fname:<38} {status}", flush=True)

    with manifest.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print("-" * 60)
    print(f"downloaded {downloaded}, skipped {skipped}, failed {failed}")
    print(f"total on disk: {total_bytes / 1e6:.1f} MB")
    print(f"manifest: {manifest}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
