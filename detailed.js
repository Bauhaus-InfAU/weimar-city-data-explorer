/* Street Section Analysis — "Detailed Buildings" (WEGA LoD2 with roofs) + Export zone.
 *
 * Loaded before app.js; everything here is called from createMapLibreBlock() in app.js and uses its
 * globals (h, _toUtm32, loadRhino3dm, _polygonObject, triggerDownload, _downloadStamp, JSZip …) at call time.
 *
 * Data: web/layers/data/weimar_detailed_buildings.{json,bin}, built by tools/extract_wega_buildings.py.
 * x, y = EPSG:3857 metres minus an anchor, z = metres above each building's lowest vertex.
 */
"use strict";

const ZONE_MAX_KM2 = 0.5;                 // export-zone size limit (keeps the 3DM light)
const MERC_WORLD = 40075016.68557849;      // EPSG:3857 circumference, m
const MERC_R = 6378137;
const SEL_RED = [0.89, 0.15, 0.16];

function _lngLatToMerc(lng, lat) {
  return [MERC_R * (lng * Math.PI) / 180, MERC_R * Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360))];
}
function _mat4mul(a, b) {                  // column-major a * b
  const o = new Float64Array(16);
  for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) {
    let s = 0;
    for (let k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k];
    o[c * 4 + r] = s;
  }
  return o;
}
function _ptInPoly(poly, x, y) {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [xi, yi] = poly[i], [xj, yj] = poly[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}
function _segX(ax, ay, bx, by, cx, cy, dx, dy) {
  const d1 = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax), d2 = (bx - ax) * (dy - ay) - (by - ay) * (dx - ax);
  const d3 = (dx - cx) * (ay - cy) - (dy - cy) * (ax - cx), d4 = (dx - cx) * (by - cy) - (dy - cy) * (bx - cx);
  return ((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) && ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0));
}
function _zoneAreaKm2(pts) {               // pts: [[lng,lat]…]
  const u = pts.map(([lng, lat]) => _toUtm32(lng, lat));
  let a = 0;
  for (let i = 0; i < u.length; i++) { const j = (i + 1) % u.length; a += u[i][0] * u[j][1] - u[j][0] * u[i][1]; }
  return Math.abs(a) / 2 / 1e6;
}

// ---------------------------------------------------------------------------
// Custom MapLibre layer
// ---------------------------------------------------------------------------
const _VS = `#version 300 es
layout(location=0) in vec3 a_pos;
layout(location=1) in float a_sel;
uniform mat4 u_matrix;
uniform vec2 u_off;
out float v_sel;
void main() {
  v_sel = a_sel;
  gl_Position = u_matrix * vec4(a_pos, 1.0);
  gl_Position.xy += u_off * gl_Position.w;
}`;
const _FS_FILL = `#version 300 es
precision highp float;
in float v_sel;
uniform vec3 u_red;
out vec4 o;
void main() { o = vec4(v_sel > 0.5 ? u_red : vec3(1.0), 1.0); }`;
const _FS_LINE = `#version 300 es
precision highp float;
in float v_sel;
out vec4 o;
void main() { o = v_sel > 0.5 ? vec4(1.0) : vec4(0.04, 0.04, 0.04, 1.0); }`;

function _program(gl, vs, fs) {
  const mk = (type, src) => {
    const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
    return s;
  };
  const p = gl.createProgram();
  gl.attachShader(p, mk(gl.VERTEX_SHADER, vs)); gl.attachShader(p, mk(gl.FRAGMENT_SHADER, fs));
  gl.linkProgram(p);
  if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
  return p;
}

/**
 * Adds the custom layer. Returns a controller:
 *   setVisible(bool), whenReady (Promise), data (after ready), setSelected(Set|null),
 *   findIntersecting(zonePolyLocal), toLocal(lng,lat)
 */
