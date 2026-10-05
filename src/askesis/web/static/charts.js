// Charts for the Andamenti page. Data come from /api/series (engine metrics); nothing is computed here.
(function () {
  "use strict";
  const css = getComputedStyle(document.documentElement);
  const C = (n) => css.getPropertyValue(n).trim();
  const fmt = (v, nd) => (v == null ? "—" : v.toLocaleString("it-IT", { maximumFractionDigits: nd, minimumFractionDigits: nd }));

  function width(el) { return Math.max(280, el.clientWidth || el.parentElement.clientWidth || 600); }

  function plot(el, series, data, opts) {
    if (!data[0] || data[0].length === 0) { el.textContent = "Dati insufficienti."; return; }
    const day = (t) => (t == null ? "—" : new Date(t * 1000).toLocaleDateString("it-IT", { day: "2-digit", month: "2-digit", year: "2-digit" }));
    const o = Object.assign({
      width: width(el), height: 220, series: [{ label: "Data", value: (u, t) => day(t) }].concat(series),
      axes: [{ stroke: C("--muted") }, { stroke: C("--muted") }],
      legend: { show: true }, cursor: { drag: { x: false, y: false } },
    }, opts || {});
    const u = new uPlot(o, data, el);
    const xs = data[0], first = xs[0], last = xs[xs.length - 1];
    if (window.__xmin && window.__xmin > first && window.__xmin < last) u.setScale("x", { min: window.__xmin, max: last });
    return u;
  }

  function band() { return { bands: [{ series: [1, 2], fill: C("--band") }] }; }

  fetch("/api/series", { credentials: "same-origin" }).then((r) => r.json()).then((d) => {
    const root = document.getElementById("charts");
    if (d.empty) { root.textContent = root.dataset.empty; return; }
    window.__xmin = d.window_start;

    // Weight: band (noise), trend, daily points
    const w = d.weight;
    const wEl = document.getElementById("weight");
    if (w.band_lo) {
      plot(wEl, [
        { label: "rumore, limite alto", stroke: "transparent", points: { show: false } },
        { label: "rumore, limite basso", stroke: "transparent", points: { show: false } },
        { label: "tendenza (EMA)", stroke: C("--petrol"), width: 2, points: { show: false } },
        { label: "pesata", stroke: "transparent", points: { show: true, size: 6, fill: C("--slate"), stroke: C("--slate") } },
      ], [w.t, w.band_hi, w.band_lo, w.ema, w.daily], Object.assign(band(), {}));
      document.getElementById("cap-weight").textContent =
        "Peso: banda del rumore ± " + fmt(1.96 * w.noise_sd, 2) + " kg (dispersione delle ultime " + w.noise_n + " pesate attorno alla tendenza)";
    } else {
      plot(wEl, [
        { label: "tendenza (EMA)", stroke: C("--petrol"), width: 2, points: { show: false } },
        { label: "pesata", stroke: "transparent", points: { show: true, size: 6, fill: C("--slate"), stroke: C("--slate") } },
      ], [w.t, w.ema, w.daily]);
      document.getElementById("cap-weight").textContent = "Peso: rumore non ancora stimabile (servono più pesate)";
    }

    // Energy: TDEE with interval as band, weekly intake mean
    const t = d.tdee, i = d.intake;
    plot(document.getElementById("energy"), [
      { label: "IC alto", stroke: "transparent", points: { show: false } },
      { label: "IC basso", stroke: "transparent", points: { show: false } },
      { label: "mantenimento stimato", stroke: C("--petrol"), width: 2 },
      { label: "intake medio", stroke: C("--amber"), width: 2, points: { show: true, size: 5 } },
    ], [t.t, t.hi, t.lo, t.v, t.t.map((x) => { const k = i.t.indexOf(x); return k >= 0 ? i.v[k] : null; })], band());

    const simple = (id, s, label, color) =>
      plot(document.getElementById(id), [{ label: label, stroke: color || C("--petrol"), width: 2, points: { show: true, size: 5 } }], [s.t, s.v]);
    simple("steps", d.steps, "passi/die");
    simple("sleep", d.sleep, "ore/notte");
    simple("run", d.run_km, "km");

    // e1RM: one line per exercise on a shared weekly axis
    const ex = Object.keys(d.e1rm);
    const eEl = document.getElementById("e1rm");
    if (ex.length) {
      const ts = Array.from(new Set(ex.flatMap((k) => d.e1rm[k].t))).sort((a, b) => a - b);
      const palette = [C("--petrol"), C("--amber"), C("--slate"), C("--muted"), C("--alert")];
      plot(eEl, ex.map((k, n) => ({ label: k, stroke: palette[n % palette.length], width: 2, points: { show: true, size: 5 }, spanGaps: true })),
        [ts].concat(ex.map((k) => ts.map((x) => { const j = d.e1rm[k].t.indexOf(x); return j >= 0 ? d.e1rm[k].v[j] : null; }))));
    } else { eEl.textContent = "Nessuna serie con RIR registrata."; }

    // Volume: horizontal bars, no chart library needed
    const vEl = document.getElementById("volume");
    const vol = Object.entries(d.volume).sort((a, b) => b[1] - a[1]);
    if (vol.length) {
      const max = vol[0][1];
      const ul = document.createElement("ul"); ul.className = "bars";
      vol.forEach(([m, v]) => {
        const li = document.createElement("li");
        const name = document.createElement("span"); name.textContent = m;
        const bar = document.createElement("span"); bar.className = "bar"; bar.style.width = (100 * v / max) + "%";
        const val = document.createElement("span"); val.textContent = fmt(v, 1);
        li.append(name, bar, val); ul.append(li);
      });
      vEl.append(ul);
    } else { vEl.textContent = "Nessuna seduta di pesi registrata."; }
  }).catch(() => {
    document.getElementById("charts").textContent = "Impossibile caricare i dati: ricarica la pagina.";
  });
})();
