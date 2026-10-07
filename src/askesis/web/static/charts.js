// Full charts of the Andamenti page, drawn only when a question is opened. Data come from /api/series (engine
// metrics); nothing is computed here except aligning weekly series on a shared time axis.
(function () {
  "use strict";
  const root = document.getElementById("charts");
  if (!root) return;
  const css = getComputedStyle(document.documentElement);
  const C = (n) => css.getPropertyValue(n).trim();
  const DAY = 86400;
  const DAYS = Number(root.dataset.days || 28);
  const nf = (v, nd) => (v == null ? "—" : v.toLocaleString("it-IT", { maximumFractionDigits: nd, minimumFractionDigits: nd }));
  const day = (t) => new Date(t * 1000).toLocaleDateString("it-IT", { day: "2-digit", month: "2-digit", year: "2-digit" });
  const plots = [];
  let data = null, range = { min: null, max: null }, loading = null;

  const width = (el) => Math.max(260, Math.floor(el.getBoundingClientRect().width));
  function axes() {
    const base = { stroke: C("--text-3"), grid: { stroke: C("--line"), width: 1 }, ticks: { stroke: C("--line"), width: 1 }, font: "12px Barlow, system-ui" };
    return [
      Object.assign({}, base, { values: (u, ts) => ts.map((t) => new Date(t * 1000).toLocaleDateString("it-IT", { day: "numeric", month: "short" })) }),
      Object.assign({}, base, { size: 48, values: (u, vs) => {
        const step = vs.length > 1 ? Math.abs(vs[1] - vs[0]) : 1;  // decimals from the tick step: no repeated labels
        const nd = step >= 1 ? 0 : step >= 0.1 ? 1 : 2;
        return vs.map((v) => nf(v, nd));
      } }),
    ];
  }
  function empty(el, text) { el.className = "empty"; el.textContent = text; }

  // Values at the crosshair, in one line under the chart (works with touch too).
  function readout(el, series, nd) {
    const p = document.createElement("p");
    p.className = "note readout";
    el.after(p);
    return (u) => {
      const i = u.cursor.idx;
      if (i == null) { p.textContent = " "; return; }
      const parts = series.map((s, k) => (s.readout === false ? null : s.label + " " + nf(u.data[k + 1][i], s.nd != null ? s.nd : nd))).filter(Boolean);
      p.textContent = day(u.data[0][i]) + " · " + parts.join(" · ");
    };
  }

  function plot(id, series, cols, opts) {
    const el = document.getElementById(id);
    if (!el || el.dataset.drawn) return;
    el.dataset.drawn = "1";
    const xs = cols[0] || [];
    const visible = xs.filter((t) => t >= range.min).length;
    const anyValue = cols.slice(1).some((c) => c.some((v, i) => v != null && xs[i] >= range.min));
    if (!visible || !anyValue) { empty(el, "Nessun dato in questo periodo."); return; }
    const show = readout(el, series, (opts && opts.nd) || 0);
    const xrange = (u, mn, mx) => [range.min, range.max];
    const o = Object.assign({
      width: width(el), height: 200, padding: [8, 8, 0, 0],
      series: [{}].concat(series.map((s) => Object.assign({ spanGaps: true }, s))),
      axes: axes(), legend: { show: false },
      cursor: { drag: { x: false, y: false }, points: { size: 8, fill: C("--card") } },
      scales: { x: { time: true, range: xrange } },
      hooks: { setCursor: [show] },
    }, opts || {});
    const u = new uPlot(o, cols, el);
    show(u);
    plots.push({ u, el });
  }

  const align = (ts, t, v) => { const m = new Map(t.map((x, k) => [x, v[k]])); return ts.map((x) => (m.has(x) ? m.get(x) : null)); };
  const union = (...a) => Array.from(new Set([].concat(...a))).sort((x, y) => x - y);
  const hidden = (label) => ({ label, stroke: "transparent", points: { show: false }, readout: false });
  const line = (label, color, extra) => Object.assign({ label, stroke: color, width: 2, points: { show: true, size: 6, fill: color, stroke: color } }, extra || {});
  const guide = (label, color) => line(label, color, { dash: [6, 4], points: { show: false } });

  const draw = {
    rate(d) {
      const w = d.weight, cor = d.corridor;
      const s = [], cols = [w.t], bands = [];
      if (cor) { s.push(hidden("obiettivo alto"), hidden("obiettivo basso")); cols.push(cor.hi, cor.lo); bands.push({ series: [1, 2], fill: "rgba(169,180,188,0.20)" }); }
      if (w.band_lo) { const k = s.length + 1; s.push(hidden("rumore alto"), hidden("rumore basso")); cols.push(w.band_hi, w.band_lo); bands.push({ series: [k, k + 1], fill: C("--band") }); }
      s.push({ label: "tendenza", stroke: C("--s1"), width: 2.5, nd: 1, points: { show: false } },
        { label: "pesata", stroke: "transparent", nd: 1, points: { show: true, size: 6, fill: C("--text-2"), stroke: C("--text-2") } });
      cols.push(w.ema, w.daily);
      plot("weight", s, cols, { height: 240, nd: 1, bands });
    },
    plan(d) {
      const t = d.tdee, e = d.energy_vs_target;
      const ets = union(t.t, e.t, d.intake.t);
      const intake = e.t.length ? align(ets, e.t, e.actual) : align(ets, d.intake.t, d.intake.v);
      plot("energy", [hidden("int. alto"), hidden("int. basso"), line("mantenimento", C("--s1")), line("assunta", C("--s2")), guide("target", C("--s3"))],
        [ets, align(ets, t.t, t.hi), align(ets, t.t, t.lo), align(ets, t.t, t.v), intake, align(ets, e.t, e.target)],
        { bands: [{ series: [1, 2], fill: C("--band") }] });
      const pr = d.protein_vs_target;
      plot("protein", [line("assunte", C("--s2")), guide("target", C("--s3"))], [pr.t, pr.actual, pr.target]);
      const se = d.sessions;
      const bars = uPlot.paths && uPlot.paths.bars ? uPlot.paths.bars({ size: [0.5, 28] }) : null;
      plot("sessions", [Object.assign(line("fatte", C("--s1")), bars ? { paths: bars, fill: C("--s1"), points: { show: false } } : {}), guide("previste", C("--s3"))],
        [se.t, se.done, se.planned], { scales: { x: { time: true, range: () => [range.min, range.max] }, y: { range: (u, mn, mx) => [0, Math.max(3, mx + 1)] } } });
    },
    strength(d) {
      const ex = Object.keys(d.e1rm);
      const el = document.getElementById("e1rm");
      if (!ex.length) { empty(el, "Nessuna serie con RIR registrata per i fondamentali."); return; }
      const ts = union(...ex.map((k) => d.e1rm[k].t));
      const palette = [C("--s1"), C("--s2"), C("--s3")];
      plot("e1rm", ex.map((k, n) => line(k, palette[n % 3], { nd: 1 })), [ts].concat(ex.map((k) => align(ts, d.e1rm[k].t, d.e1rm[k].v))), { nd: 1 });
      if (el.dataset.drawn && el.className !== "empty") {
        const lg = document.createElement("ul"); lg.className = "legend";
        ex.forEach((k, n) => { const li = document.createElement("li"); const i = document.createElement("i"); i.className = "k-s" + (n % 3 + 1); li.append(i, k); lg.append(li); });
        el.parentElement.append(lg);
      }
    },
    recovery(d) {
      const thr = d.sleep_threshold.value;
      plot("sleep", [line("ore/notte", C("--s1"), { nd: 1 }), guide("soglia", C("--s3"))], [d.sleep.t, d.sleep.v, d.sleep.t.map(() => thr)], { nd: 1 });
      const st = d.steps_target;
      plot("steps", st ? [line("passi/die", C("--s1")), guide("target", C("--s3"))] : [line("passi/die", C("--s1"))],
        st ? [d.steps.t, d.steps.v, d.steps.t.map(() => st)] : [d.steps.t, d.steps.v]);
      plot("run", [line("km", C("--s1"), { nd: 1 })], [d.run_km.t, d.run_km.v], { nd: 1 });
    },
  };

  function load() {
    if (!loading) {
      loading = fetch("/api/series", { credentials: "same-origin" }).then((r) => r.json()).then((d) => {
        data = d;
        const last = d.empty ? Date.now() / 1000 : d.last;
        range = { min: last - DAYS * DAY, max: last + DAY };
        return d;
      });
    }
    return loading;
  }

  function open(det) {
    const key = det.dataset.q;
    load().then((d) => { if (!d.empty && draw[key]) draw[key](d); })
      .catch(() => det.querySelectorAll(".chart").forEach((el) => empty(el, "Impossibile caricare i dati: ricarica la pagina.")));
  }

  root.querySelectorAll("details[data-q]").forEach((det) => {
    if (det.open) open(det);
    det.addEventListener("toggle", () => { if (det.open) open(det); });
  });
  let pending = null;
  window.addEventListener("resize", () => {
    clearTimeout(pending);
    pending = setTimeout(() => plots.forEach(({ u, el }) => u.setSize({ width: width(el), height: u.height })), 150);
  });
})();
