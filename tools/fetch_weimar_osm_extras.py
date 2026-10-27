"""TASK 42 — OSM-derived thematic layers for the Weimar city area (Overpass), clipped to the city boundary.

Output (web/layers/data/, GeoJSON EPSG:4326, licence ODbL — © OpenStreetMap contributors):
  osm_bike_parking, osm_parking_points, osm_parking_areas, osm_parking_buildings,
  osm_cycleways, osm_bike_lanes, osm_bike_services, osm_charging, osm_fuel,
  osm_rail_stations, osm_unesco, osm_heritage,
  osm_landcover (category property), osm_water_areas, osm_waterways, osm_trees
Raw Overpass answers are kept in data/raw/osm/extras/.

Run: python tools/fetch_weimar_osm_extras.py [theme ...]
"""
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

from shapely.geometry import LineString, MultiPolygon, Point, Polygon, mapping, shape
from shapely.ops import linemerge, polygonize, unary_union
from shapely.prepared import prep

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "layers" / "data"
RAW = ROOT / "data" / "raw" / "osm" / "extras"
RAW.mkdir(parents=True, exist_ok=True)
BBOX = "50.91673,11.2365,51.03776,11.40122"      # S,W,N,E  (DATA.md AOI)
URLS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
UA = "weimar-digital-twin/extras (student research)"

boundary = shape(json.load(open(OUT / "weimar_boundary.geojson", encoding="utf-8"))["features"][0]["geometry"])
pboundary = prep(boundary)


def overpass(q, name):
    cache = RAW / f"{name}.json"
    if cache.exists():
        return json.load(open(cache, encoding="utf-8"))
    for attempt in range(5):
        for u in URLS:
            try:
                req = urllib.request.Request(u, data=urllib.parse.urlencode({"data": q}).encode(), headers={"User-Agent": UA})
                with urllib.request.urlopen(req, timeout=240) as r:
                    d = json.load(r)
                json.dump(d, open(cache, "w", encoding="utf-8"))
                return d
            except Exception as e:
                print("  retry", name, u.split("/")[2], str(e)[:80], flush=True)
                time.sleep(12)
    raise RuntimeError("overpass failed: " + name)


def q_nwr(filters, geom="out geom;"):
    body = "".join(f"nwr{f}({BBOX});" for f in filters)
    return f"[out:json][timeout:200];({body});{geom}"


# ---------------------------------------------------------------- geometry assembly
def way_coords(el):
    return [(p["lon"], p["lat"]) for p in el.get("geometry", [])]


def element_geom(el):
    t = el["type"]
    if t == "node":
        return Point(el["lon"], el["lat"])
    if t == "way":
        c = way_coords(el)
        if len(c) < 2:
            return None
        if len(c) >= 4 and c[0] == c[-1]:
            return Polygon(c)
        return LineString(c)
    if t == "relation":
        outers, inners = [], []
        for m in el.get("members", []):
            if m.get("type") != "way" or "geometry" not in m:
                continue
            ln = LineString([(p["lon"], p["lat"]) for p in m["geometry"]])
            if len(ln.coords) < 2:
                continue
            (inners if m.get("role") == "inner" else outers).append(ln)
        if not outers:
            return None
        op = list(polygonize(linemerge(unary_union(outers)))) if len(outers) > 1 else list(polygonize(outers))
        if not op:
            return None
        poly = unary_union(op)
        if inners:
            ip = list(polygonize(linemerge(unary_union(inners)))) if len(inners) > 1 else list(polygonize(inners))
            if ip:
                poly = poly.difference(unary_union(ip))
        return poly
    return None


def clip(g, kind):
    """kind: point | line | polygon. Returns geometry inside the boundary or None."""
    if g is None or g.is_empty:
        return None
    if not g.is_valid:
        g = g.buffer(0)
    if kind == "point":
        g = g.centroid if g.geom_type != "Point" else g
        return g if pboundary.contains(g) else None
    if kind == "line":
        if g.geom_type in ("Polygon", "MultiPolygon"):
            g = g.boundary
        r = g.intersection(boundary)
        return None if r.is_empty else r
    if g.geom_type not in ("Polygon", "MultiPolygon"):
        return None
    r = g.intersection(boundary)
    if r.is_empty:
        return None
    if r.geom_type == "GeometryCollection":
        ps = [x for x in r.geoms if x.geom_type in ("Polygon", "MultiPolygon")]
        if not ps:
            return None
        r = unary_union(ps)
    return r


def r6(c):
    return [round(c[0], 6), round(c[1], 6)] if isinstance(c[0], (int, float)) else [r6(x) for x in c]


