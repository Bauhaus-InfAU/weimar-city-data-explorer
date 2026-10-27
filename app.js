/* Data Twin City Weimar — platform (Stage 1).
 *
 * The page shows FOUR independent, stacked map blocks, one per scroll:
 *
 *   1. Simplified Model for Analysis — viewport "computational" — MapLibre
 *   2. Weimar Model                — viewport "weimar_model"    — MapLibre
 *   3. Transport                   — viewport "transport"      — MapLibre
 *   4. Blue-green Infrastructure   — viewport "blue_green"     — MapLibre
 *
 * All four blocks are built by createMapLibreBlock (final architecture — TASK_15
 * moved Weimar Model off CesiumJS; the WEGA real-roof path was retired as not
 * worth its cost, real roof geometry for Grasshopper stays in
 * data/processed/wega_buildings_obj/ from TASK_12). They share the same DOM
 * chrome (buildBlockChrome), sidebar renderer (renderSidebar) and metadata strip
 * (renderMetadataPanel). The layer manifest is loaded once; each block filters it
 * by the `viewports` field. The deck.gl `type: "3dtiles"` path in this file is
 * now dead code — no viewport carries a 3dtiles layer.
 */

"use strict";

// ---------------------------------------------------------------------------
// Shared constants / helpers
// ---------------------------------------------------------------------------
const WEIMAR_AOI = { west: 11.2365, south: 50.91673, east: 11.40122, north: 51.03776 };
const WEIMAR_CENTER = { lon: 11.329, lat: 50.9795 };
const WEIMAR_BOUNDS = [
  [WEIMAR_AOI.west, WEIMAR_AOI.south],
  [WEIMAR_AOI.east, WEIMAR_AOI.north],
];

const OSM_TILES = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";
// CartoDB Positron — free, no API key/registration/rate-limit-token needed
// (unlike Esri Light Gray Canvas). a/b/c/d round-robin subdomains, standard
// (non-retina) 256px tiles. Used as the Computational Design viewport's base
// map (TASK_05 Part 5) instead of the plain OSM raster the other viewports use.
// The legacy raster tile endpoint (basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png,
// under any of its aliases — cartodb-basemaps-*.global.ssl.fastly.net included)
// now serves an "API KEY REQUIRED" watermark placeholder instead of map tiles —
// CARTO gated it behind an account at some point after DATA.md/TASK_05 were
// written; confirmed by fetching a real 256px PNG back with HTTP 200 (so it's
// not a network/CORS failure, just a watermarked image). Its **vector** style
// is still open, unauthenticated, CORS-enabled (checked: style.json, the
// carto.streets vector tiles, sprite and glyphs all return real data with
// Access-Control-Allow-Origin: *) — used here instead, as a full MapLibre
// style URL rather than a raster tile template.
const CARTO_POSITRON_STYLE_URL = "https://basemaps.cartocdn.com/gl/positron-gl-style/style.json";
const CARTO_DARK_STYLE_URL = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";
const CARTO_ATTRIBUTION =
  '© <a href="https://carto.com/attributions" target="_blank" rel="noopener">CARTO</a>, ' +
  '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors';

// Some upstream hosts (WEGA 3D: 3D Tiles + terrain) send no CORS headers, so the
// browser cannot fetch them directly. `tools/proxy.py` re-serves them with CORS.
// A url of "{proxy}/wega-3d-data/..." is rewritten to this base.
const PROXY_BASE = "http://localhost:8788";
function resolveUrl(url) {
  return typeof url === "string" ? url.replace("{proxy}", PROXY_BASE) : url;
}

const BASE_ATTRIBUTION =
  '© <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors';

/** Property table (InfoBox / popup) for a clicked feature; makes URLs clickable. */
function describePropsTable(properties) {
  const props = properties || {};
  const rows = Object.keys(props)
    .filter((k) => k !== "licence" && props[k] != null && props[k] !== "")
    .map((k) => {
      const v = String(props[k]);
      const cell = /^https?:\/\//.test(v)
        ? `<a href="${v}" target="_blank" rel="noopener">${v}</a>`
        : v;
      return `<tr><th>${k}</th><td>${cell}</td></tr>`;
    })
    .join("");
  return `<table class="props-table"><tbody>${rows}</tbody></table>`;
}

/** Tiny DOM helper. */
function h(tag, className, attrs) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (attrs) for (const [k, v] of Object.entries(attrs)) {
    if (v === true) el.setAttribute(k, "");
    else if (v !== false && v != null) el.setAttribute(k, v);
  }
  return el;
}


// ---------------------------------------------------------------------------
// Building click info (TASK 26) — live ALKIS WMS GetFeatureInfo
// ---------------------------------------------------------------------------
// Clicking a building (weimar_buildings_extrude) shows buildingId + height at once;
// then one GetFeatureInfo against ALKISKOMPAKT `alkis_geb` adds usage
// (gebaeudefunktion, decoded via layers/gebaeudefunktion.json) and above-ground
// floors. If ALKIS returns nothing / fails, the popup just keeps the first two rows.
const ALKIS_WMS_URL = "https://www.geoproxy.geoportal-th.de/geoproxy/services/ALKISKOMPAKT";
let gebaeudefunktionCodes = null;

async function loadGebaeudefunktion() {
  if (gebaeudefunktionCodes) return gebaeudefunktionCodes;
  try {
    const res = await fetch("layers/gebaeudefunktion.json");
    gebaeudefunktionCodes = res.ok ? await res.json() : {};
  } catch { gebaeudefunktionCodes = {}; }
  return gebaeudefunktionCodes;
}

/** Returns { function, floors } from ALKIS at lngLat, or null. */
async function fetchAlkisBuildingInfo(lngLat) {
  const e = 0.00005; // ~5 m half-box, click at its centre pixel
  const q = new URLSearchParams({
    SERVICE: "WMS", VERSION: "1.3.0", REQUEST: "GetFeatureInfo",
    LAYERS: "alkis_geb", QUERY_LAYERS: "alkis_geb", STYLES: "", CRS: "EPSG:4326",
    BBOX: `${lngLat.lat - e},${lngLat.lng - e},${lngLat.lat + e},${lngLat.lng + e}`,
    WIDTH: "101", HEIGHT: "101", I: "50", J: "50",
    INFO_FORMAT: "application/vnd.ogc.gml", FEATURE_COUNT: "1",
  });
  try {
    const res = await fetch(`${ALKIS_WMS_URL}?${q}`);
    if (!res.ok) return null;
    const doc = new DOMParser().parseFromString(await res.text(), "application/xml");
    const text = (name) => {
      const el = doc.getElementsByTagNameNS("*", name)[0];
      return el ? el.textContent.trim() : "";
    };
    const code = text("gebaeudefunktion");
    const floors = text("anzahlDerOberirdischenGeschosse");
    if (!code && !floors) return null;
    const codes = await loadGebaeudefunktion();
    return {
      function: code ? (codes[code] || `code ${code} (numeric code, no text found)`) : "",
      floors: floors ? String(Math.round(parseFloat(floors))) : "",
    };
  } catch { return null; }
}

/**
 * The click position is unreliable for ALKIS: on a pitched map a click on a
 * fill-extrusion roof/wall resolves to a ground point outside the footprint.
 * So probe points inside the building (ring vertex average, then points between
 * it and the vertices) after the click point, and take the first ALKIS answer.
 */
async function fetchAlkisBuildingInfoNear(lngLat, geometry) {
  const cands = [];
  const ring = geometry && (geometry.type === "Polygon" ? geometry.coordinates[0]
    : geometry.type === "MultiPolygon" ? geometry.coordinates[0][0] : null);
  if (ring && ring.length > 2) {
    const pts = ring.slice(0, -1);
    const cx = pts.reduce((a, p) => a + p[0], 0) / pts.length;
    const cy = pts.reduce((a, p) => a + p[1], 0) / pts.length;
    cands.push({ lng: cx, lat: cy });
    for (const p of pts.slice(0, 8)) cands.push({ lng: (cx + p[0]) / 2, lat: (cy + p[1]) / 2 });
  }
  cands.push(lngLat);
  for (const c of cands) {
    const info = await fetchAlkisBuildingInfo(c);
    if (info) return info;
  }
  return null;
}

function showBuildingPopup(map, e, f) {
  // Several building layers can be on at once (flat, footprints, 3D): one popup per click.
  const oe = e.originalEvent;
  if (oe) { if (oe._buildingPopupShown) return; oe._buildingPopupShown = true; }
  const p = f.properties;
  // TASK 30: the "Building footprints" layer carries area + areaSource baked in; popup order
  // usage, height, floors, area, buildingId, no live fallback (everything is in the file).
  const isFootprint = p.area != null && p.area !== "";
  const base = {};
  if (isFootprint) {
    if (p.usage) base.usage = p.usage;
    if (p.useCategory) base.useCategory = p.useCategory;
    if (p.height != null && p.height !== "") base["height (m)"] = p.height;
    if (p.floors) base.floors = p.floors;
    base["area (m²)"] = p.areaSource ? `${p.area} (${p.areaSource})` : p.area;
    base.buildingId = p.buildingId;
    new maplibregl.Popup({ maxWidth: "340px" }).setLngLat(e.lngLat).setHTML(describePropsTable(base)).addTo(map);
    return;
  }
  base.buildingId = p.buildingId; base.height = p.height;
  // "Heights without roofs" layer: height = eave; ridge, roof type and the attic-floor estimate next to it
  if (p.eaveHeight != null && p.eaveHeight !== "") {
    delete base.height;
    base["height without roof (m)"] = p.eaveHeight;
    if (p.ridgeHeight != null) base["ridge height (m)"] = p.ridgeHeight;
    if (p.roofType) base["roof type"] = p.roofType;
    if (String(p.atticFloorLikely) === "true") base["height incl. attic floor (m)"] = p.heightWithAttic;
  }
  // TASK 28: usage / floors are baked into the GeoJSON (ALKIS WFS). The live
  // GetFeatureInfo below is only a fallback for buildings that have no baked value.
  if (f.properties.usage) base.usage = f.properties.usage;
  if (f.properties.useCategory) base.useCategory = f.properties.useCategory;   // TASK 39
  if (f.properties.floors) base["floors (above ground)"] = f.properties.floors;
  // OSM shop/office/amenity points inside the footprint (precomputed, only when > 0; community data)
  if (Number(f.properties.poiCount) > 0) base["objects inside (OSM)"] = f.properties.poiCount;
  const popup = new maplibregl.Popup({ maxWidth: "340px" })
    .setLngLat(e.lngLat).setHTML(describePropsTable(base)).addTo(map);
  if (base.usage && base["floors (above ground)"]) return;
  fetchAlkisBuildingInfoNear(e.lngLat, f.geometry).then((info) => {
    if (!info || !popup.isOpen()) return;
    const more = { ...base };
    if (!more.usage && info.function) more.usage = info.function;
    if (!more["floors (above ground)"] && info.floors) more["floors (above ground)"] = info.floors;
    popup.setHTML(describePropsTable(more));
  });
}

// ---------------------------------------------------------------------------
// Manifest (loaded once, shared by all blocks)
// ---------------------------------------------------------------------------
let manifest = { resources: [], layers: [], terrain: null };

async function loadManifest() {
  const res = await fetch("layers/layers.json", { cache: "no-cache" });
  if (!res.ok) throw new Error(`layers.json: HTTP ${res.status}`);
  manifest = await res.json();
}

// TASK 39: colour-by-category. manifest.useCategories = [{name, color}] is the single palette for the
// map fill, the legend and the Rhino layer colours.
function paletteOf(config) {
  return manifest[(config.colorBy && config.colorBy.palette) || "useCategories"] || [];
}

function useColorExpr(config, fallback) {
  const cats = paletteOf(config);
  const prop = (config.colorBy && config.colorBy.property) || "useCategory";
  if (!cats.length) return fallback;
  return ["match", ["get", prop], ...cats.flatMap((c) => [c.name, c.color]), fallback];
}

/** 16 px tree glyph for point layers (Bauhaus green crown, black trunk). */
function makeTreeIcon() {
  const c = document.createElement("canvas"); c.width = 32; c.height = 32;
  const g = c.getContext("2d");
  g.fillStyle = "#0D0D0D"; g.fillRect(14.5, 17, 3, 12);
  g.beginPath(); g.arc(16, 12, 9, 0, Math.PI * 2); g.fillStyle = "#2B7F5B"; g.fill();
  g.lineWidth = 1.5; g.strokeStyle = "#0D0D0D"; g.stroke();
  return g.getImageData(0, 0, 32, 32);
}

const _isClear = (c) => !c || c === "transparent" || /^rgba\([^)]*,\s*0(\.0+)?\)$/.test(String(c).replace(/\s/g, "").replace(/,/g, ", "));
function _swatchOf(layer) {
  const st = layer.style || {};
  if (layer.type === "wega_mesh") return { kind: "box", fill: "#FFFFFF", stroke: "#0D0D0D" };
  if (layer.legendKind === "dot") return { kind: "dot", fill: st.markerColor || st.stroke || "#0D0D0D", stroke: "#0D0D0D" };
  const fill = _isClear(st.fill) ? null : st.fill;
  if (!st.fill && !fill) return { kind: "line", stroke: st.stroke || "#0D0D0D" };
  return { kind: "box", fill, stroke: st.stroke || null };
}

/** Legend content for a viewport: one "Layers" block for the visible layers, one block per visible colour palette. */
function legendModel(viewport) {
  const rows = [], palettes = [], seen = new Set();
  for (const g of sidebarGroupsForViewport(viewport) || []) {
    for (const item of g.items || []) {
      const layers = (item.layers || []).map(layerById).filter(Boolean);
      const vis = layers.filter((l) => layerVisibleInViewport(l, viewport));
      if (!vis.length) continue;
      const cl = vis.find((l) => l.colorBy);
      if (cl) {
        const key = cl.colorBy.palette || "useCategories";
        if (seen.has(key)) continue;
        seen.add(key);
        palettes.push({ title: cl.colorBy.legendTitle || item.name, rows: paletteOf(cl).map((c) => ({ kind: "box", fill: c.color, stroke: null, name: c.name })) });
      } else if (layers.length > 1) {
        for (const l of vis) rows.push({ ..._swatchOf(l), name: l.subName || l.name });
      } else rows.push({ ..._swatchOf(vis[0]), name: item.name });
    }
  }
  return [...(rows.length ? [{ title: "Layers", rows }] : []), ...palettes];
}

function renderLegend(legendEl, viewport, on) {
  legendEl.innerHTML = "";
  const blocks = on ? legendModel(viewport) : [];
  legendEl.hidden = blocks.length === 0;
  for (const bl of blocks) {
    const block = h("div", "use-legend-block");
    const title = h("div", "use-legend-title"); title.textContent = bl.title;
    block.append(title);
    for (const r of bl.rows) {
      const row = h("div", "use-legend-row");
      const sw = h("span", r.kind === "line" ? "use-legend-line" : "use-legend-swatch" + (r.kind === "dot" ? " dot" : ""));
      if (r.kind === "line") sw.style.background = r.stroke;
      else { if (r.fill) sw.style.background = r.fill; if (r.stroke) sw.style.boxShadow = `inset 0 0 0 1px ${r.stroke}`; }
      const t = h("span"); t.textContent = r.name;
      row.append(sw, t); block.append(row);
    }
    legendEl.append(block);
  }
}

// ---------------------------------------------------------------------------
// Base border (TASK 16) — one fixed reference rectangle, shared by all viewports
// ---------------------------------------------------------------------------
// The standard export area for the whole team. It is a static file the user
// draws once: web/layers/base_border.json — two opposite corners
// { pointA:{lng,lat}, pointB:{lng,lat} }. Loaded on every page load and drawn as
// the dashed-blue rectangle on all four maps. There is no in-UI editor — to move
// it, edit the JSON file. The export toolbar's "Base border" button (next to
// "Select area") uses this rectangle as the export clip region.

const BASE_BORDER_FALLBACK = {
  pointA: { lng: 11.301259, lat: 51.007167 },
  pointB: { lng: 11.348982, lat: 50.971379 },
};

/** Base border 2.0: same top / left / right as the Base border, the bottom edge 25 % higher
 *  (height x 0.75). Keep in sync with web/layers/base_border_2.json. */
const BASE_BORDER_2_FALLBACK = {
  pointA: { lng: 11.301259, lat: 51.007167 },
  pointB: { lng: 11.348982, lat: 50.980326 },
};

function makeRectBorder(fallback) {
  let a = { ...fallback.pointA };
  let b = { ...fallback.pointB };
  const subs = new Set();

  const rect = () => ({
    west: Math.min(a.lng, b.lng), east: Math.max(a.lng, b.lng),
    south: Math.min(a.lat, b.lat), north: Math.max(a.lat, b.lat),
  });
  const valid = (p) => p && Number.isFinite(Number(p.lng)) && Number.isFinite(Number(p.lat));

  return {
    getRect: rect,
    /** Register a listener; fires once immediately with the current rectangle. */
    subscribe(fn) {
      subs.add(fn);
      fn(rect());
      return () => subs.delete(fn);
    },
    /** Adopt corners from base_border.json (keep the fallback if missing/bad). */
    apply(obj) {
      if (obj && valid(obj.pointA) && valid(obj.pointB)) {
        a = { lng: Number(obj.pointA.lng), lat: Number(obj.pointA.lat) };
        b = { lng: Number(obj.pointB.lng), lat: Number(obj.pointB.lat) };
      }
      subs.forEach((fn) => { try { fn(rect()); } catch (e) { console.error("[baseBorder]", e); } });
    },
  };
}

const baseBorder = makeRectBorder(BASE_BORDER_FALLBACK);
const baseBorder2 = makeRectBorder(BASE_BORDER_2_FALLBACK);

/** Hand-drawn curved Base border (web/layers/base_border_curve.json: a GeoJSON Polygon /
 *  Feature / FeatureCollection, or { polygon: [[lng,lat], ...] }). Absent file = not available. */
const baseBorderCurve = (() => {
  let ring = null;   // open ring [[lng,lat], ...]
  const subs = new Set();
  const fire = () => subs.forEach((fn) => { try { fn(ring); } catch (e) { console.error("[baseBorderCurve]", e); } });
  const outerRing = (o) => {
    if (!o) return null;
    if (Array.isArray(o.polygon)) return o.polygon;
    if (o.type === "FeatureCollection") return outerRing((o.features || [])[0]);
    if (o.type === "Feature") return outerRing(o.geometry);
    if (o.type === "Polygon") return o.coordinates[0];
    if (o.type === "MultiPolygon") return o.coordinates[0][0];
    return null;
  };
  return {
    get: () => ring,
    available: () => !!ring,
    subscribe(fn) { subs.add(fn); fn(ring); return () => subs.delete(fn); },
    apply(obj) {
      let r = outerRing(obj);
      if (r && r.length > 3 && r[0][0] === r[r.length - 1][0] && r[0][1] === r[r.length - 1][1]) r = r.slice(0, -1);
      ring = r && r.length >= 3 ? r.map(([x, y]) => [Number(x), Number(y)]) : null;
      fire();
    },
  };
})();

