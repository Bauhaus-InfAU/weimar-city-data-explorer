#!/usr/bin/env python3
"""
fetch_weimar_parcels.py — build a static GeoJSON of ALKIS cadastral parcels
(Flurstücke) for the Weimar AOI from the Thuringia INSPIRE ATOM download feed.

Source: GDI-Th INSPIRE-ATOMFeed "TH Flurstücke/Grundstücke ALKIS"
  service feed : https://www.geoproxy.geoportal-th.de/inspire-dl/atom/Service/Service_09ee6963-86ba-4568-9df8-f61b14dd617d.xml
  dataset feed : http://www.geoproxy.geoportal-th.de/inspire-dl/atom/DataSet/DataSet_0fe95724-6a09-47d3-afcb-d31d14a72598.xml
    (~5 MB; the geoproxy host is slow to build this response — took ~2 min in
    testing, hence the long timeout + retries below; it is NOT a broken link.)

Unlike the DGM/CityGML feeds (1x1 / 2x2 km grid tiles), this dataset is split
per **Gemarkung** (cadastral district) statewide, each offered in two parallel
CRS variants (EPSG:25832 and EPSG:4258) — we only need one, so the 25832 links
are used and the 4258 duplicates skipped. 45 Gemarkungen intersect the Weimar
AOI bbox (the 5 Weimar city Gemarkungen — Weimar/Ehringsdorf/Oberweimar/
Schöndorf/Tiefurt — plus the surrounding rural municipalities the AOI bbox
reaches into), each a separate .zip -> one INSPIRE CadastralParcels GML file.

GML shape: `cp:CadastralParcel` with `cp:geometry/gml:Surface/gml:patches/
gml:PolygonPatch/gml:exterior/gml:Ring`, where the ring is **not** one posList
but a chain of `gml:curveMember/gml:LineString` 2-point segments to be
concatenated in order (consecutive shared endpoints deduplicated).

Kept attributes: nationalCadastralReference, areaValue (m2), label.
CRS: source 25832, reprojected to 4326 for the web (same convention as every
other GeoJSON layer in this project). Every polygon feature is run through
tools/geojson_clean.clean_features() before being written, per project policy.

Usage:
  python tools/fetch_weimar_parcels.py               # fetch feed + all zips + convert
  python tools/fetch_weimar_parcels.py --skip-feed    # reuse the already-saved feed XML
  python tools/fetch_weimar_parcels.py --dry-run      # list the Gemarkungen, download nothing
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pyproj

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geojson_clean import clean_features  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "data" / "raw" / "alkis_parcels_th"
ZIP_DIR = RAW_DIR / "zips"
OUT = REPO_ROOT / "web" / "layers" / "data" / "weimar_parcels.geojson"

DATASET_FEED_URL = (
    "http://www.geoproxy.geoportal-th.de/inspire-dl/atom/DataSet/"
    "DataSet_0fe95724-6a09-47d3-afcb-d31d14a72598.xml"
)
FEED_CACHE = RAW_DIR / "DataSet_feed.xml"

# Weimar AOI (lon/lat, EPSG:4326) — DATA.md "Область интереса (AOI) — Weimar"
AOI = (11.2365, 50.91673, 11.40122, 51.03776)  # west, south, east, north

ENTRY_RE = re.compile(
    r'<entry>.*?<title>([^<]*)</title>.*?'
    r'<link href="([^"]+)"[^>]*bbox="([^"]+)"[^>]*>.*?</entry>',
    re.S,
)

NS = {
    "gml": "http://www.opengis.net/gml/3.2",
    "cp": "http://inspire.ec.europa.eu/schemas/cp/4.0",
}

TRANSFORM = pyproj.Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)


def fetch(url: str, timeout: int, retries: int = 3, pause: float = 5.0) -> bytes:
    last_exc = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "weimar-digital-twin/parcels"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            print(f"    attempt {attempt}/{retries} failed ({exc}); retrying in {pause}s…", file=sys.stderr)
            time.sleep(pause)
    raise last_exc


def get_dataset_feed(skip_feed: bool) -> str:
    if skip_feed and FEED_CACHE.exists():
        print(f"reusing cached feed: {FEED_CACHE}")
        return FEED_CACHE.read_text(encoding="utf-8")
    print("fetching dataset feed (~5 MB, the geoproxy host is slow — allow ~2 min)…", flush=True)
    xml = fetch(DATASET_FEED_URL, timeout=240, retries=3, pause=10).decode("utf-8", "replace")
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    FEED_CACHE.write_text(xml, encoding="utf-8")
    return xml


def gemarkungen_in_aoi(xml: str, aoi) -> list[tuple[str, str]]:
    """Return [(title, zip_url)] for 25832-CRS entries whose bbox intersects the AOI."""
    w, s, e, n = aoi
    seen = set()
    hits = []
    for title, href, bbox in ENTRY_RE.findall(xml):
        if "/25832/" not in href or href in seen:
            continue
        parts = [float(x) for x in bbox.split()]
        if len(parts) != 4:
            continue
        min_lat, min_lon, max_lat, max_lon = parts
        if max_lon < w or min_lon > e or max_lat < s or min_lat > n:
            continue
        seen.add(href)
        hits.append((title, href))
    return hits


def download_zip(url: str) -> Path:
    ZIP_DIR.mkdir(parents=True, exist_ok=True)
    fname = url.rsplit("/", 1)[-1]
    dest = ZIP_DIR / fname
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    data = fetch(url, timeout=120, retries=3, pause=5)
    dest.write_bytes(data)
    return dest


def ring_from_curve_members(ring_el) -> list[tuple[float, float]]:
    """Concatenate the ordered gml:curveMember/gml:LineString posLists of a Ring
    into one closed list of (E, N) points, dropping the duplicate shared vertex
    between consecutive segments."""
    pts: list[tuple[float, float]] = []
    for curve in ring_el.findall("gml:curveMember", NS):
        ls = curve.find("gml:LineString", NS)
        if ls is None:
            continue
        poslist = ls.find("gml:posList", NS)
        if poslist is None or not poslist.text:
            continue
        nums = [float(v) for v in poslist.text.split()]
        seg = [(nums[i], nums[i + 1]) for i in range(0, len(nums), 2)]
        if pts and seg and pts[-1] == seg[0]:
            seg = seg[1:]
        pts.extend(seg)
    return pts


def parse_parcels(gml_bytes: bytes, aoi) -> list[dict]:
    """Parse cp:CadastralParcel features from one Gemarkung GML file into
    GeoJSON features in EPSG:4326, keeping only those whose bbox intersects the
    (lon/lat) AOI. Uses iterparse to avoid holding the whole (tens-of-MB) tree."""
    w, s, e, n = aoi
    features = []
    context = ET.iterparse(__import__("io").BytesIO(gml_bytes), events=("end",))
    for _, elem in context:
        if not elem.tag.endswith("}CadastralParcel"):
            continue
        try:
            ring_el = elem.find(".//cp:geometry//gml:exterior/gml:Ring", NS)
            if ring_el is None:
                continue
            pts_en = ring_from_curve_members(ring_el)
            if len(pts_en) < 4:
                continue
            if pts_en[0] != pts_en[-1]:
                pts_en.append(pts_en[0])

            lonlat = [list(TRANSFORM.transform(e_, n_)) for e_, n_ in pts_en]
            lons = [p[0] for p in lonlat]
            lats = [p[1] for p in lonlat]
            if max(lons) < w or min(lons) > e or max(lats) < s or min(lats) > n:
                continue

            ref_el = elem.find("cp:nationalCadastralReference", NS)
            area_el = elem.find("cp:areaValue", NS)
            label_el = elem.find("cp:label", NS)
            label = label_el.text if label_el is not None else None
            if label:
                # Source data spells the parcel-number slash out as the literal
                # word "<solidus>" (e.g. "31<solidus>5" for the usual "31/5")
                # instead of an actual "/" character — restore the normal form.
                label = label.replace("<solidus>", "/")

            props = {
                "nationalCadastralReference": ref_el.text if ref_el is not None else None,
                "areaValue_m2": float(area_el.text) if area_el is not None and area_el.text else None,
                "label": label,
                "licence": "dl-de/by-2-0, © GDI-Th",
            }
            features.append({
                "type": "Feature",
                "properties": props,
                "geometry": {"type": "Polygon", "coordinates": [[[round(x, 7), round(y, 7)] for x, y in lonlat]]},
            })
        finally:
            elem.clear()
    return features


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-feed", action="store_true", help="reuse cached DataSet_feed.xml")
    ap.add_argument("--dry-run", action="store_true", help="list Gemarkungen, download nothing")
    ap.add_argument("--pause", type=float, default=0.5, help="seconds between zip downloads")
    args = ap.parse_args()

    xml = get_dataset_feed(args.skip_feed)
    hits = gemarkungen_in_aoi(xml, AOI)
    print(f"Gemarkungen intersecting AOI (EPSG:25832 links): {len(hits)}")
    for title, href in hits:
        print(f"  {title:<30} {href}")
    if args.dry_run:
        return 0

    all_features: list[dict] = []
    total_bytes = 0
    for i, (title, url) in enumerate(hits, 1):
        print(f"[{i:>2}/{len(hits)}] {title} …", flush=True)
        try:
            zpath = download_zip(url)
        except Exception as exc:  # noqa: BLE001
            print(f"    FAILED to download: {exc}", file=sys.stderr)
            continue
        total_bytes += zpath.stat().st_size
        with zipfile.ZipFile(zpath) as z:
            gml_name = next(n for n in z.namelist() if n.lower().endswith(".gml"))
            gml_bytes = z.read(gml_name)
        feats = parse_parcels(gml_bytes, AOI)
        print(f"    {len(feats)} parcels kept (of the Gemarkung's total)")
        all_features.extend(feats)
        time.sleep(args.pause)

    print(f"\ntotal zips on disk: {total_bytes / 1e6:.1f} MB")
    print(f"parcels before cleaning: {len(all_features)}")

    kept, summary = clean_features(all_features, label="weimar_parcels")

    fc = {"type": "FeatureCollection", "features": kept}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    import json
    OUT.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {OUT} — {len(kept)} parcels, {OUT.stat().st_size / 1e6:.1f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
