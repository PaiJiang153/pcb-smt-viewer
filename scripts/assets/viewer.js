/* =======================================================================
   SMT 贴片定位图 — canvas viewer
   data: #pcb-data (JSON) embedded by build.py
   ======================================================================= */
(function () {
  'use strict';

  // ------------------------------------------------------------------ data
  const DATA = JSON.parse(document.getElementById('pcb-data').textContent);
  const COMPS = DATA.components;
  const PADS = DATA.pads;
  const BOARD = {
    x0: DATA.bbox[0], y0: DATA.bbox[1], x1: DATA.bbox[2], y1: DATA.bbox[3]
  };
  BOARD.w = BOARD.x1 - BOARD.x0;
  BOARD.h = BOARD.y1 - BOARD.y0;

  const LAYER_COPPER = { 1: 'top', 2: 'mid1', 3: 'mid2', 32: 'bottom' };
  const N_TOP = COMPS.filter(c => c.side === 'top').length;
  const N_BOT = COMPS.length - N_TOP;

  // per component: pads
  COMPS.forEach(c => { c.pads = []; });
  PADS.forEach(p => { if (COMPS[p.c]) COMPS[p.c].pads.push(p); });
  COMPS.forEach(c => {
    c.key = c.d;
    c.n = c.pads.length;
    c.bb = c.bb || [c.x, c.y, c.x, c.y];
    const bb = c.bb;
    c.w = Math.max(0.3, bb[2] - bb[0]);
    c.h = Math.max(0.3, bb[3] - bb[1]);
    c.cx = (bb[0] + bb[2]) / 2;
    c.cy = (bb[1] + bb[3]) / 2;
    c.area = c.w * c.h;
    c.disp = (c.value && c.value.trim()) ? c.value.trim() : (c.comment || c.val || '');
  });
  const BY_DES = new Map(COMPS.map(c => [c.d, c]));

  // ------------------------------------------------------------------ utils
  const $ = s => document.querySelector(s);
  const clamp = (v, a, b) => v < a ? a : v > b ? b : v;
  const fmt = (v, n) => (Math.round(v * Math.pow(10, n)) / Math.pow(10, n)).toFixed(n);

  function toast(msg) {
    const t = $('#toast');
    t.textContent = msg;
    t.classList.add('on');
    clearTimeout(toast._t);
    toast._t = setTimeout(() => t.classList.remove('on'), 1800);
  }

  // --------------------------------------------------------------- storage
  const LS_KEY = 'smt-placed:' + (DATA.title || 'pcb');
  const placed = new Set();
  function loadPlaced() {
    try {
      const raw = localStorage.getItem(LS_KEY);
      if (raw) JSON.parse(raw).forEach(d => placed.add(d));
    } catch (e) { /* file:// or private mode */ }
  }
  function savePlaced() {
    try { localStorage.setItem(LS_KEY, JSON.stringify([...placed])); } catch (e) {}
  }

  // ------------------------------------------------------------ geometry state
  const S = {
    side: 'top',
    z: 12, tx: 0, ty: 0,
    sel: null, hover: null,
    showSilk: true, showCu: true, showGhost: true, showLabels: true, showGrid: true,
    filterSide: 'cur', onlyUnplaced: false,
    q: '',
    flash: 0, flashStart: 0
  };
  function ax() { return S.side === 'top' ? 1 : -1; }

  // ------------------------------------------------------------------ canvas
  const cv = $('#cv');
  const ctx = cv.getContext('2d');
  const stage = $('#stage');
  let W = 0, H = 0, DPR = 1;

  function resize() {
    const r = stage.getBoundingClientRect();
    const oldW = W, oldH = H;
    DPR = Math.min(window.devicePixelRatio || 1, 2.5);
    W = Math.max(200, Math.round(r.width));
    H = Math.max(200, Math.round(r.height));
    cv.width = Math.round(W * DPR);
    cv.height = Math.round(H * DPR);
    if (oldW && oldH && (oldW !== W || oldH !== H)) {
      // keep the visible centre stable ...
      S.tx += (W - oldW) / 2;
      S.ty += (H - oldH) / 2;
      // ... and if the whole board was on screen, keep it that way
      if (S.fitZoom && S.z <= S.fitZoom * 1.03) { fit(); return; }
    }
    draw();
  }

  const w2s = (x, y) => [ax() * x * S.z + S.tx, -y * S.z + S.ty];
  const s2w = (sx, sy) => [(sx - S.tx) / (ax() * S.z), (S.ty - sy) / S.z];

  function applyWorld() {
    ctx.setTransform(DPR * ax() * S.z, 0, 0, -DPR * S.z, DPR * S.tx, DPR * S.ty);
  }
  function applyScreen() {
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
  }

  function fit(margin) {
    const ml = 26, mr = 26, mt = 22, mb = margin == null ? 74 : margin;
    const z = Math.min((W - ml - mr) / BOARD.w, (H - mt - mb) / BOARD.h);
    S.z = Math.max(1, z);
    S.fitZoom = S.z;
    const cx = (BOARD.x0 + BOARD.x1) / 2, cy = (BOARD.y0 + BOARD.y1) / 2;
    S.tx = ml + (W - ml - mr) / 2 - ax() * cx * S.z;
    S.ty = mt + (H - mt - mb) / 2 + cy * S.z;
    draw();
  }

  function zoomAt(factor, sx, sy) {
    if (sx == null) { sx = W / 2; sy = H / 2; }
    const z0 = S.z;
    const z1 = clamp(z0 * factor, 2, 1400);
    if (z1 === z0) return;
    const [wx, wy] = s2w(sx, sy);
    S.z = z1;
    S.tx = sx - ax() * wx * z1;
    S.ty = sy + wy * z1;
    draw();
  }

  // ------------------------------------------------------------- paths
  // Path2D objects are built once in board mm (Y up) and drawn under the
  // world transform.
  const paths = {};

  function polyPath(contours) {
    const p = new Path2D();
    (contours || []).forEach(c => {
      if (!c || c.length < 2) return;
      p.moveTo(c[0][0], c[0][1]);
      for (let i = 1; i < c.length; i++) p.lineTo(c[i][0], c[i][1]);
      p.closePath();
    });
    return p;
  }

  function buildPaths() {
    paths.board = polyPath(DATA.outline);
    // pours: one merged path per side so overlapping polygons do not
    // accumulate alpha (they are a union on the real board)
    const pours = { top: new Path2D(), bottom: new Path2D(), mid: new Path2D() };
    (DATA.polygons || []).forEach(pg => {
      const L = (pg.layer || '').toUpperCase();
      const target = L === 'TOP' ? pours.top : (L === 'BOTTOM' ? pours.bottom : pours.mid);
      (pg.contours || []).forEach(c => {
        if (!c || c.length < 3) return;
        target.moveTo(c[0][0], c[0][1]);
        for (let i = 1; i < c.length; i++) target.lineTo(c[i][0], c[i][1]);
        target.closePath();
      });
    });
    paths.pours = pours;

    // copper tracks: one path per layer
    paths.cu = {};
    (DATA.tracks || []).forEach(t => {
      const L = t[5];
      let p = paths.cu[L];
      if (!p) p = paths.cu[L] = new Path2D();
      p.moveTo(t[0], t[1]);
      p.lineTo(t[2], t[3]);
    });
    // copper arcs
    (DATA.arcs || []).forEach(a => {
      const L = a[6];
      let p = paths.cu[L];
      if (!p) p = paths.cu[L] = new Path2D();
      arcTo(p, a[0], a[1], a[2], a[3], a[4]);
    });

    // silk
    paths.silk = {};
    (DATA.silk || []).forEach(t => {
      const L = t[5];
      let p = paths.silk[L];
      if (!p) p = paths.silk[L] = new Path2D();
      p.moveTo(t[0], t[1]);
      p.lineTo(t[2], t[3]);
    });
    (DATA.silkarcs || []).forEach(a => {
      const L = a[6];
      let p = paths.silk[L];
      if (!p) p = paths.silk[L] = new Path2D();
      arcTo(p, a[0], a[1], a[2], a[3], a[4]);
    });
  }

  function arcTo(p, cx, cy, r, a0, a1, ccw) {
    if (!(r > 0)) return;
    const A0 = a0 * Math.PI / 180, A1 = a1 * Math.PI / 180;
    p.moveTo(cx + r * Math.cos(A0), cy + r * Math.sin(A0));
    p.arc(cx, cy, r, A0, A1, false);
  }

  // ------------------------------------------------------------- drawing
  const COL = {
    board: '#1b2a21',
    boardEdge: '#5c8a6c',
    grid1: 'rgba(200,255,220,.045)',
    grid5: 'rgba(200,255,220,.095)',
    cuTop: 'rgba(236,120,86,.95)',
    cuBot: 'rgba(102,158,244,.95)',
    pourTop: 'rgba(236,120,86,.075)',
    pourBot: 'rgba(102,158,244,.075)',
    fillTop: 'rgba(236,120,86,.20)',
    fillBot: 'rgba(102,158,244,.20)',
    silk: 'rgba(226,232,240,.70)',
    padTop: '#ffa074',
    padBot: '#8bbcff',
    padMulti: '#f0d47c',
    padMask: 'rgba(8,12,16,.55)',
    via: '#cdbb80',
    viaHole: '#0b0f14',
    box: 'rgba(126,255,176,.34)',
    boxFill: 'rgba(126,255,176,.05)',
    sel: '#4be48e',
    selFill: 'rgba(75,228,142,.16)',
    ghost: 'rgba(255,255,255,.06)',
    label: '#e2ffe9'
  };

  function drawBoardFill() {
    applyWorld();
    ctx.fillStyle = COL.board;
    ctx.fill(paths.board);
    ctx.lineWidth = Math.max(0.06, 1.6 / S.z);
    ctx.strokeStyle = COL.boardEdge;
    ctx.stroke(paths.board);
  }

  function drawGrid() {
    if (S.z < 5.5) return;
    applyWorld();
    const step1 = 1, step5 = 5;
    ctx.lineWidth = Math.max(0.02, 0.7 / S.z);
    for (const [step, col] of [[step1, COL.grid1], [step5, COL.grid5]]) {
      ctx.beginPath();
      for (let x = Math.ceil(BOARD.x0 / step) * step; x <= BOARD.x1; x += step) {
        ctx.moveTo(x, BOARD.y0); ctx.lineTo(x, BOARD.y1);
      }
      for (let y = Math.ceil(BOARD.y0 / step) * step; y <= BOARD.y1; y += step) {
        ctx.moveTo(BOARD.x0, y); ctx.lineTo(BOARD.x1, y);
      }
      ctx.strokeStyle = col;
      ctx.stroke();
    }
  }

  function drawPours(side) {
    applyWorld();
    const path = side === 'top' ? paths.pours.top : paths.pours.bottom;
    ctx.fillStyle = side === 'top' ? COL.pourTop : COL.pourBot;
    ctx.fill(path);
    ctx.lineWidth = Math.max(0.02, 0.8 / S.z);
    ctx.strokeStyle = side === 'top' ? 'rgba(255,168,120,.30)' : 'rgba(150,190,255,.30)';
    ctx.stroke(path);
  }

  let trackGroups = null;
  function addTo(map, w, t) {
    let m = map.get(w);
    if (!m) { m = new Path2D(); map.set(w, m); }
    m.moveTo(t[0], t[1]); m.lineTo(t[2], t[3]);
  }

  function buildTrackGroups() {
    trackGroups = {
      top: { route: new Map(), fill: new Map() },
      bottom: { route: new Map(), fill: new Map() }
    };
    (DATA.tracks || []).forEach(t => {
      const L = t[5];
      if (L !== 1 && L !== 32) return;
      const side = L === 1 ? 'top' : 'bottom';
      addTo(trackGroups[side].route, Math.round(t[4] * 1000) / 1000, t);
    });
    (DATA.filltracks || []).forEach(t => {
      const L = t[5];
      if (L !== 1 && L !== 32) return;
      const side = L === 1 ? 'top' : 'bottom';
      addTo(trackGroups[side].fill, Math.round(t[4] * 1000) / 1000, t);
    });
    (DATA.arcs || []).forEach(a => {
      const L = a[6];
      if (L !== 1 && L !== 32) return;
      const side = L === 1 ? 'top' : 'bottom';
      const w = Math.round(a[5] * 1000) / 1000;
      let m = trackGroups[side].route.get(w);
      if (!m) { m = new Path2D(); trackGroups[side].route.set(w, m); }
      arcTo(m, a[0], a[1], a[2], a[3], a[4]);
    });
  }

  function strokeWidthGroups(side) {
    const g = trackGroups[side];
    if (!g) return;
    // copper pour scan-lines first, dim
    ctx.strokeStyle = side === 'top' ? COL.fillTop : COL.fillBot;
    g.fill.forEach((path, w) => {
      ctx.lineWidth = Math.max(w, 0.02);
      ctx.stroke(path);
    });
    // real routing on top, bright
    ctx.strokeStyle = side === 'top' ? COL.cuTop : COL.cuBot;
    g.route.forEach((path, w) => {
      ctx.lineWidth = Math.max(w, 0.02);
      ctx.stroke(path);
    });
  }

  function drawGhost(side) {
    if (!S.showGhost) return;
    const other = side === 'top' ? 'bottom' : 'top';
    applyWorld();
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    const g = trackGroups[other];
    if (g) {
      ctx.strokeStyle = other === 'top' ? 'rgba(236,120,86,.075)' : 'rgba(102,158,244,.075)';
      g.fill.forEach((path, w) => { ctx.lineWidth = Math.max(w, 0.02); ctx.stroke(path); });
      ctx.strokeStyle = 'rgba(255,255,255,.10)';
      g.route.forEach((path, w) => { ctx.lineWidth = Math.max(w, 0.02); ctx.stroke(path); });
    }
    // other side pads
    const c = other === 'top' ? 'rgba(240,138,99,.20)' : 'rgba(110,168,255,.20)';
    ctx.fillStyle = c;
    PADS.forEach(p => {
      if (p.l !== (other === 'top' ? 'TOP' : 'BOTTOM')) return;
      padPath(p);
    });
  }

  function roundedRectPath(p, w, h, r) {
    r = Math.min(r, Math.min(w, h) / 2);
    p.moveTo(-w / 2 + r, -h / 2);
    p.lineTo(w / 2 - r, -h / 2);
    p.arcTo(w / 2, -h / 2, w / 2, -h / 2 + r, r);
    p.lineTo(w / 2, h / 2 - r);
    p.arcTo(w / 2, h / 2, w / 2 - r, h / 2, r);
    p.lineTo(-w / 2 + r, h / 2);
    p.arcTo(-w / 2, h / 2, -w / 2, h / 2 - r, r);
    p.lineTo(-w / 2, -h / 2 + r);
    p.arcTo(-w / 2, -h / 2, -w / 2 + r, -h / 2, r);
    p.closePath();
  }

  // draw one pad in world coordinates
  function padPath(p) {
    const w = Math.max(p.w, 0.03), h = Math.max(p.h, 0.03);
    ctx.save();
    ctx.translate(p.x, p.y);
    if (p.r) ctx.rotate(p.r * Math.PI / 180);
    if (p.s === 1) {
      ctx.beginPath();
      ctx.ellipse(0, 0, w / 2, h / 2, 0, 0, Math.PI * 2);
    } else {
      ctx.beginPath();
      roundedRectPath(ctx, w, h, 0);
    }
    ctx.fill();
    ctx.restore();
  }

  function drawPads(side) {
    applyWorld();
    const wantLayer = side === 'top' ? 'TOP' : 'BOTTOM';
    const list = PADS.filter(p => p.l === wantLayer);
    // solder-mask opening halo makes the pads read clearly on top of copper
    ctx.fillStyle = COL.padMask;
    list.forEach(p => {
      const g = 0.07;
      ctx.save();
      ctx.translate(p.x, p.y);
      if (p.r) ctx.rotate(p.r * Math.PI / 180);
      ctx.beginPath();
      roundedRectPath(ctx, Math.max(p.w, 0.03) + 2 * g, Math.max(p.h, 0.03) + 2 * g, Math.min(p.w, p.h) * 0.45 + g);
      ctx.fill();
      ctx.restore();
    });
    ctx.fillStyle = side === 'top' ? COL.padTop : COL.padBot;
    list.forEach(p => padPath(p));
    // multilayer (through hole) on top of both
    PADS.forEach(p => {
      if (p.l !== 'MULTILAYER') return;
      ctx.fillStyle = COL.padMask;
      ctx.save();
      ctx.translate(p.x, p.y);
      if (p.r) ctx.rotate(p.r * Math.PI / 180);
      ctx.beginPath();
      roundedRectPath(ctx, Math.max(p.w, 0.03) + 0.16, Math.max(p.h, 0.03) + 0.16, 0.2);
      ctx.fill();
      ctx.restore();
      ctx.fillStyle = COL.padMulti;
      padPath(p);
      if (p.hole > 0) {
        ctx.fillStyle = COL.viaHole;
        ctx.beginPath();
        ctx.arc(p.x, p.y, Math.max(p.hole / 2, 0.03), 0, Math.PI * 2);
        ctx.fill();
      }
    });
  }

  function drawVias() {
    applyWorld();
    const g = new Path2D(), holes = new Path2D();
    (DATA.vias || []).forEach(v => {
      const r = Math.max(v[2] / 2, 0.05);
      g.moveTo(v[0] + r, v[1]);
      g.arc(v[0], v[1], r, 0, Math.PI * 2);
      if (v[3] > 0) {
        const rh = Math.max(v[3] / 2, 0.04);
        holes.moveTo(v[0] + rh, v[1]);
        holes.arc(v[0], v[1], rh, 0, Math.PI * 2);
      }
    });
    ctx.fillStyle = COL.via;
    ctx.globalAlpha = 0.55;
    ctx.fill(g);
    ctx.globalAlpha = 1;
    ctx.fillStyle = COL.viaHole;
    ctx.fill(holes);
  }

  function drawSilk(side) {
    if (!S.showSilk) return;
    applyWorld();
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.strokeStyle = COL.silk;
    const layer = side === 'top' ? 33 : 34;
    const p = paths.silk[layer];
    if (p) {
      ctx.lineWidth = 0.13;
      ctx.stroke(p);
    }
  }

  // ------------------------------------------------------------- hit test
  function hitTest(sx, sy) {
    const [wx, wy] = s2w(sx, sy);
    let best = null, bestA = 1e9;
    for (const c of COMPS) {
      if (c.side !== S.side) continue;
      const pad = c.np ? 0.28 : 0.75;
      if (wx >= c.bb[0] - pad && wx <= c.bb[2] + pad && wy >= c.bb[1] - pad && wy <= c.bb[3] + pad) {
        if (c.area < bestA) { bestA = c.area; best = c; }
      }
    }
    return best;
  }

  // ------------------------------------------------------------- labels
  const FONT_LABEL = '600 11px "SF Mono",ui-monospace,Menlo,Consolas,monospace';

  function labelText(c) { return c.d; }

  function drawOverlays() {
    const zoom = S.z;
    const drawn = [];
    const order = COMPS.filter(c => c.side === S.side || c === S.sel || c === S.hover)
      .slice()
      .sort((a, b) => b.area - a.area);

    // component boxes: only for hover / selection (the real silkscreen
    // outline is already drawn, so boxes would only add clutter)
    applyWorld();
    for (const c of order) {
      const isSel = S.sel === c, isHov = S.hover === c;
      if (!c.np) {
        // formats without pad attribution (e.g. Gerber + centroid): draw a
        // crosshair at the component origin instead of a (meaningless) box
        const r = Math.max(0.35, 7 / zoom);
        ctx.lineWidth = isSel ? 0.09 : 0.05;
        ctx.strokeStyle = isSel ? COL.sel : (isHov ? 'rgba(126,255,176,.8)' : 'rgba(190,230,255,.55)');
        ctx.beginPath();
        ctx.moveTo(c.cx - r, c.cy); ctx.lineTo(c.cx + r, c.cy);
        ctx.moveTo(c.cx, c.cy - r); ctx.lineTo(c.cx, c.cy + r);
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(c.cx, c.cy, r * 0.45, 0, Math.PI * 2);
        ctx.stroke();
      }
      if (isSel || isHov) {
        ctx.beginPath();
        const x0 = c.bb[0] - 0.14, y0 = c.bb[1] - 0.14;
        const x1 = c.bb[2] + 0.14, y1 = c.bb[3] + 0.14;
        ctx.rect(x0, y0, Math.max(x1 - x0, 0.3), Math.max(y1 - y0, 0.24));
        ctx.lineWidth = isSel ? 0.09 : 0.06;
        ctx.strokeStyle = isSel ? COL.sel : 'rgba(126,255,176,.6)';
        ctx.fillStyle = isSel ? COL.selFill : 'rgba(126,255,176,.07)';
        ctx.fill();
        ctx.stroke();
      }
      // pin-1 dot (screen-constant size)
      if (c.pin1 && zoom > 12) {
        const r = 2.4 / zoom;
        ctx.beginPath();
        ctx.arc(c.pin1[0], c.pin1[1], r, 0, Math.PI * 2);
        ctx.fillStyle = isSel ? '#eafff2' : 'rgba(150,240,255,.92)';
        ctx.fill();
      }
    }

    // placed badges (screen space)
    applyScreen();
    for (const c of order) {
      if (!placed.has(c.d)) continue;
      const [bx, by] = w2s(c.bb[2], c.bb[1]);
      ctx.beginPath();
      ctx.arc(bx, by, 6, 0, Math.PI * 2);
      ctx.fillStyle = 'rgba(22,110,66,.95)';
      ctx.fill();
      ctx.strokeStyle = '#9dffc6';
      ctx.lineWidth = 1.4;
      ctx.stroke();
      ctx.strokeStyle = '#eafff2';
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.moveTo(bx - 2.6, by + 0.2);
      ctx.lineTo(bx - 0.6, by + 2.3);
      ctx.lineTo(bx + 2.8, by - 2.2);
      ctx.stroke();
    }

    // labels (screen space)
    applyScreen();
    ctx.font = FONT_LABEL;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    const pad = 3;
    function tryLabel(c, force) {
      const t = labelText(c);
      const tw = ctx.measureText(t).width;
      const [sx, sy] = w2s(c.cx, c.cy);
      const lx = sx, ly = sy - (c.h * S.z) / 2 - 9;
      const box = [lx - tw / 2 - pad, ly - 8, lx + tw / 2 + pad, ly + 8];
      if (box[2] < -20 || box[0] > W + 20 || box[3] < 0 || box[1] > H) return;
      if (!force) {
        for (const r of drawn) {
          if (box[0] < r[2] && box[2] > r[0] && box[1] < r[3] && box[3] > r[1]) return;
        }
      }
      drawn.push(box);
      // leader
      ctx.strokeStyle = force ? 'rgba(75,228,142,.85)' : 'rgba(180,220,200,.28)';
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(lx, ly + 7);
      ctx.lineTo(sx, sy - (c.h * S.z) / 2 - 1);
      ctx.stroke();
      // text
      ctx.lineJoin = 'round';
      ctx.strokeStyle = 'rgba(6,10,14,.92)';
      ctx.lineWidth = 3.2;
      ctx.strokeText(t, lx, ly);
      ctx.fillStyle = force ? '#eafff2' : COL.label;
      ctx.fillText(t, lx, ly);
    }
    if (S.showLabels) {
      const minZ = 6;
      for (const c of order) {
        if (S.z < minZ) break;
        if (c === S.sel || c === S.hover) continue;
        // small parts need more zoom
        if (c.area < 1.0 && S.z < 16) continue;
        if (c.area < 2.5 && S.z < 9) continue;
        tryLabel(c, false);
      }
    }
    if (S.hover && S.hover !== S.sel) tryLabel(S.hover, true);
    if (S.sel) tryLabel(S.sel, true);

    // selection crosshair / flash
    if (S.sel) drawSelection(S.sel);
  }

  function drawSelection(c) {
    applyScreen();
    const [sx, sy] = w2s(c.cx, c.cy);
    const bw = Math.max(c.w * S.z, 22), bh = Math.max(c.h * S.z, 14);
    // brackets
    const ex = bw / 2 + 7, ey = bh / 2 + 7, len = 9;
    ctx.strokeStyle = COL.sel;
    ctx.lineWidth = 2;
    [[-1, -1], [1, -1], [1, 1], [-1, 1]].forEach(([dx, dy]) => {
      ctx.beginPath();
      ctx.moveTo(sx + dx * ex, sy + dy * ey - dy * len);
      ctx.lineTo(sx + dx * ex, sy + dy * ey);
      ctx.lineTo(sx + dx * ex - dx * len, sy + dy * ey);
      ctx.stroke();
    });
    // flash animation
    const age = performance.now() - S.flashStart;
    if (S.flash && age < 900) {
      const t = age / 900;
      const rad = (Math.max(bw, bh) / 2 + 4) * (1 + t * 0.9);
      ctx.beginPath();
      ctx.arc(sx, sy, rad, 0, Math.PI * 2);
      ctx.strokeStyle = 'rgba(75,228,142,' + (0.75 * (1 - t)).toFixed(3) + ')';
      ctx.lineWidth = 2.5 * (1 - t) + 0.5;
      ctx.stroke();
      requestAnimationFrame(draw);
    } else {
      S.flash = 0;
    }
  }

  // ------------------------------------------------------------- main draw
  function draw() {
    if (!W) return;
    applyScreen();
    ctx.clearRect(0, 0, W, H);

    if (!paths.board) { buildPaths(); buildTrackGroups(); }

    drawBoardFill();

    // clip copper / silk to the board
    ctx.save();
    applyWorld();
    ctx.clip(paths.board);

    if (S.showCu) drawPours(S.side);
    if (S.showGrid) drawGrid();
    drawGhost(S.side);
    if (S.showCu) {
      applyWorld();
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      strokeWidthGroups(S.side);
    }
    drawPads(S.side);
    drawVias();
    drawSilk(S.side);
    ctx.restore();

    drawOverlays();
    updateHud();
  }

  // ------------------------------------------------------------- hud
  let sbLen = 10;
  function updateHud() {
    // scale bar
    const cands = [0.2, 0.5, 1, 2, 5, 10, 20, 50, 100];
    let best = 10, bestD = 1e9;
    for (const v of cands) {
      const d = Math.abs(v * S.z - 88);
      if (d < bestD) { bestD = d; best = v; }
    }
    sbLen = best;
    const ln = document.querySelector('#scalebar .ln');
    if (ln) ln.style.width = Math.min(best * S.z, 230).toFixed(1) + 'px';
    const lbl = $('#sblabel');
    if (lbl) lbl.textContent = (best < 1 ? best.toFixed(1) : best) + ' mm';
    renderReadout();
  }

  const tipEl = $('#tip');
  function updateTip(c, sx, sy) {
    if (!c) { tipEl.classList.remove('on'); return; }
    tipEl.innerHTML = '<b>' + esc(c.d) + '</b>' +
      esc(c.disp || '—') +
      '<span class="m"> · ' + (c.side === 'top' ? '顶层' : '底层') +
      ' · ' + ((Math.round(c.rot) % 360 + 360) % 360) + '°</span>';
    tipEl.style.left = sx + 'px';
    tipEl.style.top = sy + 'px';
    tipEl.classList.add('on');
  }

  function renderReadout() {
    const el = $('#readout');
    if (!el) return;
    const w = S.lastWorld;
    if (!w) { el.textContent = '—'; return; }
    el.textContent = '板内 ' + fmt(w[0] - BOARD.x0, 2) + ', ' +
      fmt(w[1] - BOARD.y0, 2) + ' mm   ·   ' + Math.round(S.z) + ' px/mm';
  }

  function updateReadout(wx, wy) {
    S.lastWorld = (wx == null) ? null : [wx, wy];
    renderReadout();
  }

  // ------------------------------------------------------------- selection
  function select(desOrComp, opts) {
    opts = opts || {};
    const c = typeof desOrComp === 'string' ? BY_DES.get(desOrComp) : desOrComp;
    if (!c) return;
    S.sel = c;
    S.flash = 1;
    S.flashStart = performance.now();
    if (c.side !== S.side && opts.noSideSwitch !== true) setSide(c.side);
    if (opts.zoom !== false) {
      // pick a zoom that shows ~20 mm of context around the part
      let target = clamp(Math.min(W, H) / 19, 12, 140);
      if (target > S.z || S.z > target * 2.2) S.z = target;
      S.tx = W / 2 - ax() * c.cx * S.z;
      S.ty = H / 2 + c.cy * S.z;
    }
    renderList();
    renderDetail();
    hideHint();
    draw();
    requestAnimationFrame(draw);
  }

  function deselect() {
    S.sel = null;
    renderList();
    renderDetail();
    draw();
  }

  function setSide(side) {
    if (side === S.side) return;
    const bcx = (BOARD.x0 + BOARD.x1) / 2;
    const oldAx = ax();
    const pxOld = oldAx * bcx * S.z + S.tx;
    S.side = side;
    S.tx = pxOld - ax() * bcx * S.z;
    document.querySelector('.sideseg').dataset.side = side;
    document.querySelectorAll('.sideseg button').forEach(b =>
      b.classList.toggle('on', b.dataset.side === side));
    renderList();
    renderDetail();
    draw();
  }

  // ------------------------------------------------------------- list
  const listEl = $('#list');
  const searchEl = $('#search');

  function filtered() {
    const q = S.q.trim().toLowerCase();
    let arr = COMPS.slice().sort((a, b) =>
      a.d.localeCompare(b.d, 'en', { numeric: true }));
    // while searching, ignore the side filter so any designator can be found
    if (S.filterSide === 'cur' && !q) arr = arr.filter(c => c.side === S.side);
    if (S.onlyUnplaced) arr = arr.filter(c => !placed.has(c.d));
    if (q) {
      const toks = q.split(/\s+/).filter(Boolean);
      arr = arr.filter(c => {
        const hay = [c.d, c.disp, c.comment, c.val, c.fp, String(c.n)].join(' ').toLowerCase();
        return toks.every(t => hay.indexOf(t) >= 0);
      });
      // rank: exact designator > designator prefix > designator contains > other
      const rank = c => {
        const d = c.d.toLowerCase();
        if (d === q) return 0;
        if (d.startsWith(q)) return 1;
        if (d.indexOf(q) >= 0) return 2;
        return 3;
      };
      arr = arr.slice().sort((a, b) => rank(a) - rank(b) || cmpDes(a.d, b.d));
    }
    return arr;
  }

  function renderList() {
    const arr = filtered();
    listEl.innerHTML = '';
    if (!arr.length) {
      const d = document.createElement('div');
      d.className = 'empty';
      d.textContent = S.q ? '没有匹配的元件' : (S.onlyUnplaced ? '全部已贴 ✓' : '没有元件');
      listEl.appendChild(d);
      updateProgress();
      return;
    }
    arr.forEach(c => {
      const row = document.createElement('div');
      row.className = 'row' + (S.sel === c ? ' sel' : '') + (placed.has(c.d) ? ' placed' : '');
      row.dataset.d = c.d;
      row.innerHTML =
        '<input class="chk" type="checkbox"' + (placed.has(c.d) ? ' checked' : '') + ' title="标记已贴">' +
        '<span class="des">' + esc(c.d) + '</span>' +
        '<span class="mid"><span class="val">' + esc(c.disp || '—') + '</span>' +
        '<span class="fp">' + esc(c.fp || '') + '</span></span>' +
        '<span class="sb ' + c.side + '">' + (c.side === 'top' ? '顶' : '底') + '</span>';
      listEl.appendChild(row);
    });
    updateProgress();
  }

  function scrollSelIntoView() {
    const el = listEl.querySelector('.row.sel');
    if (el && el.scrollIntoView) el.scrollIntoView({ block: 'nearest' });
  }

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"]/g, ch =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[ch]));
  }

  function updateProgress() {
    const p = placed.size, n = COMPS.length;
    $('#pbar').style.width = (n ? (p / n * 100) : 0).toFixed(1) + '%';
    $('#ptext').textContent = p + ' / ' + n;
    $('#cnt-all').textContent = n;
    $('#cnt-top').textContent = N_TOP;
    $('#cnt-bot').textContent = N_BOT;
  }

  // ------------------------------------------------------------- detail card
  const detailEl = $('#detail');
  let pvCanvas = null;

  function renderDetail() {
    const c = S.sel;
    if (!c) { detailEl.classList.add('hide'); return; }
    detailEl.classList.remove('hide');
    const xin = c.x - BOARD.x0, yin = c.y - BOARD.y0;
    const rot = ((c.rot % 360) + 360) % 360;
    const rots = { 0: '0°', 90: '90°', 180: '180°', 270: '270°' };
    detailEl.innerHTML =
      '<div class="ch"><span class="des">' + c.d + '</span>' +
      '<span class="sb ' + c.side + '">' + (c.side === 'top' ? '顶层' : '底层') + '</span>' +
      '<button class="x" id="dt-close" title="关闭 (Esc)">×</button></div>' +
      '<div class="pv"><canvas id="pv"></canvas><span class="tag">封装预览</span></div>' +
      '<dl>' +
      '<dt>值</dt><dd>' + esc(c.disp || '—') + '</dd>' +
      '<dt>型号</dt><dd class="small" title="' + esc(c.comment) + '">' + esc(c.comment || '—') + '</dd>' +
      '<dt>封装</dt><dd class="small" title="' + esc(c.fp) + '">' + esc(c.fp || '—') + '</dd>' +
      '<dt>焊盘</dt><dd>' + c.n + ' 个</dd>' +
      '<dt>板内</dt><dd>' + fmt(xin, 2) + ', ' + fmt(yin, 2) + ' mm</dd>' +
      '<dt>原始坐标</dt><dd class="small">' + fmt(c.x, 3) + ', ' + fmt(c.y, 3) + ' mm</dd>' +
      '<dt>旋转</dt><dd>' + (rots[Math.round(rot)] || (rot.toFixed(1) + '°')) + '</dd>' +
      '</dl>' +
      '<div class="acts">' +
      '<button id="dt-prev" title="上一个 (↑)">◀</button>' +
      '<button id="dt-check" class="' + (placed.has(c.d) ? '' : 'primary') + '">' +
      (placed.has(c.d) ? '✓ 已贴' : '标记已贴') + '</button>' +
      '<button id="dt-next" title="下一个 (↓)">▶</button>' +
      '</div>';
    $('#dt-close').onclick = deselect;
    $('#dt-prev').onclick = () => step(-1);
    $('#dt-next').onclick = () => step(1);
    $('#dt-check').onclick = () => togglePlaced(c);
    pvCanvas = $('#pv');
    drawPreview(c);
  }

  function stepNextUnplaced() {
    const arr = filtered().filter(c => !placed.has(c.d));
    if (!arr.length) return;
    const cur = S.sel;
    let next = arr.find(c => cmpDes(c.d, cur ? cur.d : '') > 0) || arr[0];
    select(next, { zoom: true });
    scrollSelIntoView();
  }

  function cmpDes(a, b) {
    return a.localeCompare(b, 'en', { numeric: true });
  }

  function step(dir) {
    const arr = filtered();
    if (!arr.length) return;
    let i = S.sel ? arr.indexOf(S.sel) : -1;
    if (i < 0) i = 0;
    else i = (i + dir + arr.length) % arr.length;
    select(arr[i]);
    scrollSelIntoView();
  }

  function drawPreview(c) {
    const cnv = pvCanvas;
    if (!cnv) return;
    const rect = cnv.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 3);
    const w = Math.max(60, Math.round(rect.width)), h = Math.max(40, Math.round(rect.height));
    cnv.width = Math.round(w * dpr);
    cnv.height = Math.round(h * dpr);
    const g = cnv.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, h);

    // bbox of pads (+ silk lines of this component)
    const bb0 = c.bb || [c.x, c.y, c.x, c.y];
    let x0 = bb0[0], y0 = bb0[1], x1 = bb0[2], y1 = bb0[3];
    (DATA.silk || []).forEach(t => {
      if (t[6] !== COMPS.indexOf(c)) return;
      x0 = Math.min(x0, t[0], t[2]); x1 = Math.max(x1, t[0], t[2]);
      y0 = Math.min(y0, t[1], t[3]); y1 = Math.max(y1, t[1], t[3]);
    });
    const pad = 12;
    const z = Math.min((w - 2 * pad) / Math.max(x1 - x0, 0.4),
      (h - 2 * pad) / Math.max(y1 - y0, 0.4));
    const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
    const tx = w / 2 + cx * z, ty = h / 2 - cy * z;
    const T = (x, y) => [tx - x * z, ty + y * z];

    g.lineWidth = 1;
    // grid cross
    g.strokeStyle = 'rgba(255,255,255,.06)';
    g.beginPath(); g.moveTo(0, h / 2); g.lineTo(w, h / 2); g.stroke();
    g.beginPath(); g.moveTo(w / 2, 0); g.lineTo(w / 2, h); g.stroke();

    // silk
    g.strokeStyle = 'rgba(226,232,240,.55)';
    g.lineWidth = 1.2;
    (DATA.silk || []).forEach(t => {
      if (t[6] !== COMPS.indexOf(c)) return;
      const a = T(t[0], t[1]), b = T(t[2], t[3]);
      g.beginPath(); g.moveTo(a[0], a[1]); g.lineTo(b[0], b[1]); g.stroke();
    });
    // pads
    c.pads.forEach(p => {
      g.save();
      const [px, py] = T(p.x, p.y);
      g.translate(px, py);
      g.rotate(-p.r * Math.PI / 180);
      const pw = Math.max(p.w * z, 1.2), ph = Math.max(p.h * z, 1.2);
      g.fillStyle = (p.l === 'MULTILAYER') ? COL.padMulti
        : (p.l === 'TOP' ? COL.padTop : COL.padBot);
      g.beginPath();
      if (p.s === 1) g.ellipse(0, 0, pw / 2, ph / 2, 0, 0, Math.PI * 2);
      else g.rect(-pw / 2, -ph / 2, pw, ph);
      g.fill();
      g.restore();
    });
    // pin1 marker
    if (c.pin1) {
      const [px, py] = T(c.pin1[0], c.pin1[1]);
      g.fillStyle = '#4be48e';
      g.beginPath(); g.arc(px, py, 2.4, 0, Math.PI * 2); g.fill();
      g.font = '9px ui-monospace,monospace';
      g.fillStyle = 'rgba(126,255,176,.9)';
      g.fillText('1', px + 4, py - 3);
    }
    // rotation arrow (component +X direction)
    const [ox, oy] = T(c.x, c.y);
    const a = c.rot * Math.PI / 180;
    const dx = Math.cos(a), dy = Math.sin(a);
    const len = Math.min(w, h) * 0.30;
    g.strokeStyle = 'rgba(122,240,255,.85)';
    g.lineWidth = 1.6;
    g.beginPath(); g.moveTo(ox, oy); g.lineTo(ox + dx * len, oy - dy * len); g.stroke();
    g.beginPath();
    g.arc(ox + dx * len, oy - dy * len, 2.6, 0, Math.PI * 2);
    g.fillStyle = 'rgba(122,240,255,.85)'; g.fill();
    // side tag
    g.font = '9px ui-monospace,monospace';
    g.fillStyle = 'rgba(147,164,184,.75)';
    g.fillText((c.side === 'top' ? 'TOP' : 'BOT') + ' · ' + (c.fp || ''), 6, h - 6);
  }

  // ------------------------------------------------------------- placed
  function togglePlaced(c) {
    if (placed.has(c.d)) placed.delete(c.d); else placed.add(c.d);
    savePlaced();
    renderList();
    renderDetail();
    draw();
    if (placed.has(c.d)) {
      toast(c.d + ' 已标记为已贴');
      setTimeout(() => { if (S.sel === c) stepNextUnplaced(); }, 140);
    }
  }

  // ------------------------------------------------------------- events
  // canvas pointer
  let drag = null;
  cv.addEventListener('pointerdown', e => {
    cv.setPointerCapture(e.pointerId);
    drag = { x: e.clientX, y: e.clientY, tx: S.tx, ty: S.ty, moved: false };
    cv.classList.add('drag');
  });
  cv.addEventListener('pointermove', e => {
    const r = cv.getBoundingClientRect();
    const sx = e.clientX - r.left, sy = e.clientY - r.top;
    if (drag) {
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
      if (drag.moved) {
        S.tx = drag.tx + dx; S.ty = drag.ty + dy;
        tipEl.classList.remove('on');
        draw();
        return;
      }
    }
    const h = hitTest(sx, sy);
    if (h !== S.hover) {
      S.hover = h;
      cv.classList.toggle('pick', !!h);
      draw();
    }
    updateTip(h, sx, sy);
    const [wx, wy] = s2w(sx, sy);
    updateReadout(wx, wy);
  });
  cv.addEventListener('pointerup', e => {
    const r = cv.getBoundingClientRect();
    const sx = e.clientX - r.left, sy = e.clientY - r.top;
    cv.classList.remove('drag');
    const moved = drag && drag.moved;
    drag = null;
    if (!moved) {
      const h = hitTest(sx, sy);
      if (h) select(h, { zoom: true, pad: 60 });
      else deselect();
    }
  });
  cv.addEventListener('pointerleave', () => {
    drag = null; S.hover = null; cv.classList.remove('drag', 'pick');
    updateTip(null);
    updateReadout(null);
    draw();
  });
  cv.addEventListener('wheel', e => {
    e.preventDefault();
    const r = cv.getBoundingClientRect();
    const f = Math.exp(-e.deltaY * 0.0016);
    zoomAt(f, e.clientX - r.left, e.clientY - r.top);
    hideHint();
  }, { passive: false });
  cv.addEventListener('dblclick', e => {
    const r = cv.getBoundingClientRect();
    zoomAt(1.7, e.clientX - r.left, e.clientY - r.top);
  });

  function updateCursorCoords(wx, wy) {
    // no-op placeholder for potential crosshair
  }

  // side buttons
  document.querySelectorAll('.sideseg button').forEach(b => {
    b.onclick = () => setSide(b.dataset.side);
  });

  // toolbar
  $('#tg-silk').onchange = e => { S.showSilk = e.target.checked; draw(); };
  $('#tg-cu').onchange = e => { S.showCu = e.target.checked; draw(); };
  $('#tg-ghost').onchange = e => { S.showGhost = e.target.checked; draw(); };
  $('#tg-labels').onchange = e => { S.showLabels = e.target.checked; draw(); };
  $('#tg-grid').onchange = e => { S.showGrid = e.target.checked; draw(); };
  $('#btn-fit').onclick = () => fit();
  $('#btn-csv').onclick = exportCsv;

  // list interactions
  listEl.addEventListener('click', e => {
    const row = e.target.closest('.row');
    if (!row) return;
    const c = BY_DES.get(row.dataset.d);
    if (!c) return;
    if (e.target.classList.contains('chk')) {
      togglePlaced(c);
      return;
    }
    select(c);
    scrollSelIntoView();
  });
  listEl.addEventListener('mouseover', e => {
    const row = e.target.closest('.row');
    if (!row) return;
    const c = BY_DES.get(row.dataset.d);
    if (c && S.hover !== c) { S.hover = c; draw(); }
  });

  // search
  searchEl.addEventListener('input', () => {
    S.q = searchEl.value;
    document.querySelector('.searchbox').classList.toggle('hasq', !!S.q);
    renderList();
    const arr = filtered();
    if (arr.length === 1 && S.q) select(arr[0], { noSideSwitch: false });
  });
  $('#search-clear').onclick = () => {
    searchEl.value = ''; S.q = '';
    document.querySelector('.searchbox').classList.remove('hasq');
    renderList(); searchEl.focus();
  };
  document.querySelectorAll('#sidefilter button').forEach(b => {
    b.onclick = () => {
      S.filterSide = b.dataset.f;
      document.querySelectorAll('#sidefilter button').forEach(x =>
        x.classList.toggle('on', x === b));
      renderList();
    };
  });
  $('#only-unplaced').onchange = e => { S.onlyUnplaced = e.target.checked; renderList(); };
  $('#btn-reset').onclick = () => {
    if (!placed.size) return;
    if (confirm('确定要清除所有「已贴」标记吗？')) {
      placed.clear(); savePlaced(); renderList(); renderDetail(); draw();
      toast('已清除全部标记');
    }
  };

  // help dialog
  const helpEl = $('#help');
  $('#helpbtn').onclick = () => helpEl.classList.add('on');
  $('#helpclose').onclick = () => helpEl.classList.remove('on');
  helpEl.addEventListener('click', e => { if (e.target === helpEl) helpEl.classList.remove('on'); });

  // keyboard
  document.addEventListener('keydown', e => {
    const tag = (e.target.tagName || '').toLowerCase();
    const typing = tag === 'input' || tag === 'textarea';
    if (helpEl.classList.contains('on')) {
      if (e.key === 'Escape' || e.key === 'Enter') {
        e.preventDefault();
        helpEl.classList.remove('on');
      }
      return;                      // modal swallows all other keys
    }
    if (e.key === 'Escape') {
      if (typing) { searchEl.blur(); }
      if (S.sel) deselect();
      return;
    }
    if ((e.key === '/' || (e.key === 'f' && (e.ctrlKey || e.metaKey))) && !typing) {
      e.preventDefault(); searchEl.focus(); searchEl.select(); return;
    }
    if (typing) {
      if (e.key === 'Enter') {
        const arr = filtered();
        if (arr.length) { select(arr[0]); scrollSelIntoView(); }
        searchEl.blur();
      }
      return;
    }
    switch (e.key) {
      case 'ArrowDown': e.preventDefault(); step(1); break;
      case 'ArrowUp': e.preventDefault(); step(-1); break;
      case 't': case 'T': setSide('top'); break;
      case 'b': case 'B': setSide('bottom'); break;
      case 'f': case 'F': fit(); break;
      case '+': case '=': zoomAt(1.25); break;
      case '-': case '_': zoomAt(0.8); break;
      case ' ':
        if (S.sel) { e.preventDefault(); togglePlaced(S.sel); }
        break;
    }
  });

  // hint auto hide
  let hintTimer = null;
  function hideHint() {
    const h = $('#hint');
    if (!h || h.classList.contains('gone')) return;
    clearTimeout(hintTimer);
    hintTimer = setTimeout(() => h.classList.add('gone'), 600);
  }

  // ------------------------------------------------------------- export
  function exportCsv() {
    const rows = [['位号', '层', '值', '型号', '封装', '板内X(mm)', '板内Y(mm)',
      '原始X(mm)', '原始Y(mm)', '旋转', '焊盘数', '已贴']];
    COMPS.slice().sort((a, b) => a.d.localeCompare(b.d, 'en', { numeric: true }))
      .forEach(c => {
        rows.push([c.d, c.side === 'top' ? '顶层' : '底层', c.disp || '', c.comment || '',
          c.fp || '', fmt(c.x - BOARD.x0, 3), fmt(c.y - BOARD.y0, 3),
          fmt(c.x, 3), fmt(c.y, 3), ((c.rot % 360 + 360) % 360).toFixed(0),
          String(c.n), placed.has(c.d) ? '是' : '']);
      });
    const csv = '\ufeff' + rows.map(r =>
      r.map(v => /[",\n]/.test(v) ? '"' + String(v).replace(/"/g, '""') + '"' : v).join(',')
    ).join('\r\n');
    try {
      const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = (DATA.title || 'pcb') + '_贴片清单.csv';
      document.body.appendChild(a); a.click();
      setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 500);
      toast('已导出贴片清单 CSV');
    } catch (err) {
      toast('导出失败：' + err.message);
    }
  }

  // ------------------------------------------------------------- boot
  function boot() {
    loadPlaced();
    document.querySelector('.sideseg').dataset.side = S.side;
    // header dims
    const dims = document.querySelector('.dims');
    if (dims) dims.textContent = BOARD.w.toFixed(1) + ' × ' + BOARD.h.toFixed(1) + ' mm';
    document.title = (DATA.title || 'PCB') + ' · SMT 贴片定位图';
    const h1 = document.querySelector('.brand h1');
    if (h1) h1.textContent = DATA.title || 'PCB';
    const hs = document.querySelector('#help-sub');
    if (hs) hs.textContent = (DATA.title || 'PCB') + ' · ' + hs.textContent;
    buildPaths();
    buildTrackGroups();
    renderList();
    resize();
    fit();
    // expose for tests
    window.__SMT = {
      get state() { return S; },
      comps: COMPS,
      select: d => select(d, { zoom: true }),
      setSide,
      fit,
      togglePlaced: d => togglePlaced(BY_DES.get(d)),
      zoomAt
    };
    window.__SMT_READY = true;
    hideHint();
  }

  window.addEventListener('resize', () => { resize(); });
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