function createDetailedBuildings(map, config, visible) {
  const ctl = { visible, data: null, selected: new Set(), whenReady: null };
  let gl = null, ready = false;
  let progFill, progLine, vaoFill, vaoLine, posBuf, selBuf, idxBuf, edgeBuf;
  let nIdx = 0, nEdge = 0, nOut = 0, outBuf, vaoOut, anchorMerc = null, zk = 1, sz = 1, sel = null;
  const base = config.url;

  ctl.whenReady = Promise.all([
    fetch(base + ".json", { cache: "no-cache" }).then((r) => r.json()),
    fetch(base + ".bin", { cache: "no-cache" }).then((r) => r.arrayBuffer()),
  ]).then(([meta, buf]) => {
    const S = meta.sections;
    const view = (name, Ctor) => new Ctor(buf, S[name].offset, S[name].count);
    const d = {
      meta, nb: meta.buildings, nv: meta.vertices,
      pos: view("pos", Float32Array), idxL: view("idx", Uint16Array), edgL: view("edg", Uint16Array), outL: view("edgT", Uint16Array),
      tab: view("tab", Uint32Array), bbox: view("bbox", Float32Array),
      height: view("height", Float32Array), basez: view("basez", Float32Array),
    };
    d.idx = new Uint32Array(d.idxL.length);
    d.edg = new Uint32Array(d.edgL.length);
    d.out = new Uint32Array(d.outL.length);
    for (let b = 0; b < d.nb; b++) {
      const vs = d.tab[b * 8], is = d.tab[b * 8 + 2], ic = d.tab[b * 8 + 3], es = d.tab[b * 8 + 4], ec = d.tab[b * 8 + 5], ts = d.tab[b * 8 + 6], tc = d.tab[b * 8 + 7];
      for (let k = 0; k < tc; k++) d.out[ts + k] = d.outL[ts + k] + vs;
      for (let k = 0; k < ic; k++) d.idx[is + k] = d.idxL[is + k] + vs;
      for (let k = 0; k < ec; k++) d.edg[es + k] = d.edgL[es + k] + vs;
    }
    ctl.data = d;
    sel = new Float32Array(d.nv);
    const mc = maplibregl.MercatorCoordinate.fromLngLat([meta.anchor.lng, meta.anchor.lat]);
    anchorMerc = mc;
    zk = 1 / Math.cos((meta.anchor.lat * Math.PI) / 180);
    sz = zk / MERC_WORLD;
    if (gl) upload();
    return d;
  });

  function upload() {
    const d = ctl.data;
    posBuf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, posBuf); gl.bufferData(gl.ARRAY_BUFFER, d.pos, gl.STATIC_DRAW);
    selBuf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, selBuf); gl.bufferData(gl.ARRAY_BUFFER, sel, gl.DYNAMIC_DRAW);
    idxBuf = gl.createBuffer(); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, idxBuf); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, d.idx, gl.STATIC_DRAW);
    edgeBuf = gl.createBuffer(); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, edgeBuf); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, d.edg, gl.STATIC_DRAW);
    outBuf = gl.createBuffer(); gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, outBuf); gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, d.out, gl.STATIC_DRAW);
    const mkVao = (elementBuf) => {
      const v = gl.createVertexArray(); gl.bindVertexArray(v);
      gl.bindBuffer(gl.ARRAY_BUFFER, posBuf); gl.enableVertexAttribArray(0); gl.vertexAttribPointer(0, 3, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ARRAY_BUFFER, selBuf); gl.enableVertexAttribArray(1); gl.vertexAttribPointer(1, 1, gl.FLOAT, false, 0, 0);
      gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, elementBuf);
      gl.bindVertexArray(null);
      return v;
    };
    vaoFill = mkVao(idxBuf); vaoLine = mkVao(edgeBuf); vaoOut = mkVao(outBuf);
    nIdx = d.idx.length; nEdge = d.edg.length; nOut = d.out.length;
    ready = true;
    map.triggerRepaint();
  }

  map.addLayer({
    id: config.id, type: "custom", renderingMode: "3d",
    onAdd(_m, ctx) {
      gl = ctx;
      progFill = _program(gl, _VS, _FS_FILL);
      progLine = _program(gl, _VS, _FS_LINE);
      if (ctl.data) upload();
    },
    render(_ctx, matrix) {
      if (!ready || !ctl.visible) return;
      const s = 1 / MERC_WORLD;
      const model = [s, 0, 0, 0, 0, -s, 0, 0, 0, 0, sz, 0, anchorMerc.x, anchorMerc.y, 0, 1];
      const m = new Float32Array(_mat4mul(matrix, model));
      gl.enable(gl.DEPTH_TEST); gl.depthFunc(gl.LEQUAL); gl.depthMask(true);
      gl.disable(gl.CULL_FACE); gl.disable(gl.BLEND); gl.disable(gl.STENCIL_TEST);

      gl.useProgram(progFill);
      gl.uniformMatrix4fv(gl.getUniformLocation(progFill, "u_matrix"), false, m);
      gl.uniform2f(gl.getUniformLocation(progFill, "u_off"), 0, 0);
      gl.uniform3fv(gl.getUniformLocation(progFill, "u_red"), SEL_RED);
      gl.enable(gl.POLYGON_OFFSET_FILL); gl.polygonOffset(1.5, 1.5);
      gl.bindVertexArray(vaoFill);
      gl.drawElements(gl.TRIANGLES, nIdx, gl.UNSIGNED_INT, 0);
      gl.disable(gl.POLYGON_OFFSET_FILL);

      // inner lines: 1 px; object outline: the same line drawn at four sub-pixel offsets (~2 px)
      gl.useProgram(progLine);
      gl.uniformMatrix4fv(gl.getUniformLocation(progLine, "u_matrix"), false, m);
      const uOff = gl.getUniformLocation(progLine, "u_off");
      const px = 2 / gl.drawingBufferWidth, py = 2 / gl.drawingBufferHeight;
      gl.uniform2f(uOff, 0, 0);
      gl.bindVertexArray(vaoLine);
      gl.drawElements(gl.LINES, nEdge, gl.UNSIGNED_INT, 0);
      gl.bindVertexArray(vaoOut);
      for (const [dx, dy] of [[-0.5, -0.5], [0.5, -0.5], [-0.5, 0.5], [0.5, 0.5]]) {
        gl.uniform2f(uOff, dx * px, dy * py);
        gl.drawElements(gl.LINES, nOut, gl.UNSIGNED_INT, 0);
      }
      gl.bindVertexArray(null);
    },
  });

  ctl.setVisible = (v) => { ctl.visible = v; map.triggerRepaint(); };

  /** Highlight the given building indices (Set) in red with white outlines; null clears. */
  ctl.setSelected = (set) => {
    ctl.selected = set || new Set();
    const apply = () => {
      const d = ctl.data;
      sel.fill(0);
      for (const b of ctl.selected) sel.fill(1, d.tab[b * 8], d.tab[b * 8] + d.tab[b * 8 + 1]);
      if (gl && selBuf) { gl.bindBuffer(gl.ARRAY_BUFFER, selBuf); gl.bufferSubData(gl.ARRAY_BUFFER, 0, sel); }
      map.triggerRepaint();
    };
    if (ctl.data) apply(); else ctl.whenReady.then(apply);
  };

  /** lng/lat -> local metres (EPSG:3857 minus anchor) */
  ctl.toLocal = (lng, lat) => {
    const [mx, my] = _lngLatToMerc(lng, lat);
    return [mx - ctl.data.meta.anchor.mx, my - ctl.data.meta.anchor.my];
  };

  /** Indices of buildings that overlap (even partly) the zone polygon [[x,y]…] in local metres. */
  ctl.findIntersecting = (poly) => {
    const d = ctl.data, out = new Set();
    let minx = Infinity, miny = Infinity, maxx = -Infinity, maxy = -Infinity;
    for (const [x, y] of poly) { minx = Math.min(minx, x); maxx = Math.max(maxx, x); miny = Math.min(miny, y); maxy = Math.max(maxy, y); }
    for (let b = 0; b < d.nb; b++) {
      const bx0 = d.bbox[b * 4], by0 = d.bbox[b * 4 + 1], bx1 = d.bbox[b * 4 + 2], by1 = d.bbox[b * 4 + 3];
      if (bx1 < minx || bx0 > maxx || by1 < miny || by0 > maxy) continue;
      const vs = d.tab[b * 8], vc = d.tab[b * 8 + 1];
      let hit = false;
      for (let v = vs; v < vs + vc && !hit; v++) if (_ptInPoly(poly, d.pos[v * 3], d.pos[v * 3 + 1])) hit = true;
      if (!hit) {
        const ts = d.tab[b * 8 + 6], tc = d.tab[b * 8 + 7];
        for (let k = 0; k < tc && !hit; k += 2) {
          const a = d.out[ts + k] * 3, c = d.out[ts + k + 1] * 3;
          const ax = d.pos[a], ay = d.pos[a + 1], bx = d.pos[c], by = d.pos[c + 1];
          for (let i = 0; i < poly.length; i++) {
            const p = poly[i], q = poly[(i + 1) % poly.length];
            if (_segX(ax, ay, bx, by, p[0], p[1], q[0], q[1])) { hit = true; break; }
          }
        }
      }
      if (!hit) {                       // zone corner inside the building (zone smaller than the building)
        const is = d.tab[b * 8 + 2], ic = d.tab[b * 8 + 3];
        for (const [px, py] of poly) {
          if (px < bx0 || px > bx1 || py < by0 || py > by1) continue;
          for (let k = 0; k < ic && !hit; k += 3) {
            const A = (d.idx[is + k]) * 3, B = (d.idx[is + k + 1]) * 3, C = (d.idx[is + k + 2]) * 3;
            const x1 = d.pos[A], y1 = d.pos[A + 1], x2 = d.pos[B], y2 = d.pos[B + 1], x3 = d.pos[C], y3 = d.pos[C + 1];
            const s1 = (px - x2) * (y1 - y2) - (x1 - x2) * (py - y2);
            const s2 = (px - x3) * (y2 - y3) - (x2 - x3) * (py - y3);
            const s3 = (px - x1) * (y3 - y1) - (x3 - x1) * (py - y1);
            if (!((s1 < 0 || s2 < 0 || s3 < 0) && (s1 > 0 || s2 > 0 || s3 > 0))) hit = true;
          }
          if (hit) break;
        }
      }
      if (hit) out.add(b);
    }
    return out;
  };
  return ctl;
}