async function loadBaseBorder() {
  try {
    const res = await fetch("layers/base_border.json", { cache: "no-cache" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    baseBorder.apply(await res.json());
  } catch (err) {
    console.warn("[boot] base_border.json not loaded, using built-in fallback:", err.message);
    baseBorder.apply(null);
  }
  try {
    const res = await fetch("layers/base_border_2.json", { cache: "no-cache" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    baseBorder2.apply(await res.json());
  } catch (err) {
    console.warn("[boot] base_border_2.json not loaded, using built-in fallback:", err.message);
    baseBorder2.apply(null);
  }
  try {
    const res = await fetch("layers/base_border_curve.json", { cache: "no-cache" });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    baseBorderCurve.apply(await res.json());
  } catch (err) {
    baseBorderCurve.apply(null);   // not drawn yet: the button stays disabled
  }
}

function layersForViewport(viewport) {
  return (manifest.layers || []).filter(
    (l) => Array.isArray(l.viewports) && l.viewports.includes(viewport)
  );
}

function layerById(id) {
  return (manifest.layers || []).find((l) => l.id === id);
}

/** Initial visibility of a layer in a given viewport. `visible` is one shared
 *  boolean across every viewport a layer appears in; an optional
 *  `visibleViewports` array overrides it per-viewport (a layer off elsewhere but
 *  on by default here, e.g. Buildings 3D in Weimar Model — TASK_15). */
function layerVisibleInViewport(layer, viewport) {
  if (Array.isArray(layer.visibleViewports)) return layer.visibleViewports.includes(viewport);
  return !!layer.visible;
}

/** Semantic sidebar/metadata grouping for a viewport, or null to fall back to
 *  grouping by source resource. See layers.json `sidebar`. */
function sidebarGroupsForViewport(viewport) {
  const s = manifest.sidebar || {};
  return Array.isArray(s[viewport]) ? s[viewport] : null;
}

/** Real DGM elevation as a MapLibre `raster-dem` source (Terrarium PNG tiles,
 *  built by tools/build_dgm_terrain.py). TASK 10 — Blue-green only for now. */
function terrainDemAllowed(viewport) {
  const t = manifest.terrainDem;
  if (!t || !t.url) return false;
  return Array.isArray(t.viewports) && t.viewports.includes(viewport);
}

// ---------------------------------------------------------------------------
// Shared block chrome (DOM only — no engine code)
// ---------------------------------------------------------------------------
/**
 * Builds the block <section> with map host, title, sidebar, view controls and
 * attribution strip, appends it to `mount`, and returns the useful refs.
 */
function buildBlockChrome(mount, { title, subtitle, hostClass, withExport, exportMode, materials }) {
  // Three-column layout: layers (left) | map (centre) | tools (right).
  const section = h("section", "map-block");
  const host = h("div", hostClass);
  const center = h("div", "map-center");

  // ---- left toolbar: everything about layers ----
  const sidebar = h("aside", "sidebar");
  const header = h("header", "sidebar-header");
  const headText = h("div", "sidebar-header-text");
  const hh = h("h2"); hh.textContent = title;
  const sub = h("p", "subtitle"); sub.textContent = subtitle || "";
  headText.append(hh, sub);
  header.append(headText);
  const groupsEl = h("div", "layer-groups");
  sidebar.append(header, groupsEl);

  // ---- right toolbar: everything done *with* the layers ----
  const tools = h("aside", "tools");
  const toolsHead = h("header", "sidebar-header");
  const toolsTitle = h("h2"); toolsTitle.textContent = "Tools";
  toolsHead.append(toolsTitle);
  tools.append(toolsHead);
  const toolsBody = h("div", "tools-body");
  tools.append(toolsBody);

  let exportToggleBtn = null, selectAreaBtn = null, exportRunBtn = null, baseBorderBtn = null;
  let selectPolyBtn = null, baseBorder2Btn = null, baseBorderCurveBtn = null;
  let formatRow = null, formatSel = null, originCb = null, exportBarEl = null;
  if (withExport && exportMode !== "zone") {
    const exSec = h("div", "tools-section");
    const exLabel = h("div", "tools-label"); exLabel.textContent = "Export layers";
    exportToggleBtn = h("button", "btn-export-toggle", { type: "button" });
    exportToggleBtn.textContent = "Export layers";

    const toolbar = h("div", "sidebar-toolbar");
    selectAreaBtn = h("button", "btn-select-area", { type: "button", disabled: true, hidden: true });
    selectAreaBtn.textContent = "Select area (rectangle)";
    // Curved export area: click the vertices, close on the first point / double-click / Enter.
    selectPolyBtn = h("button", "btn-select-area", { type: "button", disabled: true, hidden: true });
    selectPolyBtn.textContent = "Select area (curve)";
    // TASK 16 — same export path as "Select area", but the clip rectangle is the
    // fixed team-wide Base border (web/layers/base_border.json), not a drag.
    baseBorderBtn = h("button", "btn-base-border", { type: "button", disabled: true, hidden: true });
    baseBorderBtn.textContent = "Base border";
    baseBorder2Btn = h("button", "btn-base-border", { type: "button", disabled: true, hidden: true,
      title: "Base border 2.0: same top / left / right, bottom edge 25 % shorter" });
    baseBorder2Btn.textContent = "Base border 2.0";
    baseBorderCurveBtn = h("button", "btn-base-border", { type: "button", disabled: true, hidden: true,
      title: "Curved Base border (web/layers/base_border_curve.json)" });
    baseBorderCurveBtn.textContent = "Base border (curve)";
    exportRunBtn = h("button", "btn-export-run", { type: "button", disabled: true, hidden: true });
    exportRunBtn.textContent = "Export";
    // TASK 28 — export format: GeoJSON (zip, default) or 3DM (Rhino, built in the browser).
    formatRow = h("div", "export-format", { hidden: true });
    formatSel = h("select", "export-format-sel", { title: "Export format" });
    for (const [v, t] of [["geojson", "GeoJSON (zip)"], ["3dm", "3DM (Rhino, zip)"], ["svg", "SVG (2D plan, zip)"]]) {
      const o = h("option", null, { value: v }); o.textContent = t; formatSel.append(o);
    }
    const originLabel = h("label", "export-origin", { hidden: true,
      title: "Shift all coordinates to a local origin (centre of the area, rounded to 1 km). The offset is written to manifest.json and the document User Text (originEasting / originNorthing). Off = absolute EPSG:25832, same as the terrain / OBJ files." });
    originCb = h("input", null, { type: "checkbox", checked: true });
    const originTxt = h("span"); originTxt.textContent = " local origin (small coordinates)";
    originLabel.append(originCb, originTxt);
    formatRow.append(formatSel, originLabel);
    formatSel.addEventListener("change", () => { originLabel.hidden = formatSel.value !== "3dm"; });
    toolbar.append(formatRow, selectAreaBtn, selectPolyBtn, baseBorderBtn, baseBorder2Btn, baseBorderCurveBtn, exportRunBtn);

    // Progress / warning line — hidden until it has something to say.
    exportBarEl = h("div", "map-export", { hidden: true });
    exSec.append(exLabel, exportToggleBtn, toolbar, exportBarEl);
    toolsBody.append(exSec);
  }

  // Export zone (Street Section Analysis): draw a closed contour on the 2D map -> 3DM of everything it touches.
  let zoneBtn = null, zoneClearBtn = null, zoneExportBtn = null, zoneStatus = null;
  if (exportMode === "zone") {
    const zSec = h("div", "tools-section");
    const zLabel = h("div", "tools-label"); zLabel.textContent = "Export zone";
    zoneBtn = h("button", "btn-zone", { type: "button" }); zoneBtn.textContent = "Draw export zone";
    zoneClearBtn = h("button", "btn-zone-clear", { type: "button", disabled: true }); zoneClearBtn.textContent = "Clear zone";
    zoneExportBtn = h("button", "btn-export-run", { type: "button", disabled: true }); zoneExportBtn.textContent = "Export 3DM";
    zoneStatus = h("div", "map-export", { hidden: true });
    const zNote = h("div", "tools-note"); zNote.textContent = "Format: 3DM (Rhino). Buildings touching the zone turn red.";
    zSec.append(zLabel, zoneBtn, zoneClearBtn, zoneExportBtn, zoneStatus, zNote);
    toolsBody.append(zSec);
  }

  // Scale grid (EPSG:25832, spacing follows the zoom).
  const gridSec = h("div", "tools-section");
  const gridLabel = h("div", "tools-label"); gridLabel.textContent = "Scale grid";
  const gridBtn = h("button", "btn-grid", { type: "button" }); gridBtn.textContent = "Grid: off";
  const gridNote = h("div", "tools-note"); gridNote.textContent = "Metric grid, spacing follows the zoom.";
  gridSec.append(gridLabel, gridBtn, gridNote);
  if (exportMode !== "zone") toolsBody.append(gridSec);

  // Map image export (PNG / PDF, high resolution).
  const imgSec = h("div", "tools-section");
  const imgLabel = h("div", "tools-label"); imgLabel.textContent = "Export map image";
  const imgRow = h("div", "tools-row");
  const pngBtn = h("button", "btn-img", { type: "button" }); pngBtn.textContent = "PNG";
  const pdfBtn = h("button", "btn-img", { type: "button" }); pdfBtn.textContent = "PDF";
  imgRow.append(pngBtn, pdfBtn);
  const annotLabel = h("label", "export-origin");
  const annotCheck = h("input", null, { type: "checkbox" });
  const annotTxt = h("span"); annotTxt.textContent = " include legend";
  annotLabel.append(annotCheck, annotTxt);
  const imgNote = h("div", "tools-note"); imgNote.textContent = "Current view, high resolution.";
  imgSec.append(imgLabel, imgRow, annotLabel, imgNote);
  if (exportMode !== "zone") toolsBody.append(imgSec);

  // Downloadable field materials (e.g. Space Syntax counting sheets). `href` missing = shown but not available yet.
  if (Array.isArray(materials) && materials.length) {
    const matSec = h("div", "tools-section");
    const matLabel = h("div", "tools-label"); matLabel.textContent = "Counting materials";
    matSec.append(matLabel);
    for (const m of materials) {
      const a = m.href
        ? h("a", "btn-material", { href: m.href, download: "", target: "_blank", rel: "noopener" })
        : h("span", "btn-material is-soon", { title: "Coming soon" });
      a.textContent = m.label + (m.href ? "" : " — soon");
      matSec.append(a);
    }
    if (materials.some((m) => m.note)) {
      const n = h("div", "tools-note"); n.textContent = materials.find((m) => m.note).note; matSec.append(n);
    }
    toolsBody.append(matSec);
  }

  // ---- centre: map + overlays ----
  const viewControls = h("div", "view-controls");
  const viewSwitch = h("div", "view-switch");
  const btn2d = h("button", "active", { type: "button", "data-mode": "2d" }); btn2d.textContent = "2D";
  const btn3d = h("button", null, { type: "button", "data-mode": "3d" }); btn3d.textContent = "3D";
  viewSwitch.append(btn2d, btn3d);
  const terrainBtn = h("button", "btn-terrain", { type: "button", hidden: true, title: "Toggle 3D terrain (elevation)" });
  terrainBtn.textContent = "Terrain: off";
  viewControls.append(viewSwitch, terrainBtn);

  const attributionEl = h("footer", "attribution");
  const legendEl = h("div", "use-legend", { hidden: true });   // TASK 39: colour-by-category legend

  const annotBtn = h("button", "btn-annot", { type: "button" });
  annotBtn.textContent = "Show legend";
  const gridCanvas = h("canvas", "grid-canvas", { hidden: true });
  const gridScaleEl = h("div", "grid-label", { hidden: true });
  center.append(host, gridCanvas, viewControls, legendEl, annotBtn, gridScaleEl, attributionEl);
  section.append(sidebar, center, tools);
  mount.append(section);

  // Sources strip — normal flow, right under the three-column block.
  const metaEl = h("section", "map-meta");
  mount.append(metaEl);

  return {
    section, host, groupsEl, viewSwitch, btn2d, btn3d, terrainBtn, attributionEl, legendEl, metaEl,
    exportToggleBtn, selectAreaBtn, selectPolyBtn, baseBorderBtn, baseBorder2Btn, baseBorderCurveBtn, exportRunBtn, exportBarEl,
    formatRow, formatSel, originCb, pngBtn, pdfBtn, gridBtn, gridNote, annotBtn, annotCheck,
    zoneBtn, zoneClearBtn, zoneExportBtn, zoneStatus, gridCanvas, gridScaleEl,
  };
}

// ---------------------------------------------------------------------------
// Metadata block (under the map) — reusable, data-driven
// ---------------------------------------------------------------------------
/**
 * Fills `container` with a single one-line list of the source resources this
 * viewport's layers come from — resource name linked to its portal, nothing
 * else. Order follows manifest.resources.
 */
function renderMetadataPanel(container, viewport) {
  container.innerHTML = "";
  const resources = manifest.resources || [];
  const used = new Set(layersForViewport(viewport).map((l) => l.resourceId));
  const list = resources.filter((r) => used.has(r.id));
  if (list.length === 0) return;

  const label = h("span", "meta-label");
  label.textContent = "Sources:";
  container.append(label);

  list.forEach((r, i) => {
    const a = h("a", "meta-src", { href: r.url || "#", target: "_blank", rel: "noopener" });
    a.textContent = r.name;
    container.append(a);
    if (i < list.length - 1) {
      const sep = h("span", "meta-sep");
      sep.textContent = "·";
      container.append(sep);
    }
  });
}

// ---------------------------------------------------------------------------
// Shared sidebar renderer (engine-agnostic)
// ---------------------------------------------------------------------------
/**
 * @param handlers {{ setLayerVisible(id, on):void, onEnable?(layer):void }}
 */
function renderSidebar(groupsEl, viewport, handlers) {
  groupsEl.innerHTML = "";

  const semantic = sidebarGroupsForViewport(viewport);
  if (semantic) {
    renderSemanticSidebar(groupsEl, semantic, handlers, viewport);
    return;
  }

  const resources = manifest.resources || [];
  const layers = layersForViewport(viewport);
  const known = new Set(resources.map((r) => r.id));

  layers.filter((l) => !known.has(l.resourceId)).forEach((l) =>
    console.warn(`[${viewport}] layer "${l.id}" -> unknown resourceId "${l.resourceId}".`)
  );

  const groups = resources
    .map((r) => ({ resource: r, layers: layers.filter((l) => l.resourceId === r.id) }))
    .filter((g) => g.layers.length > 0);

  if (groups.length === 0) {
    groupsEl.innerHTML = '<p class="panel-empty">No layers assigned to this viewport yet.</p>';
    return;
  }

  for (const { resource, layers: groupLayers } of groups) {
    const group = h("div", "group");
    const gHeader = h("div", "group-header");
    const toggle = h("span", "group-toggle"); toggle.textContent = "▾";
    const gTitle = h("a", "group-title", { href: resource.url || "#", target: "_blank", rel: "noopener" });
    gTitle.textContent = resource.name;
    gTitle.addEventListener("click", (e) => e.stopPropagation());
    gHeader.append(toggle, gTitle);
    gHeader.addEventListener("click", () => group.classList.toggle("collapsed"));

    const body = h("div", "group-body");
    for (const layer of groupLayers) body.append(renderLayerRow(layer, handlers, viewport));

    group.append(gHeader, body);
    groupsEl.append(group);
  }
}

// --- semantic grouping (manifest.sidebar) -------------------------------------
/**
 * Groups are by meaning, not by source. Each item is one row / one eye toggle
 * that can drive several layer ids at once (e.g. "Pedestrian roads" = two ATKIS
 * sources under one switch). Names only — no descriptions or source links here.
 */
function renderSemanticSidebar(groupsEl, groupDefs, handlers, viewport) {
  for (const g of groupDefs) {
    const group = h("div", "group" + (g.collapsed ? " collapsed" : ""));
    const gHeader = h("div", "group-header");
    const toggle = h("span", "group-toggle"); toggle.textContent = "▾";
    const gTitle = h("span", "group-title"); gTitle.textContent = g.name;
    gHeader.append(toggle, gTitle);
    gHeader.addEventListener("click", () => group.classList.toggle("collapsed"));

    const body = h("div", "group-body");
    for (const item of g.items || []) body.append(renderSidebarItem(item, handlers, viewport));

    group.append(gHeader, body);
    groupsEl.append(group);
  }
}

function renderSidebarItem(item, handlers, viewport) {
  const layers = (item.layers || []).map(layerById).filter(Boolean);
  const isOn = () => layers.length > 0 &&
    layers.every((l) => layerVisibleInViewport(l, viewport));

  const row = h("div", "layer-row");
  const main = h("div", "layer-main");
  const name = h("span", "layer-name");
  name.textContent = item.name;
  name.title = item.name;
  const eye = h("button", "eye-btn", { type: "button" });

  const multi = layers.length > 1 && !item.categories;
  const subPaints = [];
  const subRows = [];   // [{ layerId, el }] — for the per-sublayer export checkboxes
  const paint = () => {
    const n = layers.filter((l) => layerVisibleInViewport(l, viewport)).length;
    const on = layers.length > 0 && n === layers.length, partial = n > 0 && !on;
    row.classList.toggle("on", n > 0);
    row.classList.toggle("off", n === 0);
    row.classList.toggle("partial", partial);
    eye.textContent = n > 0 ? "👁" : "🚫";
    eye.title = on ? "Visible — click to hide" : partial ? "Some layers visible — click to show all" : "Hidden — click to show";
    subPaints.forEach((f) => f());
  };

  eye.addEventListener("click", () => {
    const next = !isOn();
    for (const l of layers) {
      delete l.visibleViewports;
      l.visible = next;
      handlers.setLayerVisible(l.id, next);
      if (next && handlers.onEnable) handlers.onEnable(l);
    }
    paint();
  });

  paint();
  main.append(name);
  const palette = item.categories ? (manifest[item.categories] || []) : null;
  let panel = null;
  if (multi) {
    const caret = h("button", "cat-btn", { type: "button", title: "Show the layers inside" });
    caret.textContent = "+";
    main.append(caret);
    panel = h("div", "sub-panel", { hidden: true });
    for (const l of layers) {
      const sr = h("div", "sub-row");
      const sn = h("span", "sub-name"); sn.textContent = l.subName || l.name;
      const se = h("button", "eye-btn", { type: "button" });
      const sp = () => {
        const v = layerVisibleInViewport(l, viewport);
        sr.classList.toggle("off", !v);
        se.textContent = v ? "👁" : "🚫";
        se.title = v ? "Visible — click to hide" : "Hidden — click to show";
      };
      se.addEventListener("click", () => {
        const next = !layerVisibleInViewport(l, viewport);
        delete l.visibleViewports; l.visible = next;
        handlers.setLayerVisible(l.id, next);
        if (next && handlers.onEnable) handlers.onEnable(l);
        paint();
      });
      subPaints.push(sp); sp();
      sr.append(sn, se); panel.append(sr);
      subRows.push({ layerId: l.id, el: sr });
    }
    caret.addEventListener("click", () => {
      panel.hidden = !panel.hidden;
      caret.classList.toggle("open", !panel.hidden);
      caret.textContent = panel.hidden ? "+" : "–";
    });
  }
  if (palette && palette.length) {
    const caret = h("button", "cat-btn", { type: "button", title: "Choose categories" });
    caret.textContent = "+";
    main.append(caret);
    panel = h("div", "cat-panel", { hidden: true });
    const allNone = h("div", "cat-actions");
    const bAll = h("button", null, { type: "button" }); bAll.textContent = "All";
    const bNone = h("button", null, { type: "button" }); bNone.textContent = "None";
    allNone.append(bAll, bNone);
    panel.append(allNone);
    const boxes = [];
    const apply = () => {
      const on = boxes.filter((b) => b.cb.checked).map((b) => b.name);
      handlers.setLayerFilter && handlers.setLayerFilter(item.layers || [], item.filterProperty || "useCategory",
        on.length === boxes.length ? null : on);
    };
    for (const c of palette) {
      const lab = h("label", "cat-item");
      const cb = h("input", null, { type: "checkbox" }); cb.checked = true;
      const sw = h("span", "use-legend-swatch"); sw.style.background = c.color;
      const t = h("span"); t.textContent = c.name;
      lab.append(cb, sw, t);
      cb.addEventListener("change", apply);
      panel.append(lab);
      boxes.push({ cb, name: c.name });
    }
    bAll.addEventListener("click", () => { boxes.forEach((b) => (b.cb.checked = true)); apply(); });
    bNone.addEventListener("click", () => { boxes.forEach((b) => (b.cb.checked = false)); apply(); });
    caret.addEventListener("click", () => {
      panel.hidden = !panel.hidden;
      caret.classList.toggle("open", !panel.hidden);
      caret.textContent = panel.hidden ? "+" : "–";
    });
  }
  main.append(eye);
  row.append(main);
  if (panel) row.append(panel);
  if (handlers.decorateRow) handlers.decorateRow(row, item.layers || [], item.name, subRows);
  return row;
}

function renderLayerRow(layer, handlers, viewport) {
  const on0 = layerVisibleInViewport(layer, viewport);
  const row = h("div", "layer-row " + (on0 ? "on" : "off"));
  const main = h("div", "layer-main");
  const name = h("span", "layer-name");
  name.textContent = layer.name;
  name.title = layer.name;

  const eye = h("button", "eye-btn", { type: "button" });
  eye.textContent = on0 ? "👁" : "🚫";
  eye.title = on0 ? "Visible — click to hide" : "Hidden — click to show";

  let cur = on0;
  eye.addEventListener("click", () => {
    const next = !cur;
    cur = next;
    delete layer.visibleViewports;
    layer.visible = next;
    handlers.setLayerVisible(layer.id, next);
    row.classList.toggle("on", next);
    row.classList.toggle("off", !next);
    eye.textContent = next ? "👁" : "🚫";
    eye.title = next ? "Visible — click to hide" : "Hidden — click to show";
    if (next && handlers.onEnable) handlers.onEnable(layer);
  });

  main.append(name, eye);
  row.append(main);
  if (handlers.decorateRow) handlers.decorateRow(row, [layer.id], layer.name);
  return row;
}

/** Attribution strip HTML from the currently-visible layers of a viewport. */
function attributionHtml(viewport, engineCredit, baseAttribution = BASE_ATTRIBUTION) {
  const active = layersForViewport(viewport)
    .filter((l) => layerVisibleInViewport(l, viewport) && l.attribution && l.attribution !== "—")
    .map((l) => l.attribution);
  return [baseAttribution, engineCredit, ...new Set(active)].filter(Boolean).join(" · ");
}

// ===========================================================================
// Layer export (TASK 09) — checkbox selection + area rectangle + zip
// ===========================================================================
//
// Replaces the Cesium-era area-select design (old TASK 03): the drawing engine
// is MapLibre, and the layers that go into the package come from an independent
// set of checkboxes, NOT from what is currently visible (👁).
//
// The clip rules (unchanged from the original plan):
//   - points        — kept when the point falls inside the rectangle
//   - lines/polygons — kept WHOLE when the feature bbox intersects the
//                      rectangle (geometry is never cut at the border)
//   - weimar_admin_boundary, when selected — always whole, never clipped
//   - empty intersection is not an error — an empty FeatureCollection is
//     written and the user is warned in the UI
//   - wms / 3dtiles layers have no exportable geometry: their checkbox is
//     shown but disabled
//
// createExportController() is engine-agnostic glue wired by createMapLibreBlock()
// for every viewport (all four are MapLibre since TASK_15).

const _emptyFC = () => ({ type: "FeatureCollection", features: [] });

/** Corner ring of the export selection: the curved polygon when there is one, else the rectangle. */
function _selectionRing(s) {
  if (s.polygon && s.polygon.length >= 3) return s.polygon;
  return [[s.west, s.south], [s.east, s.south], [s.east, s.north], [s.west, s.north]];
}

function _rectPolygon(s) {
  if (s.polygon && s.polygon.length >= 3) {
    return { type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [[...s.polygon, s.polygon[0]]] } };
  }
  return {
    type: "Feature",
    properties: {},
    geometry: {
      type: "Polygon",
      coordinates: [[
        [s.west, s.south], [s.east, s.south], [s.east, s.north],
        [s.west, s.north], [s.west, s.south],
      ]],
    },
  };
}

function _bboxFromLngLat(a, b) {
  return {
    west: Math.min(a.lng, b.lng), east: Math.max(a.lng, b.lng),
    south: Math.min(a.lat, b.lat), north: Math.max(a.lat, b.lat),
  };
}

/** true if any coordinate position anywhere in `coords` is inside the rect. */
function _anyPositionInRect(coords, s) {
  if (typeof coords[0] === "number") {
    const [x, y] = coords;
    return x >= s.west && x <= s.east && y >= s.south && y <= s.north;
  }
  return coords.some((c) => _anyPositionInRect(c, s));
}

/** Recursive bbox of an arbitrarily nested GeoJSON coordinate array. */
function _coordsBBox(coords, bb) {
  bb = bb || { minX: Infinity, minY: Infinity, maxX: -Infinity, maxY: -Infinity };
  if (typeof coords[0] === "number") {
    const [x, y] = coords;
    if (x < bb.minX) bb.minX = x;
    if (x > bb.maxX) bb.maxX = x;
    if (y < bb.minY) bb.minY = y;
    if (y > bb.maxY) bb.maxY = y;
  } else {
    for (const c of coords) _coordsBBox(c, bb);
  }
  return bb;
}

function _bboxIntersects(bb, s) {
  return !(bb.maxX < s.west || bb.minX > s.east || bb.maxY < s.south || bb.minY > s.north);
}

/** Exact test against a curved selection (open ring [[lng,lat]...]); the bbox test already passed. */
function _geomHitsRing(g, ring) {
  const pts = [];
  const lines = [];      // [[x,y]...] each; polygon outer rings included
  const polys = [];      // polygon outer rings
  const walk = (gg) => {
    if (!gg) return;
    switch (gg.type) {
      case "GeometryCollection": (gg.geometries || []).forEach(walk); break;
      case "Point": pts.push(gg.coordinates); break;
      case "MultiPoint": pts.push(...gg.coordinates); break;
      case "LineString": lines.push(gg.coordinates); break;
      case "MultiLineString": lines.push(...gg.coordinates); break;
      case "Polygon": lines.push(gg.coordinates[0]); polys.push(gg.coordinates[0]); break;
      case "MultiPolygon": for (const p of gg.coordinates) { lines.push(p[0]); polys.push(p[0]); } break;
    }
  };
  walk(g);
  for (const [x, y] of pts) if (_ptInPoly(ring, x, y)) return true;
  for (const l of lines) {
    for (const [x, y] of l) if (_ptInPoly(ring, x, y)) return true;
    for (let i = 0; i < l.length - 1; i++) {
      for (let j = 0; j < ring.length; j++) {
        const q = ring[j], r = ring[(j + 1) % ring.length];
        if (_segX(l[i][0], l[i][1], l[i + 1][0], l[i + 1][1], q[0], q[1], r[0], r[1])) return true;
      }
    }
  }
  for (const o of polys) if (_ptInPoly(o, ring[0][0], ring[0][1])) return true;   // selection inside a big polygon
  return false;
}

function _geomIntersectsSelection(g, s) {
  if (!g) return false;
  if (g.type === "GeometryCollection")
    return (g.geometries || []).some((gg) => _geomIntersectsSelection(gg, s));
  if (g.type === "Point" || g.type === "MultiPoint") {
    if (!_anyPositionInRect(g.coordinates, s)) return false;
    return s.polygon ? _geomHitsRing(g, s.polygon) : true;
  }
  if (!g.coordinates || !g.coordinates.length) return false;
  if (!_bboxIntersects(_coordsBBox(g.coordinates), s)) return false;
  return s.polygon ? _geomHitsRing(g, s.polygon) : true;
}

/** Clip one FeatureCollection to the selection rectangle per the rules above. */
// Category filters currently set from the sidebar ("+" panel of rows such as "Parcels use"),
// keyed `${viewport}:${layerId}` -> { prop, values } (values = array of allowed values).
// The export applies the same filter, so it contains exactly what the map shows.
const _categoryFilters = new Map();

function clipFeatureCollection(fc, selection, layerId) {
  const feats = (fc && fc.features) || [];
  const kept = layerId === "weimar_admin_boundary"
    ? feats.slice()
    : feats.filter((f) => _geomIntersectsSelection(f && f.geometry, selection));
  return { type: "FeatureCollection", features: kept };
}

function _downloadStamp() {
  return new Date().toISOString().replace(/[:]/g, "-").replace(/\..+$/, "").replace("T", "_");
}

function triggerDownload(blob, filename) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.append(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 0);
}

/** Filesystem-safe id derived from a sidebar item's display name. */
function _slug(s) {
  return String(s).toLowerCase().trim().replace(/[^a-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "layer";
}

/**
 * Fetches + clips every source layer of one export group (a sidebar row).
 * Shared by the GeoJSON zip and the 3DM export. Returns the merged clipped
 * features and per-source-layer metadata { id, layer, res, featureCount }.
 */
async function collectGroupFeatures(group, selection, resById) {
  const mergedFeatures = [];
  const layerMetas = [];
  for (const id of group.ids) {
    const layer = layerById(id);
    if (!layer) continue;
    const res = resById.get(layer.resourceId);

    let fc;
    try {
      console.log(`[export] fetching ${id} …`);
      const r = await fetch(resolveUrl(layer.url), { cache: "force-cache" });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      fc = await r.json();
    } catch (err) {
      throw new Error(`could not fetch layer "${id}": ${err.message}`);
    }

    let clipped = clipFeatureCollection(fc, selection, id);
    const flt = group.filters && group.filters.get(id);
    if (flt) {
      const allowed = new Set(flt.values);
      clipped = { type: "FeatureCollection",
        features: clipped.features.filter((f) => allowed.has(f.properties && f.properties[flt.prop])) };
    }
    console.log(`[export] ${id}: ${(fc.features || []).length} → ${clipped.features.length} kept`);
    mergedFeatures.push(...clipped.features);
    layerMetas.push({ id, layer, res, featureCount: clipped.features.length });
  }
  return { mergedFeatures, layerMetas };
}

/**
 * Fetches every selected group's GeoJSON, clips it, and packs a zip:
 * one file per sidebar row (`group.fileId`.geojson) + `manifest.json` +
 * `ATTRIBUTION.txt`. A group can span several underlying layer ids — e.g.
 * "Pedestrian roads" is one sidebar toggle backed by two ATKIS sources — in
 * which case their clipped features are merged into that one output file, so
 * the export matches what the sidebar shows as a single layer.
 * `groups`: [{ fileId, name, ids: [layerId…] }]
 * Returns { blob, empties: [fileId…], total }.
 */
async function buildExportZip(viewport, groups, selection, opts = {}) {
  if (typeof JSZip === "undefined") throw new Error("JSZip is not loaded");
  const zip = new JSZip();
  const includeBaseBorder = !!opts.includeBaseBorder;
  const resById = new Map((manifest.resources || []).map((r) => [r.id, r]));
  const generated = new Date().toISOString();

  const manifestOut = {
    generated,
    viewport,
    crs: "EPSG:4326",
    selection,
    selectionGeometry: _rectPolygon(selection).geometry,
    clipRules: {
      points: "kept when inside the selection (rectangle or curve)",
      linesAndPolygons: "kept whole when the feature touches the selection (rectangle: bbox test; curve: exact test); never cut at the border",
      weimar_admin_boundary: "always whole, never clipped",
    },
    layers: [],
  };

  const attrib = [
    "Data Twin City Weimar — layer export",
    `Generated: ${generated}`,
    `Viewport: ${viewport}`,
    `Selection (EPSG:4326): W ${selection.west}  S ${selection.south}  E ${selection.east}  N ${selection.north}`,
    "",
    "Base map shown for context is NOT part of this package:",
    "  © OpenStreetMap contributors (ODbL) · © CARTO",
    "",
    "Exported layers",
    "==============",
    "",
  ];

  const empties = [];
  for (const group of groups) {
    const { mergedFeatures, layerMetas } = await collectGroupFeatures(group, group.selection || selection, resById);

    const fileName = `${group.fileId}.geojson`;
    if (mergedFeatures.length === 0) empties.push(group.fileId);
    zip.file(fileName, JSON.stringify({ type: "FeatureCollection", features: mergedFeatures }));

    manifestOut.layers.push({
      file: fileName,
      id: group.fileId,
      name: group.name,
      type: "geojson",
      featureCount: mergedFeatures.length,
      empty: mergedFeatures.length === 0,
      clip: group.ids.includes("weimar_admin_boundary") ? "none (whole)" : "bbox-intersect / point-in-rect",
      sourceLayers: layerMetas.map((m) => m.id),
      attribution: [...new Set(layerMetas.map((m) => m.layer.attribution).filter(Boolean))].join(" / "),
      sourceResource: [...new Set(layerMetas.map((m) => (m.res ? m.res.name : m.layer.resourceId)))].join(", "),
      sourceUrl: (layerMetas.find((m) => m.res && m.res.url) || {}).res?.url || "",
      sourceDoc: layerMetas.map((m) => m.layer.sourceDoc).filter(Boolean).join(", "),
    });

    attrib.push(`- ${group.name}  [${fileName}]  (${mergedFeatures.length} feature(s))`);
    for (const m of layerMetas) {
      attrib.push(`    ${m.layer.attribution || "(no attribution string on record)"}`);
      attrib.push(`    Source: ${m.res ? m.res.name : m.layer.resourceId}${m.res && m.res.url ? `  <${m.res.url}>` : ""}`);
    }
    attrib.push("");
  }

  // TASK 16 — when the export area IS the team-wide Base border, ship the
  // rectangle itself as its own GeoJSON layer (a single Polygon feature), so the
  // exported package carries the reference frame it was clipped to.
  if (includeBaseBorder) {
    const borderFeature = _rectPolygon(selection);
    borderFeature.properties = {
      name: "Base border",
      note: "Team-wide standard export area (web/layers/base_border.json)",
      west: selection.west, south: selection.south,
      east: selection.east, north: selection.north,
    };
    const borderFC = { type: "FeatureCollection", features: [borderFeature] };
    zip.file("base_border.geojson", JSON.stringify(borderFC));
    manifestOut.layers.push({
      file: "base_border.geojson",
      id: "base_border",
      name: "Base border",
      type: "geojson",
      featureCount: 1,
      empty: false,
      clip: "none (the selection rectangle itself)",
      attribution: "Data Twin City Weimar — team-wide reference frame",
      sourceResource: "Data Twin City Weimar",
      sourceUrl: "",
      sourceDoc: "",
    });
    attrib.push("- Base border  [base_border.geojson]  (1 feature)");
    attrib.push("    Data Twin City Weimar — team-wide reference frame");
    attrib.push("    Source: web/layers/base_border.json");
    attrib.push("");
  }

  zip.file("manifest.json", JSON.stringify(manifestOut, null, 2));
  zip.file("ATTRIBUTION.txt", attrib.join("\n"));

  console.log("[export] zipping …");
  const blob = await zip.generateAsync({ type: "blob" });
  console.log(`[export] done — ${blob.size} bytes`);
  return { blob, empties, total: groups.length };
}

// ===========================================================================
// 3DM export (TASK 28, geometry reworked in TASK 29) — Rhino file built in the
// browser with rhino3dm.js (WASM)
// ===========================================================================
//
// Same selection / fetch / clip path as the GeoJSON zip (collectGroupFeatures),
// then: WGS84 -> EPSG:25832 (proj4), one Rhino layer per platform layer, objects
// with Attribute User Text. No server, no native packages.
//   buildings      -> Rhino Extrusion (closed solid, z 0 .. height; courtyards = inner
//                     profiles); no height -> flat single-face Brep, counted in manifest
//   parcels/polys  -> flat single-face Brep at z = 0 (Brep.createTrimmedPlane; with holes:
//                     bottom face of a thin capped Extrusion) — no triangulation, no meshes
//   lines          -> polyline curves;  points -> points
// Everything is double precision (no float32 meshes). Rings are cleaned (repeated /
// collinear vertices) and self-intersecting rings are NOT built: their ids are listed
// in manifest.json (`skippedIds`). "Local origin" mode subtracts a rounded origin
// (manifest + document User Text originEasting / originNorthing / crs).

const RHINO3DM_URL = "https://unpkg.com/rhino3dm@8.4.0/rhino3dm.module.min.js";
const EXPORT_3DM_WARN_OBJECTS = 15000;
// rhino3dm.js 8.4.0 File3dm.toByteArray() silently returns a truncated buffer (Rhino: "damaged geometry
// table") or aborts once a document passes ~150 MB (works at 141 MB, broken at ~200 MB — tested in Node).
// Bigger exports are split into several .3dm files of at most this estimated size each.
const EXPORT_3DM_PART_BYTES = 100 * 1048576;
// Measured in TASK 29 (headless Edge, default Base border): file bytes and build ms per object.
const EXPORT_3DM_EST = {
  // bytes measured on a real 76 734-object export (Extrusion ≈ 1.6 KB, planar Brep: parcel ≈ 9 KB, footprint ≈ 5.6 KB)
  building: { bytes: 1700, ms: 1.5 }, parcel: { bytes: 9000, ms: 1.5 }, footprint: { bytes: 5600, ms: 1.5 },
  line: { bytes: 800, ms: 0.5 }, point: { bytes: 300, ms: 0.5 },
};
let _rhinoPromise = null;
let _projReady = false;

function loadRhino3dm() {
  if (!_rhinoPromise) {
    _rhinoPromise = import(RHINO3DM_URL).then((m) => (m.default || m)());
    _rhinoPromise.catch(() => { _rhinoPromise = null; });
  }
  return _rhinoPromise;
}

/** [lng, lat] (WGS84) -> [E, N] (EPSG:25832) */
function _toUtm32(lng, lat) {
  if (!_projReady) {
    proj4.defs("EPSG:25832", "+proj=utm +zone=32 +ellps=GRS80 +towgs84=0,0,0,0,0,0,0 +units=m +no_defs");
    _projReady = true;
  }
  return proj4("EPSG:4326", "EPSG:25832", [lng, lat]);
}

/** [E, N] (EPSG:25832) -> [lng, lat] (WGS84) */
function _fromUtm32(e, n) {
  _toUtm32(0, 0); // makes sure the projection is defined
  return proj4("EPSG:25832", "EPSG:4326", [e, n]);
}

/** Turns every colour of the loaded basemap style into a grey of the same luminance. */
function grayscaleBasemap(map) {
  const ctx = document.createElement("canvas").getContext("2d");
  const toGray = (c) => {
    ctx.fillStyle = "#000000"; ctx.fillStyle = c;
    const v = ctx.fillStyle;
    let r, g, b, a = 1;
    if (v[0] === "#") { r = parseInt(v.slice(1, 3), 16); g = parseInt(v.slice(3, 5), 16); b = parseInt(v.slice(5, 7), 16); }
    else { const m = v.match(/[\d.]+/g); if (!m) return c; [r, g, b] = m.map(Number); if (m.length > 3) a = Number(m[3]); }
    const y = Math.round(0.299 * r + 0.587 * g + 0.114 * b);
    return a < 1 ? `rgba(${y},${y},${y},${a})` : `rgb(${y},${y},${y})`;
  };
  const conv = (v) => typeof v === "string" ? (/^(#|rgb|hsl)/i.test(v) ? toGray(v) : v)
    : Array.isArray(v) ? v.map(conv) : v;
  for (const layer of map.getStyle().layers || []) {
    for (const [k, val] of Object.entries(layer.paint || {})) {
      if (!/color$/.test(k)) continue;
      try { map.setPaintProperty(layer.id, k, conv(val)); } catch (e) { /* skip */ }
    }
  }
}

const _tick = () => new Promise((r) => setTimeout(r, 0));

/** Non-empty scalar properties as strings (empty / null / objects are dropped). */
function _userStrings(props, keys) {
  const out = {};
  const src = props || {};
  for (const k of keys || Object.keys(src)) {
    const v = src[k];
    if (v === null || v === undefined || v === "" || typeof v === "object") continue;
    out[k] = String(v);
  }
  return out;
}

/** Ring [[lng,lat]…] -> flat [x0,y0,x1,y1…] in local metres, closing vertex dropped. */
function _ringXY(ring, ox, oy) {
  const closed = ring.length > 1 && ring[0][0] === ring[ring.length - 1][0] && ring[0][1] === ring[ring.length - 1][1];
  const n = closed ? ring.length - 1 : ring.length;
  const xy = new Array(n * 2);
  for (let i = 0; i < n; i++) {
    const [e, nn] = _toUtm32(ring[i][0], ring[i][1]);
    xy[2 * i] = e - ox; xy[2 * i + 1] = nn - oy;
  }
  return xy;
}

function _signedArea(xy) {
  let a = 0;
  for (let i = 0, n = xy.length / 2; i < n; i++) {
    const j = (i + 1) % n;
    a += xy[2 * i] * xy[2 * j + 1] - xy[2 * j] * xy[2 * i + 1];
  }
  return a / 2;
}

function _reverseXY(xy) {
  const n = xy.length / 2, rev = new Array(xy.length);
  for (let k = 0; k < n; k++) { rev[2 * k] = xy[2 * (n - 1 - k)]; rev[2 * k + 1] = xy[2 * (n - 1 - k) + 1]; }
  return rev;
}

/** Drop repeated and collinear vertices, so the profile curves are valid for Extrusion / Brep. */
function _cleanRingXY(xy) {
  let pts = [];
  for (let i = 0; i < xy.length; i += 2) {
    const n = pts.length;
    if (n && Math.abs(pts[n - 2] - xy[i]) < 1e-6 && Math.abs(pts[n - 1] - xy[i + 1]) < 1e-6) continue;
    pts.push(xy[i], xy[i + 1]);
  }
  let changed = true;
  while (changed && pts.length >= 6) {
    changed = false;
    const n = pts.length / 2, keep = [];
    for (let i = 0; i < n; i++) {
      const a = (i + n - 1) % n, b = (i + 1) % n;
      const cross = (pts[2 * i] - pts[2 * a]) * (pts[2 * b + 1] - pts[2 * a + 1]) - (pts[2 * i + 1] - pts[2 * a + 1]) * (pts[2 * b] - pts[2 * a]);
      if (Math.abs(cross) < 1e-6) { changed = true; continue; }
      keep.push(pts[2 * i], pts[2 * i + 1]);
    }
    if (changed) pts = keep;
  }
  return pts;
}

/** true if any two non-adjacent segments of the closed ring (flat xy, no repeated end vertex) cross. */
function _ringSelfIntersects(xy) {
  const n = xy.length / 2;
  const cr = (ax, ay, bx, by, cx, cy) => (bx - ax) * (cy - ay) - (by - ay) * (cx - ax);
  for (let i = 0; i < n; i++) {
    const i2 = (i + 1) % n;
    const ax = xy[2 * i], ay = xy[2 * i + 1], bx = xy[2 * i2], by = xy[2 * i2 + 1];
    for (let j = i + 2; j < n; j++) {
      const j2 = (j + 1) % n;
      if (j2 === i) continue; // adjacent through the closing edge
      const cx = xy[2 * j], cy = xy[2 * j + 1], dx = xy[2 * j2], dy = xy[2 * j2 + 1];
      if (Math.max(ax, bx) < Math.min(cx, dx) || Math.max(cx, dx) < Math.min(ax, bx) ||
          Math.max(ay, by) < Math.min(cy, dy) || Math.max(cy, dy) < Math.min(ay, by)) continue;
      const d1 = cr(ax, ay, bx, by, cx, cy), d2 = cr(ax, ay, bx, by, dx, dy);
      const d3 = cr(cx, cy, dx, dy, ax, ay), d4 = cr(cx, cy, dx, dy, bx, by);
      if (((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0))) return true;
    }
  }
  return false;
}

/** Closed PolylineCurve from a flat xy ring (closing vertex appended), z = 0. */
function _ringCurve(rhino, xy) {
  const n = xy.length / 2;
  const pl = new rhino.Polyline(n + 1);
  for (let i = 0; i < n; i++) pl.add(xy[2 * i], xy[2 * i + 1], 0);
  pl.add(xy[0], xy[1], 0);
  return pl.toPolylineCurve();
}

/**
 * Polygon (outer + holes, GeoJSON rings) -> native Rhino geometry.
 *   height > 0 : Extrusion (closed solid, z 0..height; holes = inner profiles)
 *   height = 0 : single-face planar Brep at z = 0
 * Returns { geom, kind: "extrusion" | "brep" } or { skip: reason }.
 * Outer ring is oriented CCW, holes CW; repeated / collinear vertices are removed;
 * a self-intersecting ring is refused (skip: "self-intersecting").
 */
function _polygonObject(rhino, rings, height, ox, oy) {
  const rs = rings.map((r, i) => {
    const xy = _cleanRingXY(_ringXY(r, ox, oy));
    if (xy.length < 6) return null;
    return (_signedArea(xy) > 0) === (i === 0) ? xy : _reverseXY(xy);
  });
  if (!rs[0]) return { skip: "degenerate ring" };
  const holes = rs.slice(1).filter(Boolean);
  for (const xy of [rs[0], ...holes]) if (_ringSelfIntersects(xy)) return { skip: "self-intersecting" };

  if (height > 0) {
    const ex = rhino.Extrusion.create(_ringCurve(rhino, rs[0]), height, true);
    if (!ex) return { skip: "extrusion failed" };
    for (const h of holes) if (!ex.addInnerProfile(_ringCurve(rhino, h))) return { skip: "inner profile rejected" };
    return { geom: ex, kind: "extrusion" };
  }
  if (!holes.length) {
    const b = rhino.Brep.createTrimmedPlane(rhino.Plane.worldXY(), _ringCurve(rhino, rs[0]));
    return b ? { geom: b, kind: "brep" } : { skip: "planar brep failed" };
  }
  // holes: bottom cap (z = 0) of a thin capped extrusion = one planar face with inner loops
  const ex = rhino.Extrusion.create(_ringCurve(rhino, rs[0]), 1, true);
  if (!ex) return { skip: "extrusion failed" };
  for (const h of holes) if (!ex.addInnerProfile(_ringCurve(rhino, h))) return { skip: "inner profile rejected" };
  const brep = ex.toBrep(true), F = brep.faces();
  for (let i = 0; i < F.count; i++) {
    const face = F.get(i), d = face.duplicateFace(false), bb = d.getBoundingBox();
    if (Math.abs(bb.max[2]) < 1e-9) return { geom: d, kind: "brep" };
  }
  return { skip: "planar brep with holes failed" };
}

/** Number of Rhino objects one GeoJSON geometry becomes (multi-part = one object per part). */
function _objectCount(g) {
  if (!g) return 0;
  switch (g.type) {
    case "Polygon": case "LineString": case "Point": return 1;
    case "MultiPolygon": case "MultiLineString": case "MultiPoint": return g.coordinates.length;
    default: return 0;
  }
}

// TASK 35: Space Syntax layers are her hand-drawn networks, clipped to Base border (city centre only) —
// no export buffer any more (TASK_32's 2 km edge-effect buffer is gone; the export area for these groups
// is just the Base border/selection rectangle, same as every other group).
// City-wide variants (weimar_ss_*_city) export the same way: curves, z = 0, their own Rhino layer.
function _isSsLayer(layer) { return /^weimar_ss_(car|pedestrian)(_city2?)?$/.test(layer.id); }   // TASK 31
function _isFootprintLayer(layer) { return layer.id === "weimar_building_footprints" || layer.id === "weimar_building_use_footprints"; }
// TASK 39: "Building use" layers — same geometry as footprints / 3D buildings, split by useCategory
// into child Rhino layers under one parent layer (layer.rhinoLayer).
function _isUseLayer(layer) { return !!(layer.colorBy && layer.colorBy.property === "useCategory" && layer.rhinoLayer); }
function _isBuildingLayer(layer) {
  return /^weimar_buildings/.test(layer.id) || /buildings_footprint/.test(layer.url || "");
}
function _isParcelLayer(layer) { return /^weimar_parcels(_use|_outline)?$/.test(layer.id); }

/**
 * Builds the .3dm + manifest + attribution zip. Returns
 * { blob, empties, total, summary, bytes, layerStats, manifestOut } or null when
 * the user cancels the large-selection warning. `opts.confirmLarge(n)` can be
 * overridden (tests).
 */
async function build3dmExport(viewport, groups, selection, opts = {}) {
  if (typeof JSZip === "undefined") throw new Error("JSZip is not loaded");
  if (typeof proj4 === "undefined") throw new Error("proj4 not loaded");
  const progress = opts.onProgress || (() => {});
  const t0 = performance.now();
  const resById = new Map((manifest.resources || []).map((r) => [r.id, r]));

  progress("3DM: loading rhino3dm (WASM)…");
  const rhino = await loadRhino3dm();

  // 1. fetch + clip everything first, so the size can be judged before building
  const collected = [];
  let totalFeatures = 0;
  for (const group of groups) {
    progress(`3DM: fetching “${group.name}”…`);
    const c = await collectGroupFeatures(group, group.selection || selection, resById);
    collected.push({ group, ...c });
    totalFeatures += c.mergedFeatures.length;
  }
  // large-selection warning: per-layer object counts + estimated size / time
  const kindOf = (metas) => (metas.some((m) => _isFootprintLayer(m.layer)) ? "footprint"
    : metas.some((m) => _isBuildingLayer(m.layer)) ? "building"
    : metas.some((m) => _isParcelLayer(m.layer)) ? "parcel" : null);
  const breakdown = collected.map((c) => {
    let objs = 0, bytes = 0, ms = 0;
    for (const f of c.mergedFeatures) {
      const g = f.geometry, n = _objectCount(g);
      const k = kindOf(c.layerMetas) || (g && /Point/.test(g.type) ? "point" : g && /Line/.test(g.type) ? "line" : "parcel");
      objs += n; bytes += n * EXPORT_3DM_EST[k].bytes; ms += n * EXPORT_3DM_EST[k].ms;
    }
    return { name: c.group.name, objects: objs, bytes, ms };
  });
  const totalObjects = breakdown.reduce((a, r) => a + r.objects, 0);
  const estBytes = breakdown.reduce((a, r) => a + r.bytes, 0);
  const estSecs = breakdown.reduce((a, r) => a + r.ms, 0) / 1000;
  const confirmLarge = opts.confirmLarge || ((info) => window.confirm(
    `This selection holds ${info.totalObjects.toLocaleString()} Rhino objects — the 3DM will take about ${Math.round(info.estSecs)} s to build and be about ${(info.estBytes / 1048576).toFixed(0)} MB (estimate).\n\n` +
    info.breakdown.map((r) => `  ${r.name}: ${r.objects.toLocaleString()}`).join("\n") +
    "\n\nOK = continue, Cancel = go back and pick a smaller area."));
  if (totalObjects > EXPORT_3DM_WARN_OBJECTS && !confirmLarge({ totalObjects, estBytes, estSecs, breakdown })) return null;

  // 2. local origin (optional)
  const sw = _toUtm32(selection.west, selection.south), ne = _toUtm32(selection.east, selection.north);
  const useOrigin = !!opts.localOrigin;
  const ox = useOrigin ? Math.round((sw[0] + ne[0]) / 2 / 1000) * 1000 : 0;
  const oy = useOrigin ? Math.round((sw[1] + ne[1]) / 2 / 1000) * 1000 : 0;

  // 3. Rhino document(s) — split into parts of <= EXPORT_3DM_PART_BYTES (estimate), see above
  const generated = new Date().toISOString();
  const parts = [];          // { est, objects, layers: Set<Rhino layer name>, bytes, file }
  let doc = null, part = null;
  const newDoc = () => {
    doc = new rhino.File3dm();
    doc.settings().modelUnitSystem = rhino.UnitSystem.Meters;
    try {
      doc.strings().set("source", "Data Twin City Weimar");
      doc.strings().set("crs", "EPSG:25832");
      doc.strings().set("originEasting", String(ox));
      doc.strings().set("originNorthing", String(oy));
    } catch (e) { console.warn("[3dm] document strings not written:", e.message); }
    part = { est: 0, objects: 0, layers: new Set(), bytes: null };
  };
  const finishDoc = async () => {
    progress(`3DM: writing file${parts.length ? ` (part ${parts.length + 1})` : ""}…`);
    await _tick();
    const bytes = doc.toByteArray();
    const count = doc.objects().count;
    if (doc.delete) doc.delete();
    doc = null;
    // read back: a truncated buffer comes back as null / with fewer objects
    const check = rhino.File3dm.fromByteArray(bytes);
    const ok = !!check && check.objects().count === count;
    if (check && check.delete) check.delete();
    if (!ok) throw new Error(`rhino3dm wrote a damaged .3dm (part ${parts.length + 1}, ${(bytes.length / 1048576).toFixed(0)} MB) — pick a smaller area or fewer layers`);
    part.bytes = bytes;
    parts.push(part);
  };
  newDoc();

  const usedNames = new Set();
  const layerStats = [];
  let built = 0;
  const uniqueName = (name) => {
    let nm = name || "layer", i = 2;
    while (usedNames.has(nm)) nm = `${name} (${i++})`;
    usedNames.add(nm);
    return nm;
  };
  const addLayer = (nm) => {
    const l = new rhino.Layer(); l.name = nm;
    part.layers.add(nm);
    return { idx: doc.layers().add(l), name: nm };
  };

  for (let gi = 0; gi < collected.length; gi++) {
    const { group, mergedFeatures, layerMetas } = collected[gi];
    // whole group into a fresh part when it does not fit into the current one but fits into an empty one
    const groupEst = breakdown[gi].bytes;
    if (part.objects > 0 && part.est + groupEst > EXPORT_3DM_PART_BYTES && groupEst <= EXPORT_3DM_PART_BYTES) {
      await finishDoc(); newDoc();
    }
    const objEst = breakdown[gi].objects ? groupEst / breakdown[gi].objects : 0;
    const ssMeta = layerMetas.find((m) => _isSsLayer(m.layer));
    const isS = !!ssMeta;                                              // TASK 31: Space Syntax networks
    const useMeta = layerMetas.find((m) => _isUseLayer(m.layer));
    const isU = !!useMeta;                                             // TASK 39: split by useCategory
    const layName = uniqueName(isS ? (ssMeta.layer.rhinoLayer || group.name) : isU ? useMeta.layer.rhinoLayer : group.name);
    const isF = layerMetas.some((m) => _isFootprintLayer(m.layer));   // TASK 30: flat, z=0, named by buildingId
    const isB = !isF && layerMetas.some((m) => _isBuildingLayer(m.layer));
    const isP = layerMetas.some((m) => _isParcelLayer(m.layer));
    // Parent layer + one child per category (always all of them, so the Layers panel looks the same for any
    // selection; empty categories stay empty). Child names are not in usedNames: Rhino only needs them unique per parent.
    const useCats = manifest[(useMeta && useMeta.layer.colorBy.palette) || "useCategories"] || [];
    // (re)created in every part this group spills into, with the same names
    let lay, childIdx, curLayerIdx;
    const setupLayers = () => {
      lay = addLayer(layName);
      childIdx = new Map();
      if (isU) {
        const parentId = doc.layers().get(lay.idx).id;
        for (const c of useCats) {
          const l = new rhino.Layer(); l.name = c.name; l.parentLayerId = parentId;
          const m = /^#?([0-9a-f]{6})$/i.exec(c.color || "");
          if (m) l.color = { r: parseInt(m[1].slice(0, 2), 16), g: parseInt(m[1].slice(2, 4), 16), b: parseInt(m[1].slice(4, 6), 16), a: 255 };
          childIdx.set(c.name, doc.layers().add(l));
        }
      }
      curLayerIdx = lay.idx;
    };
    setupLayers();
    const st = { name: layName, features: mergedFeatures.length, objects: 0, flatNoHeight: 0, skipped: 0, skippedIds: [], types: {},
      children: isU ? Object.fromEntries(useCats.map((c) => [c.name, 0])) : undefined,
      kind: isU ? (isF ? "footprints_by_use" : isP ? "parcels_by_use" : "buildings_by_use") : isS ? "spacesyntax" : isF ? "footprints" : isB ? "buildings" : isP ? "parcels" : "generic" };
    const keys = isS ? ["ssId", "network", "length", "srcClass"] : isF ? ["usage", "useCategory", "height", "floors", "buildingId", "area", "areaSource"] : isB ? ["usage", "useCategory", "height", "floors", "buildingId", "eaveHeight", "ridgeHeight", "roofType", "heightWithAttic", "atticFloorLikely"] : isP ? ["usage", "useCategory", "area", "areaSource"] : null;

    const attrFor = (f, name) => {
      const a = new rhino.ObjectAttributes();
      a.layerIndex = curLayerIdx;
      if (name) a.name = String(name);
      const us = _userStrings(f.properties, keys);
      for (const k in us) a.setUserString(k, us[k]);
      return a;
    };
    const nameOf = (f) => {
      const p = f.properties || {};
      return isS ? p.ssId : (isB || isF) ? p.buildingId : isP ? (p.label || p.nationalCadastralReference) : (p.name || p.label || p.id);
    };

    for (let i = 0; i < mergedFeatures.length; i++) {
      const f = mergedFeatures[i];
      const g = f.geometry;
      if (part.objects > 0 && part.est + objEst * _objectCount(g) > EXPORT_3DM_PART_BYTES) {
        await finishDoc(); newDoc(); setupLayers();
      }
      const before = st.objects;
      try {
        const nm = nameOf(f);
        let cat = null;
        if (isU) {
          cat = childIdx.has(f.properties && f.properties.useCategory) ? f.properties.useCategory : "Other";
          curLayerIdx = childIdx.get(cat);
        }
        const polys = !g ? [] : g.type === "Polygon" ? [g.coordinates] : g.type === "MultiPolygon" ? g.coordinates : null;
        if (polys) {
          const h = isB ? Number(f.properties && f.properties.height) : 0;
          const hasH = isB && Number.isFinite(h) && h > 0;
          if (isB && !hasH) st.flatNoHeight++;
          for (const rings of polys) {
            const r = _polygonObject(rhino, rings, hasH ? h : 0, ox, oy);
            if (r.skip) {
              st.skipped++;
              if (st.skippedIds.length < 500) st.skippedIds.push({ id: nm || null, reason: r.skip });
              continue;
            }
            doc.objects().add(r.geom, attrFor(f, nm)); st.objects++;
            if (cat) st.children[cat]++;
            st.types[r.kind] = (st.types[r.kind] || 0) + 1;
          }
        } else if (g && (g.type === "LineString" || g.type === "MultiLineString")) {
          const lines = g.type === "LineString" ? [g.coordinates] : g.coordinates;
          for (const ln of lines) {
            if (ln.length < 2) { st.skipped++; continue; }
            if (isS && ln.length === 2) {   // straight segment -> a single LineCurve
              const [a, b] = ln.map(([lng, lat]) => _toUtm32(lng, lat));
              doc.objects().addCurve(new rhino.LineCurve([a[0] - ox, a[1] - oy, 0], [b[0] - ox, b[1] - oy, 0]), attrFor(f, nm));
              st.objects++; st.types.line = (st.types.line || 0) + 1; continue;
            }
            const pl = new rhino.Polyline(ln.length);
            for (const [lng, lat] of ln) { const [e, n] = _toUtm32(lng, lat); pl.add(e - ox, n - oy, 0); }
            doc.objects().addCurve(pl.toPolylineCurve(), attrFor(f, nm)); st.objects++; st.types.curve = (st.types.curve || 0) + 1;
          }
        } else if (g && (g.type === "Point" || g.type === "MultiPoint")) {
          const pts = g.type === "Point" ? [g.coordinates] : g.coordinates;
          for (const [lng, lat] of pts) {
            const [e, n] = _toUtm32(lng, lat);
            doc.objects().add(new rhino.Point([e - ox, n - oy, 0]), attrFor(f, nm)); st.objects++; st.types.point = (st.types.point || 0) + 1;
          }
        } else st.skipped++;
      } catch (err) {
        console.warn("[3dm] feature skipped:", err.message); st.skipped++;
      }
      part.objects += st.objects - before;
      part.est += objEst * (st.objects - before);
      if (++built % 250 === 0) {
        progress(`3DM: building objects… ${built.toLocaleString()} / ${totalFeatures.toLocaleString()}`);
        await _tick();
      }
    }
    layerStats.push(st);
  }

  // Base border as its own layer (closed polyline) when it is the export area
  if (opts.includeBaseBorder) {
    const lay = addLayer(uniqueName("Base border"));
    const a = new rhino.ObjectAttributes(); a.layerIndex = lay.idx; a.name = "Base border";
    const ring = _selectionRing(selection);
    const pl = new rhino.Polyline(ring.length + 1);
    for (const [lng, lat] of [...ring, ring[0]]) {
      const [x, y] = _toUtm32(lng, lat); pl.add(x - ox, y - oy, 0);
    }
    doc.objects().addCurve(pl.toPolylineCurve(), a);
    part.objects++;
    layerStats.push({ name: lay.name, kind: "base_border", features: 1, objects: 1, flatNoHeight: 0, skipped: 0, skippedIds: [], types: { curve: 1 } });
  }

  await finishDoc();

  // 4. zip: .3dm file(s) + manifest + attribution
  const zip = new JSZip();
  const fileName = `weimar_${viewport}.3dm`;
  parts.forEach((p, i) => {
    p.file = parts.length === 1 ? fileName : `weimar_${viewport}_part${i + 1}of${parts.length}.3dm`;
    zip.file(p.file, p.bytes);
  });
  const totalBytes = parts.reduce((a, p) => a + p.bytes.length, 0);
  const objTotal = layerStats.reduce((a, s) => a + s.objects, 0);
  const manifestOut = {
    generated, viewport, format: "3dm (rhino3dm.js 8.4.0)", file: parts.length === 1 ? fileName : undefined,
    files: parts.map((p) => ({ file: p.file, objectCount: p.objects, bytes: p.bytes.length, rhinoLayers: [...p.layers] })),
    filesNote: parts.length === 1 ? undefined
      : `Split into ${parts.length} files (rhino3dm.js cannot write one .3dm over ~150 MB). Open part 1, then File > Import the other parts — layers with the same name merge.`,
    crs: "EPSG:25832", units: "Meters",
    origin: { applied: useOrigin, originEasting: ox, originNorthing: oy, E: ox, N: oy,
      note: useOrigin ? "coordinates in the file = EPSG:25832 minus (E, N)" : "none — absolute EPSG:25832" },
    precisionNote: "All geometry is Extrusion / Brep / curve / point (double precision); no meshes. Absolute EPSG:25832 coordinates are exact.",
    selection, selectionGeometry: _rectPolygon(selection).geometry, selectionCrs: "EPSG:4326",
    clipRules: {
      points: "kept when inside the selection (rectangle or curve)",
      linesAndPolygons: "kept whole when the feature touches the selection (rectangle: bbox test; curve: exact test); never cut at the border",
      weimar_admin_boundary: "always whole, never clipped",
    },
    geometry: {
      buildings: "Rhino Extrusion (closed solid), z 0..height (metres above z = 0, no terrain draping); courtyards = inner profiles; no height => flat single-face Brep (counted in flatNoHeight)",
      parcels: "flat single-face Brep at z = 0 (no triangulation)", lines: "polyline curves", points: "points",
    },
    objectCount: objTotal,
    layers: layerStats.map((s, i) => ({
      rhinoLayer: s.name, kind: s.kind, childLayers: s.children, featureCount: s.features, objectCount: s.objects,
      flatNoHeight: s.flatNoHeight, skipped: s.skipped, skippedIds: s.skippedIds, objectTypes: s.types,
      attribution: i < collected.length
        ? [...new Set(collected[i].layerMetas.map((m) => m.layer.attribution).filter(Boolean))].join(" / ")
        : "Data Twin City Weimar — team-wide reference frame",
      sourceLayers: i < collected.length ? collected[i].layerMetas.map((m) => m.id) : [],
    })),
  };
  const attrib = [
    "Data Twin City Weimar — 3DM export", `Generated: ${generated}`, `Viewport: ${viewport}`,
    `Selection (EPSG:4326): W ${selection.west}  S ${selection.south}  E ${selection.east}  N ${selection.north}`,
    "CRS: EPSG:25832 (ETRS89 / UTM 32N), units: metres",
    ...(parts.length > 1 ? [`Files: ${parts.map((p) => p.file).join(", ")} — open part 1, then File > Import the others (same-named layers merge).`] : []),
    useOrigin ? `Local origin applied: add E ${ox}, N ${oy} to get EPSG:25832` : "Absolute EPSG:25832 coordinates (no origin shift)",
    "",
    "Base map shown for context is NOT part of this package:", "  © OpenStreetMap contributors (ODbL) · © CARTO", "",
    "Exported layers", "==============", "",
  ];
  collected.forEach((c, i) => {
    attrib.push(`- ${c.group.name}  [Rhino layer “${layerStats[i].name}”]  (${layerStats[i].objects} object(s))`);
    for (const m of c.layerMetas) {
      attrib.push(`    ${m.layer.attribution || "(no attribution string on record)"}`);
      attrib.push(`    Source: ${m.res ? m.res.name : m.layer.resourceId}${m.res && m.res.url ? `  <${m.res.url}>` : ""}`);
    }
    attrib.push("");
  });
  if (opts.includeBaseBorder) attrib.push("- Base border  (1 polyline)", "    Data Twin City Weimar — team-wide reference frame", "");
  zip.file("manifest.json", JSON.stringify(manifestOut, null, 2));
  zip.file("ATTRIBUTION.txt", attrib.join("\n"));
  const blob = await zip.generateAsync({ type: "blob", compression: "DEFLATE" });
  const secs = ((performance.now() - t0) / 1000).toFixed(1);
  const empties = collected.filter((c) => c.mergedFeatures.length === 0).map((c) => c.group.fileId);
  const flatTotal = layerStats.reduce((a, s) => a + s.flatNoHeight, 0);
  console.log(`[export] 3dm done — ${objTotal} objects, ${parts.length} .3dm file(s) ${totalBytes} B, zip ${blob.size} B, ${secs}s`);
  return {
    blob, empties, total: groups.length, bytes: parts.length === 1 ? parts[0].bytes : parts.map((p) => p.bytes), layerStats, manifestOut,
    summary: `(${objTotal.toLocaleString()} objects, ${(totalBytes / 1048576).toFixed(1)} MB .3dm${parts.length > 1 ? ` in ${parts.length} files — open part 1, File › Import the rest` : ""}, ${secs} s${flatTotal ? `, ${flatTotal} buildings without height are flat` : ""})`,
  };
}

// ===========================================================================
// SVG export — one 2D vector drawing, one <g> per platform layer (Illustrator /
// Inkscape layers via inkscape:groupmode). Same fetch / clip path as the other
// formats. Units: 1 SVG unit = 1 m in EPSG:25832, y flipped (north up), shifted
// to an integer-metre origin that is written to manifest.json. Heights are not
// represented (plan view); feature properties become data-* attributes.
// ===========================================================================

const _xmlEsc = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
const _svgNum = (v) => String(Math.round(v * 100) / 100);

async function buildSvgExport(viewport, groups, selection, opts = {}) {
  if (typeof JSZip === "undefined") throw new Error("JSZip is not loaded");
  if (typeof proj4 === "undefined") throw new Error("proj4 not loaded");
  const progress = opts.onProgress || (() => {});
  const resById = new Map((manifest.resources || []).map((r) => [r.id, r]));
  const generated = new Date().toISOString();

  const sw = _toUtm32(selection.west, selection.south), ne = _toUtm32(selection.east, selection.north);
  const ox = Math.floor(Math.min(sw[0], ne[0])), oy = Math.ceil(Math.max(sw[1], ne[1]));
  const bounds = { x0: Infinity, y0: Infinity, x1: -Infinity, y1: -Infinity };
  const P = (lng, lat) => {
    const [e, n] = _toUtm32(lng, lat);
    const x = e - ox, y = oy - n;
    if (x < bounds.x0) bounds.x0 = x; if (x > bounds.x1) bounds.x1 = x;
    if (y < bounds.y0) bounds.y0 = y; if (y > bounds.y1) bounds.y1 = y;
    return `${_svgNum(x)} ${_svgNum(y)}`;
  };
  const ringD = (ring, close) => {
    let d = "";
    ring.forEach(([lng, lat], i) => { d += `${i ? "L" : "M"}${P(lng, lat)}`; });
    return d + (close ? "Z" : "");
  };

  const collected = [];
  for (const group of groups) {
    progress(`SVG: fetching “${group.name}”…`);
    collected.push({ group, ...(await collectGroupFeatures(group, group.selection || selection, resById)) });
  }

  const layerStats = [];
  const body = [];
  let built = 0;
  const total = collected.reduce((a, c) => a + c.mergedFeatures.length, 0);
  for (const { group, mergedFeatures, layerMetas } of collected) {
    const useMeta = layerMetas.find((m) => _isUseLayer(m.layer));
    const useCats = useMeta ? (manifest[useMeta.layer.colorBy.palette || "useCategories"] || []) : [];
    const colorOf = new Map(useCats.map((c) => [c.name, c.color]));
    const st = { name: group.name, features: mergedFeatures.length, elements: 0, skipped: 0 };
    const parts = [];
    for (const f of mergedFeatures) {
      const g = f.geometry;
      const props = f.properties || {};
      let attrs = "";
      for (const [k, v] of Object.entries(_userStrings(props))) {
        if (/^[A-Za-z_][\w.-]*$/.test(k)) attrs += ` data-${_xmlEsc(k.toLowerCase())}="${_xmlEsc(v)}"`;
      }
      const col = colorOf.get(props.useCategory);
      const fill = ` fill="${_xmlEsc(col || "#e6e6e6")}"`;
      if (!g) { st.skipped++; continue; }
      if (g.type === "Polygon" || g.type === "MultiPolygon") {
        const polys = g.type === "Polygon" ? [g.coordinates] : g.coordinates;
        const d = polys.map((rings) => rings.map((r) => ringD(r, true)).join("")).join("");
        parts.push(`<path class="poly"${fill} fill-rule="evenodd" d="${d}"${attrs}/>`); st.elements++;
      } else if (g.type === "LineString" || g.type === "MultiLineString") {
        const lines = g.type === "LineString" ? [g.coordinates] : g.coordinates;
        const d = lines.filter((l) => l.length >= 2).map((l) => ringD(l, false)).join("");
        if (!d) { st.skipped++; continue; }
        parts.push(`<path class="line" d="${d}"${attrs}/>`); st.elements++;
      } else if (g.type === "Point" || g.type === "MultiPoint") {
        const pts = g.type === "Point" ? [g.coordinates] : g.coordinates;
        for (const [lng, lat] of pts) {
          const [cx, cy] = P(lng, lat).split(" ");
          parts.push(`<circle class="pt" fill="${_xmlEsc(col || "#0d0d0d")}" cx="${cx}" cy="${cy}" r="1"${attrs}/>`); st.elements++;
        }
      } else st.skipped++;
      if (++built % 250 === 0) {
        progress(`SVG: building… ${built.toLocaleString()} / ${total.toLocaleString()}`);
        await _tick();
      }
    }
    const id = _xmlEsc(group.fileId);
    body.push(`<g id="${id}" inkscape:groupmode="layer" inkscape:label="${_xmlEsc(group.name)}">\n${parts.join("\n")}\n</g>`);
    layerStats.push(st);
  }

  if (opts.includeBaseBorder) {
    const d = _selectionRing(selection).map(([lng, lat], i) => `${i ? "L" : "M"}${P(lng, lat)}`).join("") + "Z";
    body.push(`<g id="base_border" inkscape:groupmode="layer" inkscape:label="Base border">\n<path class="border" d="${d}"/>\n</g>`);
    layerStats.push({ name: "Base border", features: 1, elements: 1, skipped: 0 });
  }

  if (!isFinite(bounds.x0)) { bounds.x0 = 0; bounds.y0 = 0; bounds.x1 = 1; bounds.y1 = 1; }
  const pad = 5;
  const vx = Math.floor(bounds.x0 - pad), vy = Math.floor(bounds.y0 - pad);
  const vw = Math.ceil(bounds.x1 + pad) - vx, vh = Math.ceil(bounds.y1 + pad) - vy;
  const svg = [
    `<?xml version="1.0" encoding="UTF-8"?>`,
    `<svg xmlns="http://www.w3.org/2000/svg" xmlns:inkscape="http://www.inkscape.org/namespaces/inkscape" width="${vw}" height="${vh}" viewBox="${vx} ${vy} ${vw} ${vh}">`,
    `<title>Data Twin City Weimar — ${_xmlEsc(viewport)}</title>`,
    `<desc>EPSG:25832, 1 unit = 1 m, y flipped. x = Easting - ${ox}, y = ${oy} - Northing.</desc>`,
    `<style>.poly{stroke:#0d0d0d;stroke-width:.3}.line{fill:none;stroke:#0d0d0d;stroke-width:.3}.border{fill:none;stroke:#0f5fb5;stroke-width:1;stroke-dasharray:6 3}</style>`,
    ...body,
    `</svg>`,
  ].join("\n");

  const zip = new JSZip();
  const fileName = `weimar_${viewport}.svg`;
  zip.file(fileName, svg);
  const manifestOut = {
    generated, viewport, format: "svg", file: fileName,
    crs: "EPSG:25832", units: "1 SVG unit = 1 m",
    origin: { originEasting: ox, originNorthingTop: oy,
      note: "EPSG:25832 Easting = x + originEasting; Northing = originNorthingTop - y (y axis is flipped)" },
    selection, selectionGeometry: _rectPolygon(selection).geometry, selectionCrs: "EPSG:4326",
    clipRules: {
      points: "kept when inside the selection (rectangle or curve)",
      linesAndPolygons: "kept whole when the feature touches the selection (rectangle: bbox test; curve: exact test); never cut at the border",
      weimar_admin_boundary: "always whole, never clipped",
    },
    geometry: "plan view only: polygons (evenodd, holes kept), polylines, points as 1 m circles; building heights are not drawn",
    layers: layerStats.map((s, i) => ({
      svgGroup: i < collected.length ? collected[i].group.fileId : "base_border", name: s.name,
      featureCount: s.features, elementCount: s.elements, skipped: s.skipped,
      attribution: i < collected.length
        ? [...new Set(collected[i].layerMetas.map((m) => m.layer.attribution).filter(Boolean))].join(" / ")
        : "Data Twin City Weimar — team-wide reference frame",
      sourceLayers: i < collected.length ? collected[i].layerMetas.map((m) => m.id) : [],
    })),
  };
  const attrib = [
    "Data Twin City Weimar — SVG export", `Generated: ${generated}`, `Viewport: ${viewport}`,
    `Selection (EPSG:4326): W ${selection.west}  S ${selection.south}  E ${selection.east}  N ${selection.north}`,
    "CRS: EPSG:25832 (ETRS89 / UTM 32N), 1 SVG unit = 1 m",
    "", "Base map shown for context is NOT part of this package:", "  © OpenStreetMap contributors (ODbL) · © CARTO", "",
    "Exported layers", "==============", "",
  ];
  collected.forEach((c, i) => {
    attrib.push(`- ${c.group.name}  [SVG group “${c.group.fileId}”]  (${layerStats[i].elements} element(s))`);
    for (const m of c.layerMetas) {
      attrib.push(`    ${m.layer.attribution || "(no attribution string on record)"}`);
      attrib.push(`    Source: ${m.res ? m.res.name : m.layer.resourceId}${m.res && m.res.url ? `  <${m.res.url}>` : ""}`);
    }
    attrib.push("");
  });
  zip.file("manifest.json", JSON.stringify(manifestOut, null, 2));
  zip.file("ATTRIBUTION.txt", attrib.join("\n"));
  const blob = await zip.generateAsync({ type: "blob", compression: "DEFLATE" });
  const elTotal = layerStats.reduce((a, s) => a + s.elements, 0);
  const empties = collected.filter((c) => c.mergedFeatures.length === 0).map((c) => c.group.fileId);
  console.log(`[export] svg done — ${elTotal} elements, svg ${svg.length} B, zip ${blob.size} B`);
  return {
    blob, empties, total: groups.length, svg, manifestOut,
    summary: `(${elTotal.toLocaleString()} elements, ${(svg.length / 1048576).toFixed(1)} MB .svg)`,
  };
}

/**
 * Wires the export UI for one MapLibre viewport onto `map` / `ui`.
 * Sidebar buttons: "Export layers" (toggle) → reveals row checkboxes +
 * "Select area" + "⬇ Export" (run). "Export" finalises: fetch → clip → zip →
 * download. The strip under the map shows only progress / warnings.
 * Returns { decorateRow, refresh } — pass decorateRow into renderSidebar() so
 * each sidebar row gets its own export checkbox.
 */
function createExportController({ map, viewport, ui }) {
  const rows = []; // { rowEl, exportableIds:[], checkbox, wrapper }
  let active = false;
  let selection = null;   // {west,south,east,north} once a rectangle is drawn
  let selectionFromBaseBorder = false; // true when `selection` is the Base border rect
  let disarmDraw = null;  // teardown for an in-progress "Select area" session

  // Status / warning line under the map. The action buttons ("Select area",
  // "Export") live in the sidebar toolbar (see buildBlockChrome). The strip
  // stays hidden whenever it has nothing to say.
  const statusEl = h("span", "export-status");
  ui.exportBarEl.append(statusEl);
  const setStatus = (text) => {
    statusEl.textContent = text || "";
    ui.exportBarEl.hidden = !text;
  };

  const exportableIdsOf = (ids) =>
    ids.filter((id) => { const l = layerById(id); return l && l.type === "geojson"; });

  // One export file per sidebar row (`decorateRow` call), not per raw layer
  // id — a row like "Pedestrian roads" can be backed by several ATKIS layer
  // ids under one toggle, and packing them separately meant checking that one
  // row produced two incomplete files instead of the one the sidebar implies.
  function checkedGroups() {
    const groups = [];
    for (const r of rows) {
      if (!r.checkbox.checked || r.checkbox.disabled) continue;
      if (r.exportableIds.length === 0) continue;
      // Multi-layer row: only the sublayers whose own export box is ticked.
      const ids = r.subs.length
        ? r.exportableIds.filter((id) => { const s = r.subs.find((x) => x.layerId === id); return !s || s.cb.checked; })
        : r.exportableIds;
      if (ids.length === 0) continue;
      const filters = new Map();
      for (const id of ids) { const f = _categoryFilters.get(`${viewport}:${id}`); if (f) filters.set(id, f); }
      const first = [...filters.values()][0];
      const suffix = first ? ` — ${first.values.length <= 3 ? first.values.join(", ") : first.values.length + " categories"}` : "";
      const n0 = groups.length;
      if (ids.length === r.exportableIds.length) { groups.push({ fileId: r.fileId, name: r.name + suffix, ids }); groups[n0].filters = filters; continue; }
      if (ids.length === 1) {
        const l = layerById(ids[0]);
        groups.push({ fileId: ids[0], name: ((l && (l.subName || l.name)) || r.name) + suffix, ids });
      } else {
        groups.push({ fileId: _slug(`${r.name}_${ids.join("_")}`).slice(0, 80), name: r.name + suffix, ids });
      }
      groups[n0].filters = filters;
    }
    return groups;
  }

  function refresh() {
    const n = checkedGroups().length;
    if (ui.selectAreaBtn) ui.selectAreaBtn.disabled = !active || n === 0;
    if (ui.selectPolyBtn) ui.selectPolyBtn.disabled = !active || n === 0;
    if (ui.baseBorderBtn) ui.baseBorderBtn.disabled = !active || n === 0;
    if (ui.baseBorder2Btn) ui.baseBorder2Btn.disabled = !active || n === 0;
    if (ui.baseBorderCurveBtn) ui.baseBorderCurveBtn.disabled = !active || n === 0 || !baseBorderCurve.available();
    if (ui.exportRunBtn) ui.exportRunBtn.disabled = !(active && n > 0 && !!selection);
  }

  function clearRectangle() {
    selection = null;
    selectionFromBaseBorder = false;
    const src = map.getSource("__export_rect");
    if (src) src.setData(_emptyFC());
  }

  function ensureRectLayers() {
    if (map.getSource("__export_rect")) return;
    map.addSource("__export_rect", { type: "geojson", data: _emptyFC() });
    map.addLayer({
      id: "__export_rect_fill", type: "fill", source: "__export_rect",
      paint: { "fill-color": "#0F5FB5", "fill-opacity": 0.12 },
    });
    map.addLayer({
      id: "__export_rect_line", type: "line", source: "__export_rect",
      paint: { "line-color": "#0F5FB5", "line-width": 2, "line-dasharray": [2, 1] },
    });
  }

  function paintRect(s) {
    ensureRectLayers();
    map.getSource("__export_rect").setData(s ? _rectPolygon(s) : _emptyFC());
  }

  /** Curved selection from a ring [[lng,lat]...]: bbox for the fast test + the ring itself. */
  function ringSelection(ring) {
    const sel = { west: Infinity, east: -Infinity, south: Infinity, north: -Infinity, polygon: ring.map((p) => [p[0], p[1]]) };
    for (const [x, y] of ring) {
      if (x < sel.west) sel.west = x;
      if (x > sel.east) sel.east = x;
      if (y < sel.south) sel.south = y;
      if (y > sel.north) sel.north = y;
    }
    return sel;
  }

  // Curved selection: click vertices; close by clicking the first point, double-click or Enter; Esc cancels.
  function beginDrawPolygon() {
    if (ui.selectPolyBtn.disabled) return;
    if (disarmDraw) disarmDraw();
    clearRectangle();
    refresh();
    ensureRectLayers();
    map.dragPan.disable();
    map.doubleClickZoom.disable();
    map.getCanvas().style.cursor = "crosshair";
    map.__drawing = true;
    setStatus("Click the vertices of the export area · click the first point, double-click or press Enter to close · Esc cancels.");

    const pts = [];
    const paintPts = (cursor) => {
      const ring = cursor ? [...pts, cursor] : pts;
      const feats = [];
      if (ring.length >= 3) feats.push({ type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [[...ring, ring[0]]] } });
      else if (ring.length === 2) feats.push({ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: ring } });
      map.getSource("__export_rect").setData({ type: "FeatureCollection", features: feats });
    };
    const finish = () => {
      teardown();
      if (pts.length < 3) { clearRectangle(); setStatus("Need at least 3 points, try again."); refresh(); return; }
      selection = ringSelection(pts);
      selectionFromBaseBorder = false;
      paintRect(selection);
      setStatus("Curved area selected: click “Export” to download, or “Select area (curve)” again to redraw.");
      refresh();
    };
    const onClick = (e) => {
      if (pts.length >= 3) {
        const a = map.project(pts[0]);
        if (Math.hypot(a.x - e.point.x, a.y - e.point.y) < 12) return finish();
      }
      pts.push([e.lngLat.lng, e.lngLat.lat]);
      paintPts();
    };
    const onDbl = (e) => { e.preventDefault(); if (pts.length >= 2) pts.pop(); finish(); };
    const onMove = (e) => { if (pts.length) paintPts([e.lngLat.lng, e.lngLat.lat]); };
    const onKey = (e) => {
      if (e.key === "Escape") { teardown(); clearRectangle(); setStatus(""); refresh(); }
      else if (e.key === "Enter") finish();
    };
    function teardown() {
      map.off("click", onClick); map.off("dblclick", onDbl); map.off("mousemove", onMove);
      document.removeEventListener("keydown", onKey);
      map.dragPan.enable();
      map.doubleClickZoom.enable();
      map.getCanvas().style.cursor = "";
      map.__drawing = false;
      disarmDraw = null;
    }
    disarmDraw = teardown;
    map.on("click", onClick); map.on("dblclick", onDbl); map.on("mousemove", onMove);
    document.addEventListener("keydown", onKey);
  }

  function beginDraw() {
    if (ui.selectAreaBtn.disabled) return;
    if (disarmDraw) disarmDraw();
    clearRectangle();
    refresh();

    map.dragPan.disable();
    map.boxZoom.disable();
    map.getCanvas().style.cursor = "crosshair";
    setStatus("Drag on the map to draw the export rectangle…");

    let start = null;
    const onMove = (e) => paintRect(_bboxFromLngLat(start, e.lngLat));
    const onUp = (e) => {
      const s = _bboxFromLngLat(start, e.lngLat);
      teardown();
      if (Math.abs(s.east - s.west) < 1e-7 || Math.abs(s.north - s.south) < 1e-7) {
        clearRectangle(); setStatus("Rectangle too small — try again."); refresh();
        return;
      }
      selection = s;
      selectionFromBaseBorder = false;
      paintRect(s);
      setStatus("Area selected — click “Export” to download, or “Select area” again to redraw.");
      refresh();
    };
    const onDown = (e) => {
      start = e.lngLat;
      map.on("mousemove", onMove);
      map.once("mouseup", onUp);
    };
    function teardown() {
      map.off("mousedown", onDown);
      map.off("mousemove", onMove);
      map.off("mouseup", onUp);
      map.dragPan.enable();
      map.boxZoom.enable();
      map.getCanvas().style.cursor = "";
      disarmDraw = null;
    }
    disarmDraw = teardown;
    map.on("mousedown", onDown);
  }

  function setActive(on) {
    active = on;
    for (const r of rows) {
      r.wrapper.hidden = !on;
      for (const sb of r.subs) sb.wrapper.hidden = !on;
      if (!on) r.checkbox.checked = false;
    }
    if (ui.exportToggleBtn) {
      ui.exportToggleBtn.textContent = on ? "Cancel export" : "Export layers";
      ui.exportToggleBtn.classList.toggle("active", on);
    }
    if (ui.selectAreaBtn) ui.selectAreaBtn.hidden = !on;
    if (ui.selectPolyBtn) ui.selectPolyBtn.hidden = !on;
    if (ui.baseBorderBtn) ui.baseBorderBtn.hidden = !on;
    if (ui.baseBorder2Btn) ui.baseBorder2Btn.hidden = !on;
    if (ui.baseBorderCurveBtn) ui.baseBorderCurveBtn.hidden = !on;
    if (ui.exportRunBtn) ui.exportRunBtn.hidden = !on;
    if (ui.formatRow) ui.formatRow.hidden = !on;
    if (!on) {
      if (disarmDraw) disarmDraw();
      clearRectangle();
      setStatus("");
    }
    refresh();
  }

  function decorateRow(rowEl, layerIds, label, subRows = []) {
    const main = rowEl.querySelector(".layer-main") || rowEl;
    const exportableIds = exportableIdsOf(layerIds);
    const wrapper = h("label", "export-check-wrap", { hidden: !active });
    const cb = h("input", "export-check", { type: "checkbox" });
    if (exportableIds.length === 0) {
      cb.disabled = true;
      wrapper.title = "No exportable vector geometry (raster / 3D-tiles layer)";
    } else {
      wrapper.title = "Include this layer in the export package";
    }
    wrapper.append(cb);
    main.insertBefore(wrapper, main.firstChild);
    // One export checkbox per sublayer of a multi-layer row ("Parcels" → roads & paths, …).
    const subs = [];
    for (const sr of subRows) {
      if (!exportableIds.includes(sr.layerId)) continue;
      const sw = h("label", "export-check-wrap", { hidden: !active, title: "Include this sublayer in the export" });
      const scb = h("input", "export-check", { type: "checkbox" });
      scb.checked = true;
      scb.addEventListener("change", refresh);
      sw.append(scb);
      sr.el.insertBefore(sw, sr.el.firstChild);
      subs.push({ layerId: sr.layerId, cb: scb, wrapper: sw });
    }
    cb.addEventListener("change", () => {
      if (cb.checked && subs.length) {
        // start from what is visible on the map; if nothing is, take all
        const vis = subs.map((s) => { const l = layerById(s.layerId); return !!l && layerVisibleInViewport(l, viewport); });
        const any = vis.some(Boolean);
        subs.forEach((s, i) => { s.cb.checked = any ? vis[i] : true; });
        const panel = rowEl.querySelector(".sub-panel"), caret = rowEl.querySelector(".cat-btn");
        if (panel) { panel.hidden = false; if (caret) { caret.classList.add("open"); caret.textContent = "–"; } }
      }
      refresh();
    });
    // Single-id rows keep the layer's own id as the filename (unchanged,
    // most rows); multi-id rows get one merged file named after the row.
    const fileId = exportableIds.length === 1 ? exportableIds[0] : _slug(label || exportableIds.join("_"));
    rows.push({ rowEl, exportableIds, checkbox: cb, wrapper, subs, fileId, name: label || exportableIds[0] || fileId });
  }

  if (ui.exportToggleBtn)
    ui.exportToggleBtn.addEventListener("click", () => setActive(!active));
  if (ui.selectAreaBtn)
    ui.selectAreaBtn.addEventListener("click", beginDraw);
  if (ui.selectPolyBtn)
    ui.selectPolyBtn.addEventListener("click", beginDrawPolygon);

  // TASK 16 — "Base border": use the fixed team-wide rectangle
  // (web/layers/base_border.json) as the export selection. Nothing else about the
  // export flow changes — same fetch → clip → zip as "Select area".
  const useBorder = (btn, getSel, label) => {
    if (!btn) return;
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      if (disarmDraw) disarmDraw();
      const sel = getSel();
      if (!sel) return;
      selection = sel;
      selectionFromBaseBorder = true;
      paintRect(selection);
      setStatus(`${label} set as export area: it will be included as its own layer. Click “Export” to download, or draw a custom area.`);
      refresh();
    });
  };
  useBorder(ui.baseBorderBtn, () => ({ ...baseBorder.getRect() }), "Base border");
  useBorder(ui.baseBorder2Btn, () => ({ ...baseBorder2.getRect() }), "Base border 2.0");
  useBorder(ui.baseBorderCurveBtn, () => { const r = baseBorderCurve.get(); return r ? ringSelection(r) : null; }, "Curved Base border");

  async function runExport() {
    if (!ui.exportRunBtn || ui.exportRunBtn.disabled) return;
    const groups = checkedGroups();
    ui.exportRunBtn.disabled = true;
    setStatus("Packing… (fetching + clipping layers)");
    try {
      const fmt = ui.formatSel ? ui.formatSel.value : "geojson";
      const border = { includeBaseBorder: selectionFromBaseBorder };
      let res;
      if (fmt === "3dm") {
        res = await build3dmExport(viewport, groups, selection, {
          ...border, localOrigin: !!(ui.originCb && ui.originCb.checked), onProgress: setStatus,
        });
        if (!res) { setStatus("3DM export cancelled — pick a smaller area."); return; }
      } else if (fmt === "svg") {
        res = await buildSvgExport(viewport, groups, selection, { ...border, onProgress: setStatus });
      } else {
        res = await buildExportZip(viewport, groups, selection, border);
      }
      const { blob, empties, total } = res;
      triggerDownload(blob, `weimar_export_${viewport}_${fmt === "geojson" ? "" : fmt + "_"}${_downloadStamp()}.zip`);
      const borderNote = selectionFromBaseBorder ? (fmt === "geojson" ? " + base_border.geojson" : " + Base border layer") : "";
      const extra = res.summary ? ` ${res.summary}` : "";
      setStatus(empties.length
        ? `Downloaded ${total} layer(s)${borderNote}.${extra} ⚠ No features in the selected area for: ${empties.join(", ")} (exported as empty).`
        : `Downloaded ${total} layer(s)${borderNote}.${extra}`);
    } catch (err) {
      console.error("[export] failed:", err);
      setStatus(`Export failed: ${err.message}`);
    } finally {
      refresh();
    }
  }
  if (ui.exportRunBtn)
    ui.exportRunBtn.addEventListener("click", runExport);

  setActive(false);
  return { decorateRow, refresh };
}

// ---------------------------------------------------------------------------
// Map image export (PNG / PDF, high resolution)
// ---------------------------------------------------------------------------
async function captureMapPng(map, targetLongSidePx = 6000) {
  const canvas = map.getCanvas();
  const prevRatio = map.getPixelRatio ? map.getPixelRatio() : window.devicePixelRatio;
  const cssLong = Math.max(canvas.clientWidth, canvas.clientHeight) || 1;
  const ratio = Math.max(1, Math.min(4, targetLongSidePx / cssLong));
  const canSet = typeof map.setPixelRatio === "function";
  try {
    if (canSet) {
      map.setPixelRatio(ratio);
      await new Promise((res) => { const t = setTimeout(res, 10000); map.once("idle", () => { clearTimeout(t); res(); }); map.triggerRepaint(); });
    }
    // The WebGL buffer is only readable inside a render frame.
    const dataUrl = await new Promise((res) => {
      map.once("render", () => res(canvas.toDataURL("image/png")));
      map.triggerRepaint();
    });
    return { dataUrl, width: canvas.width, height: canvas.height };
  } finally {
    if (canSet) map.setPixelRatio(prevRatio);
  }
}

/** Draws the legend blocks into a 2D context (bottom-left), scale factor k = canvas px per css px. */
function drawLegendOnCanvas(ctx, blocks, W, H, k) {
  if (!blocks.length) return;
  const fs = 11, lh = 16, pad = 10, sw = 12, gapB = 8;
  const font = `${fs}px "Helvetica Neue", Helvetica, Arial, sans-serif`;
  const bold = `700 ${fs * 0.92}px "Helvetica Neue", Helvetica, Arial, sans-serif`;
  ctx.save();
  ctx.font = font;
  let wText = 0;
  for (const bl of blocks) for (const r of bl.rows) wText = Math.max(wText, ctx.measureText(r.name).width);
  const bw = Math.max(170, wText + sw + 6) + pad * 2;
  const bh = blocks.reduce((a, bl) => a + (bl.rows.length + 1) * lh + gapB, 0) + pad * 2 - gapB;
  const availH = H - (112 + 70) * k;
  const sc = Math.min(1, availH / (bh * k)) * k;
  ctx.translate(W - 12 * k - bw * sc, 112 * k);
  ctx.scale(sc, sc);
  ctx.fillStyle = "#FFFFFF"; ctx.fillRect(0, 0, bw, bh);
  ctx.strokeStyle = "#0D0D0D"; ctx.lineWidth = 1; ctx.strokeRect(0.5, 0.5, bw - 1, bh - 1);
  let y = pad;
  for (const bl of blocks) {
    y += lh * 0.75;
    ctx.fillStyle = "#0D0D0D"; ctx.font = bold; ctx.fillText(bl.title.toUpperCase(), pad, y);
    ctx.font = font;
    for (const r of bl.rows) {
      y += lh;
      const x = pad;
      if (r.kind === "line") { ctx.fillStyle = r.stroke; ctx.fillRect(x, y - fs * 0.45, sw, 3); }
      else if (r.kind === "dot") {
        ctx.beginPath(); ctx.arc(x + sw / 2, y - fs * 0.85 + sw / 2, sw * 0.42, 0, Math.PI * 2);
        ctx.fillStyle = r.fill; ctx.fill(); ctx.strokeStyle = r.stroke; ctx.lineWidth = 1; ctx.stroke();
      } else {
        if (r.fill) { ctx.fillStyle = r.fill; ctx.fillRect(x, y - fs * 0.85, sw, sw); }
        if (r.stroke) { ctx.strokeStyle = r.stroke; ctx.lineWidth = 1; ctx.strokeRect(x, y - fs * 0.85, sw, sw); }
      }
      ctx.fillStyle = "#0D0D0D"; ctx.fillText(r.name, x + sw + 6, y);
    }
    y += lh * 0.25 + gapB;
  }
  ctx.restore();
}

async function exportMapImage(map, fmt, viewport, title, attribution, opts = {}) {
  const shot = await captureMapPng(map);
  const { width, height } = shot;
  let dataUrl = shot.dataUrl;
  if (opts.legendBlocks || opts.drawGrid) {            // compose legend / grid scale onto the map picture
    const img = new Image();
    await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = dataUrl; });
    const cv = document.createElement("canvas"); cv.width = width; cv.height = height;
    const ctx = cv.getContext("2d");
    ctx.drawImage(img, 0, 0);
    const k = width / (map.getCanvas().clientWidth || width);
    if (opts.drawGrid) opts.drawGrid(ctx, width / k, height / k, k, true);
    if (opts.legendBlocks) drawLegendOnCanvas(ctx, opts.legendBlocks, width, height, k);
    dataUrl = cv.toDataURL("image/png");
  }
  const base = `weimar_map_${viewport}_${_downloadStamp()}`;
  if (fmt === "png") {
    triggerDownload(await (await fetch(dataUrl)).blob(), `${base}.png`);
    return;
  }
  if (!window.jspdf) throw new Error("PDF library not loaded");
  const landscape = width >= height;
  const pdf = new window.jspdf.jsPDF({ orientation: landscape ? "l" : "p", unit: "mm", format: "a3" });
  const pw = pdf.internal.pageSize.getWidth(), ph = pdf.internal.pageSize.getHeight();
  const m = 12, capH = 14;
  const kk = Math.min((pw - 2 * m) / width, (ph - 2 * m - capH) / height);
  const w = width * kk, hgt = height * kk;
  pdf.addImage(dataUrl, "PNG", m, m, w, hgt, undefined, "FAST");
  pdf.setFont("helvetica", "bold"); pdf.setFontSize(11);
  pdf.text(`Weimar - ${title}`, m, m + hgt + 7);
  pdf.setFont("helvetica", "normal"); pdf.setFontSize(7);
  pdf.text(String(attribution || "").slice(0, 220), m, m + hgt + 12, { maxWidth: pw - 2 * m });
  pdf.save(`${base}.pdf`);
}

