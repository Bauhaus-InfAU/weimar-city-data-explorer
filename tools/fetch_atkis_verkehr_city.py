#!/usr/bin/env python3
"""
fetch_atkis_verkehr_city.py — ATKIS Basis-DLM "Verkehr" (atkis_thema:ATKIS_VERKEHR) for the WHOLE city, straight from
the WFS, for the city-wide Space Syntax networks.

Why: the TASK_07 source was downloaded by hand (8 rectangles through the OpenData Download Client) and misses the
eastern half of Ortsteil Taubach (~1 300 buildings, no street axis at all). The WFS behind that client
(adv_atkis_thema_wfs on geoproxy) has no captcha, so the whole city bbox is fetched here, paged.

Output goes to data/processed/atkis_verkehr_city/weimar_atkis_<objektart>.geojson — a SEPARATE set: the platform
file of that type unchanged (her QGIS edits included) + the WFS features whose gmlId it lacks (`addedFromWfs`). The
platform's web/layers/data/weimar_atkis_*.geojson (design layers, TASK_07/31) are not touched. Same parsing, same
keep-if-it-touches-the-boundary rule and same geojson_clean pass as fetch_weimar_atkis_verkehr.py.
Raw WFS pages: data/raw/atkis_verkehr_th_city/page_<n>.xml (reused on rerun; --refresh downloads again).

Run:  python tools/fetch_atkis_verkehr_city.py [--refresh]
"""
import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path
from xml.etree import ElementTree as ET

from shapely.geometry import shape

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fetch_weimar_atkis_verkehr as fv  # noqa: E402  (parser + boundary, reused)
from geojson_clean import clean_features  # noqa: E402

ROOT = fv.REPO_ROOT
RAW = ROOT / "data" / "raw" / "atkis_verkehr_th_city"
OUT = ROOT / "data" / "processed" / "atkis_verkehr_city"
WFS_URL = "https://www.geoproxy.geoportal-th.de/geoproxy/services/adv_atkis_thema_wfs"
PAGE = 2000


def city_bbox_25832():
    from pyproj import Transformer
    from shapely.ops import transform
    geom, _ = fv.load_boundary()
    to_utm = Transformer.from_crs(4326, 25832, always_xy=True).transform
    x0, y0, x1, y1 = transform(to_utm, geom).bounds
    return int(x0) - 200, int(y0) - 200, int(x1) + 200, int(y1) + 200


def fetch_pages(refresh):
    RAW.mkdir(parents=True, exist_ok=True)
    bbox = ",".join(map(str, city_bbox_25832())) + ",urn:ogc:def:crs:EPSG::25832"
    base = (f"{WFS_URL}?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature&TYPENAMES=atkis_thema:ATKIS_VERKEHR"
            f"&BBOX={bbox}")
    hits = urllib.request.urlopen(base + "&RESULTTYPE=hits", timeout=180).read().decode()
    matched = int(re.search(r'numberMatched="(\d+)"', hits).group(1))
    print(f"WFS: {matched} features in the city bbox {bbox}")
    pages = []
    for n, start in enumerate(range(0, matched, PAGE)):
        p = RAW / f"page_{n:03d}.xml"
        if refresh or not p.exists():
            url = base + f"&COUNT={PAGE}&STARTINDEX={start}&SORTBY=gml:id"
            p.write_bytes(urllib.request.urlopen(url, timeout=600).read())
            print(f"  page {n}: {p.stat().st_size // 1024} KB")
        pages.append(p)
    (RAW / "request.json").write_text(json.dumps({"url": base, "numberMatched": matched, "pageSize": PAGE}, indent=1))
    return pages, matched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()
    pages, matched = fetch_pages(args.refresh)
    _geom, bprep = fv.load_boundary()
    groups, seen = {}, set()
    n_total = n_dup = n_nogeom = n_outside = 0
    for p in pages:
        with open(p, "rb") as f:
            for _, elem in ET.iterparse(f, events=("end",)):
                if elem.tag != fv.FEATURE_TAG:
                    continue
                n_total += 1
                gid = elem.get(fv.GML + "id")
                if gid in seen:
                    n_dup += 1; elem.clear(); continue
                seen.add(gid)
                objektart = elem.findtext(fv.A + "ObjektartText") or "UNKNOWN"
                geom_el = elem.find(fv.GEOM_TAG)
                geom = fv.parse_geometry(geom_el) if geom_el is not None else None
                if geom is None:
                    n_nogeom += 1; elem.clear(); continue
                if not bprep.intersects(shape(geom)):
                    n_outside += 1; elem.clear(); continue
                props = fv.scalar_props(elem)
                props["ObjektartText"] = objektart
                if gid:
                    props.setdefault("gmlId", gid)
                groups.setdefault(objektart, []).append({"type": "Feature", "properties": props, "geometry": geom})
                elem.clear()
    print(f"{n_total} parsed ({matched} matched) | {n_dup} duplicates across pages | {n_nogeom} no geometry | "
          f"{n_outside} outside the boundary | {len(seen) - n_nogeom - n_outside} kept")
    if len(seen) != matched:
        print(f"WARNING: {len(seen)} unique ids != {matched} matched — paging lost features", file=sys.stderr)
    OUT.mkdir(parents=True, exist_ok=True)
    for objektart in sorted(groups):
        kept, _ = clean_features(groups[objektart], label=objektart)
        # The platform file of the same type is the truth wherever it has a feature: her QGIS edits (folder
        # "New roads", September) split/reshaped WFS features under the same gmlId and added new ones without a
        # gmlId. So: every platform feature as is + only those WFS features whose gmlId the platform file lacks.
        platform = fv.OUT_DIR / f"weimar_atkis_{fv.slug(objektart)}.geojson"
        base = json.loads(platform.read_text(encoding="utf-8"))["features"] if platform.exists() else []
        have = {f["properties"].get("gmlId") for f in base}
        added = [dict(f, properties=dict(f["properties"], addedFromWfs=True))
                 for f in kept if f["properties"].get("gmlId") not in have]
        out = OUT / f"weimar_atkis_{fv.slug(objektart)}.geojson"
        out.write_text(json.dumps({"type": "FeatureCollection", "features": base + added}, ensure_ascii=False),
                       encoding="utf-8")
        print(f"  -> {out.name:<44} {len(base):5d} platform + {len(added)} added from WFS")


if __name__ == "__main__":
    main()
