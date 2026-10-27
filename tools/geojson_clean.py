#!/usr/bin/env python3
"""
geojson_clean.py — shared polygon hygiene for every GeoJSON we generate.

Why this exists
---------------
Degenerate polygon rings make CesiumJS throw, inside its render loop, e.g.

    DeveloperError: EllipsoidRhumbLine must have distinct start and end set.

which halts the whole viewer (blank map, "Rendering has stopped"). Shapefiles
and CityGML `posList` rings produce several bad shapes: zero-length edges
(consecutive coincident vertices), self-touching rings (a non-consecutive
vertex coinciding with an earlier one), bow-ties, holes poking outside the
shell, etc.

An earlier hand-rolled epsilon cleaner handled consecutive duplicates and an
explicit self-touch check, but it did not catch every case. This version delegates the whole problem to **Shapely / GEOS**:

    shapely.geometry.shape(geom)
      -> shapely.validation.make_valid(g)      # fix self-intersections, bow-ties,
                                               #   holes outside the shell, …
      -> shapely.set_precision(f, 1e-8)        # snap to a ~1 mm grid: make_valid
                                               #   keeps coincident / sub-mm-apart
                                               #   vertices, and Cesium's rhumb-line
                                               #   edge subdivision still throws
                                               #   "distinct start and end" on any
                                               #   near-zero-length edge. 1e-8° is
                                               #   far below real footprint detail.
      -> make_valid again if the snap re-broke validity
      -> shapely.geometry.mapping(fixed)

**Every polygon GeoJSON built from Shapefile / CityGML / WFS MUST be passed
through `clean_features()` before it is written to disk** — a mandatory step in
the conversion pattern for any new polygon layer (Tier 3 onward, TASK_02
Part B, future work), not just the files it was first written for.

Contract for the repaired geometry
----------------------------------
  * Polygon         -> kept as Polygon.
  * MultiPolygon    -> kept as MultiPolygon (valid GeoJSON; Cesium's
                       GeoJsonDataSource renders it). We do NOT split it into
                       separate features and do NOT pick the largest part.
                       If a Polygon input becomes a MultiPolygon (building split
                       into disconnected pieces) that is counted separately as
                       `became_multipolygon`.
  * empty / zero area / GeometryCollection / anything non-polygonal
                    -> feature is unrecoverable: dropped, with its id logged.

`clean_features()` prints every drop with a reason + feature id and an
end-of-run summary, and returns that summary dict:
  {in, kept, repaired, became_multipolygon, dropped, dropped_reasons}
"""

from __future__ import annotations

import shapely
from shapely.geometry import mapping, shape
from shapely.validation import make_valid

_POLY_TYPES = ("Polygon", "MultiPolygon")

# Coordinate snap grid, in degrees. 1e-8° ≈ 1.1 mm (lat) / 0.7 mm (lon at 51°N) —
# below any real building / parcel / boundary detail, above float noise.
SNAP_DEG = 1e-8


def _feat_id(feat):
    p = feat.get("properties") or {}
    for k in ("buildingId", "plan_id", "planID", "id", "name", "NAME"):
        if p.get(k):
            return p[k]
    return "?"


def clean_feature(feat):
    """Repair one feature's polygon geometry.

    Returns (feature|None, status) where status is one of:
      "untouched"  non-polygon geometry, left as-is
      "ok"         polygon geometry already valid, unchanged
      "repaired"   polygon geometry was invalid, fixed (still a (Multi)Polygon)
      "multipolygon"  fixed AND a single Polygon became a MultiPolygon
      "<reason>"   unrecoverable -> feature is None
    """
    geom = feat.get("geometry") or {}
    gtype = geom.get("type")
    if gtype not in _POLY_TYPES:
        return feat, "untouched"

    try:
        g = shape(geom)
    except Exception as exc:  # noqa: BLE001 — malformed coordinates
        return None, f"shape-error ({exc.__class__.__name__})"

    was_valid = g.is_valid and not g.is_empty
    fixed = make_valid(g)
    fixed = shapely.set_precision(fixed, grid_size=SNAP_DEG)
    if not fixed.is_empty and not fixed.is_valid:
        fixed = make_valid(fixed)

    if fixed.is_empty:
        return None, "empty after make_valid"
    ft = fixed.geom_type
    if ft not in _POLY_TYPES:
        return None, f"non-polygonal after make_valid ({ft})"
    if fixed.area == 0:
        return None, "zero area after make_valid"

    feat["geometry"] = mapping(fixed)

    if was_valid and ft == gtype:
        return feat, "ok"
    if gtype == "Polygon" and ft == "MultiPolygon":
        return feat, "multipolygon"
    return feat, "repaired"


def clean_features(features, label=""):
    """Clean a list of GeoJSON features in place; return (kept_list, summary)."""
    tag = f" {label}" if label else ""
    kept = []
    s = {"in": len(features), "kept": 0, "repaired": 0,
         "became_multipolygon": 0, "dropped": 0, "dropped_reasons": {}}

    for i, feat in enumerate(features):
        cf, status = clean_feature(feat)
        if cf is None:
            s["dropped"] += 1
            s["dropped_reasons"][status] = s["dropped_reasons"].get(status, 0) + 1
            print(f"  [clean{tag}] dropped feature #{i} (id={_feat_id(feat)}): {status}")
            continue
        if status == "multipolygon":
            s["became_multipolygon"] += 1
            s["repaired"] += 1
        elif status == "repaired":
            s["repaired"] += 1
        kept.append(cf)

    s["kept"] = len(kept)
    reasons = ", ".join(f"{k}: {v}" for k, v in sorted(s["dropped_reasons"].items())) or "none"
    print(f"  [clean{tag}] {s['in']} features in -> {s['kept']} kept | "
          f"{s['repaired']} repaired (of which {s['became_multipolygon']} became MultiPolygon) | "
          f"{s['dropped']} dropped ({reasons})")
    return kept, s
