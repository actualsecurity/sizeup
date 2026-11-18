// Small SVG charts for the station's signal metrics (#606): Settings > Radios > Signal (signal-panel.js) and the
// sparks on Settings > Station > Health (health.js). No library: one y axis a chart (two measures: two charts, one
// over the other on the same time axis), thin marks, a hairline grid, the event ticks along the top, and a crosshair
// whose readout lists every layer at that x. Colors are the tokens' --viz-* (style.css); text is never the series'.
//   SignalChart.plot(opts) -> <figure class="sg-fig">
//     x0, x1        the x domain (epoch s when time: true, else a number: dB for a distribution)
//     y             [lo, hi] (else from the layers, padded); yfmt (v -> text); unit (on the axis: "dB", "/min")
//     h             the plot's height in px (the width is the box's)
//     layers        [{type, name, cls, ...}]: band {pts: [[x, lo, hi]]}, line / step {pts: [[x, y]]}, hline {y},
//                   vline {x} (label at its top, row: which line), dots {pts: [[x, y, info]]}, bars {pts: [[xa, xb, y]]}, stack {names, classes, pts: [[xa, xb,
//                   [v...]]]}, spans {spans: [[xa, xb]]}, whisker {pts: [[x, lo, mid, hi]]}; label: a direct label at
//                   the right end (a reference line's value); gap: a line / band breaks over a gap longer than this
//     events        [{at, ev, text}]: a tick each on top (ticks closer than TICK_PX: one, with the count)
//     label         the figure's name (aria-label)
//   SignalChart.spark(values, opts) -> <svg class="sg-spark">: a line over equal steps (null: a gap), its title
"use strict";