// ===========================================================================
// MapLibre block — viewports "computational", "transport", "blue_green"
// ===========================================================================

/** WMS GetMap tile-URL template for a MapLibre `raster` source. */
/** "obk://<classIdx>/<bbox 3857>" tiles: fetch the three OBK WMS tiles, keep only the pixels of one legend class and
 *  re-colour them in the project palette. Lets one WMS (colour-coded server-side) behave as several layers. */
const _obkCache = new Map();
function _obkTile(layer, bbox) {
  const key = layer + "|" + bbox;
  if (!_obkCache.has(key)) {
    if (_obkCache.size > 300) _obkCache.delete(_obkCache.keys().next().value);
    const url = `https://www.geoproxy.geoportal-th.de/geoproxy/services?service=WMS&version=1.1.1&request=GetMap&layers=${layer}&styles=&format=image/png&transparent=true&srs=EPSG:3857&width=512&height=512&bbox=${bbox}`;
    _obkCache.set(key, fetch(url).then((r) => r.blob()).then(createImageBitmap).then((bmp) => {
      const c = document.createElement("canvas"); c.width = 512; c.height = 512;
      const g = c.getContext("2d", { willReadFrequently: true }); g.drawImage(bmp, 0, 0);
      return g.getImageData(0, 0, 512, 512);
    }).catch(() => null));
  }
  return _obkCache.get(key);
}
let _obkRegistered = false;
function registerObkProtocol() {
  if (_obkRegistered) return;
  _obkRegistered = true;
  maplibregl.addProtocol("obk", async (params) => {
    const m = /^obk:\/\/(\d+)\/(.+)$/.exec(params.url);
    const cls = (manifest.layers || []).find((l) => l.type === "wms_class" && l.classIdx === Number(m[1]));
    const bbox = m[2];
    const out = new ImageData(512, 512);
    const [tr, tg, tb] = cls.classRgb;
    const hex = cls.classColor.replace("#", "");
    const [pr, pg, pb] = [0, 2, 4].map((i) => parseInt(hex.slice(i, i + 2), 16));
    const imgs = await Promise.all(cls.wmsLayers.map((l) => _obkTile(l, bbox)));
    for (const im of imgs) {
      if (!im) continue;
      const d = im.data, o = out.data;
      for (let i = 0; i < d.length; i += 4) {
        if (d[i + 3] < 40) continue;
        if (Math.abs(d[i] - tr) + Math.abs(d[i + 1] - tg) + Math.abs(d[i + 2] - tb) < 60) {
          o[i] = pr; o[i + 1] = pg; o[i + 2] = pb; o[i + 3] = 255;
        }
      }
    }
    const c = document.createElement("canvas"); c.width = 512; c.height = 512;
    c.getContext("2d").putImageData(out, 0, 0);
    const blob = await new Promise((res) => c.toBlob(res, "image/png"));
    return { data: await blob.arrayBuffer() };
  });
}