def write(name, feats):
    fc = {"type": "FeatureCollection", "features": feats}
    with open(OUT / f"{name}.geojson", "w", encoding="utf-8") as fh:
        json.dump(fc, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"  {name}: {len(feats)} features, {(OUT / (name + '.geojson')).stat().st_size / 1024:.0f} KB", flush=True)


def feat(g, props):
    m = mapping(g)
    m["coordinates"] = r6(m["coordinates"])
    return {"type": "Feature", "properties": props, "geometry": m}


PKEYS = ["name", "operator", "capacity", "access", "fee", "parking", "surface", "highway", "bicycle", "cycleway",
         "network", "brand", "socket:type2", "ref", "heritage", "historic", "wikidata", "landuse", "leisure", "natural",
         "waterway", "water", "species", "genus", "leaf_type", "height", "building", "amenity", "shop", "railway", "description"]


def props_of(el, extra=None):
    t = el.get("tags", {})
    p = {k: t[k] for k in PKEYS if k in t}
    p["osmId"] = f"{el['type'][0]}{el['id']}"
    if extra:
        p.update(extra)
    return p


def build(name, query, kind, keep=lambda el: True, classify=None):
    d = overpass(query, name)
    feats = []
    for el in d.get("elements", []):
        if not keep(el):
            continue
        g = clip(element_geom(el), kind)
        if g is None:
            continue
        feats.append(feat(g, props_of(el, classify(el) if classify else None)))
    write(name, feats)
    return feats


# ---------------------------------------------------------------- themes
def t_bike_parking():
    build("osm_bike_parking", q_nwr(['["amenity"="bicycle_parking"]']), "point")


def t_parking():
    d = overpass(q_nwr(['["amenity"="parking"]']), "osm_parking_all")
    pts, areas, blds = [], [], []
    for el in d["elements"]:
        t = el.get("tags", {})
        is_b = ("building" in t and t["building"] != "no") or t.get("parking") in ("multi-storey", "underground")
        if el["type"] == "node":
            g = clip(element_geom(el), "point")
            if g is not None:
                pts.append(feat(g, props_of(el)))
            continue
        g = element_geom(el)
        if g is None or g.geom_type not in ("Polygon", "MultiPolygon"):
            continue
        c = clip(g, "polygon")
        if c is None:
            continue
        (blds if is_b else areas).append(feat(c, props_of(el)))
    write("osm_parking_points", pts)
    write("osm_parking_areas", areas)
    write("osm_parking_buildings", blds)


def t_cycle():
    build("osm_cycleways", q_nwr(['["highway"="cycleway"]']), "line")
    lane_tags = ['["cycleway"~"lane|track|shared_lane|share_busway|opposite|opposite_lane|opposite_track"]',
                 '["cycleway:left"~"lane|track|shared_lane|share_busway|opposite"]',
                 '["cycleway:right"~"lane|track|shared_lane|share_busway|opposite"]',
                 '["cycleway:both"~"lane|track|shared_lane|share_busway"]',
                 '["bicycle_road"="yes"]', '["cyclestreet"="yes"]',
                 '["highway"~"^(path|footway|track|pedestrian)$"]["bicycle"="designated"]']
    d = overpass("[out:json][timeout:200];(" + "".join(f"way{f}({BBOX});" for f in lane_tags) + ");out geom;", "osm_bike_lanes")
    feats = []
    for el in d["elements"]:
        t = el.get("tags", {})
        if t.get("highway") == "cycleway":
            continue
        kind = "bicycle road" if t.get("bicycle_road") == "yes" or t.get("cyclestreet") == "yes" else \
               "designated path" if t.get("bicycle") == "designated" else "lane / track on road"
        g = clip(element_geom(el), "line")
        if g is not None:
            feats.append(feat(g, props_of(el, {"infra": kind})))
    write("osm_bike_lanes", feats)
    build("osm_bike_services", q_nwr(['["amenity"~"^(bicycle_rental|bicycle_repair_station|bicycle_wash)$"]', '["shop"="bicycle"]']), "point")


def t_charging():
    build("osm_charging", q_nwr(['["amenity"="charging_station"]']), "point")


def t_fuel():
    build("osm_fuel", q_nwr(['["amenity"="fuel"]']), "point")


def t_rail():
    d = overpass(q_nwr(['["railway"~"^(station|halt)$"]', '["building"="train_station"]', '["public_transport"="station"]["train"="yes"]']),
                 "osm_rail_stations")
    feats = []
    for el in d["elements"]:
        g = element_geom(el)
        if g is None:
            continue
        for kind in (["polygon"] if g.geom_type in ("Polygon", "MultiPolygon") else ["point"]):
            c = clip(g, kind)
            if c is not None:
                feats.append(feat(c, props_of(el)))
    write("osm_rail_stations", feats)


