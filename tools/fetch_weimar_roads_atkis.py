#!/usr/bin/env python3
"""
fetch_weimar_roads_atkis.py — build the real Weimar street network from the
INSPIRE TH "Verkehrsnetze ATKIS Basis-DLM" ATOM feed (TASK_05b correction).

TASK_05 Part 3 originally concluded "no WFS exists for the road network" —
that was wrong, just the wrong service name was tried. The correct source
(found and given in TASK_05b):

  service feed : https://www.geoproxy.geoportal-th.de/inspire-dl/atom/Service/Service_52cd670c-aa20-4f6c-80cb-6fb14df8ae67.xml
  dataset feed : http://www.geoproxy.geoportal-th.de/inspire-dl/atom/DataSet/DataSet_e98cb550-e246-4b9b-8a29-49029a51f5cc.xml

The dataset feed is small (~11 KB) and fast — it is NOT tiled by Gemarkung or
grid like the parcels/CityGML/DGM feeds: it lists exactly one .zip per network
type, **statewide, no AOI split possible**:

  tn-a  Flugverkehrsnetz (air)
  tn-c  Seilbahnverkehrsnetz (cable)
  tn-ra Schienenverkehrsnetz (rail)
  tn-ro Straßenverkehrsnetz (road)   <-- this script
  tn-w  Wasserstraßenverkehrsnetz (waterway)

Each offered in both EPSG:25832 and EPSG:4258 (parallel, pick one — 25832
used here). `tn-ro_25832.zip` is 92.8 MB zipped -> **1.84 GB** of GML
uncompressed. Since there is no finer split to fetch, the whole statewide zip
is downloaded once (92.8 MB is a normal one-time download, not the
"gigabytes, bail out" case the task warns about) and then **streamed**
(`xml.etree.ElementTree.iterparse`, never materialising the 1.84 GB tree)
straight out of the zip, filtering to the Weimar AOI as we go — nothing
Weimar-irrelevant is ever held in memory.

GML structure (INSPIRE tn-ro 4.0):
  - `tn-ro:RoadLink` — the actual centreline segments (geometry +
    `net:fictitious`), **every real street is one of these**, unlike
    `Klassifiz_StrNetz` which only carries classified routes. No name/class
    attributes live on RoadLink itself.
  - `tn-ro:Road` — a named/classified grouping of RoadLinks: carries
    `tn:geographicalName` (street name), `tn-ro:nationalRoadCode` (e.g. "B62",
    "L3247" — empty for unclassified local streets), `tn-ro:localRoadCode`,
    and a list of `net:link[@xlink:href="#RoadLink_..."]` references to its
    member RoadLinks. Many RoadLinks (typically minor streets) belong to no
    Road at all — those just get null name/code fields.

Two-pass streaming approach (each pass ~2 minutes on the full 1.84 GB file):
  1. Stream all `RoadLink`, keep only those whose raw EPSG:25832 bbox
     intersects the Weimar AOI (checked in map units before reprojecting —
     cheap). Result: a few thousand kept links out of ~536 000 statewide.
  2. Stream all `Road`, and for every kept link id referenced by a Road's
     `net:link`, attach that Road's name/codes to the kept link's properties.

Reprojected 25832->4326 (project convention). Attributes kept: fictitious,
name (from Road geographicalName), nationalRoadCode, localRoadCode.

Usage:
  python tools/fetch_weimar_roads_atkis.py               # download + convert
  python tools/fetch_weimar_roads_atkis.py --skip-download  # reuse the zip already in data/raw
"""

from __future__ import annotations

import argparse
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pyproj

sys.path.insert(0, str(Path(__file__).resolve().parent))
from geojson_clean import clean_features  # noqa: E402 (no-op for lines; kept for a consistent pipeline)

REPO_ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = REPO_ROOT / "data" / "raw" / "atkis_roads_th"
ZIP_PATH = RAW_DIR / "tn-ro_25832.zip"
GML_NAME_HINT = "tn-ro_EPSG_25832.gml"  # actual name has a date prefix, matched via suffix
OUT = REPO_ROOT / "web" / "layers" / "data" / "weimar_roads.geojson"

ZIP_URL = "http://www.geoproxy.geoportal-th.de/download-service/inspire/annex1/tn/tn-ro_25832.zip"

# Weimar AOI (lon/lat, EPSG:4326) — DATA.md; reprojected to 25832 once, used
# for a cheap raw-coordinate bbox filter before any reprojection happens.
AOI_LONLAT = (11.2365, 50.91673, 11.40122, 51.03776)  # west, south, east, north

NET = "{http://inspire.ec.europa.eu/schemas/net/4.0}"
GML = "{http://www.opengis.net/gml/3.2}"
TNRO = "{http://inspire.ec.europa.eu/schemas/tn-ro/4.0}"
TN = "{http://inspire.ec.europa.eu/schemas/tn/4.0}"
GN = "{http://inspire.ec.europa.eu/schemas/gn/4.0}"
XLINK = "{http://www.w3.org/1999/xlink}"

TO_25832 = pyproj.Transformer.from_crs("EPSG:4326", "EPSG:25832", always_xy=True)
TO_4326 = pyproj.Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)


def aoi_bbox_25832() -> tuple[float, float, float, float]:
    w, s, e, n = AOI_LONLAT
    corners = [TO_25832.transform(x, y) for x, y in [(w, s), (e, n), (w, n), (e, s)]]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return min(xs), min(ys), max(xs), max(ys)