function wmsTileTemplate(config) {
  const params = new URLSearchParams({
    service: "WMS",
    version: "1.1.1",
    request: "GetMap",
    layers: config.wmsLayers || config.layers || "",
    styles: "",
    format: config.wmsFormat || "image/png",
    transparent: String(config.wmsTransparent ?? true),
    srs: "EPSG:3857",
    width: "256",
    height: "256",
    bbox: "__BBOX__",
  });
  return `${resolveUrl(config.url)}?${params.toString()}`.replace("__BBOX__", "{bbox-epsg-3857}");
}

/** Rough MapLibre zoom at which an ALKIS-style `minCameraHeight` (m) layer draws. */
function zoomForMinHeight(metres) {
  return metres && metres <= 800 ? 17 : 15.5;
}

function createMapLibreBlock(mount, opts) {
  const { title, subtitle, viewport, basemap } = opts;
  const ui = buildBlockChrome(mount, { title, subtitle, hostClass: "maplibre-host", withExport: true, exportMode: opts.exportMode, materials: opts.materials });
  const baseAttribution = basemap?.attribution || BASE_ATTRIBUTION;
  // Either a full remote style URL (e.g. CARTO Positron's published vector
  // style — see its own comment above) or the plain OSM raster built inline.
  const baseStyle = basemap?.style || basemap?.styleUrl || {
    version: 8,
    sources: {
      osm: { type: "raster", tiles: [OSM_TILES], tileSize: 256, attribution: baseAttribution },
    },
    layers: [{ id: "__osm", type: "raster", source: "osm" }],
  };

  const map = new maplibregl.Map({
    container: ui.host,
    attributionControl: false,
    style: baseStyle,
    center: [WEIMAR_CENTER.lon, WEIMAR_CENTER.lat],
    zoom: 12,
    pitch: 0,
    maxPitch: 70,
  });
  map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "top-right");
  map.fitBounds(WEIMAR_BOUNDS, { padding: 24, duration: 0 });

  // id -> { config, layerIds:[], setVisible(v) }
  const managed = new Map();
  // url -> MapLibre source id. Lets two layer entries (e.g. a flat render and
  // an extruded render of the same footprints) share one fetched GeoJSON
  // instead of downloading/parsing a large file twice.
  const geojsonSourceByUrl = new Map();

  // --- 3D Tiles (b3dm), via deck.gl Tile3DLayer — no MapLibre-native support ---
  // One deck.gl MapboxOverlay per block, created lazily on first "3dtiles"
  // layer; MapboxOverlay reads MapLibre's own camera each frame, so no manual
  // sync code is needed. `interleaved: true` lets deck.gl's 3D content
  // depth-sort against MapLibre's own layers (terrain hillshade, extrusions).
  let deckOverlay = null;
  const deck3DConfigs = new Map(); // id -> layer config (live object from the manifest)

  function syncDeck3DLayers() {
    deckOverlay.setProps({
      layers: [...deck3DConfigs.values()].map((config) => new deck.Tile3DLayer({
        id: config.id,
        data: resolveUrl(config.url),
        visible: layerVisibleInViewport(config, viewport),
        opacity: config.opacity ?? 1,
        pointSize: 2,
        // WEGA's b3dm tiles carry materials via the legacy KHR_techniques_webgl
        // glTF extension (Cesium's native renderer supports it; loaders.gl's
        // GLTFLoader does not and throws "no shader code", dropping the whole
        // tile). Excluding it falls back to the mesh's own vertex colours
        // (still the per-roof-type colouring baked into the geometry) instead
        // of failing to load.
        loadOptions: { gltf: { excludeExtensions: { KHR_techniques_webgl: false } } },
      })),
    });
  }

  function add3DTilesLayer(config) {
    if (!deckOverlay) {
      deckOverlay = new deck.MapboxOverlay({ layers: [] });
      map.addControl(deckOverlay);
    }
    deck3DConfigs.set(config.id, config);
    syncDeck3DLayers();
    managed.set(config.id, {
      config,
      layerIds: [],
      setVisible: () => syncDeck3DLayers(),
    });
  }

  function addWmsClassLayer(config) {
    registerObkProtocol();
    const srcId = `src__${config.id}`;
    map.addSource(srcId, { type: "raster", tiles: [`obk://${config.classIdx}/{bbox-epsg-3857}`], tileSize: 512 });
    map.addLayer({
      id: config.id, type: "raster", source: srcId,
      paint: { "raster-opacity": config.opacity ?? 1 },
      layout: { visibility: layerVisibleInViewport(config, viewport) ? "visible" : "none" },
    });
    managed.set(config.id, { config, layerIds: [config.id],
      setVisible: (v) => map.setLayoutProperty(config.id, "visibility", v ? "visible" : "none") });
  }

  function addWmsLayer(config) {
    const srcId = `src__${config.id}`;
    map.addSource(srcId, { type: "raster", tiles: [wmsTileTemplate(config)], tileSize: 256 });
    map.addLayer({
      id: config.id,
      type: "raster",
      source: srcId,
      paint: { "raster-opacity": config.opacity ?? 1 },
      layout: { visibility: layerVisibleInViewport(config, viewport) ? "visible" : "none" },
    });
    managed.set(config.id, {
      config,
      layerIds: [config.id],
      setVisible: (v) => map.setLayoutProperty(config.id, "visibility", v ? "visible" : "none"),
    });
  }

  /** Resolves (creating if needed) the shared GeoJSON source for a layer's url. */
  function sharedGeoJsonSource(config) {
    const resolvedUrl = resolveUrl(config.url);
    let srcId = geojsonSourceByUrl.get(resolvedUrl);
    if (!srcId) {
      srcId = `srcdata__${geojsonSourceByUrl.size}`;
      map.addSource(srcId, { type: "geojson", data: resolvedUrl });
      geojsonSourceByUrl.set(resolvedUrl, srcId);
    }
    return srcId;
  }

  function addGeoJsonLayer(config) {
    const srcId = sharedGeoJsonSource(config);
    const style = config.style || {};
    const vis = layerVisibleInViewport(config, viewport) ? "visible" : "none";
    const stroke = style.stroke || "#4c9ffe";
    const fill = config.colorBy ? useColorExpr(config, style.fill || "#d5d8dc") : (style.fill || "rgba(76,159,254,0.15)");
    const point = style.markerColor || stroke;

    const fillId = `${config.id}__fill`;
    const lineId = `${config.id}__line`;
    const circleId = `${config.id}__circle`;

    const isPoly = ["match", ["geometry-type"], ["Polygon", "MultiPolygon"], true, false];
    const isLine = ["match", ["geometry-type"],
      ["Polygon", "MultiPolygon", "LineString", "MultiLineString"], true, false];
    const isPoint = ["match", ["geometry-type"], ["Point", "MultiPoint"], true, false];

    // Optional zoom-dependent contrast boost: many small polygons (parcels,
    // ...) shrink below a screen pixel when zoomed far out, so a subtle
    // close-up fill/stroke effectively disappears at city/regional scale.
    // `style.fillOpacityStops` / `style.strokeWidthStops` (arrays of
    // [zoom, value]) build a MapLibre `interpolate` expression instead of the
    // flat constant, so far-away views get more contrast while the close-up
    // look (the constant values) is unchanged. Omit them for plain constants.
    const zoomExpr = (stops, flatValue) =>
      stops ? ["interpolate", ["linear"], ["zoom"], ...stops.flat()] : flatValue;
    const fillOpacity = zoomExpr(style.fillOpacityStops, style.fillOpacity ?? 1);
    const lineWidth = zoomExpr(style.strokeWidthStops, style.strokeWidth ?? 1.5);

    const withF = (f) => (config.filterExpr ? ["all", f, config.filterExpr] : f);
    const op = config.opacity ?? 1;
    map.addLayer({
      id: fillId, type: "fill", source: srcId,
      filter: withF(isPoly),
      paint: { "fill-color": fill, "fill-opacity": op < 1 ? ["*", op, fillOpacity] : fillOpacity }, layout: { visibility: vis },
    });
    map.addLayer({
      id: lineId, type: "line", source: srcId,
      filter: withF(isLine),
      paint: { "line-color": stroke, "line-width": lineWidth, "line-opacity": op },
      layout: { visibility: vis, "line-join": "round", "line-cap": "round" },
    });
    const iconId = `${config.id}__icon`;
    const labelId = `${config.id}__label`;
    if (style.labelField) {
      // text-only layer (e.g. counting-gate names): its own eye, independent of the point layer
      map.addLayer({
        id: labelId, type: "symbol", source: srcId, filter: withF(isPoint),
        layout: {
          visibility: vis, "text-field": ["to-string", ["get", style.labelField]],
          "text-font": ["Montserrat SemiBold", "Open Sans Bold", "Noto Sans Regular"],
          "text-size": 13, "text-offset": [0, -1.1], "text-anchor": "bottom",
          "text-allow-overlap": true, "text-ignore-placement": true,
        },
        paint: { "text-color": style.labelColor || "#222", "text-halo-color": "#ffffff", "text-halo-width": 1.6, "text-opacity": op },
      });
      managed.set(config.id, { config, layerIds: [labelId],
        setVisible: (v) => map.setLayoutProperty(labelId, "visibility", v ? "visible" : "none") });
      return;
    }
    if (config.markerIcon === "tree") {
      if (!map.hasImage("icon_tree")) map.addImage("icon_tree", makeTreeIcon(), { pixelRatio: 2 });
      map.addLayer({
        id: iconId, type: "symbol", source: srcId, filter: withF(isPoint),
        layout: { visibility: vis, "icon-image": "icon_tree", "icon-size": 1, "icon-allow-overlap": true, "icon-ignore-placement": true },
        paint: { "icon-opacity": op },
      });
    } else {
      map.addLayer({
        id: circleId, type: "circle", source: srcId,
        filter: withF(isPoint),
        paint: {
          "circle-radius": style.markerRadius ?? (style.markerSize === "large" ? 7 : 5),
          "circle-color": point,
          "circle-stroke-color": "#0d0f13",
          "circle-stroke-width": style.markerStrokeWidth ?? 1.5,
          "circle-opacity": op, "circle-stroke-opacity": op,
        },
        layout: { visibility: vis },
      });
    }

    const layerIds = [fillId, lineId, config.markerIcon === "tree" ? iconId : circleId];
    // `interactive: false` (background/reference layers, e.g. the city boundary):
    // no click popup, no pointer cursor — clicks fall through to the layers above.
    const interactive = config.interactive !== false;
    for (const lid of interactive ? layerIds : []) {
      map.on("click", lid, (e) => {
        if (map.__drawing || opts.noPopups) return;
        const f = e.features && e.features[0];
        if (!f) return;
        if (f.properties && f.properties.buildingId) return showBuildingPopup(map, e, f);
        // optional manifest `popupFields` (TASK 31): show only these properties, in this order
        const props = Array.isArray(config.popupFields)
          ? Object.fromEntries(config.popupFields.map((k) => [k, f.properties[k]])) : f.properties;
        new maplibregl.Popup({ maxWidth: "340px" })
          .setLngLat(e.lngLat)
          .setHTML(describePropsTable(props))
          .addTo(map);
      });
      map.on("mouseenter", lid, () => { map.getCanvas().style.cursor = "pointer"; });
      map.on("mouseleave", lid, () => { map.getCanvas().style.cursor = ""; });
    }

    managed.set(config.id, {
      config,
      layerIds,
      setVisible: (v) => layerIds.forEach((lid) =>
        map.setLayoutProperty(lid, "visibility", v ? "visible" : "none")),
    });
  }

  /**
   * Same source as addGeoJsonLayer, rendered as `fill-extrusion` instead of a
   * flat fill — `config.extrusion` picks the height attribute. Used for a
   * "show as 3D volume" variant of an existing flat polygon layer, sharing the
   * one GeoJSON source (via sharedGeoJsonSource) instead of re-fetching it.
   */
  function addGeoJsonExtrusionLayer(config) {
    const srcId = sharedGeoJsonSource(config);
    const style = config.style || {};
    const ext = config.extrusion || {};
    const heightProp = ext.heightProperty || "height";
    const vis = layerVisibleInViewport(config, viewport) ? "visible" : "none";
    const fill = config.colorBy ? useColorExpr(config, style.fill || "#d5d8dc") : (style.fill || "rgba(160,180,200,0.75)");

    const extrudeId = `${config.id}__extrude`;
    map.addLayer({
      id: extrudeId, type: "fill-extrusion", source: srcId,
      filter: ["match", ["geometry-type"], ["Polygon", "MultiPolygon"], true, false],
      paint: {
        "fill-extrusion-color": fill,
        "fill-extrusion-height": ["coalesce", ["to-number", ["get", heightProp]], ext.fallbackHeight ?? 3],
        "fill-extrusion-base": ext.base ?? 0,
        "fill-extrusion-opacity": style.opacity ?? 0.85,
      },
      layout: { visibility: vis },
    });

    map.on("click", extrudeId, (e) => {
      if (map.__drawing || opts.noPopups) return;
      const f = e.features && e.features[0];
      if (!f) return;
      if (f.properties && f.properties.buildingId) return showBuildingPopup(map, e, f);
      new maplibregl.Popup({ maxWidth: "340px" })
        .setLngLat(e.lngLat)
        .setHTML(describePropsTable(f.properties))
        .addTo(map);
    });
    map.on("mouseenter", extrudeId, () => { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", extrudeId, () => { map.getCanvas().style.cursor = ""; });

    managed.set(config.id, {
      config,
      layerIds: [extrudeId],
      setVisible: (v) => map.setLayoutProperty(extrudeId, "visibility", v ? "visible" : "none"),
    });
  }

  function setLayerVisible(id, visible) {
    const entry = managed.get(id);
    if (!entry) return;
    // once toggled, plain `visible` is authoritative (see layerVisibleInViewport)
    delete entry.config.visibleViewports;
    entry.config.visible = visible;
    entry.setVisible(visible);
    refreshLegend();
    ui.attributionEl.innerHTML = attributionHtml(viewport, "MapLibre GL", baseAttribution);
  }

  /** Show only features whose `prop` is in `values` (null = everything). */
  function setLayerFilter(ids, prop, values) {
    for (const id of ids) {
      if (values === null) _categoryFilters.delete(`${viewport}:${id}`);
      else _categoryFilters.set(`${viewport}:${id}`, { prop, values: values.slice() });
      const entry = managed.get(id);
      if (!entry) continue;
      entry.baseFilters = entry.baseFilters || new Map();
      for (const lid of entry.layerIds) {
        if (!entry.baseFilters.has(lid)) entry.baseFilters.set(lid, map.getFilter(lid) || null);
        const base = entry.baseFilters.get(lid);
        const f = values === null ? null : ["in", ["get", prop], ["literal", values]];
        map.setFilter(lid, f && base ? ["all", base, f] : (f || base));
      }
    }
  }

  // --- initial view ---
  const flyToBase = (duration) => {
    const r = baseBorder.getRect();
    map.fitBounds([[r.west, r.south], [r.east, r.north]], { padding: 40, duration });
  };
  if (opts.startView === "base") flyToBase(0);
  else if (opts.startView === "church") {
    // Herderkirche (Stadtkirche St. Peter und Paul), seen from the south-east (camera looks north-west)
    map.jumpTo({ center: [11.32955, 50.98121], zoom: 17.4, pitch: 62, bearing: -45 });
    ui.btn2d.classList.remove("active"); ui.btn3d.classList.add("active");
  } else if (opts.startView === "city3d") {
    map.jumpTo({ center: [11.3297, 50.9795], zoom: 16.6, pitch: 60, bearing: -20 });
    ui.btn2d.classList.remove("active"); ui.btn3d.classList.add("active");
  }

  // --- legend (conditional signs): the visible layers + palettes, shown on demand ---
  let legendOn = false;
  const baseSymbolIds = [];
  const refreshLegend = () => {
    renderLegend(ui.legendEl, viewport, legendOn);
    ui.annotBtn.textContent = legendOn ? "Hide legend" : "Show legend";
    ui.annotBtn.classList.toggle("active", legendOn);
  };
  ui.annotBtn.addEventListener("click", () => { legendOn = !legendOn; refreshLegend(); });

  // --- scale grid: screen-aligned, thin dashed black lines at 50 % opacity; the cell size is shown below ---
  const GRID_STEPS = [1, 2, 5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000];
  const gridAnchor = [WEIMAR_CENTER.lon, WEIMAR_CENTER.lat];
  const fmtLen = (m) => (m >= 1000 ? `${m / 1000} km` : `${m} m`);
  /** Draws the grid into ctx (css px, scaled by k); returns the label text. */
  function drawGrid(ctx, W, H, k, withLabel) {
    const lat = map.getCenter().lat;
    const mpp = (78271.517 * Math.cos((lat * Math.PI) / 180)) / Math.pow(2, map.getZoom());
    const step = GRID_STEPS.find((v) => v / mpp >= 90) || GRID_STEPS[GRID_STEPS.length - 1];
    const px = step / mpp;
    const a = map.project(gridAnchor);
    const x0 = ((a.x % px) + px) % px, y0 = ((a.y % px) + px) % px;
    ctx.save();
    ctx.scale(k, k);
    ctx.strokeStyle = "rgba(13,13,13,0.5)"; ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
    ctx.beginPath();
    for (let x = x0; x <= W; x += px) { ctx.moveTo(Math.round(x) + 0.5, 0); ctx.lineTo(Math.round(x) + 0.5, H); }
    for (let y = y0; y <= H; y += px) { ctx.moveTo(0, Math.round(y) + 0.5); ctx.lineTo(W, Math.round(y) + 0.5); }
    ctx.stroke();
    ctx.restore();
    const text = `Grid cell ${fmtLen(step)}`;
    if (withLabel) {
      ctx.save(); ctx.scale(k, k);
      ctx.font = '700 12px "Helvetica Neue", Helvetica, Arial, sans-serif';
      const tw = ctx.measureText(text).width + 20;
      ctx.fillStyle = "#FFFFFF"; ctx.fillRect(W / 2 - tw / 2, H - 38, tw, 24);
      ctx.strokeStyle = "#0D0D0D"; ctx.setLineDash([]); ctx.strokeRect(W / 2 - tw / 2 + 0.5, H - 37.5, tw - 1, 23);
      ctx.fillStyle = "#0D0D0D"; ctx.textAlign = "center"; ctx.fillText(text, W / 2, H - 21);
      ctx.restore();
    }
    return text;
  }
  function wireGrid() {
    if (!ui.gridBtn) return;
    let on = false;
    const cv = ui.gridCanvas;
    const redraw = () => {
      if (!on) return;
      const W = ui.host.clientWidth, H = ui.host.clientHeight, dpr = window.devicePixelRatio || 1;
      cv.width = W * dpr; cv.height = H * dpr; cv.style.width = W + "px"; cv.style.height = H + "px";
      const ctx = cv.getContext("2d");
      ctx.clearRect(0, 0, cv.width, cv.height);
      ui.gridScaleEl.textContent = drawGrid(ctx, W, H, dpr, false);
    };
    map.on("move", redraw); map.on("resize", redraw);
    ui.gridBtn.addEventListener("click", () => {
      on = !on;
      ui.gridBtn.textContent = on ? "Grid: on" : "Grid: off";
      ui.gridBtn.classList.toggle("active", on);
      cv.hidden = !on; ui.gridScaleEl.hidden = !on;
      if (on) redraw();
      ui.gridNote.textContent = on ? "Screen grid; the cell size is shown below the map." : "Metric grid, spacing follows the zoom.";
    });
    map.__gridOn = () => on;
  }

  function onEnable(layer) {
    if (layer.minCameraHeight) {
      const z = zoomForMinHeight(layer.minCameraHeight);
      if (map.getZoom() < z) map.easeTo({ zoom: z + 0.4, duration: 900 });
    }
  }

  // --- 2D / 3D = camera pitch ---
  const buttons = [ui.btn2d, ui.btn3d];
  buttons.forEach((btn) => {
    btn.addEventListener("click", () => {
      map.easeTo({ pitch: btn.dataset.mode === "3d" ? 55 : 0, duration: 300 });
      buttons.forEach((b) => b.classList.toggle("active", b === btn));
    });
  });

  // --- real DGM terrain (raster-dem) — TASK 10, only where tagged in layers.json ---
  function wireDemTerrain() {
    const cfg = manifest.terrainDem;
    const SRC = "weimar_dgm";
    const HILLSHADE = "weimar_dgm__hillshade";
    // MapLibre wants an absolute tile-URL template; the manifest keeps it
    // page-relative (static tiles under web/terrain/). Absolutise the part
    // before "{z}" and leave the placeholders untouched.
    const demUrl = resolveUrl(cfg.url);
    const [demBase, demRest] = demUrl.split("{z}");
    const demTiles = /^https?:/i.test(demUrl)
      ? demUrl
      : new URL(demBase, document.baseURI).href + "{z}" + demRest;
    map.addSource(SRC, {
      type: "raster-dem",
      tiles: [demTiles],
      tileSize: cfg.tileSize ?? 256,
      encoding: cfg.encoding ?? "terrarium",
      minzoom: cfg.minzoom ?? 0,
      maxzoom: cfg.maxzoom ?? 15,
      ...(Array.isArray(cfg.bounds) ? { bounds: cfg.bounds } : {}),
      attribution: cfg.attribution || "",
    });
    map.addLayer({
      id: HILLSHADE,
      type: "hillshade",
      source: SRC,
      layout: { visibility: "none" },
      paint: {
        "hillshade-exaggeration": 0.6,
        "hillshade-shadow-color": "#3a3a3a",
        "hillshade-highlight-color": "#ffffff",
      },
    });

    ui.terrainBtn.hidden = false;
    let on = false;
    ui.terrainBtn.addEventListener("click", () => {
      on = !on;
      map.setTerrain(on ? { source: SRC, exaggeration: cfg.exaggeration ?? 1 } : null);
      map.setLayoutProperty(HILLSHADE, "visibility", on ? "visible" : "none");
      ui.terrainBtn.classList.toggle("active", on);
      ui.terrainBtn.textContent = on ? "Terrain: on" : "Terrain: off";
      if (on && map.getPitch() < 1) map.easeTo({ pitch: 60, duration: 600 });
    });
  }

  let detailed = null;

  /** Everything outside the city boundary is painted over in the page colour: all layers are clipped to the city. */
  async function addCityMask() {
    try {
      const bl = layerById("weimar_admin_boundary");
      const fc = await (await fetch(resolveUrl(bl.url))).json();
      const g = fc.features[0].geometry;
      const outer = g.type === "Polygon" ? g.coordinates[0] : g.coordinates[0][0];
      const world = [[-180, -85], [180, -85], [180, 85], [-180, 85], [-180, -85]];
      map.addSource("__city_mask", { type: "geojson", data: { type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [world, outer] } } });
      const before = map.getLayer("__base_border_line") ? "__base_border_line" : undefined;
      map.addLayer({ id: "__city_mask", type: "fill", source: "__city_mask", paint: { "fill-color": "#FFFFFF", "fill-opacity": 1 } }, before);
      for (const layer of layersForViewport(viewport)) {
        if (layer.onTop && managed.has(layer.id)) managed.get(layer.id).layerIds.forEach((lid) => map.moveLayer(lid));
      }
      for (const id of ["__export_rect_fill", "__export_rect_line", "__zone_fill", "__zone_line", "__zone_pts", "__base_border_line", "__base_border2_line", "__base_border_curve_line"]) {
        if (map.getLayer(id)) map.moveLayer(id);
      }
    } catch (err) { console.warn("[mask] city mask failed:", err); }
  }

  map.on("load", () => {
    try { grayscaleBasemap(map); } catch (err) { console.warn("[basemap] grayscale failed:", err); }
    for (const l of map.getStyle().layers || []) if (l.type === "symbol") baseSymbolIds.push(l.id);
    for (const layer of layersForViewport(viewport)) {
      try {
        if (layer.type === "wega_mesh") {
          detailed = createDetailedBuildings(map, layer, layerVisibleInViewport(layer, viewport));
          managed.set(layer.id, { config: layer, layerIds: [layer.id], setVisible: (v) => detailed.setVisible(v) });
        }
        else if (layer.type === "wms_class") addWmsClassLayer(layer);
        else if (layer.type === "wms") addWmsLayer(layer);
        else if (layer.type === "geojson" && layer.renderAs === "extrude") addGeoJsonExtrusionLayer(layer);
        else if (layer.type === "geojson") addGeoJsonLayer(layer);
        else if (layer.type === "3dtiles") add3DTilesLayer(layer);
        else console.warn(`[${viewport}] "${layer.id}" type "${layer.type}" not supported in a MapLibre block.`);
      } catch (err) {
        console.error(`[${viewport}] failed to add "${layer.id}":`, err);
      }
    }
    addCityMask();
    for (const layer of layersForViewport(viewport)) {
      if (layer.onTop && managed.has(layer.id)) managed.get(layer.id).layerIds.forEach((lid) => map.moveLayer(lid));
    }
    for (const id of baseSymbolIds) map.setLayoutProperty(id, "visibility", "none");   // street / place names off
    if (terrainDemAllowed(viewport)) {
      try { wireDemTerrain(); }
      catch (err) { console.error(`[${viewport}] DGM terrain wiring failed:`, err); }
    }
    const exporter = ui.exportToggleBtn ? createExportController({ map, viewport, ui }) : null;
    renderSidebar(ui.groupsEl, viewport, { setLayerVisible, setLayerFilter, onEnable, decorateRow: exporter ? exporter.decorateRow : undefined });
    if (opts.exportMode === "zone" && detailed) {
      const to2D = () => {
        map.easeTo({ pitch: 0, bearing: 0, duration: 400 });
        ui.btn2d.classList.add("active"); ui.btn3d.classList.remove("active");
      };
      createZoneExport({ map, ui, viewport, detailed, parcelsConfig: layerById("weimar_parcels_outline"), to2D });
    }
    for (const [btn, fmt] of [[ui.pngBtn, "png"], [ui.pdfBtn, "pdf"]]) {
      if (!btn) continue;
      const label = btn.textContent;
      btn.addEventListener("click", async () => {
        btn.disabled = true; btn.textContent = "…";
        try {
          await exportMapImage(map, fmt, viewport, title, ui.attributionEl.textContent, {
            legendBlocks: ui.annotCheck.checked ? legendModel(viewport) : null,
            drawGrid: map.__gridOn && map.__gridOn() ? drawGrid : null,
          });
        } catch (err) { console.error("[map image] failed:", err); alert(`Map image export failed: ${err.message}`); }
        finally { btn.disabled = false; btn.textContent = label; }
      });
    }
    wireGrid();
    renderMetadataPanel(ui.metaEl, viewport);
    refreshLegend();
    if (exporter) exporter.refresh();
    ui.attributionEl.innerHTML = attributionHtml(viewport, "MapLibre GL", baseAttribution);

    // Base border (TASK 16) — dashed-blue reference rectangle on top of the data
    // layers, from web/layers/base_border.json (same on every map).
    if (!opts.noBaseBorder && (viewport === "computational" || viewport === "space_syntax")) wireBaseBorderOverlay();
  });

  /** Dashed-blue base-border rectangle for this map, from base_border.json. */
  function wireBaseBorderOverlay() {
    if (!map.getSource("__base_border")) {
      map.addSource("__base_border", { type: "geojson", data: _emptyFC() });
      map.addLayer({
        id: "__base_border_line", type: "line", source: "__base_border",
        paint: { "line-color": "#0D0D0D", "line-width": 3, "line-dasharray": [1.4, 1.1] },
      });
    }
    baseBorder.subscribe((rect) => {
      const src = map.getSource("__base_border");
      if (src) src.setData(_rectPolygon(rect));
    });
    // Base border 2.0 and the curved border: blue / red so they read apart from the black one.
    if (!map.getSource("__base_border2")) {
      map.addSource("__base_border2", { type: "geojson", data: _emptyFC() });
      map.addLayer({ id: "__base_border2_line", type: "line", source: "__base_border2",
        paint: { "line-color": "#0F5FB5", "line-width": 2.5, "line-dasharray": [1.4, 1.1] } });
      map.addSource("__base_border_curve", { type: "geojson", data: _emptyFC() });
      map.addLayer({ id: "__base_border_curve_line", type: "line", source: "__base_border_curve",
        paint: { "line-color": "#E3262A", "line-width": 2.5, "line-dasharray": [1.4, 1.1] } });
    }
    baseBorder2.subscribe((rect) => {
      const src = map.getSource("__base_border2");
      if (src) src.setData(_rectPolygon(rect));
    });
    baseBorderCurve.subscribe((ring) => {
      const src = map.getSource("__base_border_curve");
      if (src) src.setData(ring ? _rectPolygon({ polygon: ring }) : _emptyFC());
    });
  }

  // Test hook — lets headless checks reach the map for a given viewport.
  (window.__mlMaps || (window.__mlMaps = {}))[viewport] = map;

  return { engine: "maplibre", map };
}

