#!/usr/bin/env python3
"""
fetch_alkis_wfs_attrs.py — download ALKIS attributes for Weimar (TASK_28 part B).

Source: GDI-Th WFS 2.0 "adv_alkis_v2_wfs" (ALKIS simplified, AdV 2.0)
  https://www.geoproxy.geoportal-th.de/geoproxy/services/adv_alkis_v2_wfs
Feature types:
  ave:GebaeudeBauwerk -> oid (== our buildingId), funktion (text), anzahlgs (floors)
  ave:Flurstueck      -> flstkennz, flaeche (official m2), tntxt (Tatsaechliche Nutzung
                         per parcel: "Nutzart; m2|Nutzart; m2|...")
Raw pages are stored unchanged in data/raw/alkis_wfs_v2/<type>_<n>.xml (paged by
STARTINDEX, sorted by ave:oid so paging is stable). bake_alkis_attrs.py reads them.
"""
import sys, time, urllib.parse, urllib.request
from pathlib import Path

BASE = "https://www.geoproxy.geoportal-th.de/geoproxy/services/adv_alkis_v2_wfs"
OUT = Path(__file__).resolve().parents[1] / "data" / "raw" / "alkis_wfs_v2"
# EPSG:25832 bbox around the whole AOI of our building / parcel layers (with margin)
BBOX = "654000,5640000,670500,5657500"
PAGE = 5000
TYPES = {
    "GebaeudeBauwerk": "ave:oid,ave:funktion,ave:anzahlgs",
    "Flurstueck": "ave:flstkennz,ave:flaeche,ave:tntxt",
}

def get(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=300) as r:
                return r.read()
        except Exception as e:
            print("  retry", i, e, file=sys.stderr); time.sleep(5 * (i + 1))
    raise SystemExit("failed: " + url)

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for t, props in TYPES.items():
        start, n = 0, 0
        while True:
            q = urllib.parse.urlencode({
                "SERVICE": "WFS", "VERSION": "2.0.0", "REQUEST": "GetFeature",
                "TYPENAMES": "ave:" + t, "COUNT": PAGE, "STARTINDEX": start,
                "SORTBY": "ave:oid", "PROPERTYNAME": props,
                "SRSNAME": "urn:ogc:def:crs:EPSG::25832",
                "BBOX": BBOX + ",urn:ogc:def:crs:EPSG::25832",
            })
            data = get(BASE + "?" + q)
            (OUT / f"{t}_{n:03d}.xml").write_bytes(data)
            got = data.count(b"<wfs:member>")
            print(t, start, got, len(data)); n += 1
            if got < PAGE: break
            start += PAGE

if __name__ == "__main__":
    main()
