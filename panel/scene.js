/* The world behind the panel: pixel-art Chinese countryside (farms, villages, rice terraces, a lake) under
   misty mountains, with Japanese castles floating overhead. One full day every 5 minutes, synced to the clock,
   so every page and reload shows the same hour. Farmers, samurai and villagers walk the paths; at night they
   launch sky lanterns and set lanterns on the lake. Runs once per browser tab and survives Streamlit reruns.
   Debug: ?tod=0.9 starts the day at that phase (0 = midnight, 0.5 = noon); window.__xcpWorld.phase(p). */
(function () {
  if (window.__xcpWorld) { window.__xcpWorld.poke(); return; }

  const DAY = 300;                    // seconds in one full day
  const FPS = 24;
  const reduced = window.matchMedia && matchMedia('(prefers-reduced-motion: reduce)').matches;
  const qs = new URLSearchParams(location.search);
  let phaseOffset = qs.has('tod') ? parseFloat(qs.get('tod')) - ((Date.now() / 1000) % DAY) / DAY : 0;

  // ------------------------------------------------------------------ helpers
  const hex = h => [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)];
  const css = c => `rgb(${c[0] | 0},${c[1] | 0},${c[2] | 0})`;
  const mix = (a, b, t) => [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
  const mul = (a, b) => [a[0] * b[0] / 255, a[1] * b[1] / 255, a[2] * b[2] / 255];
  const clamp = (v, a, b) => (v < a ? a : v > b ? b : v);
  const smooth = (a, b, x) => { const t = clamp((x - a) / (b - a), 0, 1); return t * t * (3 - 2 * t); };
  function rng(seed) {
    return function () {
      seed |= 0; seed = seed + 0x6D2B79F5 | 0;
      let t = Math.imul(seed ^ seed >>> 15, 1 | seed);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }
  const BAYER = [[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]].map(r => r.map(v => (v + 0.5) / 16));
  const dith = (x, y, t) => t > BAYER[y & 3][x & 3];
  function mk(w, h) { const c = document.createElement('canvas'); c.width = Math.max(1, w); c.height = Math.max(1, h); return c; }
  function px(g, x, y, w, h, col) { g.fillStyle = typeof col === 'string' ? col : css(col); g.fillRect(x | 0, y | 0, w | 0, h | 0); }

  // palette pulled from the two reference pieces: tan dither, rust armour, ink, stone, night indigo, torchlight
  const C = {
    ink: '#1a1410', rust: '#b5432a', rustD: '#7a2a1c', tan: '#e8b868', parch: '#f6dca6', gold: '#e6b422',
    stone: '#8c8792', stoneD: '#4a4652', stoneL: '#b3adb8', plaster: '#efe6d2', roof: '#3d4150', roofL: '#5c6273',
    pink: '#f4b6c2', pinkD: '#e07a95', pinkL: '#fde3ea', leaf: '#6f8f3a', leafD: '#4f6b2b', leafL: '#9cb65a',
    wood: '#6b4a3a', woodD: '#3b2418', straw: '#d9b76a', strawD: '#a8843f', path: '#c9a26a', pathD: '#a7834f',
    water: '#8fb2b0', skin: '#e2b48c', skinD: '#b98561', flame: '#f7b33b', flameL: '#ffe08a', moon: '#f3e6b0',
  };

  // sky keyframes: [phase, top, horizon, ambient (multiply), night 0..1]
  const KEYS = [
    [0.00, '#0d0a1f', '#2b2148', '#5a5b98', 1.00],
    [0.19, '#15112c', '#3b2c58', '#615f98', 1.00],
    [0.235, '#3a2d62', '#d9825e', '#c49092', 0.50],
    [0.285, '#d59656', '#f3cc90', '#f5dfcb', 0.08],
    [0.35, '#e6ad58', '#f7dfae', '#ffffff', 0.00],
    [0.64, '#e3a650', '#f5d8a2', '#fff5e8', 0.00],
    [0.715, '#c9683f', '#f1a360', '#f2b48a', 0.10],
    [0.765, '#6a3a5c', '#c86f5e', '#b48898', 0.50],
    [0.82, '#231a40', '#503a66', '#737196', 0.88],
    [0.88, '#120e27', '#2e2350', '#5a5b98', 1.00],
    [1.00, '#0d0a1f', '#2b2148', '#5a5b98', 1.00],
  ].map(k => [k[0], hex(k[1]), hex(k[2]), hex(k[3]), k[4]]);

  function sky(p) {
    let i = 0;
    while (i < KEYS.length - 2 && KEYS[i + 1][0] <= p) i++;
    const a = KEYS[i], b = KEYS[i + 1], t = smooth(0, 1, (p - a[0]) / (b[0] - a[0]));
    return { top: mix(a[1], b[1], t), hor: mix(a[2], b[2], t), amb: mix(a[3], b[3], t), night: a[4] + (b[4] - a[4]) * t };
  }

  // ------------------------------------------------------------------ tintable assets (day colours → current light)
  let tintKey = 0, AMB = [255, 255, 255], HOR = [245, 216, 162];
  class Tinted {
    constructor(base, haze) { this.base = base; this.haze = haze || 0; this.out = mk(base.width, base.height); this.key = -1; }
    get() {
      if (this.key === tintKey) return this.out;
      const g = this.out.getContext('2d'), w = this.out.width, h = this.out.height;
      g.globalCompositeOperation = 'source-over'; g.clearRect(0, 0, w, h); g.drawImage(this.base, 0, 0);
      g.globalCompositeOperation = 'multiply'; g.fillStyle = css(AMB); g.fillRect(0, 0, w, h);
      g.globalCompositeOperation = 'destination-in'; g.drawImage(this.base, 0, 0);
      if (this.haze > 0) {
        g.globalCompositeOperation = 'source-atop'; g.globalAlpha = this.haze; g.fillStyle = css(HOR);
        g.fillRect(0, 0, w, h); g.globalAlpha = 1;
      }
      g.globalCompositeOperation = 'source-over';
      this.key = tintKey;
      return this.out;
    }
  }

  // ------------------------------------------------------------------ canvas
  const cvs = document.createElement('canvas');
  cvs.id = 'xcp-world';
  cvs.setAttribute('aria-hidden', 'true');
  document.body.prepend(cvs);
  const ctx = cvs.getContext('2d');
  let S = 3, W = 400, H = 240, world = null;

  // ------------------------------------------------------------------ building the world
  function ridge(rand, n, x0, x1, hMin, hMax, wMin, wMax, pow) {
    const bumps = [];
    for (let i = 0; i < n; i++) {
      bumps.push({ x: x0 + (x1 - x0) * (i + 0.2 + rand() * 0.6) / n, h: hMin + rand() * (hMax - hMin),
        w: wMin + rand() * (wMax - wMin) });
    }
    const r = new Float32Array(W + 2);
    for (let x = 0; x < W + 2; x++) {
      let best = 0;
      for (const b of bumps) {
        const d = Math.abs(x - b.x) / b.w;
        if (d < 1) best = Math.max(best, b.h * Math.pow(1 - d * d, pow));
      }
      r[x] = best;
    }
    return { r, bumps };
  }

  function buffer() {                            // direct pixel writes for the big layers
    const c = mk(W, H), g = c.getContext('2d'), img = g.createImageData(W, H), d = img.data;
    return { c, g, set(x, y, col) { const i = (y * W + x) * 4; d[i] = col[0]; d[i + 1] = col[1]; d[i + 2] = col[2]; d[i + 3] = 255; },
      done() { g.putImageData(img, 0, 0); return c; } };
  }

  function mountains(base, rgd, col, fog, fogStart, rim) {
    const b = buffer();
    const A = hex(col), F = hex(fog), L = mix(A, [255, 250, 235], 0.28), D = mix(A, [20, 15, 30], 0.18);
    const lv = [0, 1, 2, 3, 4].map(k => [A, D, L].map(cc => mix(cc, F, k / 4)));   // fogged shades, precomputed
    for (let x = 0; x < W; x++) {
      const top = Math.round(base - rgd.r[x]);
      for (let y = Math.max(0, top); y < H; y++) {
        const t = (y - top) / Math.max(1, base - top);
        let ci = 0;
        if (rim && y === top && rgd.r[x] > rgd.r[x + 1]) ci = 2;                  // lit left faces
        else if (rgd.r[x] < rgd.r[x + 1] - 0.4 && dith(x, y, 0.55)) ci = 1;       // shaded right faces
        const m = smooth(fogStart, 1.05, t);
        const k = m > 0 ? Math.min(4, Math.round(m * 4 + (dith(x, y, 0.5) ? 0.5 : 0))) : 0;
        b.set(x, y, lv[k][ci]);
      }
    }
    return b.done();
  }

  function roofRow(g, x, y, w, col, tip) {      // a roof course with upturned eaves
    px(g, x, y, w, 1, col);
    if (tip) { px(g, x - 1, y - 1, 1, 1, col); px(g, x + w, y - 1, 1, 1, col); }
  }

  function castle(scale, variant, rand) {       // an imperial keep on a floating rock; returns sprite + lights
    const w = Math.round(64 * scale), h = Math.round(92 * scale);
    const c = mk(w, h), g = c.getContext('2d'), lights = [], torches = [];
    const cx = Math.round(w / 2), s = v => Math.max(1, Math.round(v * scale));
    const islandTop = Math.round(h * 0.52), islandBot = h - 1, iw = s(27);
    // floating island: grass cap, layered rock, dangling roots
    for (let y = islandTop; y <= islandBot; y++) {
      const t = (y - islandTop) / (islandBot - islandTop);
      const half = Math.max(1, Math.round(iw * Math.pow(1 - t, 0.75) * (0.92 + rand() * 0.16)));
      for (let x = cx - half; x <= cx + half; x++) {
        let col = t < 0.06 ? C.leaf : t < 0.1 ? C.leafD : (t > 0.55 && dith(x, y, 0.5) ? C.woodD : C.wood);
        if (t > 0.1 && (x === cx - half || x === cx + half)) col = C.woodD;
        if (t > 0.2 && t < 0.5 && dith(x, y, 0.85)) col = '#8a6a52';
        px(g, x, y, 1, 1, col);
      }
    }
    for (let i = 0; i < 6; i++) {                // roots / vines
      const x = cx - iw + 4 + Math.round(rand() * iw * 2 - 8), len = s(4 + rand() * 10);
      const y0 = islandTop + s(3 + rand() * 8);
      for (let k = 0; k < len; k++) px(g, x + (k % 5 === 4 ? 1 : 0), y0 + k, 1, 1, k % 3 ? C.leafD : C.woodD);
    }
    // cherry tree on the island edge
    const tx = cx + iw - s(7), ty = islandTop - s(1);
    px(g, tx, ty - s(6), s(1), s(6), C.woodD);
    for (let k = 0; k < 26; k++) {
      const a = rand() * 6.28, r = rand() * s(5);
      px(g, tx + Math.cos(a) * r * 1.3, ty - s(8) + Math.sin(a) * r * 0.8, 1, 1, [C.pink, C.pinkD, C.pinkL][k % 3]);
    }
    // stone base (ishigaki)
    const baseW = s(34), baseH = s(7), by = islandTop - baseH;
    for (let y = 0; y < baseH; y++) {
      const ww = baseW - Math.round((baseH - y) * 0.6);
      for (let x = 0; x < ww; x++) px(g, cx - (ww >> 1) + x, by + y, 1, 1, dith(x, y, 0.3) ? C.stoneL : (dith(x + 1, y, 0.7) ? C.stone : C.stoneD));
    }
    // tiers: white walls, dark roofs with upturned eaves, windows, gables, a gold ridge
    const tiers = variant === 'tower' ? 4 : variant === 'small' ? 2 : 4;
    let y = by, tw = s(28);
    for (let i = 0; i < tiers; i++) {
      const wallH = s(i === 0 ? 7 : 5), roofW = tw + s(6);
      y -= wallH;
      const wallCol = variant === 'tower' ? C.stone : C.plaster;
      px(g, cx - (tw >> 1), y, tw, wallH, wallCol);
      px(g, cx - (tw >> 1), y + wallH - 1, tw, 1, variant === 'tower' ? C.stoneD : '#cfc3a8');
      const nWin = Math.max(1, Math.floor(tw / s(6)));
      for (let k = 0; k < nWin; k++) {
        const wx = cx - (tw >> 1) + Math.round((k + 0.5) * tw / nWin) - (s(1) >> 1), wy = y + Math.max(1, (wallH >> 1) - 1);
        px(g, wx, wy, s(1), s(2) > 1 ? 2 : 1, C.ink);
        lights.push({ x: wx, y: wy, th: 0.25 + rand() * 0.5 });
      }
      const rh = s(3);
      y -= rh;
      for (let k = 0; k < rh; k++) roofRow(g, cx - (roofW >> 1) + k, y + k, roofW - 2 * k, k === 0 ? C.roofL : C.roof, k === rh - 1);
      if (i % 2 === 0 && tw > s(12)) {           // chidori gable
        const gw = s(6);
        for (let k = 0; k < gw >> 1; k++) px(g, cx - k, y - (gw >> 1) + k + 1, 2 * k + 1, 1, k === 0 ? C.gold : C.roof);
        px(g, cx, y - (gw >> 1) + 2, 1, 1, C.plaster);
      }
      tw = Math.max(s(6), tw - s(variant === 'tower' ? 5 : 6));
    }
    px(g, cx - (tw >> 1), y - s(2), tw + 1, s(2), C.roof);           // top ridge
    px(g, cx - (tw >> 1) - 1, y - s(3), 1, s(2), C.gold);             // shachihoko
    px(g, cx + (tw >> 1) + 1, y - s(3), 1, s(2), C.gold);
    const top = y - s(3);
    if (variant === 'tower') {                   // stone watchtower turrets with torches (the night tower)
      for (const side of [-1, 1]) {
        const tx2 = cx + side * s(15), tw2 = s(5);
        px(g, tx2 - (tw2 >> 1), by - s(12), tw2, s(12), C.stoneD);
        px(g, tx2 - (tw2 >> 1) - 1, by - s(13), tw2 + 2, s(1), C.stone);
        torches.push({ x: tx2, y: by - s(15) });
      }
      torches.push({ x: cx, y: top - s(2) });
    } else {
      torches.push({ x: cx - (baseW >> 1) + 1, y: by - s(2) });
      torches.push({ x: cx + (baseW >> 1) - 2, y: by - s(2) });
    }
    // a thread of waterfall off the rock
    const fall = { x: cx - iw + s(6), y: islandTop + s(2), len: Math.round(h * 0.42) };
    return { sprite: new Tinted(c, 0), w, h, lights, torches, fall, islandTop };
  }

  function house(g, x, yb, w, rand, thatch) {
    const wallH = 4 + Math.round(rand() * 3), roofH = 3 + (w > 12 ? 1 : 0);
    const wall = C.plaster, frame = '#8a7a66';
    px(g, x, yb - wallH, w, wallH, wall);
    px(g, x, yb - wallH, 1, wallH, frame); px(g, x + w - 1, yb - wallH, 1, wallH, frame);
    px(g, x + (w >> 1) - 1, yb - 3, 2, 3, C.woodD);                          // door
    const wins = [];
    for (let k = 3; k < w - 3; k += 4) if (Math.abs(k - (w >> 1)) > 2) { px(g, x + k, yb - wallH + 2, 1, 1, C.ink); wins.push({ x: x + k, y: yb - wallH + 2 }); }
    const ry = yb - wallH;
    for (let k = 0; k < roofH; k++) {
      const col = thatch ? (k === 0 ? C.straw : dith(k, x, 0.5) ? C.strawD : C.straw) : (k === 0 ? C.roofL : C.roof);
      if (thatch) px(g, x - 2 + k, ry - roofH + k, w + 4 - 2 * k, 1, col);
      else roofRow(g, x - 2 + k, ry - roofH + k + 1, w + 4 - 2 * k, col, k === roofH - 1);
    }
    if (!thatch) { px(g, x - 3, ry - 1, 1, 1, C.roof); px(g, x + w + 2, ry - 1, 1, 1, C.roof); }
    return wins;
  }

  function tree(g, x, yb, r, kind, rand) {
    px(g, x, yb - r - 2, 1, r + 2, C.woodD);
    const cols = kind === 'cherry' ? [C.pink, C.pinkD, C.pinkL, C.pink] : kind === 'pine' ? ['#3f5a2c', '#2f4721', '#4f6b2b'] : [C.leaf, C.leafD, C.leafL];
    for (let k = 0; k < r * r * 3; k++) {
      const a = rand() * 6.283, d = Math.sqrt(rand()) * r;
      px(g, x + Math.cos(a) * d * 1.25, yb - r - 2 + Math.sin(a) * d * 0.85, 1, 1, cols[(k + (Math.sin(a) > 0.3 ? 1 : 0)) % cols.length]);
    }
  }

  function pagoda(g, x, yb, tiers) {
    let w = 11, y = yb;
    for (let i = 0; i < tiers; i++) {
      px(g, x - (w >> 1) + 2, y - 3, w - 4, 3, i === 0 ? C.rustD : C.rust);
      px(g, x - 1, y - 2, 1, 1, C.ink);
      roofRow(g, x - (w >> 1), y - 4, w, C.roof, true);
      y -= 4; w = Math.max(5, w - 2);
    }
    px(g, x, y - 4, 1, 4, C.gold);
    return { x, y: yb - 2 };
  }

  function torii(g, x, yb, h) {
    px(g, x, yb - h, 1, h, C.rust); px(g, x + 8, yb - h, 1, h, C.rust);
    px(g, x - 2, yb - h - 1, 13, 1, C.ink); px(g, x - 1, yb - h, 11, 1, C.rust);
    px(g, x - 3, yb - h - 2, 1, 1, C.ink); px(g, x + 11, yb - h - 2, 1, 1, C.ink);
    px(g, x, yb - h + 3, 9, 1, C.rust);
  }

  // ------------------------------------------------------------------ people
  const PEOPLE = ['farmer', 'yoke', 'samurai', 'woman', 'monk', 'child'];
  const WEIGHTS = [0.24, 0.18, 0.2, 0.2, 0.09, 0.09];
  const pick = r => { let a = r, i = 0; for (; i < WEIGHTS.length - 1; i++) { a -= WEIGHTS[i]; if (a < 0) break; } return PEOPLE[i]; };

  function drawNear(type, frame, night) {        // 15x26 sprite, feet at the bottom centre
    const c = mk(15, 26), g = c.getContext('2d'), X = 7, F = 25;
    const legs = (col) => {
      if (frame === 1) { px(g, X - 2, F - 4, 1, 4, col); px(g, X + 2, F - 4, 1, 4, col); }
      else { px(g, X - 1, F - 4, 1, 4, col); px(g, X + 1, F - 4, 1, 4, col); }
    };
    const up = frame === 2;                       // arms raised (launching a lantern)
    const kid = type === 'child', top = kid ? 7 : 0;
    const body = { farmer: '#5d6f8a', yoke: '#8a6a4a', samurai: C.rust, woman: '#c8506a', monk: '#d98c3a', child: C.leaf }[type];
    const bodyD = mix(hex(body), [20, 15, 20], 0.3);
    if (type === 'woman') {
      for (let y = F - 12; y <= F; y++) { const ww = 5 + ((y - (F - 12)) > 8 ? 2 : 0); px(g, X - (ww >> 1) + (frame === 1 && y > F - 2 ? 1 : 0), y, ww, 1, y > F - 1 ? bodyD : body); }
      px(g, X - 2, F - 9, 5, 2, C.gold);                                      // obi
    } else if (type === 'samurai') {
      legs('#2a2230');
      px(g, X - 3, F - 8, 7, 4, '#2a2230');                                   // hakama
      px(g, X - 2, F - 14, 5, 6, C.rust);
      for (let k = 0; k < 3; k++) px(g, X - 2, F - 13 + 2 * k, 5, 1, k === 1 ? C.gold : C.rustD);
      px(g, X - 3, F - 14, 1, 3, C.rustD); px(g, X + 3, F - 14, 1, 3, C.rustD); // sode
      px(g, X - 6, F - 7, 4, 1, '#3a3036'); px(g, X - 7, F - 6, 1, 1, '#3a3036'); // scabbard
      px(g, X + 3, F - 9, 2, 1, C.ink);                                       // hilt
      px(g, X, 1, 1, F - 16, C.woodD);                                        // sashimono pole
      px(g, X + 1, 2, 4, 6, C.rust); px(g, X + 2, 4, 2, 2, C.plaster);        // banner + mon
    } else if (type === 'monk') {
      legs('#7a4a1c');
      px(g, X - 2, F - 13, 5, 10, body); px(g, X - 2, F - 11, 5, 1, bodyD);
      px(g, X + 4, F - 20, 1, 20, C.woodD); px(g, X + 3, F - 21, 3, 2, C.gold); // shakujo
    } else {
      legs(kid ? '#3b4152' : '#3b4152');
      px(g, X - 2, F - 12 + (kid ? 4 : 0), 5, kid ? 5 : 8, body);
      px(g, X - 2, F - 7 + (kid ? 2 : 0), 5, 1, bodyD);
    }
    // arms
    const armY = F - 12 + (kid ? 4 : 0);
    if (up) { px(g, X - 3, armY - 5, 1, 5, body); px(g, X + 3, armY - 5, 1, 5, body); }
    else if (type !== 'samurai') { px(g, X - 3, armY + (frame === 1 ? 1 : 0), 1, 4, bodyD); px(g, X + 3, armY + (frame === 1 ? 0 : 1), 1, 4, bodyD); }
    // head
    const hy = F - 16 + (kid ? 4 : 0) + (type === 'samurai' ? 0 : 0);
    px(g, X - 1, hy, 3, 4, C.skin); px(g, X + 1, hy + 1, 1, 1, C.ink);
    if (type === 'monk') px(g, X - 1, hy, 3, 1, C.skinD);
    else if (type === 'woman') { px(g, X - 2, hy - 2, 4, 3, C.ink); px(g, X - 1, hy - 3, 2, 1, C.ink); px(g, X + 2, hy - 3, 2, 1, C.gold); }
    else if (type === 'samurai') {                                            // kabuto + gold horns
      px(g, X - 2, hy - 2, 5, 3, '#2a2230'); px(g, X - 3, hy, 7, 1, '#2a2230');
      px(g, X - 3, hy - 4, 1, 2, C.gold); px(g, X + 3, hy - 4, 1, 2, C.gold); px(g, X - 2, hy - 3, 1, 1, C.gold); px(g, X + 2, hy - 3, 1, 1, C.gold);
    } else {                                                                  // conical straw hat
      px(g, X - 4, hy, 9, 1, C.strawD); px(g, X - 3, hy - 1, 7, 1, C.straw); px(g, X - 1, hy - 2, 3, 1, C.straw); px(g, X, hy - 3, 1, 1, C.strawD);
    }
    // what they carry
    if (type === 'farmer' && !up) {                                           // hoe over the shoulder
      for (let k = 0; k < 9; k++) px(g, X - 3 + k, armY + 3 - k, 1, 1, C.wood);
      px(g, X + 5, armY - 7, 2, 2, C.stone);
    }
    if (type === 'yoke' && !up) {                                             // carrying pole with baskets
      const bob = frame === 1 ? 1 : 0;
      px(g, 0, armY, 15, 1, C.wood);
      px(g, 0, armY + 1, 1, 3 + bob, C.woodD); px(g, 14, armY + 1, 1, 3 + bob, C.woodD);
      px(g, 0, armY + 4 + bob, 3, 3, C.strawD); px(g, 12, armY + 4 + bob, 3, 3, C.strawD); px(g, 0, armY + 4 + bob, 3, 1, C.leafL); px(g, 12, armY + 4 + bob, 3, 1, C.flame);
    }
    if (type === 'woman' && !up && !night) {                                  // wagasa parasol by day
      px(g, X + 3, hy - 5, 1, 12, C.woodD);
      px(g, X - 1, hy - 7, 9, 1, C.rust); px(g, X - 2, hy - 6, 11, 1, C.rustD); px(g, X + 1, hy - 8, 5, 1, C.rust);
    }
    return c;
  }

  function drawFar(type, frame) {                 // 7x12 sprite for the village street
    const c = mk(7, 12), g = c.getContext('2d'), X = 3, F = 11;
    const body = { farmer: '#5d6f8a', yoke: '#8a6a4a', samurai: C.rust, woman: '#c8506a', monk: '#d98c3a', child: C.leaf }[type];
    const kid = type === 'child' ? 2 : 0;
    if (type !== 'woman') { if (frame === 1) { px(g, X - 1, F - 2, 1, 2, '#3b4152'); px(g, X + 1, F - 2, 1, 2, '#3b4152'); } else px(g, X, F - 2, 1, 2, '#3b4152'); }
    px(g, X - 1, F - 6 + kid, 3, type === 'woman' ? 6 - kid : 4 - kid, body);
    if (frame === 2) { px(g, X - 2, F - 9, 1, 3, body); px(g, X + 2, F - 9, 1, 3, body); }
    px(g, X, F - 7 + kid, 1, 1, C.skin);
    if (type === 'samurai') { px(g, X - 1, F - 8, 3, 1, '#2a2230'); px(g, X - 1, F - 9, 1, 1, C.gold); px(g, X + 1, F - 9, 1, 1, C.gold); px(g, X, 0, 1, 3, C.rust); }
    else if (type === 'woman') px(g, X - 1, F - 8, 2, 1, C.ink);
    else if (type !== 'monk') { px(g, X - 2, F - 8 + kid, 5, 1, C.straw); px(g, X, F - 9 + kid, 1, 1, C.strawD); }
    if (type === 'yoke') { px(g, 0, F - 5, 7, 1, C.wood); px(g, 0, F - 4, 1, 2, C.strawD); px(g, 6, F - 4, 1, 2, C.strawD); }
    return c;
  }

  function drawWorker(frame) {                   // a farmer planting rice: standing / bent
    const c = mk(11, 16), g = c.getContext('2d');
    if (frame === 0) {
      px(g, 4, 12, 1, 4, '#3b4152'); px(g, 6, 12, 1, 4, '#3b4152'); px(g, 3, 6, 5, 6, '#5d6f8a');
      px(g, 4, 3, 3, 3, C.skin); px(g, 1, 3, 9, 1, C.strawD); px(g, 2, 2, 7, 1, C.straw); px(g, 4, 1, 3, 1, C.straw);
      px(g, 8, 8, 1, 3, C.leafL);
    } else {
      px(g, 3, 12, 1, 4, '#3b4152'); px(g, 5, 12, 1, 4, '#3b4152'); px(g, 3, 9, 6, 3, '#5d6f8a');
      px(g, 8, 9, 2, 2, C.skin); px(g, 7, 7, 4, 1, C.strawD); px(g, 8, 6, 3, 1, C.straw);
      px(g, 8, 12, 1, 3, '#5d6f8a'); px(g, 9, 14, 1, 2, C.leafL);
    }
    return c;
  }

  // ------------------------------------------------------------------ glows (untinted light)
  const glowCache = {};
  function glow(r, col) {
    const k = r + col;
    if (glowCache[k]) return glowCache[k];
    const c = mk(2 * r + 1, 2 * r + 1), g = c.getContext('2d'), A = hex(col);
    for (let y = -r; y <= r; y++) for (let x = -r; x <= r; x++) {
      const d = Math.sqrt(x * x + y * y) / r;
      if (d <= 1 && dith(x + r, y + r, Math.pow(d, 0.8))) { g.fillStyle = `rgba(${A[0]},${A[1]},${A[2]},${(1 - d) * 0.55})`; g.fillRect(x + r, y + r, 1, 1); }
    }
    return (glowCache[k] = c);
  }

  function build() {
    const rand = rng(20260926);
    const w = { rand };
    // horizon lines
    w.farBase = Math.round(H * 0.62); w.midBase = Math.round(H * 0.72); w.villageY = Math.round(H * 0.765);
    w.streetY = Math.round(H * 0.79); w.pathY = Math.round(H * 0.93);
    w.lake = { cx: Math.round(W * 0.68), cy: Math.round(H * 0.835), rx: Math.round(W * 0.17), ry: Math.max(5, Math.round(H * 0.032)) };
    // stars
    w.stars = []; for (let i = 0; i < Math.round(W * H / 700); i++) w.stars.push({ x: rand() * W | 0, y: rand() * H * 0.62 | 0, t: rand() * 6, big: rand() < 0.08 });
    // far karst peaks, misty at the base
    const far = ridge(rand, Math.ceil(W / 26) + 3, -20, W + 20, H * 0.12, H * 0.3, 9, 22, 0.32);
    w.far = new Tinted(mountains(w.farBase, far, '#a393b8', '#f0d9b0', 0.25, true), 0.5);
    // mid hills with a pagoda
    const mid = ridge(rand, Math.ceil(W / 34) + 2, -30, W + 30, H * 0.06, H * 0.17, 16, 40, 0.5);
    const midC = mountains(w.midBase, mid, '#6c6f8e', '#dcc7a2', 0.35, true);
    { const g = midC.getContext('2d');
      let best = mid.bumps.filter(b => b.x > W * 0.55 && b.x < W * 0.92).sort((a, b) => b.h - a.h)[0] || mid.bumps[0];
      w.pagoda = pagoda(g, Math.round(best.x), Math.round(w.midBase - best.h) + 1, 4);
      for (let i = 0; i < W / 5; i++) { const x = rand() * W | 0, y = Math.round(w.midBase - mid.r[x]); if (mid.r[x] > 3) px(g, x, y - 1, 1, 2, '#3f4a3a'); }
    }
    w.mid = new Tinted(midC, 0.22);
    // castles: far & small, middle tower (torches, like the night tower), near & grand
    const sc = clamp(Math.min(H / 250, W / 330), 0.42, 1.3);   // narrow screens get smaller castles
    // (placed where the page banners look out: the middle band of the sky)
    w.castles = [
      { ...castle(0.42 * sc, 'small', rand), x: W * 0.47, y: H * 0.25, bob: 0.9, per: 11, haze: 0.5, depth: 0 },
      { ...castle(0.62 * sc, 'tower', rand), x: W * 0.7, y: H * 0.3, bob: 1.5, per: 9, haze: 0.22, depth: 1 },
      { ...castle(0.86 * sc, 'grand', rand), x: W * 0.3, y: H * 0.34, bob: 2.2, per: 13, haze: 0, depth: 2 },
    ];
    for (const c of w.castles) c.sprite.haze = c.haze;
    // clouds
    w.clouds = [];
    for (let i = 0; i < 7; i++) {
      const cw = 18 + rand() * 34 | 0, ch = 6 + rand() * 6 | 0, c = mk(cw, ch), g = c.getContext('2d');
      for (let k = 0; k < cw * ch * 0.9; k++) {
        const a = rand(), x = a * cw, yy = ch - 1 - Math.pow(rand(), 1.6) * ch * (1 - Math.abs(a - 0.5) * 1.5);
        if (yy >= 0) px(g, x, yy, 2, 1, yy > ch * 0.62 ? '#e7cba0' : dith(x | 0, yy | 0, 0.5) ? '#fbeed4' : '#f4e1bd');
      }
      w.clouds.push({ img: new Tinted(c, 0.1), x: rand() * W, y: H * (0.06 + rand() * 0.38), v: 0.6 + rand() * 1.6, depth: i % 3 });
    }
    // hills, terraces and the village
    const vb = buffer();
    const hills = ridge(rand, Math.ceil(W / 60) + 2, -40, W + 40, H * 0.02, H * 0.07, 30, 70, 0.8);
    const hillBase = Math.round(H * 0.83), HW = hex(C.water), HL = hex(C.leafL), HG = hex(C.leaf), HD = hex(C.leafD);
    for (let x = 0; x < W; x++) {
      const top = Math.round(hillBase - hills.r[x] - H * 0.05);
      for (let y = Math.max(0, top); y < H; y++) {
        const d = y - top, terrace = (d % 4 === 0);                                // rice terraces follow the hill
        vb.set(x, y, terrace ? (x % 9 < 5 && d > 3 ? HW : HL) : (dith(x, y, 0.6) ? HG : HD));
      }
    }
    const vc = vb.done(), vg = vc.getContext('2d');
    w.lights = [];
    const vy = w.villageY;
    for (let x = Math.round(W * 0.03); x < W * 0.47;) {
      const hw = 9 + Math.round(rand() * 8), thatch = rand() < 0.35;
      if (rand() < 0.25) tree(vg, x + 3, vy, 3, rand() < 0.5 ? 'cherry' : 'leaf', rand);
      else for (const win of house(vg, x, vy + Math.round(rand() * 2), hw, rand, thatch)) w.lights.push({ ...win, th: 0.2 + rand() * 0.6, r: 3 });
      x += hw + 4 + Math.round(rand() * 8);
    }
    for (let i = 0; i < 5; i++) tree(vg, W * (0.48 + rand() * 0.45) | 0, vy + 1, 2 + Math.round(rand() * 2), ['cherry', 'leaf', 'pine'][i % 3], rand);
    for (let i = 0; i < 12; i++) { const x = W * (0.02 + rand() * 0.45) | 0; for (let k = 0; k < 7; k++) px(vg, x, vy - k - 1, 1, 1, k % 2 ? '#7a9a3a' : C.leafD); }  // bamboo
    torii(vg, Math.round(w.lake.cx - w.lake.rx - 6), w.lake.cy - 1, 9);
    w.village = new Tinted(vc, 0.08);
    // lake spans
    w.lakeRows = [];
    for (let y = -w.lake.ry; y <= w.lake.ry; y++) {
      const half = Math.round(w.lake.rx * Math.sqrt(1 - (y * y) / (w.lake.ry * w.lake.ry)));
      w.lakeRows.push({ y: w.lake.cy + y, x0: w.lake.cx - half, x1: w.lake.cx + half });
    }
    // fields, path and the lake shore
    const fb = buffer(), fTop = Math.round(H * 0.8);
    const inLake = (x, y) => { const dx = (x - w.lake.cx) / w.lake.rx, dy = (y - w.lake.cy) / w.lake.ry; return dx * dx + dy * dy <= 1; };
    const FC = Object.fromEntries(Object.entries({ shore: C.woodD, shore2: '#4a5a2a', path: C.path, pathD: C.pathD,
      g1: '#4f6b2b', g2: '#5f7d2f', wat: C.water, sprout: '#8fb07a', sproutL: C.leafL, row1: '#7d9a3e', row2: '#6a8634' })
      .map(([k, v]) => [k, hex(v)]));
    for (let y = fTop; y < H; y++) {
      const depth = (y - fTop) / (H - fTop), stripe = Math.max(2, Math.round(2 + depth * 5));
      for (let x = 0; x < W; x++) {
        if (inLake(x, y)) continue;
        const dx = (x - w.lake.cx) / (w.lake.rx + 2), dy = (y - w.lake.cy) / (w.lake.ry + 1.5);
        let col;
        if (dx * dx + dy * dy <= 1) col = dith(x, y, 0.5) ? FC.shore : FC.shore2;                 // shore
        else if (y >= w.pathY - 4 && y <= w.pathY + 1) col = dith(x, y, 0.85) ? FC.pathD : FC.path; // the path
        else if (y > w.pathY + 1) col = dith(x, y, 0.3 + depth * 0.2) ? FC.g1 : FC.g2;             // near grass
        else if (x > W * 0.08 && x < W * 0.44 && y > fTop + 5 && y < w.pathY - 8) {                // flooded paddies
          const cell = (Math.floor(x / 14) + Math.floor((y - fTop) / 6)) % 2, bund = x % 14 === 0 || (y - fTop) % 6 === 0;
          col = bund ? FC.row2 : (cell && dith(x, y, 0.35) ? FC.sprout : (x % 3 === 0 && y % 2 ? FC.sproutL : FC.wat));
        }
        else col = (Math.floor((y - fTop) / stripe) % 2) ? FC.row1 : FC.row2;                      // crop rows
        fb.set(x, y, col);
      }
    }
    const fc = fb.done(), fg = fc.getContext('2d');
    for (let i = 0; i < W / 3; i++) { const x = rand() * W | 0, y = w.pathY + 2 + (rand() * (H - w.pathY - 2) | 0); px(fg, x, y - 1, 1, 2, C.leafL); }     // grass tufts
    for (let i = 0; i < 14; i++) { const x = (w.lake.cx - w.lake.rx + rand() * w.lake.rx * 2) | 0; px(fg, x, w.lake.cy + w.lake.ry - 1 - (rand() * 3 | 0), 1, 3, '#4a6a2a'); } // reeds
    // stone lantern by the path, fence posts
    const lx = Math.round(W * 0.9), ly = w.pathY - 5;
    px(fg, lx - 1, ly - 2, 3, 3, C.stoneD); px(fg, lx - 2, ly - 5, 5, 3, C.stone); px(fg, lx - 1, ly - 4, 3, 1, C.ink); px(fg, lx - 3, ly - 6, 7, 1, C.stoneL); px(fg, lx, ly - 7, 1, 1, C.stone);
    w.stoneLantern = { x: lx, y: ly - 4 };
    for (let x = Math.round(W * 0.47); x < W * 0.62; x += 5) { px(fg, x, w.pathY - 8, 1, 4, C.wood); px(fg, x, w.pathY - 7, 5, 1, C.wood); }
    w.fields = new Tinted(fc, 0);
    // foreground cherry trees framing the scene
    const tc = mk(W, H), tg = tc.getContext('2d');
    const bigTree = (bx, trunkH, r) => {
      for (let k = 0; k < trunkH; k++) px(tg, bx + Math.round(Math.sin(k / 6) * 2), H - k, 3, 1, k % 5 ? C.woodD : C.wood);
      const top = H - trunkH;
      for (let b = 0; b < 5; b++) { const a = -2.4 + b * 0.55; for (let k = 0; k < r; k++) px(tg, bx + 1 + Math.cos(a) * k, top + Math.sin(a) * k * 0.6, 1, 1, C.woodD); }
      for (let k = 0; k < r * r * 2.6; k++) {
        const a = rand() * 6.283, d = Math.sqrt(rand()) * r;
        const x = bx + Math.cos(a) * d * 1.3, y = top - r * 0.35 + Math.sin(a) * d * 0.7;
        px(tg, x, y, 1, 1, Math.sin(a) > 0.35 ? C.pinkD : (dith(x | 0, y | 0, 0.7) ? C.pink : C.pinkL));
      }
      return { x: bx - r * 1.3, y: top - r * 1.05, w: r * 2.6, h: r * 1.4 };
    };
    w.blossoms = [bigTree(Math.round(W * 0.06), Math.round(H * 0.3), Math.round(H * 0.11)),
                  bigTree(Math.round(W * 0.965), Math.round(H * 0.2), Math.round(H * 0.07))];
    w.front = new Tinted(tc, 0);
    // sprites
    w.near = {}; w.far = w.far; w.farSp = {};
    for (const t of PEOPLE) {
      w.near[t] = [0, 1, 2].map(f => new Tinted(drawNear(t, f, false), 0));
      w.near[t + ':n'] = [0, 1, 2].map(f => new Tinted(drawNear(t, f, true), 0));
      w.farSp[t] = [0, 1, 2].map(f => new Tinted(drawFar(t, f), 0.06));
    }
    w.worker = [0, 1].map(f => new Tinted(drawWorker(f), 0));
    // cast
    const walker = (lane, x) => ({ lane, x: x === undefined ? rand() * W : x, dir: rand() < 0.5 ? -1 : 1, type: pick(rand()),
      speed: lane === 'near' ? 6 + rand() * 6 : 2.5 + rand() * 3, dist: 0, idle: 0, launch: 0, lantern: rand() < 0.45, jit: rand() * 3 | 0 });
    w.walkers = [];
    for (let i = 0; i < clamp(Math.round(W / 95), 3, 7); i++) w.walkers.push(walker('near'));
    for (let i = 0; i < clamp(Math.round(W / 80), 3, 8); i++) w.walkers.push(walker('far'));
    w.newWalker = walker;
    w.workers = [];
    for (let i = 0; i < clamp(Math.round(W / 120), 2, 5); i++) w.workers.push({ x: W * (0.1 + rand() * 0.32), y: Math.round(H * (0.84 + rand() * 0.04)), t: rand() * 3 });
    w.shore = [{ x: w.lake.cx - Math.round(w.lake.rx * 0.55), y: w.lake.cy + w.lake.ry + 2, t: 0 }, { x: w.lake.cx + Math.round(w.lake.rx * 0.35), y: w.lake.cy + w.lake.ry + 2, t: 2 }];
    w.skyLanterns = []; w.waterLanterns = []; w.petals = []; w.birds = []; w.fireflies = [];
    for (let i = 0; i < 14; i++) w.fireflies.push({ x: w.lake.cx + (rand() - 0.5) * w.lake.rx * 2.4, y: w.lake.cy - 6 - rand() * 14, t: rand() * 9 });
    w.sky = mk(W, H); w.skyKey = -1; w.shoot = null;
    return w;
  }

  // ------------------------------------------------------------------ per-frame
  function paintSky(w, s) {
    const g = w.sky.getContext('2d'), img = g.createImageData(W, H), d = img.data, N = 7;
    const bands = Array.from({ length: N }, (_, k) => mix(s.top, s.hor, k / (N - 1)));   // dithered bands, like the samurai piece
    for (let y = 0; y < H; y++) {
      const t = clamp(y / (H * 0.66), 0, 1), lv = t * (N - 1), lo = Math.floor(lv), fr = lv - lo;
      for (let x = 0; x < W; x++) {
        const c = bands[Math.min(N - 1, lo + (dith(x, y, fr) ? 1 : 0))], i = (y * W + x) * 4;
        d[i] = c[0]; d[i + 1] = c[1]; d[i + 2] = c[2]; d[i + 3] = 255;
      }
    }
    g.putImageData(img, 0, 0);
  }

  function sun(p, s) {
    const g = ctx;
    if (p > 0.225 && p < 0.775) {
      const t = (p - 0.225) / 0.55, x = W * (0.06 + 0.88 * t), y = H * 0.62 - Math.sin(Math.PI * t) * H * 0.5;
      const warm = 1 - Math.sin(Math.PI * t);
      g.drawImage(glow(12, warm > 0.5 ? '#ffb070' : '#fff1c4'), x - 12, y - 12);
      for (let dy = -5; dy <= 5; dy++) for (let dx = -5; dx <= 5; dx++) if (dx * dx + dy * dy <= 26) px(g, x + dx, y + dy, 1, 1, mix(hex('#fff4cf'), hex('#ffb070'), warm));
      return { x, y, kind: 'sun' };
    }
    const q = ((p + 0.25) % 1) / 0.5;
    if (q >= 0 && q <= 1) {                      // crescent moon, like the night tower's
      const x = W * (0.08 + 0.84 * q), y = H * 0.6 - Math.sin(Math.PI * q) * H * 0.48, r = 6;
      g.drawImage(glow(11, '#f3e6b0'), x - 11, y - 11);
      for (let dy = -r; dy <= r; dy++) for (let dx = -r; dx <= r; dx++) {
        if (dx * dx + dy * dy <= r * r && (dx - 3) * (dx - 3) + (dy + 1) * (dy + 1) > (r - 1) * (r - 1)) px(g, x + dx, y + dy, 1, 1, C.moon);
      }
      return { x, y, kind: 'moon' };
    }
    return null;
  }

  function lightOn(night, th) { return night > th; }

  let last = 0, tPrev = 0, stages = [], stageScan = 0, cssKey = -1;
  function loop() {                               // a timer loop: steady 24 fps, paused in hidden tabs
    setTimeout(loop, reduced ? 1000 : 1000 / FPS);
    if (document.hidden || !world) return;
    try { frame(performance.now()); } catch (e) { if (!loop.warned) { loop.warned = true; console.warn('world frame', e); } }
  }

  function frame(now) {
    const dt = Math.min(0.25, (now - (tPrev || now)) / 1000); tPrev = now; last = now;
    const w = world, T = Date.now() / 1000, p = ((T / DAY + phaseOffset) % 1 + 1) % 1, s = sky(p), n = s.night;
    AMB = s.amb; HOR = s.hor;
    const key = Math.floor(T * 2);
    if (key !== tintKey) tintKey = key;
    if (w.skyKey !== key) { paintSky(w, s); w.skyKey = key; }
    const rand = w.rand;

    ctx.drawImage(w.sky, 0, 0);
    // stars and the odd shooting star
    if (n > 0.05) {
      for (const st of w.stars) {
        const a = n * (0.45 + 0.55 * Math.abs(Math.sin(T * 0.9 + st.t * 7)));
        ctx.fillStyle = `rgba(255,244,214,${a.toFixed(2)})`; ctx.fillRect(st.x, st.y, 1, 1);
        if (st.big) { ctx.fillRect(st.x - 1, st.y, 3, 1); ctx.fillRect(st.x, st.y - 1, 1, 3); }
      }
      if (!w.shoot && n > 0.8 && rand() < 0.004 && !reduced) w.shoot = { x: rand() * W, y: rand() * H * 0.25, life: 1 };
      if (w.shoot) {
        const sh = w.shoot; sh.x += 70 * dt; sh.y += 26 * dt; sh.life -= dt * 1.6;
        for (let k = 0; k < 6; k++) { ctx.fillStyle = `rgba(255,250,230,${(sh.life * (1 - k / 6)).toFixed(2)})`; ctx.fillRect(sh.x - k * 2.6, sh.y - k, 1, 1); }
        if (sh.life <= 0) w.shoot = null;
      }
    }
    const orb = sun(p, s);
    // birds by day: a few cranes crossing
    if (n < 0.3 && !reduced) {
      if (w.birds.length < 4 && rand() < 0.003) { const y = H * (0.1 + rand() * 0.2); for (let k = 0; k < 3; k++) w.birds.push({ x: -10 - k * 7, y: y + k * 3, v: 11, t: k }); }
      for (const b of w.birds) {
        b.x += b.v * dt; const up = Math.floor(T * 4 + b.t) % 2;
        ctx.fillStyle = css(mul(hex('#f6efe0'), AMB)); ctx.fillRect(b.x, b.y, 1, 1); ctx.fillRect(b.x - 1, b.y - up, 1, 1); ctx.fillRect(b.x + 1, b.y - up, 1, 1);
      }
      w.birds = w.birds.filter(b => b.x < W + 10);
    }
    // back to front
    const cloud = depth => { for (const c of w.clouds) if (c.depth === depth) { c.x += c.v * dt * (reduced ? 0 : 1); if (c.x > W + 40) c.x = -60; ctx.drawImage(c.img.get(), Math.round(c.x), Math.round(c.y)); } };
    const drawCastle = c => {
      const oy = reduced ? 0 : Math.sin(T / c.per * 6.283) * c.bob;
      const x = Math.round(c.x - c.w / 2), y = Math.round(c.y - c.h * 0.52 + oy);
      ctx.drawImage(c.sprite.get(), x, y);
      // waterfall thread
      const fcol = mul(hex('#cfe6ef'), AMB);
      for (let k = 0; k < c.fall.len; k++) {
        if (((k + Math.floor(T * 12)) % 5) < 3) { ctx.fillStyle = `rgba(${fcol[0] | 0},${fcol[1] | 0},${fcol[2] | 0},${(0.8 - k / c.fall.len * 0.8).toFixed(2)})`; ctx.fillRect(x + c.fall.x + (k > 3 ? 1 : 0), y + c.fall.y + k, 1, 1); }
      }
      c.at = { x, y };
    };
    cloud(0);
    drawCastle(w.castles[0]);
    ctx.drawImage(w.far.get(), 0, 0);
    cloud(1);
    drawCastle(w.castles[1]);
    ctx.drawImage(w.mid.get(), 0, 0);
    drawCastle(w.castles[2]);
    cloud(2);
    ctx.drawImage(w.village.get(), 0, 0);

    // village street walkers (far)
    const walk = (wk, lane) => {
      if (wk.launch > 0) { wk.launch -= dt; if (wk.launch <= 0) spawnSky(wk); }
      else if (wk.idle > 0) wk.idle -= dt;
      else {
        wk.x += wk.dir * wk.speed * dt * (reduced ? 0 : 1); wk.dist += wk.speed * dt;
        if (rand() < 0.0015) wk.idle = 1 + rand() * 3;
      }
      if (wk.x < -24 || wk.x > W + 24) Object.assign(wk, w.newWalker(lane, wk.dir > 0 ? -20 : W + 20), { dir: wk.dir });
    };
    const drawWalker = wk => {
      const near = wk.lane === 'near', f = wk.launch > 0 ? 2 : (wk.idle > 0 ? 0 : Math.floor(wk.dist / (near ? 3.5 : 2.5)) % 2);
      const set = near ? (w.near[wk.type + (n > 0.45 ? ':n' : '')] || w.near[wk.type]) : w.farSp[wk.type];
      const img = set[f].get(), yb = near ? w.pathY + wk.jit : w.streetY + (wk.jit >> 1);
      const x = Math.round(wk.x - img.width / 2), y = yb - img.height + 1;
      if (wk.dir < 0) { ctx.save(); ctx.translate(x + img.width, y); ctx.scale(-1, 1); ctx.drawImage(img, 0, 0); ctx.restore(); }
      else ctx.drawImage(img, x, y);
      wk.hand = { x: Math.round(wk.x) + (near ? wk.dir * 3 : wk.dir), y: yb - (near ? 12 : 5), head: yb - (near ? 22 : 11) };
    };
    const far = w.walkers.filter(k => k.lane === 'far'), nearW = w.walkers.filter(k => k.lane === 'near');
    for (const wk of far) { walk(wk, 'far'); drawWalker(wk); }

    // village and castle lights (they come on one by one at dusk)
    ctx.globalCompositeOperation = 'lighter';
    for (const L of w.lights) if (lightOn(n, L.th)) { ctx.drawImage(glow(3, '#ffb347'), L.x - 3, L.y - 3); px(ctx, L.x, L.y, 1, 1, '#ffd27a'); }
    for (const c of w.castles) {
      if (!c.at) continue;
      const fade = c.depth === 0 ? 0.6 : 1;
      for (const L of c.lights) if (lightOn(n, L.th)) { ctx.globalAlpha = fade; ctx.drawImage(glow(2, '#ffb347'), c.at.x + L.x - 2, c.at.y + L.y - 2); px(ctx, c.at.x + L.x, c.at.y + L.y, 1, 1, '#ffd27a'); }
      if (n > 0.2) for (const tch of c.torches) {           // flickering torches, like the night tower
        const fl = Math.sin(T * 13 + tch.x) > 0 ? 1 : 0;
        ctx.globalAlpha = fade * clamp(n * 1.4, 0, 1);
        ctx.drawImage(glow(5 + fl, '#f7a13b'), c.at.x + tch.x - 5 - fl, c.at.y + tch.y - 5 - fl);
        px(ctx, c.at.x + tch.x, c.at.y + tch.y - fl, 1, 2, '#ffe08a');
      }
      ctx.globalAlpha = 1;
    }
    if (n > 0.3 && w.pagoda) ctx.drawImage(glow(3, '#ff9a4a'), w.pagoda.x - 3, w.pagoda.y - 3);
    ctx.globalCompositeOperation = 'source-over';

    // the lake mirrors the sky
    const water = mix(mix(mix(s.hor, s.top, 0.35), [74, 128, 138], 0.55), [0, 0, 0], 0.12 + 0.3 * n);  // sky in teal water
    const waterL = mix(water, [255, 245, 225], 0.3);
    for (const r of w.lakeRows) {
      const t = (r.y - (w.lake.cy - w.lake.ry)) / (2 * w.lake.ry);
      px(ctx, r.x0, r.y, r.x1 - r.x0 + 1, 1, mix(water, [20, 25, 40], t * 0.35));
    }
    for (let k = 0; k < 18; k++) {
      const r = w.lakeRows[(k * 7 + Math.floor(T * 0.7)) % w.lakeRows.length], span = r.x1 - r.x0;
      if (span > 6) px(ctx, r.x0 + ((k * 37 + Math.floor(T * 5)) % (span - 4)), r.y, 3, 1, waterL);
    }
    if (orb && orb.x > w.lake.cx - w.lake.rx && orb.x < w.lake.cx + w.lake.rx) {
      ctx.globalCompositeOperation = 'lighter';
      for (const r of w.lakeRows) if ((r.y + Math.floor(T * 3)) % 2) px(ctx, orb.x - 1 + ((r.y * 3) % 3), r.y, 2, 1, orb.kind === 'moon' ? 'rgba(243,230,176,.5)' : 'rgba(255,220,160,.45)');
      ctx.globalCompositeOperation = 'source-over';
    }
    // water lanterns drift across the lake at night
    const onLake = (x, y) => { const dx = (x - w.lake.cx) / w.lake.rx, dy = (y - w.lake.cy) / w.lake.ry; return dx * dx + dy * dy <= 0.92; };
    for (const L of w.waterLanterns) {
      L.x += L.vx * dt * (reduced ? 0 : 1); L.t += dt;
      L.a = Math.min(1, L.t / 2) * clamp((n - 0.15) / 0.3, 0, 1) * (onLake(L.x, L.y) ? 1 : Math.max(0, 1 - L.out)); if (!onLake(L.x, L.y)) L.out = (L.out || 0) + dt;
      if (L.a <= 0.01 && L.t > 3) L.dead = true;
      ctx.globalCompositeOperation = 'lighter'; ctx.globalAlpha = L.a;
      ctx.drawImage(glow(4, '#ff9a3c'), L.x - 4, L.y - 4);
      for (let k = 1; k < 5; k++) if ((k + Math.floor(T * 4)) % 2) px(ctx, L.x, L.y + k, 1, 1, 'rgba(255,170,80,.35)');
      ctx.globalCompositeOperation = 'source-over';
      px(ctx, L.x - 1, L.y - 1, 3, 2, '#ffb347'); px(ctx, L.x, L.y - 1, 1, 1, '#ffe08a');
      ctx.globalAlpha = 1;
    }
    w.waterLanterns = w.waterLanterns.filter(L => !L.dead);

    ctx.drawImage(w.fields.get(), 0, 0);
    // rice planters in the paddies
    for (const wk of w.workers) {
      wk.t += dt; const img = w.worker[Math.floor(wk.t / 1.3) % 2].get();
      ctx.drawImage(img, Math.round(wk.x - 5), wk.y - img.height + 1);
    }
    // lake folk set lanterns on the water at night
    if (n > 0.35) for (const f of w.shore) {
      const img = w.farSp.woman[0].get();
      ctx.drawImage(img, f.x - 3, f.y - img.height + 1);
      f.t -= dt;
      if (f.t <= 0 && w.waterLanterns.length < 26 && n > 0.6 && !reduced) {
        w.waterLanterns.push({ x: f.x + (rand() < 0.5 ? -2 : 2), y: w.lake.cy + w.lake.ry - 2 - (rand() * w.lake.ry * 1.2 | 0), vx: (rand() - 0.5) * 2.4, t: 0 });
        f.t = 2.5 + rand() * 5;
      }
    }
    // path walkers (near), drawn back to front
    nearW.sort((a, b) => a.jit - b.jit);
    for (const wk of nearW) { walk(wk, 'near'); drawWalker(wk); }
    ctx.drawImage(w.front.get(), 0, 0);

    // lights carried by people, the stone lantern, fireflies
    ctx.globalCompositeOperation = 'lighter';
    if (n > 0.45) {
      for (const wk of w.walkers) {
        if (!wk.hand) continue;
        if (wk.launch > 0) { ctx.drawImage(glow(wk.lane === 'near' ? 6 : 4, '#ff9a3c'), wk.hand.x - 6, wk.hand.head - 6); px(ctx, wk.hand.x - 1, wk.hand.head - 2, 3, 3, '#ffb347'); }
        else if (wk.lantern || wk.type === 'woman') { ctx.drawImage(glow(wk.lane === 'near' ? 5 : 3, '#ffb347'), wk.hand.x - 5, wk.hand.y - 5); px(ctx, wk.hand.x, wk.hand.y, wk.lane === 'near' ? 2 : 1, wk.lane === 'near' ? 3 : 2, '#ffcf6a'); }
      }
      ctx.drawImage(glow(7, '#ffb347'), w.stoneLantern.x - 7, w.stoneLantern.y - 7); px(ctx, w.stoneLantern.x - 1, w.stoneLantern.y, 3, 1, '#ffe08a');
      if (!reduced) for (const f of w.fireflies) { const on = Math.sin(T * 2 + f.t * 3) > 0.55; if (on) px(ctx, f.x + Math.sin(T * 0.7 + f.t) * 5, f.y + Math.cos(T * 0.5 + f.t) * 3, 1, 1, '#d9f26a'); }
    }
    ctx.globalCompositeOperation = 'source-over';

    // sky lanterns: villagers launch them all night
    function spawnSky(wk) {
      if (!wk.hand) return;
      w.skyLanterns.push({ x: wk.hand.x, y: wk.hand.head - 2, vx: (rand() - 0.3) * 2, vy: -(3 + rand() * 3), t: rand() * 6, big: wk.lane === 'near' });
    }
    if (n > 0.6 && !reduced && w.skyLanterns.length < 70 && rand() < dt * 0.8) {
      const cand = w.walkers.filter(k => k.launch <= 0 && k.idle <= 0 && k.x > 4 && k.x < W - 4);
      if (cand.length) { const wk = cand[(rand() * cand.length) | 0]; wk.launch = 1.6; }
    }
    ctx.globalCompositeOperation = 'lighter';
    for (const L of w.skyLanterns) {
      L.t += dt; L.y += L.vy * dt; L.x += (L.vx + Math.sin(L.t * 0.8) * 1.2) * dt;
      const hFrac = clamp(L.y / H, 0, 1), sz = L.big ? (hFrac > 0.55 ? 2 : 1) : 1;
      const a = clamp((n - 0.2) / 0.4, 0, 1) * clamp(0.35 + hFrac, 0.25, 1) * (0.85 + 0.15 * Math.sin(L.t * 9));
      ctx.globalAlpha = a;
      ctx.drawImage(glow(sz + 3, '#ff9a3c'), L.x - sz - 3, L.y - sz - 3);
      px(ctx, L.x - (sz >> 1), L.y - sz, sz + 1, sz + 1, '#ffb347');
      if (sz > 1) px(ctx, L.x, L.y - 1, 1, 1, '#ffe08a');
      if (L.y < -8 || a < 0.02) L.dead = true;
    }
    ctx.globalAlpha = 1; ctx.globalCompositeOperation = 'source-over';
    w.skyLanterns = w.skyLanterns.filter(L => !L.dead);

    // cherry petals on the wind
    if (!reduced) {
      if (w.petals.length < Math.round(W / 6) && rand() < 0.9) {
        const b = w.blossoms[rand() < 0.75 ? 0 : 1], fromTop = rand() < 0.3;
        w.petals.push(fromTop ? { x: rand() * W, y: -2, vx: 3 + rand() * 5, vy: 4 + rand() * 4, t: rand() * 9 }
          : { x: b.x + rand() * b.w, y: b.y + rand() * b.h, vx: 3 + rand() * 6, vy: 3 + rand() * 4, t: rand() * 9 });
      }
      const pc = [mul(hex(C.pink), AMB), mul(hex(C.pinkL), AMB), mul(hex(C.pinkD), AMB)].map(css);
      for (const pt of w.petals) {
        pt.t += dt; pt.x += (pt.vx + Math.sin(pt.t * 2) * 4) * dt; pt.y += pt.vy * dt;
        ctx.fillStyle = pc[(pt.t * 3 | 0) % 3];
        ctx.fillRect(pt.x | 0, pt.y | 0, (pt.t * 4 | 0) % 3 ? 1 : 2, 1);
        if (pt.y > H || pt.x > W + 4) pt.dead = true;
      }
      w.petals = w.petals.filter(pt => !pt.dead);
    }

    // windows onto the world: page banners show exactly the part of the world behind them
    if (now - stageScan > 800) { stages = Array.from(document.querySelectorAll('.xcp-stage')); stageScan = now; }
    for (const st of stages) {
      let sc = st.querySelector(':scope > canvas.xcp-stage-cv');
      if (!sc) { sc = document.createElement('canvas'); sc.className = 'xcp-stage-cv'; sc.setAttribute('aria-hidden', 'true'); st.prepend(sc); }
      if (sc.offsetParent === null) continue;          // hidden: the world already shows straight through
      const r = st.getBoundingClientRect();
      if (r.bottom < 0 || r.top > innerHeight || r.width < 2) continue;
      const sx = r.left / S, sy = r.top / S, sw = r.width / S, sh = r.height / S;
      const cw = Math.max(1, Math.round(sw)), ch = Math.max(1, Math.round(sh));
      if (sc.width !== cw || sc.height !== ch) { sc.width = cw; sc.height = ch; }
      const g = sc.getContext('2d');
      g.clearRect(0, 0, cw, ch);
      g.drawImage(cvs, sx, sy, sw, sh, 0, 0, cw, ch);
    }
    // the panel follows the hour too
    const ck = Math.floor(T);
    if (ck !== cssKey) {
      cssKey = ck;
      const rs = document.documentElement.style;
      rs.setProperty('--world-night', n.toFixed(3));
      rs.setProperty('--world-sky', css(s.hor));
      rs.setProperty('--world-top', css(s.top));
      document.documentElement.dataset.worldHour = n > 0.6 ? 'night' : n > 0.15 ? 'twilight' : 'day';
    }
  }

  function size() {
    if (innerWidth < 2 || innerHeight < 2) { setTimeout(size, 250); return; }   // not laid out yet
    S = clamp(Math.round(innerHeight / 250), 2, 5);
    W = Math.ceil(innerWidth / S); H = Math.ceil(innerHeight / S);
    cvs.width = W; cvs.height = H;
    cvs.style.width = W * S + 'px'; cvs.style.height = H * S + 'px';
    ctx.imageSmoothingEnabled = false;
    world = build();
    tintKey = -2;
  }
  let rt = 0;
  addEventListener('resize', () => { clearTimeout(rt); rt = setTimeout(size, 200); });
  size();

  // a lantern button: hide the panels and watch the world (Esc brings them back)
  const btn = document.createElement('button');
  btn.id = 'xcp-world-btn'; btn.type = 'button'; btn.title = 'Watch the world (Esc to return)'; btn.textContent = '🏮';
  btn.addEventListener('click', () => document.body.classList.toggle('xcp-world-view'));
  addEventListener('keydown', e => { if (e.key === 'Escape') document.body.classList.remove('xcp-world-view'); });
  document.body.appendChild(btn);

  window.__xcpWorld = {
    poke() { if (!document.body.contains(cvs)) document.body.prepend(cvs); if (!document.body.contains(btn)) document.body.appendChild(btn); },
    phase(p) { phaseOffset = p - ((Date.now() / 1000) % DAY) / DAY; world.skyKey = -1; },
  };
  loop();
})();