// ---------------------------------------------------------------------------
// Boot — load manifest once, then build the four blocks (top to bottom)
// ---------------------------------------------------------------------------
async function boot() {
  const mount = document.getElementById("blocks");
  try {
    await loadManifest();
  } catch (err) {
    console.error("[boot] could not load layers.json:", err);
    mount.hidden = false; mount.innerHTML = "<p id=\"boot-msg\">Failed to load layers.json — see console.</p>";
    return;
  }
  // Team-wide Base border (TASK 16) — before the blocks so their overlays paint
  // the rectangle on first render, never an empty state.
  await loadBaseBorder();

  mount.innerHTML = "";
  initLanding(mount);
}

// ---------------------------------------------------------------------------
// Landing page + lazy map sections. A map is built the first time its entry is
// opened (MapLibre needs a visible container to size itself), then only shown /
// hidden afterwards.
// ---------------------------------------------------------------------------
const SECTIONS = [
  { id: "combined", title: "Urban Morphology", subtitle: "Buildings, parcels, mobility, districts" },
  { id: "computational", title: "Simplified Model for Analysis", subtitle: "Precise geometry for Rhino / Grasshopper", startView: "base",
    // extra map windows stacked under the main one in the same section (each is its own viewport in layers.json)
    subMaps: [{ id: "space_syntax", title: "Space Syntax", subtitle: "Roads and counting gates for visual people / traffic counts", startView: "base",
      materials: [
        { label: "Counting sheets (PDF)", href: "materials/counting_sheets.pdf" },
        { label: "Gate maps (PDF)" },
        { label: "Counting gates table (Excel)", note: "Paper counting sheet, gate maps and a blank table of the gates for repeat counts." },
      ] }] },
  { id: "weimar_model", title: "Detailled Buildings (LOD2)", subtitle: "Buildings with roofs", startView: "church",
    exportMode: "zone", noBaseBorder: true, blank: true, noPopups: true },
  { id: "blue_green", title: "Natural Elements", subtitle: "Water, geology, terrain, protected areas" },
];

