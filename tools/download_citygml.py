#!/usr/bin/env python3
"""
download_citygml.py — fetch Thuringia 3D building models (CityGML LoD1 / LoD2)
for the Weimar Area of Interest.

Source: GDI-Th ATOM download service
  service feed : https://geoportal.geoportal-th.de/dienste/atom_th_gebaeude?type=service&id=12b7cb23-e5c6-4110-b42e-5389d10a6549
  dataset feed : ?type=dataset&id=97d152b8-9e00-49f3-9ae4-8bbb30873562

The dataset is distributed only as uniform 2x2 km tiles covering the whole
state (no Weimar-only bundle). Tile file name encodes the SW corner in km:
  LoD2_32_<E_km>_<N_km>_2_TH.zip   (32 = UTM zone, 2 = 2 km tile)
Each .zip (~8-9 MB) holds one CityGML `.gml` (~100+ MB uncased) + a `.txt`.
CRS: EPSG:25832 + DHHN2016 heights. Licence: dl-de/by-2-0, © GDI-Th.

This is the SOURCE geometry (for the eventual Grasshopper export and for making
our own 3D Tiles). CityGML is not directly displayable in Cesium — a conversion
step (citygml-tools / 3DCityDB / FME) comes later.

Stdlib only. Resumable. Writes a manifest CSV.

Usage:
  python tools/download_citygml.py                 # LoD2, Weimar AOI
  python tools/download_citygml.py --lod LoD1
  python tools/download_citygml.py --lod both
  python tools/download_citygml.py --dry-run
  python tools/download_citygml.py --bbox W S E N  # custom lon/lat box
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import time
import urllib.request
from pathlib import Path

AOI_DEFAULT = (11.2365, 50.91673, 11.40122, 51.03776)  # W, S, E, N  (DATA.md)

DATASET_FEED = (
    "https://geoportal.geoportal-th.de/dienste/atom_th_gebaeude"
    "?type=dataset&id=97d152b8-9e00-49f3-9ae4-8bbb30873562"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_ROOT = REPO_ROOT / "data" / "raw" / "citygml_th"

LINK_RE = re.compile(
    r'<link\s+rel="section"\s+href="([^"]+\.zip)"'
    r'(?:[^>]*?bbox="([^"]+)")?',
    re.IGNORECASE,
)


def fetch(url: str, timeout: int = 180) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "weimar-digital-twin/citygml"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def intersects(tile_bbox, aoi) -> bool:
    """tile_bbox = (minLat, minLon, maxLat, maxLon); aoi = (W, S, E, N)."""
    min_lat, min_lon, max_lat, max_lon = tile_bbox
    w, s, e, n = aoi
    return not (max_lon < w or min_lon > e or max_lat < s or min_lat > n)


def parse_feed(xml: str, lod: str, aoi):
    seen = set()
    for href, bbox_str in LINK_RE.findall(xml):
        if f"/{lod}/" not in href or href in seen or not bbox_str:
            continue
        seen.add(href)
        parts = [float(x) for x in bbox_str.split()]
        if len(parts) != 4:
            continue
        tb = tuple(parts)  # minLat, minLon, maxLat, maxLon
        if intersects(tb, aoi):
            yield href, tb


def run_one_lod(lod: str, aoi, feed: str, dry_run: bool, pause: float) -> int:
    out_dir = OUT_ROOT / lod
    out_dir.mkdir(parents=True, exist_ok=True)
    tiles = sorted(parse_feed(feed, lod, aoi))
    print(f"\n[{lod}] tiles intersecting AOI: {len(tiles)}  ->  {out_dir}")
    if not tiles:
        return 0

    rows, total, ok, skip, fail = [], 0, 0, 0, 0
    for i, (url, tb) in enumerate(tiles, 1):
        fname = url.rsplit("/", 1)[-1]
        dest = out_dir / fname
        size = dest.stat().st_size if dest.exists() else 0
        if dry_run:
            status = "dry-run"
        elif size > 0:
            status, skip = "skip (exists)", skip + 1
        else:
            try:
                data = fetch(url)
                dest.write_bytes(data)
                size, status, ok = len(data), "ok", ok + 1
                time.sleep(pause)
            except Exception as exc:  # noqa: BLE001
                status, fail = f"FAIL {exc}", fail + 1
        total += size
        rows.append({"file": fname, "min_lat": tb[0], "min_lon": tb[1],
                     "max_lat": tb[2], "max_lon": tb[3], "bytes": size,
                     "status": status, "url": url})
        print(f"  [{i:>3}/{len(tiles)}] {fname:<32} {status}", flush=True)

    with (out_dir / "_manifest.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"  [{lod}] ok {ok}, skipped {skip}, failed {fail}, {total/1e6:.1f} MB on disk")
    return fail


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lod", default="LoD2", choices=["LoD1", "LoD2", "both"])
    ap.add_argument("--bbox", nargs=4, type=float, metavar=("W", "S", "E", "N"), default=AOI_DEFAULT)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--pause", type=float, default=0.3)
    args = ap.parse_args()

    aoi = tuple(args.bbox)
    print(f"AOI (W,S,E,N): {aoi}")
    print("fetching dataset feed (~2 MB)…", flush=True)
    feed = fetch(DATASET_FEED).decode("utf-8", "replace")

    lods = ["LoD1", "LoD2"] if args.lod == "both" else [args.lod]
    failed = sum(run_one_lod(l, aoi, feed, args.dry_run, args.pause) for l in lods)
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
