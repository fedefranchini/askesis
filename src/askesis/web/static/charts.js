// Charts for the Andamenti page. Data come from /api/series (engine metrics); nothing is computed here,
// except aligning weekly series on a shared time axis for drawing.
(function () {
  "use strict";
  const css = getComputedStyle(document.documentElement);
  const C = (n) => css.getPropertyValue(n).trim();
  const DAY = 86400;
  const nf = (v, nd) => (v == null ? "—" : v.toLocaleString("it-IT", { maximumFractionDigits: nd, minimumFractionDigits: nd }));
  const day = (t) => new Date(t * 1000).toLocaleDateString("it-IT", { day: "2-digit", month: "2-digit", year: "2-digit" });
  const plots = [];
  let range = { min: null, max: null };

  function width(el) { return Math.max(260, Math.floor(el.getBoundingClientRect().width)); }

  function axes(unit) {
    const base = { stroke: C("--text-3"), grid: { stroke: C("--line"), width: 1 }, ticks: { stroke: C("--line"), width: 1 }, font: "12px Barlow, system-ui" };
    return [
      Object.assign({}, base, { values: (u, ts) => ts.map((t) => new Date(t * 1000).toLocaleDateString("it-IT", { day: "numeric", month: "short" })) }),
      Object.assign({}, base, { size: 48, values: (u, vs) => vs.map((v) => nf(v, Math.abs(v) < 10 && v % 1 ? 1 : 0)) }),
    ];
  }

  // A one-line readout under each chart replaces the legend: values at the crosshair.
  function readout(el, series, nd) {
    const p = document.createElement("p");
    p.className = "note readout";
    p.setAttribute("aria-live", "off");
    el.after(p);
    return (u) => {
      const i = u.cursor.idx;
      if (i == null) { p.textContent = " "; return; }
      const parts = series.map((s, k) => (s.readout === false ? null : s.label + " " + nf(u.data[k + 1][i], s.nd != null ? s.nd : nd))).filter(Boolean);
      p.textContent = day(u.data[0][i]) + " · " + parts.join(" · ");
    };
  }

  function plot(id, series, data, opts) {
    const el = document.getElementById(id);
    if (!data[0] || data[0].length === 0) { el.className = "empty"; el.textContent = "Dati insufficienti per questo periodo."; return; }
    const show = readout(el, series, (opts && opts.nd) || 0);
    const o = Object.assign({
      width: width(el), height: 200, padding: [8, 8, 0, 0],
      series: [{}].concat(series.map((s) => Object.assign({ spanGaps: true }, s))),
      axes: axes(), legend: { show: false },
      cursor: { drag: { x: false, y: false }, points: { size: 7, fill: C("--card") } },
      scales: { x: { time: true, range: (u, mn, mx) => [range.min != null ? range.min : mn, range.max != null ? range.max : mx] } },
      hooks: { setCursor: [show] },
    }, opts || {});
    const u = new uPlot(o, data, el);
    show(u);
    plots.push({ u, el });
    return u;
  }

  function align(ts, t, v) { const m = new Map(t.map((x, k) => [x, v[k]])); return ts.map((x) => (m.has(x) ? m.get(x) : null)); }
  function union() { return Array.from(new Set([].concat.apply([], arguments))).sort((a, b) => a - b); }
  const band = (a, b) => ({ bands: [{ series: [a, b], fill: C("--band") }] });
  const hidden = (label) => ({ label, stroke: "transparent", points: { show: false }, readout: false });
  const line = (label, color, extra) => Object.assign({ label, stroke: color, width: 2, points: { show: true, size: 5, fill: color, stroke: color } }, extra || {});

  function setPeriod(days, last) {
    range = { min: last - days * DAY, max: last + DAY };
    plots.forEach(({ u }) => u.setScale("x", { min: range.min, max: range.max }));
    document.querySelectorAll(".periods button").forEach((b) => b.setAttribute("aria-pressed", String(Number(b.dataset.days) === days)));
  }

  fetch("/api/series", { credentials: "same-origin" }).then((r) => r.json()).then((d) => {
    const root = document.getElementById("charts");
    if (d.empty) { root.className = "empty"; root.textContent = root.dataset.empty; return; }
    range = { min: d.last - 91 * DAY, max: d.last + DAY };

    // Weight: noise band around the trend, daily weigh-ins as dots (the page's signature)
    const w = d.weight;
    const dots = { label: "pesata", stroke: "transparent", nd: 1, points: { show: true, size: 5, fill: C("--text-2"), stroke: C("--text-2") } };
    const trend = { label: "tendenza", stroke: C("--s1"), width: 2.5, nd: 1, points: { show: false } };
    if (w.band_lo) {
      plot("weight", [hidden("banda alta"), hidden("banda bassa"), trend, dots], [w.t, w.band_hi, w.band_lo, w.ema, w.daily],
        Object.assign(band(1, 2), { height: 240, nd: 1 }));
      document.getElementById("cap-weight").textContent = "Punti: pesate. Linea: tendenza. Banda del rumore ± " + nf(1.96 * w.noise_sd, 2) +
        " kg (dispersione delle ultime " + w.noise_n + " pesate attorno alla tendenza): ciò che sta dentro è rumore.";
    } else {
      plot("weight", [trend, dots], [w.t, w.ema, w.daily], { height: 240, nd: 1 });
      document.getElementById("cap-weight").textContent = "Punti: pesate. Linea: tendenza. Rumore non ancora stimabile: servono più pesate.";
    }

    // Energy: estimated maintenance with its interval, weekly intake vs target
    const t = d.tdee, e = d.energy_vs_target;
    const ets = union(t.t, e.t, d.intake.t);
    const intake = e.t.length ? align(ets, e.t, e.actual) : align(ets, d.intake.t, d.intake.v);
    plot("energy", [hidden("int. alto"), hidden("int. basso"), line("mantenimento", C("--s1")), line("assunta", C("--s2")),
      line("target", C("--s3"), { dash: [6, 4], points: { show: false } })],
      [ets, align(ets, t.t, t.hi), align(ets, t.t, t.lo), align(ets, t.t, t.v), intake, align(ets, e.t, e.target)], band(1, 2));

    const pr = d.protein_vs_target;
    plot("protein", [line("assunte", C("--s2")), line("target", C("--s3"), { dash: [6, 4], points: { show: false } })], [pr.t, pr.actual, pr.target]);

    const se = d.sessions;
    const bars = uPlot.paths && uPlot.paths.bars ? uPlot.paths.bars({ size: [0.5, 28] }) : null;
    plot("sessions", [Object.assign(line("fatte", C("--s1")), bars ? { paths: bars, fill: C("--s1"), points: { show: false } } : {}),
      line("previste", C("--s3"), { dash: [6, 4], points: { show: false } })],
      [se.t, se.done, se.planned], { scales: { x: { time: true, range: (u, mn, mx) => [range.min != null ? range.min : mn, range.max != null ? range.max : mx] }, y: { range: (u, mn, mx) => [0, Math.max(3, mx + 1)] } } });

    const thr = d.sleep_threshold.value;
    plot("sleep", [line("ore/notte", C("--s1"), { nd: 1 }), line("soglia", C("--s3"), { dash: [6, 4], points: { show: false }, readout: false })],
      [d.sleep.t, d.sleep.v, d.sleep.t.map(() => thr)], { nd: 1 });
    plot("steps", [line("passi/die", C("--s1"))], [d.steps.t, d.steps.v]);
    plot("run", [line("km", C("--s1"), { nd: 1 })], [d.run_km.t, d.run_km.v], { nd: 1 });

    // e1RM: up to three lifts on a shared weekly axis
    const ex = Object.keys(d.e1rm);
    if (ex.length) {
      const ts = union.apply(null, ex.map((k) => d.e1rm[k].t));
      const palette = [C("--s1"), C("--s2"), C("--s3")];
      plot("e1rm", ex.map((k, n) => line(k, palette[n % 3], { nd: 1 })), [ts].concat(ex.map((k) => align(ts, d.e1rm[k].t, d.e1rm[k].v))), { nd: 1 });
      const lg = document.createElement("ul"); lg.className = "legend";
      ex.forEach((k, n) => { const li = document.createElement("li"); const i = document.createElement("i"); i.className = "k-s" + (n % 3 + 1); li.append(i, k); lg.append(li); });
      document.getElementById("e1rm").parentElement.append(lg);
    } else { const el = document.getElementById("e1rm"); el.className = "empty"; el.textContent = "Nessuna serie con RIR registrata."; }

    // Volume: horizontal bars with the reference band (parameter with its source), no chart library needed
    const vEl = document.getElementById("volume");
    const vol = Object.entries(d.volume).sort((a, b) => b[1] - a[1]);
    const ref = d.volume_ref.range;
    if (vol.length) {
      document.getElementById("cap-volume").textContent = "Serie frazionarie per muscolo. Fascia " + ref[0] + "–" + ref[1] +
        " serie a settimana: riferimento per l'ipertrofia, evidenza di bassa certezza (" + d.volume_ref.claim + ").";
      const max = Math.max(vol[0][1], ref[1]) * 1.1;
      const ul = document.createElement("ul"); ul.className = "bars";
      vol.forEach(([m, v]) => {
        const li = document.createElement("li");
        const name = document.createElement("span"); name.textContent = m;
        const track = document.createElement("span"); track.className = "track";
        const band = document.createElement("span"); band.className = "ref";
        band.style.left = (100 * ref[0] / max) + "%"; band.style.width = (100 * (ref[1] - ref[0]) / max) + "%";
        const bar = document.createElement("span"); bar.className = "bar"; bar.style.width = (100 * v / max) + "%";
        track.append(band, bar);
        const val = document.createElement("span"); val.className = "v"; val.textContent = nf(v, 1);
        li.append(name, track, val); ul.append(li);
      });
      vEl.append(ul);
    } else { vEl.className = "empty"; vEl.textContent = "Nessuna seduta di pesi registrata."; }

    document.querySelectorAll(".periods button").forEach((b) => b.addEventListener("click", () => setPeriod(Number(b.dataset.days), d.last)));
    let pending = null;
    window.addEventListener("resize", () => {
      clearTimeout(pending);
      pending = setTimeout(() => plots.forEach(({ u, el }) => u.setSize({ width: width(el), height: u.height })), 150);
    });
  }).catch(() => {
    document.getElementById("charts").textContent = "Impossibile caricare i dati: ricarica la pagina.";
  });
})();