/** Landing intro: the main open-data sources, name = link (one per distinct portal URL). */
function renderLandingSources() {
  const ul = document.getElementById("landing-sources");
  if (!ul) return;
  const order = ["geoproxy_th", "wega3d", "stadt_weimar", "umweltportal_th", "atkis_verkehr_th", "openstreetmap"];
  const byId = new Map((manifest.resources || []).map((r) => [r.id, r]));
  const seen = new Set();
  for (const id of order) {
    const r = byId.get(id);
    if (!r || !r.url || seen.has(r.url)) continue;
    seen.add(r.url);
    const li = h("li");
    const a = h("a", null, { href: r.url, target: "_blank", rel: "noopener" });
    a.textContent = r.name;
    li.append(a);
    ul.append(li);
  }
}

function initLanding(mount) {
  renderLandingSources();
  const menu = document.getElementById("menu");
  const stage = document.getElementById("stage");
  const built = new Set();
  const panels = new Map();
  const buttons = new Map();

  function open(id, scroll = true) {
    const sec = SECTIONS.find((x) => x.id === id);
    if (!sec) return;
    for (const [pid, panel] of panels) panel.hidden = pid !== id;
    for (const [bid, btn] of buttons) btn.classList.toggle("active", bid === id);
    stage.hidden = false;
    if (!built.has(id)) {
      built.add(id);
      const build = (host, o) => createMapLibreBlock(host, {
        title: o.title, subtitle: o.subtitle, viewport: o.id, startView: o.startView,
        exportMode: o.exportMode, noBaseBorder: o.noBaseBorder, noPopups: o.noPopups, materials: o.materials,
        basemap: o.blank
          ? { style: { version: 8, sources: {}, layers: [{ id: "__bg", type: "background", paint: { "background-color": "#FFFFFF" } }] }, attribution: "" }
          : { styleUrl: CARTO_POSITRON_STYLE_URL, attribution: CARTO_ATTRIBUTION },
      });
      build(panels.get(id), sec);
      for (const sub of sec.subMaps || []) {
        const subHost = h("div", "stage-sub");
        panels.get(id).append(subHost);
        build(subHost, sub);
      }
    } else {
      for (const vp of [id, ...(sec.subMaps || []).map((x) => x.id)]) {
        const m = window.__mlMaps && window.__mlMaps[vp];
        if (m) requestAnimationFrame(() => m.resize());
      }
    }
    history.replaceState(null, "", "#" + id);
    if (scroll) stage.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  SECTIONS.forEach((sec, i) => {
    const btn = h("button", "menu-item", { type: "button" });
    const num = h("span", "menu-num"); num.textContent = String(i + 1).padStart(2, "0");
    const name = h("span", "menu-name"); name.textContent = sec.title;
    const arr = h("span", "menu-arrow"); arr.textContent = "↓";
    btn.append(num, name, arr);
    btn.addEventListener("click", () => open(sec.id));
    menu.append(btn);
    buttons.set(sec.id, btn);
    const panel = h("div", "stage-panel", { hidden: true });
    stage.append(panel);
    panels.set(sec.id, panel);
  });

  const initial = location.hash.slice(1);
  if (SECTIONS.some((x) => x.id === initial)) open(initial, false);
}

boot();
