#!/usr/bin/env python3
"""
bake_alkis_attrs.py — TASK_28 part B: bake ALKIS attributes into the layer GeoJSONs.

Reads the raw WFS pages from data/raw/alkis_wfs_v2/ (see fetch_alkis_wfs_attrs.py) and
writes into web/layers/data/:

 weimar_buildings_footprint_height.geojson
     usage  <- ave:funktion (text already decoded by the service)
     floors <- ave:anzahlgs (above-ground storeys, int)
     Matched by id: ALKIS ave:oid == buildingId + "BL" (BL = ALKIS object-type suffix;
     our 16-char id is the oid without it). Buildings without an id match fall back to
     "representative point inside an (unused) ALKIS footprint". Existing non-empty
     values are kept.
     Empty stays absent (no 0 / "unknown").
 weimar_parcels.geojson
     usage      <- dominant entry of ave:tntxt ("Nutzart; m2|Nutzart; m2|...": the
                   authority's own split of the parcel by Tatsaechliche Nutzung);
                   the Nutzart with the largest area wins. Text, not a code.
     area       <- ave:flaeche (official ALKIS area, m2), areaSource "official";
                   else the official INSPIRE areaValue_m2 already in the file (parcels no
                   longer in the live WFS); else computed in EPSG:25832, "computed".
     Matched by flstkennz == nationalCadastralReference.

Backups of the originals: data/raw/<layer>_pre_task28_backup/. Afterwards every
polygon file goes through geojson_clean.clean_features() (project policy).
"""
import json, re, shutil, sys
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from geojson_clean import clean_features  # noqa: E402

RAW = ROOT / "data" / "raw" / "alkis_wfs_v2"
DATA = ROOT / "web" / "layers" / "data"
NS = "{http://repository.gdi-de.org/schemas/adv/produkt/alkis-vereinfacht/2.0}"

def members(kind):
    for p in sorted(RAW.glob(f"{kind}_*.xml")):
        for _, el in ET.iterparse(p):
            if el.tag == NS + kind:
                yield {c.tag.replace(NS, ""): (c.text or "").strip() for c in el if len(c) == 0}
                el.clear()

def dominant_usage(tntxt):
    best, best_a = "", -1.0
    tot = {}
    for part in tntxt.split("|"):
        if ";" not in part: continue
        name, a = part.rsplit(";", 1)
        try: a = float(a)
        except ValueError: continue
        tot[name.strip()] = tot.get(name.strip(), 0) + a   # same Nutzart may appear twice
    for name, a in tot.items():
        if a > best_a: best, best_a = name, a
    return best

def backup(name):
    d = ROOT / "data" / "raw" / f"{Path(name).stem}_pre_task28_backup"
    if not d.exists():
        d.mkdir(); shutil.copy2(DATA / name, d / name)

def load(name):
    return json.load(open(DATA / name, encoding="utf-8"))

def save(name, fc):
    fc["features"], _ = clean_features(fc["features"], label=name)
    with open(DATA / name, "w", encoding="utf-8") as f:
        json.dump(fc, f, ensure_ascii=False, separators=(",", ":"))

GML = "{http://www.opengis.net/gml/3.2}"

def polygons_by_oid(kind, wanted):
    """oid -> shapely Polygon (exterior ring, EPSG:25832) for the wanted oids."""
    from shapely.geometry import Polygon
    out = {}
    for p in sorted(RAW.glob(f"{kind}_*.xml")):
        for _, el in ET.iterparse(p):
            if el.tag != NS + kind: continue
            oid = (el.findtext(NS + "oid") or "").strip()
            if oid in wanted:
                pts = []
                for ext in el.iter(GML + "exterior"):
                    for pl in ext.iter(GML + "posList"):
                        v = [float(x) for x in pl.text.split()]
                        pts += list(zip(v[0::2], v[1::2]))
                    break
                if len(pts) >= 3:
                    g = Polygon(pts)
                    if not g.is_valid: g = g.buffer(0)
                    out[oid] = g
            el.clear()
    return out