window.SignalChart = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const PAD = { l: 40, r: 56, t: 12, b: 18 };     // px: the y labels, the direct labels, the event ticks, the x labels
  const TICK_PX = 8;
  const W_DEFAULT = 720;

  function s(tag, attrs = {}, cls = "") {
    const n = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, String(v));
    if (cls) n.setAttribute("class", cls);
    return n;
  }
  const div = (cls, text) => {
    const d = document.createElement("div");
    d.className = cls;
    if (text !== undefined) d.textContent = text;
    return d;
  };
  const finite = (v) => typeof v === "number" && isFinite(v);
  const r1 = (v) => (finite(v) ? (Math.round(v * 10) / 10).toString() : "—");

  // Round ticks: 1, 2 or 5 times a power of ten, about n of them.
  function ticks(lo, hi, n) {
    if (!(hi > lo)) return [lo];
    const raw = (hi - lo) / Math.max(1, n), p = Math.pow(10, Math.floor(Math.log10(raw)));
    const step = [1, 2, 5, 10].map((m) => m * p).find((x) => x >= raw) || raw;
    const out = [];
    for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Math.round(v / step) * step);
    return out;
  }

  const TZ = () => (window.cad && cad.scene && cad.scene.timezone) || "America/New_York";
  function tfmt(t, span) {
    const d = new Date(t * 1000);
    const o = span > 2 * 86400 ? { month: "short", day: "numeric" } : { hour: "2-digit", minute: "2-digit", hourCycle: "h23" };
    try { return new Intl.DateTimeFormat("en-US", { ...o, timeZone: TZ() }).format(d); } catch (_) { return d.toISOString().slice(11, 16); }
  }
  function tfull(t) {
    try {
      return new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
                                                hourCycle: "h23", timeZone: TZ() }).format(new Date(t * 1000));
    } catch (_) { return new Date(t * 1000).toISOString().slice(0, 16); }
  }
  // The console's zone's offset from UTC at t (s), so day and multi-hour ticks fall on local midnights and hours.
  function tzOffset(t) {
    try {
      const p = Object.fromEntries(new Intl.DateTimeFormat("en-US", { timeZone: TZ(), hourCycle: "h23", year: "numeric",
        month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" })
        .formatToParts(new Date(t * 1000)).map((x) => [x.type, x.value]));
      return (Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour % 24, +p.minute, +p.second) / 1000) - Math.floor(t);
    } catch (_) { return 0; }
  }
  // Time ticks on whole hours / local days: stepped in the zone's wall-clock time and each one turned back into a
  // time with the offset at that tick, so a DST change on the way never leaves them on 23:00 or 01:00 (#686).
  function timeTicks(x0, x1, n) {
    const span = x1 - x0;
    const step = [900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 259200, 604800].find((x) => span / x <= n) || 604800;
    const out = [];
    if (step < 3600) {
      for (let t = Math.ceil(x0 / step) * step; t <= x1; t += step) out.push(t);
      return out;
    }
    const utc = (w) => { const t = w - tzOffset(w - tzOffset(w)); return w - tzOffset(t); };   // wall clock -> time
    for (let w = Math.ceil((x0 + tzOffset(x0)) / step) * step, k = 0; k < 1000; w += step, k++) {
      const t = utc(w);
      if (t > x1) break;
      if (t >= x0 && (!out.length || t > out[out.length - 1])) out.push(t);
    }
    return out;
  }

  function extent(layers) {
    let lo = Infinity, hi = -Infinity;
    const see = (v) => { if (finite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } };
    for (const L of layers) {
      if (L.noscale) continue;
      if (L.type === "hline") see(L.y);
      for (const p of L.pts || []) {
        if (L.type === "band") { see(p[1]); see(p[2]); }
        else if (L.type === "bars") see(p[2]);
        else if (L.type === "stack") see((p[2] || []).reduce((a, b) => a + (b || 0), 0));
        else if (L.type === "whisker") { see(p[1]); see(p[3]); }
        else see(p[1]);
      }
    }
    return lo <= hi ? [lo, hi] : null;
  }

  // Path pieces over the points, broken where two are further apart than gap.
  function runs(pts, gap) {
    const out = [];
    let cur = [];
    for (const p of pts) {
      if (cur.length && gap && p[0] - cur[cur.length - 1][0] > gap) { out.push(cur); cur = []; }
      cur.push(p);
    }
    if (cur.length) out.push(cur);
    return out;
  }

  function plot(o) {
    const fig = document.createElement("figure");
    fig.className = "sg-fig";
    if (o.label) fig.setAttribute("aria-label", o.label);
    const W = Math.max(280, Math.round(o.width || (o.box && o.box.clientWidth) || W_DEFAULT));
    const H = (o.h || 120) + PAD.t + PAD.b;
    const layers = (o.layers || []).filter(Boolean);
    let [ylo, yhi] = o.y || extent(layers) || [0, 1];
    if (!o.y) {
      const pad = Math.max((yhi - ylo) * 0.08, o.ymin_span ? (o.ymin_span - (yhi - ylo)) / 2 : 0.5);
      ylo = o.zero ? Math.min(0, ylo) : ylo - pad;
      yhi += pad;
      if (o.zero) ylo = Math.min(0, ylo);
    }
    if (!(yhi > ylo)) yhi = ylo + 1;
    const x0 = o.x0, x1 = o.x1 > o.x0 ? o.x1 : o.x0 + 1;
    const pw = W - PAD.l - PAD.r, ph = H - PAD.t - PAD.b;
    const X = (x) => PAD.l + ((x - x0) / (x1 - x0)) * pw;
    const Y = (y) => PAD.t + (1 - (y - ylo) / (yhi - ylo)) * ph;
    const yfmt = o.yfmt || r1;
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img" }, "sg-svg");
    if (o.label) svg.setAttribute("aria-label", o.label);
    // the grid and the axes: recessive, hairline
    const grid = s("g", {}, "sg-grid");
    for (const v of ticks(ylo, yhi, Math.max(2, Math.round(ph / 36)))) {
      grid.append(s("line", { x1: PAD.l, x2: PAD.l + pw, y1: Y(v), y2: Y(v) }));
      const t = s("text", { x: PAD.l - 6, y: Y(v) + 4, "text-anchor": "end" }, "sg-ax");
      t.textContent = yfmt(v);
      grid.append(t);
    }
    if (o.unit) {
      const u = s("text", { x: PAD.l - 6, y: PAD.t - 2, "text-anchor": "end" }, "sg-ax sg-unit");
      u.textContent = o.unit;
      grid.append(u);
    }
    const xt = o.time ? timeTicks(x0, x1, Math.max(2, Math.floor(pw / 90))) : ticks(x0, x1, Math.max(2, Math.floor(pw / 60)));
    for (const v of xt) {
      grid.append(s("line", { x1: X(v), x2: X(v), y1: PAD.t + ph, y2: PAD.t + ph + 3 }));
      const t = s("text", { x: X(v), y: H - 4, "text-anchor": "middle" }, "sg-ax");
      t.textContent = o.time ? tfmt(v, x1 - x0) : (o.xfmt || r1)(v);
      grid.append(t);
    }
    svg.append(grid);
    const marks = s("g", {}, "sg-marks");
    const labels = [];
    for (const L of layers) {
      const cls = `sg-${L.type} ${L.cls || ""}`.trim();
      if (L.type === "spans") {
        for (const [a, b] of L.spans || []) {
          const r = s("rect", { x: X(Math.max(a, x0)), y: PAD.t, width: Math.max(1, X(Math.min(b, x1)) - X(Math.max(a, x0))), height: ph }, cls);
          r.append(Object.assign(s("title"), { textContent: `${L.name}: ${tfull(a)} – ${tfull(b)}` }));
          marks.append(r);
        }
      } else if (L.type === "band") {
        for (const run of runs(L.pts || [], L.gap)) {
          const top = run.map((p) => `${X(p[0]).toFixed(1)},${Y(p[2]).toFixed(1)}`);
          const bot = run.slice().reverse().map((p) => `${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`);
          marks.append(s("path", { d: `M${top.join("L")}L${bot.join("L")}Z` }, cls));
        }
      } else if (L.type === "line" || L.type === "step") {
        for (const run of runs(L.pts || [], L.gap)) {
          let d = "";
          run.forEach((p, i) => {
            const x = X(p[0]).toFixed(1), y = Y(p[1]).toFixed(1);
            if (!i) d += `M${x},${y}`;
            else d += L.type === "step" ? `H${x}V${y}` : `L${x},${y}`;
          });
          if (L.type === "step" && run.length) d += `H${X(x1).toFixed(1)}`;
          marks.append(s("path", { d }, cls));
        }
        const last = (L.pts || [])[(L.pts || []).length - 1];
        if (L.label && last) labels.push([Y(last[1]), L.label, L.cls]);
      } else if (L.type === "hline") {
        if (finite(L.y) && L.y >= ylo && L.y <= yhi) {
          marks.append(s("line", { x1: PAD.l, x2: PAD.l + pw, y1: Y(L.y), y2: Y(L.y) }, cls));
          if (L.label) labels.push([Y(L.y), L.label, L.cls]);
        }
      } else if (L.type === "vline") {
        if (finite(L.x) && L.x >= x0 && L.x <= x1) {
          marks.append(s("line", { x1: X(L.x), x2: X(L.x), y1: PAD.t, y2: PAD.t + ph }, cls));
          if (L.label) {
            const t = s("text", { x: X(L.x) + 3, y: PAD.t + 10 + 12 * (L.row || 0) }, "sg-dl");
            t.textContent = L.label;
            marks.append(t);
          }
        }
      } else if (L.type === "dots") {
        for (const p of L.pts || []) {
          if (!finite(p[1])) continue;
          const c = s("circle", { cx: X(p[0]).toFixed(1), cy: Y(p[1]).toFixed(1), r: L.r || 3 }, cls);
          if (p[2]) c.append(Object.assign(s("title"), { textContent: p[2] }));
          marks.append(c);
        }
      } else if (L.type === "whisker") {
        for (const p of L.pts || []) {
          if (finite(p[1]) && finite(p[3])) marks.append(s("line", { x1: X(p[0]), x2: X(p[0]), y1: Y(p[1]), y2: Y(p[3]) }, `${cls} sg-wl`));
          if (finite(p[2])) marks.append(s("circle", { cx: X(p[0]), cy: Y(p[2]), r: 3 }, cls));
        }
      } else if (L.type === "bars") {
        for (const p of L.pts || []) {
          if (!finite(p[2]) || !p[2]) continue;
          const xa = X(p[0]) + 1, w = Math.min(24, Math.max(1, X(p[1]) - X(p[0]) - 2));
          const y0 = Y(Math.max(ylo, 0)), y = Y(p[2]);
          marks.append(s("rect", { x: xa.toFixed(1), y: Math.min(y, y0).toFixed(1), width: w.toFixed(1), height: Math.abs(y0 - y).toFixed(1) }, cls));
        }
      } else if (L.type === "stack") {
        for (const p of L.pts || []) {
          let acc = 0;
          const xa = X(p[0]) + 1, w = Math.min(24, Math.max(1, X(p[1]) - X(p[0]) - 2));
          (p[2] || []).forEach((v, i) => {
            if (!v) return;
            const ya = Y(acc), yb = Y(acc + v);
            acc += v;
            marks.append(s("rect", { x: xa.toFixed(1), y: yb.toFixed(1), width: w.toFixed(1), height: Math.max(0.5, ya - yb - 1).toFixed(1) },
              `sg-stack ${(L.classes || [])[i] || ""}`));
          });
        }
      }
    }
    svg.append(marks);
    // direct labels at the right end (reference lines: Open, Close, Suggested), never on top of each other
    labels.sort((a, b) => a[0] - b[0]);
    let lastY = -Infinity;
    for (const [y, text, cls] of labels) {
      const yy = Math.max(y + 4, lastY + 12);
      lastY = yy;
      const t = s("text", { x: PAD.l + pw + 4, y: yy }, "sg-dl");
      t.textContent = text;
      svg.append(t);
      if (cls) t.setAttribute("data-of", cls);
    }
    // the events: a tick each along the top; ticks closer than TICK_PX are one, with the count
    const evs = (o.events || []).filter((e) => finite(e.at) && e.at >= x0 && e.at <= x1).sort((a, b) => a.at - b.at);
    const groups = [];
    for (const e of evs) {
      const g = groups[groups.length - 1];
      if (g && X(e.at) - X(g[0].at) < TICK_PX) g.push(e);
      else groups.push([e]);
    }
    const eg = s("g", {}, "sg-events");
    for (const g of groups) {
      const x = X(g[0].at);
      const t = s("g", { tabindex: 0 }, "sg-ev");
      t.append(s("line", { x1: x, x2: x, y1: PAD.t, y2: PAD.t + ph }, "sg-evl"));
      t.append(s("path", { d: `M${(x - 4).toFixed(1)},${PAD.t - 8}h8l-4,6z` }, "sg-evm"));
      if (g.length > 1) {
        const n = s("text", { x: x + 6, y: PAD.t - 2 }, "sg-ax");
        n.textContent = String(g.length);
        t.append(n);
      }
      t.append(Object.assign(s("title"), { textContent: g.map((e) => `${tfull(e.at)} ${e.text || e.ev}`).join("\n") }));
      eg.append(t);
    }
    svg.append(eg);
    // the crosshair and its readout: every layer's value at the nearest x
    const cross = s("line", { y1: PAD.t, y2: PAD.t + ph, x1: -10, x2: -10 }, "sg-cross");
    svg.append(cross);
    const tip = div("sg-tip");
    tip.hidden = true;
    const readout = (x) => {
      const rows = [];
      const near = (pts) => {
        let best = null, bd = Infinity;
        for (const p of pts || []) { const d = Math.abs(p[0] - x); if (d < bd) { bd = d; best = p; } }
        return best && Math.abs(X(best[0]) - X(x)) <= Math.max(12, pw / 60) ? best : null;
      };
      for (const L of layers) {
        if (!L.name || L.type === "spans") continue;
        if (L.type === "hline") { rows.push([L.name, yfmt(L.y), L.cls]); continue; }
        if (L.type === "vline") { if (finite(L.x)) rows.push([L.name, (o.xfmt || r1)(L.x), L.cls]); continue; }
        if (L.type === "bars") {
          const p = (L.pts || []).find((q) => x >= q[0] && x < q[1]);
          if (p) rows.push([L.name, yfmt(p[2]), L.cls]);
          continue;
        }
        if (L.type === "stack") {
          const p = (L.pts || []).find((q) => x >= q[0] && x < q[1]);
          if (p) (L.names || []).forEach((nm, i) => { if (p[2][i]) rows.push([nm, String(p[2][i]), (L.classes || [])[i]]); });
          continue;
        }
        if (L.type === "step") {
          let cur = null;
          for (const p of L.pts || []) if (p[0] <= x) cur = p;
          if (cur) rows.push([L.name, yfmt(cur[1]), L.cls]);
          continue;
        }
        const p = near(L.pts);
        if (!p) continue;
        if (L.type === "band") rows.push([L.name, `${yfmt(p[1])}–${yfmt(p[2])}`, L.cls]);
        else if (L.type === "whisker") rows.push([L.name, `${yfmt(p[2])} (${yfmt(p[1])}–${yfmt(p[3])})`, L.cls]);
        else rows.push([L.type === "dots" && p[2] ? `${L.name} · ${p[2]}` : L.name, yfmt(p[1]), L.cls]);
      }
      for (const e of evs) if (Math.abs(X(e.at) - X(x)) <= TICK_PX) rows.push(["", e.text || e.ev, "sg-evk"]);
      return rows;
    };
    const move = (ev) => {
      const rect = svg.getBoundingClientRect ? svg.getBoundingClientRect() : { left: 0, width: W };
      const px = ((ev.clientX - rect.left) / (rect.width || W)) * W;
      if (px < PAD.l || px > PAD.l + pw) { leave(); return; }
      const x = x0 + ((px - PAD.l) / pw) * (x1 - x0);
      cross.setAttribute("x1", px);
      cross.setAttribute("x2", px);
      const rows = readout(x);
      const head = div("sg-tip-h", o.time ? tfull(x) : `${(o.xfmt || r1)(x)}${o.xunit ? " " + o.xunit : ""}`);
      tip.replaceChildren(head, ...rows.map(([name, v, cls]) => {
        const r = div("sg-tip-r");
        const key = document.createElement("span");
        key.className = `sg-key ${cls || ""}`;
        r.append(key, div("sg-tip-v", v), div("sg-tip-n", name));
        return r;
      }));
      tip.hidden = false;
      // placed in the figure's own pixels (the svg may be drawn narrower than its viewBox)
      const fr = fig.getBoundingClientRect ? fig.getBoundingClientRect() : { left: 0, width: W };
      const cx = ev.clientX - fr.left, fw = fr.width || W;
      const right = cx > fw * 0.55;            // past the middle: the readout on the cursor's left
      tip.style.left = right ? "auto" : `${cx + 12}px`;
      tip.style.right = right ? `${fw - cx + 12}px` : "auto";
    };
    const leave = () => { tip.hidden = true; cross.setAttribute("x1", -10); cross.setAttribute("x2", -10); };
    svg.addEventListener("pointermove", move);
    svg.addEventListener("pointerleave", leave);
    const empty = !layers.some((L) => !L.ref && ((L.pts && L.pts.length) || (L.spans && L.spans.length)));   // (not a reference line)
    if (empty) {
      const t = s("text", { x: PAD.l + pw / 2, y: PAD.t + ph / 2, "text-anchor": "middle" }, "sg-ax sg-none");
      t.textContent = o.empty || "No data";
      svg.append(t);
    }
    fig.append(svg, tip);
    if (o.legend) fig.append(legend(o.legend));
    fig.dataset.empty = empty ? "1" : "";
    return fig;
  }

  // The legend: a short key in the mark's color (a line for lines, a swatch for fills), the name in text ink.
  function legend(items) {
    const box = div("sg-legend");
    for (const [name, cls, shape] of items) {
      const it = document.createElement("span");
      it.className = "sg-li";
      const k = document.createElement("span");
      k.className = `sg-key ${shape === "fill" ? "fill" : shape === "dot" ? "dot" : ""} ${cls || ""}`.trim();
      it.append(k, document.createTextNode(name));
      box.append(it);
    }
    return box;
  }

  // A sparkline: values over equal steps (null: a gap), no axes; its title says what and the last value.
  function spark(values, o = {}) {
    const W = o.w || 120, H = o.h || 18;
    const vals = (values || []).map((v) => (finite(v) ? v : null));
    const have = vals.filter((v) => v !== null);
    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img" }, `sg-spark ${o.cls || ""}`.trim());
    const title = s("title");
    title.textContent = o.title || "";
    svg.append(title);
    if (o.title) svg.setAttribute("aria-label", o.title);
    if (!have.length) return svg;
    let lo = Math.min(...have, o.lo ?? Infinity), hi = Math.max(...have, o.hi ?? -Infinity);
    if (!(hi > lo)) { hi = lo + 1; lo -= 0; }
    const X = (i) => 1 + (i / Math.max(1, vals.length - 1)) * (W - 2), Y = (v) => H - 2 - ((v - lo) / (hi - lo)) * (H - 4);
    let d = "", pen = false;
    vals.forEach((v, i) => {
      if (v === null) { pen = false; return; }
      d += `${pen ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`;
      pen = true;
    });
    svg.append(s("path", { d }, "sg-sp"));
    const li = vals.length - 1 - [...vals].reverse().findIndex((v) => v !== null);
    svg.append(s("circle", { cx: X(li).toFixed(1), cy: Y(vals[li]).toFixed(1), r: 2 }, "sg-spd"));
    return svg;
  }

  return { plot, spark, legend, ticks, timeTicks, tzOffset };
})();