def download_zip() -> Path:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    if ZIP_PATH.exists() and ZIP_PATH.stat().st_size > 0:
        print(f"reusing {ZIP_PATH} ({ZIP_PATH.stat().st_size / 1e6:.1f} MB)")
        return ZIP_PATH
    print(f"downloading {ZIP_URL} (~93 MB, one-time — statewide file, no finer tiling exists)…")
    req = urllib.request.Request(ZIP_URL, headers={"User-Agent": "weimar-digital-twin/roads-atkis"})
    with urllib.request.urlopen(req, timeout=300) as r, open(ZIP_PATH, "wb") as out:
        out.write(r.read())
    print(f"saved {ZIP_PATH} ({ZIP_PATH.stat().st_size / 1e6:.1f} MB)")
    return ZIP_PATH


def open_gml_stream(zpath: Path):
    z = zipfile.ZipFile(zpath)
    name = next(n for n in z.namelist() if n.endswith(".gml"))
    return z.open(name)


def pass1_collect_links(zpath: Path, bbox) -> dict:
    """Stream all RoadLink; keep those whose raw 25832 posList bbox intersects
    the Weimar AOI bbox. Returns {link_id: {"coords_en": [...], "fictitious": bool}}."""
    minx, miny, maxx, maxy = bbox
    kept = {}
    n_total = 0
    t0 = time.time()
    f = open_gml_stream(zpath)
    for _, elem in ET.iterparse(f, events=("end",)):
        if elem.tag != TNRO + "RoadLink":
            continue
        n_total += 1
        geom = elem.find(f"{NET}centrelineGeometry/{GML}LineString/{GML}posList")
        if geom is not None and geom.text:
            nums = [float(v) for v in geom.text.split()]
            pts = [(nums[i], nums[i + 1]) for i in range(0, len(nums), 2)]
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            if not (max(xs) < minx or min(xs) > maxx or max(ys) < miny or min(ys) > maxy):
                link_id = elem.get(f"{GML}id")
                fict = elem.find(f"{NET}fictitious")
                kept[link_id] = {
                    "coords_en": pts,
                    "fictitious": (fict.text == "true") if fict is not None else None,
                }
        elem.clear()
        if n_total % 100000 == 0:
            print(f"  pass1: {n_total} RoadLink scanned, {len(kept)} kept so far ({time.time()-t0:.0f}s)")
    print(f"pass1 done: {n_total} RoadLink total, {len(kept)} intersect the Weimar AOI ({time.time()-t0:.0f}s)")
    return kept


def pass2_join_road_attrs(zpath: Path, kept_ids: set) -> dict:
    """Stream all Road; for every net:link referencing a kept RoadLink id,
    attach that Road's name/codes. Returns {link_id: {name, nationalRoadCode, localRoadCode}}."""
    attrs = {}
    n_roads = 0
    t0 = time.time()
    f = open_gml_stream(zpath)
    for _, elem in ET.iterparse(f, events=("end",)):
        if elem.tag != TNRO + "Road":
            continue
        n_roads += 1
        refs = [
            l.get(f"{XLINK}href", "").lstrip("#")
            for l in elem.findall(f"{NET}link")
        ]
        relevant = [r for r in refs if r in kept_ids]
        if relevant:
            name_el = elem.find(f"{TN}geographicalName/{GN}GeographicalName/{GN}spelling/{GN}SpellingOfName/{GN}text")
            nrc_el = elem.find(TNRO + "nationalRoadCode")
            lrc_el = elem.find(TNRO + "localRoadCode")
            info = {
                "name": name_el.text if name_el is not None else None,
                "nationalRoadCode": nrc_el.text if nrc_el is not None else None,
                "localRoadCode": lrc_el.text if lrc_el is not None else None,
            }
            for r in relevant:
                attrs[r] = info
        elem.clear()
        if n_roads % 200000 == 0:
            print(f"  pass2: {n_roads} Road scanned ({time.time()-t0:.0f}s)")
    print(f"pass2 done: {n_roads} Road total, {len(attrs)} kept links matched to a named Road ({time.time()-t0:.0f}s)")
    return attrs


def to_features(links: dict, road_attrs: dict) -> list[dict]:
    features = []
    for link_id, info in links.items():
        lonlat = [[round(x, 7), round(y, 7)] for x, y in (TO_4326.transform(e, n) for e, n in info["coords_en"])]
        extra = road_attrs.get(link_id, {})
        props = {
            "roadLinkId": link_id,
            "fictitious": info["fictitious"],
            "name": extra.get("name"),
            "nationalRoadCode": extra.get("nationalRoadCode"),
            "localRoadCode": extra.get("localRoadCode"),
            "licence": "dl-de/by-2-0, © GDI-Th",
        }
        features.append({
            "type": "Feature",
            "properties": props,
            "geometry": {"type": "LineString", "coordinates": lonlat},
        })
    return features


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skip-download", action="store_true", help="reuse the zip already in data/raw")
    args = ap.parse_args()

    zpath = ZIP_PATH if args.skip_download else download_zip()
    if args.skip_download and not zpath.exists():
        print(f"--skip-download given but {zpath} doesn't exist", file=sys.stderr)
        return 1

    bbox = aoi_bbox_25832()
    print(f"Weimar AOI in EPSG:25832: {bbox}")

    links = pass1_collect_links(zpath, bbox)
    road_attrs = pass2_join_road_attrs(zpath, set(links.keys()))

    features = to_features(links, road_attrs)
    named = sum(1 for f in features if f["properties"]["name"])
    print(f"{len(features)} road-link features, {named} with a Road name/classification joined")

    kept, summary = clean_features(features, label="weimar_roads_atkis")

    fc = {"type": "FeatureCollection", "features": kept}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    import json
    OUT.write_text(json.dumps(fc, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {OUT} — {len(kept)} segments, {OUT.stat().st_size / 1e6:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