def buildings():
    name = "weimar_buildings_footprint_height.geojson"; backup(name)
    alk = {m["oid"]: m for m in members("GebaeudeBauwerk") if m.get("oid")}
    fc = load(name); n = len(fc["features"])
    st = dict(total=n, matched=0, matched_by_id=0, matched_by_centroid=0, usage=0, floors=0, height=0)
    # pass 1: by id
    match = {}
    for i, f in enumerate(fc["features"]):
        oid = f["properties"].get("buildingId", "") + "BL"
        if oid in alk: match[i] = alk[oid]; st["matched_by_id"] += 1
    # pass 2: representative point inside a not-yet-used ALKIS footprint
    from shapely.geometry import shape, Point
    from shapely.strtree import STRtree
    from pyproj import Transformer
    tr = Transformer.from_crs(4326, 25832, always_xy=True).transform
    from shapely.ops import transform
    used = {m["oid"] for m in match.values()}
    polys = polygons_by_oid("GebaeudeBauwerk", set(alk) - used)
    oids = list(polys); tree = STRtree([polys[o] for o in oids])
    for i, f in enumerate(fc["features"]):
        if i in match: continue
        pt = transform(tr, shape(f["geometry"])).representative_point()
        for j in tree.query(pt, predicate="within"):
            match[i] = alk[oids[j]]; st["matched_by_centroid"] += 1; break
    for i, f in enumerate(fc["features"]):
        p = f["properties"]; m = match.get(i)
        if m:
            st["matched"] += 1
            if m.get("funktion") and not p.get("usage"): p["usage"] = m["funktion"]
            if m.get("anzahlgs") and not p.get("floors"):
                try: p["floors"] = int(round(float(m["anzahlgs"])))
                except ValueError: pass
        st["usage"] += bool(p.get("usage")); st["floors"] += bool(p.get("floors"))
        st["height"] += p.get("height") not in (None, "")
    st["alkis_records"] = len(alk)
    save(name, fc); return st

def parcels():
    import shapely
    from shapely.geometry import shape
    from shapely.ops import transform
    from pyproj import Transformer
    tr = Transformer.from_crs(4326, 25832, always_xy=True).transform
    name = "weimar_parcels.geojson"; backup(name)
    alk = {m["flstkennz"]: m for m in members("Flurstueck") if m.get("flstkennz")}
    fc = load(name); n = len(fc["features"])
    st = dict(total=n, matched=0, usage=0, area_official=0, area_computed=0)
    for f in fc["features"]:
        p = f["properties"]; m = alk.get(p.get("nationalCadastralReference"))
        if m: st["matched"] += 1
        u = dominant_usage(m["tntxt"]) if m and m.get("tntxt") else ""
        if u: p["usage"] = u; st["usage"] += 1
        fl = m.get("flaeche") if m else ""
        try: fl = float(fl) if fl else None
        except ValueError: fl = None
        if fl:
            p["area"] = round(fl); p["areaSource"] = "official"; st["area_official"] += 1
        elif p.get("areaValue_m2"):
            # parcel not (any more) in the live WFS: keep the official INSPIRE areaValue
            # (ALKIS area from the ATOM dataset the geometry came from)
            p["area"] = round(float(p["areaValue_m2"])); p["areaSource"] = "official"
            st["area_official"] += 1; st["area_official_from_atom"] = st.get("area_official_from_atom", 0) + 1
        else:
            p["area"] = round(transform(tr, shape(f["geometry"])).area, 1)
            p["areaSource"] = "computed"; st["area_computed"] += 1
    st["alkis_records"] = len(alk)
    save(name, fc); return st

if __name__ == "__main__":
    what = sys.argv[1:] or ["buildings", "parcels"]
    for w in what: print(w, json.dumps({"buildings": buildings, "parcels": parcels}[w](), indent=1))