def t_unesco():
    d = overpass(q_nwr(['["heritage:operator"="whc"]', '["whc:inscription_date"]', '["boundary"="protected_area"]["protection_title"~"UNESCO|World Heritage|Welterbe",i]',
                        '["name"~"Klassisches Weimar|Bauhaus.*Weimar|UNESCO",i]["heritage"]', '["historic"]["name"~"Welterbe|World Heritage",i]']),
                 "osm_unesco")
    feats = []
    for el in d["elements"]:
        g = element_geom(el)
        if g is None:
            continue
        kind = "polygon" if g.geom_type in ("Polygon", "MultiPolygon") else "point"
        c = clip(g, kind)
        if c is not None:
            t = el.get("tags", {})
            role = "buffer zone" if ("buffer" in json.dumps(t).lower() or "pufferzone" in json.dumps(t).lower()) else \
                   ("area" if kind == "polygon" else "object")
            feats.append(feat(c, props_of(el, {"unescoRole": role})))
    write("osm_unesco", feats)


def t_heritage():
    d = overpass(q_nwr(['["heritage"]', '["historic"~"^(monument|memorial|castle|manor|building|ruins|archaeological_site|city_gate|wayside_cross|wayside_shrine|fort)$"]']),
                 "osm_heritage")
    feats = []
    for el in d["elements"]:
        g = element_geom(el)
        if g is None:
            continue
        kind = "polygon" if g.geom_type in ("Polygon", "MultiPolygon") else "point"
        c = clip(g, kind)
        if c is not None:
            feats.append(feat(c if kind == "polygon" else c, props_of(el)))
    write("osm_heritage", feats)


LANDCOVER = [
    ("Forest", lambda t: t.get("landuse") == "forest" or t.get("natural") == "wood"),
    ("Park & garden", lambda t: t.get("leisure") in ("park", "garden", "playground", "dog_park", "common") or t.get("landuse") in ("recreation_ground", "village_green")),
    ("Meadow & grass", lambda t: t.get("landuse") in ("meadow", "grass") or t.get("natural") in ("grassland", "heath") or t.get("landcover") == "grass"),
    ("Cemetery", lambda t: t.get("landuse") == "cemetery" or t.get("amenity") == "grave_yard"),
    ("Allotments", lambda t: t.get("landuse") == "allotments"),
    ("Sport", lambda t: t.get("leisure") in ("pitch", "sports_centre", "stadium", "golf_course", "track", "swimming_pool", "miniature_golf")),
    ("Orchard & vineyard", lambda t: t.get("landuse") in ("orchard", "vineyard", "plant_nursery") or t.get("crop") == "fruit"),
    ("Farmland", lambda t: t.get("landuse") in ("farmland", "farmyard")),
    ("Scrub & wetland", lambda t: t.get("natural") in ("scrub", "wetland", "fell", "moor") or t.get("wetland")),
]


def t_landcover():
    d = overpass(q_nwr(['["landuse"~"^(forest|meadow|grass|cemetery|allotments|orchard|vineyard|plant_nursery|farmland|farmyard|recreation_ground|village_green)$"]',
                        '["natural"~"^(wood|grassland|heath|scrub|wetland|fell|moor)$"]',
                        '["leisure"~"^(park|garden|playground|dog_park|common|pitch|sports_centre|stadium|golf_course|track|miniature_golf)$"]',
                        '["amenity"="grave_yard"]']), "osm_landcover")
    feats = []
    for el in d["elements"]:
        if el["type"] == "node":
            continue
        t = el.get("tags", {})
        cat = next((n for n, f in LANDCOVER if f(t)), None)
        if not cat:
            continue
        c = clip(element_geom(el), "polygon")
        if c is not None:
            f = feat(c, props_of(el, {"landcover": cat}))
            f["_a"] = c.area
            feats.append(f)
    feats.sort(key=lambda f: -f.pop("_a"))        # large first, so small parts draw on top
    write("osm_landcover", feats)


def t_water():
    build("osm_water_areas", q_nwr(['["natural"="water"]', '["waterway"="riverbank"]', '["landuse"="reservoir"]']), "polygon")
    build("osm_waterways", q_nwr(['["waterway"~"^(river|stream|canal|ditch|drain)$"]']), "line")


def t_trees():
    build("osm_trees", q_nwr(['["natural"="tree"]'], geom="out;"), "point")


THEMES = {"bike_parking": t_bike_parking, "parking": t_parking, "cycle": t_cycle, "charging": t_charging, "fuel": t_fuel,
          "rail": t_rail, "unesco": t_unesco, "heritage": t_heritage, "landcover": t_landcover, "water": t_water, "trees": t_trees}

if __name__ == "__main__":
    for n in (sys.argv[1:] or THEMES):
        print("==", n, flush=True)
        THEMES[n]()
        time.sleep(10)
