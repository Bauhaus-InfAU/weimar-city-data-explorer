#!/usr/bin/env python3
"""
build_building_footprints.py — TASK_30: flat "Building footprints" layer.

Reads web/layers/data/weimar_buildings_footprint_height.geojson (already enriched by
bake_alkis_attrs.py) and writes web/layers/data/weimar_building_footprints.geojson:
same polygons and buildingId, attributes usage / height / floors (empty stays absent)
plus area (m2) and areaSource.

area: the simplified ALKIS WFS (ave:GebaeudeBauwerk) carries no area field (only oid,
aktualit, gebnutzbez, funktion, anzahlgs), so no official value exists -> every area is
computed in EPSG:25832 from the geometry, areaSource = "computed".
Output goes through geojson_clean.clean_features().
Rebuild: python tools/build_building_footprints.py
"""
import json, sys
from pathlib import Path
from pyproj import Transformer
from shapely.geometry import shape
from shapely.ops import transform

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from geojson_clean import clean_features  # noqa: E402

DATA = ROOT / "web" / "layers" / "data"
SRC, DST = DATA / "weimar_buildings_footprint_height.geojson", DATA / "weimar_building_footprints.geojson"
tr = Transformer.from_crs(4326, 25832, always_xy=True).transform

def main():
    fc = json.load(open(SRC, encoding="utf-8"))
    out = []
    for f in fc["features"]:
        p = f["properties"]
        q = {k: p[k] for k in ("buildingId", "usage", "height", "floors") if p.get(k) not in (None, "")}
        out.append({"type": "Feature", "properties": q, "geometry": f["geometry"]})
    out, summary = clean_features(out, label=DST.name)
    st = dict(total=len(out), usage=0, height=0, floors=0, area=0, official=0, computed=0)
    for f in out:
        p = f["properties"]
        p["area"] = round(transform(tr, shape(f["geometry"])).area, 1)
        p["areaSource"] = "computed"
        for k in ("usage", "height", "floors"): st[k] += k in p
        st["area"] += 1; st["computed"] += 1
    res = {"type": "FeatureCollection", "name": "weimar_building_footprints",
           "crs": fc.get("crs"), "source": "Thuringia LoD2 footprints + ALKIS WFS attributes; area computed in EPSG:25832",
           "features": out}
    with open(DST, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, separators=(",", ":"))
    st["bytes"] = DST.stat().st_size
    print(json.dumps(st, indent=1)); print(summary)

if __name__ == "__main__":
    main()