// ---------------------------------------------------------------------------
// Export zone: draw a closed contour on the 2D map -> select -> 3DM
// ---------------------------------------------------------------------------
function createZoneExport({ map, ui, viewport, detailed, parcelsConfig, to2D }) {
  const SRC = "__zone";
  let pts = [];                 // [[lng,lat]…] while drawing
  let zone = null;              // closed polygon [[lng,lat]…] once valid
  let selected = new Set();
  let drawing = false;
  let parcelsPromise = null;

  const setStatus = (t, warn) => { ui.zoneStatus.textContent = t || ""; ui.zoneStatus.classList.toggle("warn", !!warn); ui.zoneStatus.hidden = !t; };
  const refresh = () => { ui.zoneExportBtn.disabled = !(zone && selected.size > 0); ui.zoneClearBtn.disabled = !(zone || drawing); };

  function ensureLayers() {
    if (map.getSource(SRC)) return;
    map.addSource(SRC, { type: "geojson", data: { type: "FeatureCollection", features: [] } });
    map.addLayer({ id: "__zone_fill", type: "fill", source: SRC, filter: ["==", ["geometry-type"], "Polygon"],
      paint: { "fill-color": "#E3262A", "fill-opacity": 0.1 } });
    map.addLayer({ id: "__zone_line", type: "line", source: SRC,
      paint: { "line-color": "#E3262A", "line-width": 2.5, "line-dasharray": [2, 1.2] } });
    map.addLayer({ id: "__zone_pts", type: "circle", source: SRC, filter: ["==", ["geometry-type"], "Point"],
      paint: { "circle-radius": 4, "circle-color": "#ffffff", "circle-stroke-color": "#E3262A", "circle-stroke-width": 2 } });
  }
  function paint(list, closed, cursor) {
    ensureLayers();
    const feats = [];
    const ring = cursor ? [...list, cursor] : list;
    if (closed && list.length >= 3) feats.push({ type: "Feature", properties: {}, geometry: { type: "Polygon", coordinates: [[...list, list[0]]] } });
    else if (ring.length >= 2) feats.push({ type: "Feature", properties: {}, geometry: { type: "LineString", coordinates: ring } });
    for (const p of list) feats.push({ type: "Feature", properties: {}, geometry: { type: "Point", coordinates: p } });
    map.getSource(SRC).setData({ type: "FeatureCollection", features: feats });
  }

  function stopDrawing() {
    drawing = false; map.__drawing = false;
    map.off("click", onClick); map.off("mousemove", onMove); map.off("dblclick", onDbl);
    document.removeEventListener("keydown", onKey);
    map.doubleClickZoom.enable();
    map.getCanvas().style.cursor = "";
    ui.zoneBtn.classList.remove("active");
  }
  function clearAll() {
    if (drawing) stopDrawing();
    pts = []; zone = null; selected = new Set();
    if (map.getSource(SRC)) paint([], false);
    detailed.setSelected(null);
    setStatus(`Draw a closed contour on the 2D map. Max size ${ZONE_MAX_KM2} km².`);
    refresh();
  }

  function finish() {
    if (pts.length < 3) { setStatus("Need at least 3 points.", true); return; }
    const area = _zoneAreaKm2(pts);
    stopDrawing();
    if (area > ZONE_MAX_KM2) {
      setStatus(`Zone too large: ${area.toFixed(2)} km² (max ${ZONE_MAX_KM2} km²). Draw a smaller one.`, true);
      pts = []; paint([], false); zone = null; selected = new Set(); detailed.setSelected(null); refresh();
      return;
    }
    zone = pts.slice(); pts = [];
    paint(zone, true);
    const local = zone.map(([lng, lat]) => detailed.toLocal(lng, lat));
    selected = detailed.findIntersecting(local);
    detailed.setSelected(selected);
    setStatus(`${selected.size.toLocaleString()} buildings in the zone · ${area.toFixed(3)} km² (max ${ZONE_MAX_KM2}).`);
    refresh();
  }

  function onClick(e) {
    const p = [e.lngLat.lng, e.lngLat.lat];
    if (pts.length >= 3) {
      const a = map.project(pts[0]), b = e.point;
      if (Math.hypot(a.x - b.x, a.y - b.y) < 12) return finish();
    }
    pts.push(p); paint(pts, false);
    setStatus(`${pts.length} point(s) · click the first point, double-click or press Enter to close · Esc cancels.`);
    refresh();
  }
  function onDbl(e) {
    e.preventDefault();
    if (pts.length >= 2) pts.pop();      // the double click added the last vertex twice
    finish();
  }
  function onMove(e) {
    if (!pts.length) return;
    const c = [e.lngLat.lng, e.lngLat.lat];
    paint(pts, false, c);
    const a = _zoneAreaKm2([...pts, c]);
    if (pts.length >= 2) setStatus(`${pts.length} point(s) · ≈ ${a.toFixed(3)} km² of max ${ZONE_MAX_KM2}${a > ZONE_MAX_KM2 ? " — too large" : ""}`, a > ZONE_MAX_KM2);
  }
  function onKey(e) {
    if (e.key === "Escape") clearAll();
    else if (e.key === "Enter") finish();
  }

  ui.zoneBtn.addEventListener("click", () => {
    clearAll();
    to2D();
    drawing = true; map.__drawing = true;
    ui.zoneBtn.classList.add("active");
    map.doubleClickZoom.disable();
    map.getCanvas().style.cursor = "crosshair";
    map.on("click", onClick); map.on("mousemove", onMove); map.on("dblclick", onDbl);
    document.addEventListener("keydown", onKey);
    setStatus("Click points on the map to draw the zone (2D view).");
    refresh();
  });
  ui.zoneClearBtn.addEventListener("click", clearAll);

  // ---- 3DM export -------------------------------------------------------
  const ringHits = (rings, poly) => {          // parcel rings [[lng,lat]] vs zone polygon, both lng/lat
    const outer = rings[0];
    for (const [x, y] of outer) if (_ptInPoly(poly, x, y)) return true;
    for (const [x, y] of poly) if (_ptInPoly(outer, x, y)) return true;
    for (let i = 0; i < outer.length - 1; i++) for (let j = 0; j < poly.length; j++) {
      const q = poly[j], r = poly[(j + 1) % poly.length];
      if (_segX(outer[i][0], outer[i][1], outer[i + 1][0], outer[i + 1][1], q[0], q[1], r[0], r[1])) return true;
    }
    return false;
  };

  async function runExport() {
    if (ui.zoneExportBtn.disabled) return;
    ui.zoneExportBtn.disabled = true;
    const t0 = performance.now();
    try {
      setStatus("Preparing 3DM…");
      const rhino = await loadRhino3dm();
      const d = detailed.data;
      const generated = new Date().toISOString();
      // zone centre -> local origin (rounded to 1 km): Rhino meshes are float32, absolute UTM would be too coarse
      const cs = zone.map(([lng, lat]) => _toUtm32(lng, lat));
      const ox = Math.round(cs.reduce((a, c) => a + c[0], 0) / cs.length / 1000) * 1000;
      const oy = Math.round(cs.reduce((a, c) => a + c[1], 0) / cs.length / 1000) * 1000;
      const doc = new rhino.File3dm();
      doc.settings().modelUnitSystem = rhino.UnitSystem.Meters;
      try {
        doc.strings().set("source", "Data Twin City Weimar — Street Section Analysis");
        doc.strings().set("crs", "EPSG:25832");
        doc.strings().set("originEasting", String(ox));
        doc.strings().set("originNorthing", String(oy));
      } catch (e) { /* optional */ }
      const mkLayer = (name, rgb) => {
        const l = new rhino.Layer(); l.name = name;
        if (rgb) l.color = { r: rgb[0], g: rgb[1], b: rgb[2], a: 255 };
        return doc.layers().add(l);
      };
      const layB = mkLayer("Detailed Buildings", [40, 40, 40]);
      const layP = mkLayer("Parcels", [120, 120, 120]);
      const layZ = mkLayer("Export zone", [227, 38, 42]);

      // buildings -> meshes
      const list = [...selected];
      let nTri = 0;
      for (let n = 0; n < list.length; n++) {
        const b = list[n];
        const vs = d.tab[b * 8], vc = d.tab[b * 8 + 1], is = d.tab[b * 8 + 2], ic = d.tab[b * 8 + 3];
        const mesh = new rhino.Mesh();
        for (let v = vs; v < vs + vc; v++) {
          const [e, nn] = proj4("EPSG:3857", "EPSG:25832", [d.pos[v * 3] + d.meta.anchor.mx, d.pos[v * 3 + 1] + d.meta.anchor.my]);
          mesh.vertices().add(e - ox, nn - oy, d.pos[v * 3 + 2]);
        }
        for (let k = 0; k < ic; k += 3) mesh.faces().addTriFace(d.idx[is + k] - vs, d.idx[is + k + 1] - vs, d.idx[is + k + 2] - vs);
        mesh.normals().computeNormals(); mesh.compact();
        const a = new rhino.ObjectAttributes();
        a.layerIndex = layB; a.name = d.meta.ids[b];
        a.setUserString("buildingId", d.meta.ids[b]);
        a.setUserString("height", d.height[b].toFixed(2));
        a.setUserString("baseZ_ellipsoidal", d.basez[b].toFixed(2));
        doc.objects().add(mesh, a);
        nTri += ic / 3;
        if (n % 150 === 0) { setStatus(`Building meshes… ${n}/${list.length}`); await _tick(); }
      }

      // parcels touching the zone -> flat Breps
      setStatus("Parcels…"); await _tick();
      let nParcels = 0, pSkipped = 0;
      if (parcelsConfig) {
        parcelsPromise = parcelsPromise || fetch(parcelsConfig.url).then((r) => r.json());
        const fc = await parcelsPromise;
        let zx0 = Infinity, zy0 = Infinity, zx1 = -Infinity, zy1 = -Infinity;
        for (const [x, y] of zone) { zx0 = Math.min(zx0, x); zx1 = Math.max(zx1, x); zy0 = Math.min(zy0, y); zy1 = Math.max(zy1, y); }
        for (const f of fc.features) {
          const g = f.geometry; if (!g) continue;
          const polys = g.type === "Polygon" ? [g.coordinates] : g.type === "MultiPolygon" ? g.coordinates : [];
          for (const rings of polys) {
            const o = rings[0];
            let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
            for (const [x, y] of o) { if (x < x0) x0 = x; if (x > x1) x1 = x; if (y < y0) y0 = y; if (y > y1) y1 = y; }
            if (x1 < zx0 || x0 > zx1 || y1 < zy0 || y0 > zy1 || !ringHits(rings, zone)) continue;
            const r = _polygonObject(rhino, rings, 0, ox, oy);
            if (r.skip) { pSkipped++; continue; }
            const a = new rhino.ObjectAttributes();
            a.layerIndex = layP;
            const p = f.properties || {};
            a.name = String(p.label || p.nationalCadastralReference || "");
            const us = _userStrings(p, ["usage", "useCategory", "area", "areaSource"]);
            for (const k in us) a.setUserString(k, us[k]);
            doc.objects().add(r.geom, a); nParcels++;
          }
        }
      }

      // zone outline
      const pl = new rhino.Polyline(zone.length + 1);
      for (const c of [...cs, cs[0]]) pl.add(c[0] - ox, c[1] - oy, 0);
      const za = new rhino.ObjectAttributes(); za.layerIndex = layZ; za.name = "Export zone";
      doc.objects().addCurve(pl.toPolylineCurve(), za);

      setStatus("Writing file…"); await _tick();
      const bytes = doc.toByteArray();
      if (doc.delete) doc.delete();

      const area = _zoneAreaKm2(zone);
      const zip = new JSZip();
      const fileName = `weimar_street_section_zone.3dm`;
      zip.file(fileName, bytes);
      zip.file("manifest.json", JSON.stringify({
        generated, viewport, format: "3dm (rhino3dm.js 8.4.0)", file: fileName, crs: "EPSG:25832", units: "Meters",
        origin: { originEasting: ox, originNorthing: oy, note: "coordinates in the file = EPSG:25832 minus (E, N); z = metres above each building's lowest point" },
        zone: { areaKm2: Number(area.toFixed(4)), maxKm2: ZONE_MAX_KM2, polygonLngLat: zone },
        selectionRule: "every building mesh that overlaps the zone, even partly, is exported whole",
        layers: { "Detailed Buildings": { objects: list.length, triangles: nTri, geometry: "triangle mesh (WEGA LoD2 with roofs)" },
          Parcels: { objects: nParcels, skipped: pSkipped, geometry: "flat single-face Brep, z = 0" }, "Export zone": { objects: 1 } },
      }, null, 2));
      zip.file("ATTRIBUTION.txt", [
        "Data Twin City Weimar — Street Section Analysis, 3DM export", `Generated: ${generated}`,
        "CRS: EPSG:25832, units metres; local origin applied: add E " + ox + ", N " + oy,
        "", "Detailed Buildings: © GDI-Th, WEGA 3D-Gebäudemodelle LoD2, Datenlizenz Deutschland – Namensnennung 2.0",
        "Parcels: © GDI-Th, ALKIS, Datenlizenz Deutschland – Namensnennung 2.0",
      ].join("\n"));
      const blob = await zip.generateAsync({ type: "blob", compression: "DEFLATE" });
      triggerDownload(blob, `weimar_street_section_zone_${_downloadStamp()}.zip`);
      setStatus(`Downloaded: ${list.length.toLocaleString()} buildings, ${nParcels} parcels · ${(bytes.length / 1048576).toFixed(1)} MB .3dm · ${((performance.now() - t0) / 1000).toFixed(1)} s · local origin E ${ox} / N ${oy}`);
    } catch (err) {
      console.error("[zone export] failed:", err);
      setStatus(`Export failed: ${err.message}`, true);
    } finally {
      refresh();
    }
  }
  ui.zoneExportBtn.addEventListener("click", runExport);
  clearAll();
  return { clear: clearAll };
}
