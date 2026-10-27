"""TASK_26 Part B — OSM shop/office/amenity POIs -> per-building count (`poiCount`).

Fetches POIs for the Weimar AOI via Overpass (raw copy: data/raw/osm/weimar_poi_osm.json),
drops street furniture, counts points inside each footprint of
web/layers/data/weimar_buildings_footprint_height.geojson and writes `poiCount`
(only N>0) back into that file. OSM = community data, ODbL, may be incomplete.
"""
import json, sys
from pathlib import Path
import shapely
from shapely.geometry import shape, Point
from shapely import STRtree

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_weimar_pedestrian_osm import fetch_overpass, W, S, E, N

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data/raw/osm/weimar_poi_osm.json"
BLD = ROOT / "web/layers/data/weimar_buildings_footprint_height.geojson"
FURNITURE = {  # amenity=* values that are street furniture / infrastructure, not venues
    "bench", "parking", "parking_space", "parking_entrance", "bicycle_parking", "motorcycle_parking",
    "waste_basket", "recycling", "waste_disposal", "waste_transfer_station", "vending_machine",
    "shelter", "post_box", "drinking_water", "bicycle_repair_station", "charging_station",
    "toilets", "clock", "fountain", "grit_bin", "telephone", "give_box", "bbq", "table",
    "lounger", "watering_place", "hunting_stand", "bicycle_rental", "compressed_air", "loading_dock",
    "photo_booth", "public_bookcase", "letter_box", "fire_hydrant", "kneipp_water_cure",
}

if RAW.exists():
    data = json.load(open(RAW, encoding="utf-8"))
else:
    q = (f'[out:json][timeout:180];(nwr["shop"]({S},{W},{N},{E});nwr["office"]({S},{W},{N},{E});'
         f'nwr["amenity"]({S},{W},{N},{E}););out center tags;')
    data = fetch_overpass(q)
    json.dump(data, open(RAW, "w", encoding="utf-8"))

pts = []
for el in data["elements"]:
    t = el.get("tags", {})
    if not (t.get("shop") or t.get("office")) and t.get("amenity") in FURNITURE:
        continue
    lon = el.get("lon") or el.get("center", {}).get("lon")
    lat = el.get("lat") or el.get("center", {}).get("lat")
    if lon is not None: pts.append(Point(lon, lat))
print("raw elements", len(data["elements"]), "-> POIs kept", len(pts))

gj = json.load(open(BLD, encoding="utf-8"))
geoms = [shape(f["geometry"]) for f in gj["features"]]
tree = STRtree(geoms)
counts = [0] * len(geoms)
outside = 0
for p in pts:
    hit = tree.query(p, predicate="within")
    if len(hit) == 0: outside += 1
    for i in hit: counts[i] += 1
withpoi = 0
for f, c in zip(gj["features"], counts):
    f["properties"].pop("poiCount", None)
    if c > 0: f["properties"]["poiCount"] = c; withpoi += 1
json.dump(gj, open(BLD, "w", encoding="utf-8"), ensure_ascii=False)
print("buildings with POIs:", withpoi, "max", max(counts), "| POIs not inside any footprint:", outside)
