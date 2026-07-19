"use strict";
/* prgraf blast-radius UI — self-contained, no external libraries.
 *
 * Layout idea: impact score IS radial distance. Seeds sit at the center;
 * a node with score s is placed at radius R(s) where high score -> close in.
 * The force solver only spreads nodes ANGULARLY within their ring and
 * resolves collisions; it never moves a node off its score radius. That is
 * what makes the decay legible instead of a generic hairball. */

const SVGNS = "http://www.w3.org/2000/svg";
const KIND_COLOR = {
  File: "var(--kind-file)", Class: "var(--kind-class)",
  Function: "var(--kind-function)", Type: "var(--kind-type)", Test: "var(--kind-test)",
};
const RISK_COLOR = {
  critical: "var(--risk-critical)", high: "var(--risk-high)",
  medium: "var(--risk-medium)", low: "var(--risk-low)",
};

const $ = (id) => document.getElementById(id);
const el = {};
let sim = null;

document.addEventListener("DOMContentLoaded", () => {
  Object.assign(el, {
    repo: $("repo-path"), base: $("base-ref"), head: $("head-ref"),
    reviewBtn: $("review-btn"), status: $("status-line"), verdict: $("verdict"),
    findings: $("findings"), logArea: $("log-area"), empty: $("empty-state"),
    toolbar: $("graph-toolbar"), legend: $("legend"), fitBtn: $("fit-btn"),
    labelsToggle: $("labels-toggle"), svg: $("graph"), viewport: $("viewport"),
    rings: $("rings"), edges: $("edges"), nodes: $("nodes"),
    detail: $("detail-panel"), tooltip: $("tooltip"), themeToggle: $("theme-toggle"),
  });

  el.reviewBtn.addEventListener("click", runReview);
  el.fitBtn.addEventListener("click", () => sim && sim.fit());
  el.labelsToggle.addEventListener("change", () =>
    el.viewport.classList.toggle("labels-hidden", !el.labelsToggle.checked));
  el.themeToggle.addEventListener("click", toggleTheme);
  el.svg.addEventListener("click", (e) => { if (e.target === el.svg) closeDetail(); });
  [el.repo, el.base, el.head].forEach((i) =>
    i.addEventListener("keydown", (e) => { if (e.key === "Enter") runReview(); }));
  const exportBtn = $("export-btn");
  if (exportBtn) exportBtn.addEventListener("click", exportReport);

  initPanZoom();

  // Embedded mode: a shared static snapshot inlines its data. Render it
  // immediately and swap the live controls for a read-only snapshot note.
  // Same renderer as live mode — no second implementation to drift.
  if (window.__PRGRAF_DATA__) enterEmbeddedMode(window.__PRGRAF_DATA__);
});

function enterEmbeddedMode(payload) {
  const controls = document.querySelector(".controls");
  if (controls) {
    const when = payload.generated_at || "";
    controls.innerHTML = `
      <div class="snapshot-note">
        <span class="snap-tag">SNAPSHOT</span>
        <div class="snap-repo">${escapeHtml(payload.repo || "")}</div>
        <div class="snap-range">${escapeHtml(payload.base || "")} … ${escapeHtml(payload.head || "HEAD")}</div>
        ${when ? `<div class="snap-when">${escapeHtml(when)}</div>` : ""}
      </div>`;
  }
  const exportBtn = $("export-btn");
  if (exportBtn) exportBtn.remove();
  render(payload);
}

/* ---------------- data flow ---------------- */

async function runReview() {
  const body = {
    repo_path: el.repo.value.trim() || ".",
    base: el.base.value.trim() || "HEAD~1",
    head: el.head.value.trim() || "HEAD",
  };
  setLoading(true);
  el.status.textContent = "Building graph and reviewing…";
  try {
    const res = await fetch("/api/review", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok || data.status === "error") throw new Error(data.message || `HTTP ${res.status}`);
    window.__lastReview = { ...data, repo: body.repo_path, base: body.base, head: body.head };
    render(data);
    el.status.textContent =
      `${data.findings.length} finding(s) · ${data.graph.nodes.length} nodes in radius`;
    const exportBtn = $("export-btn");
    if (exportBtn) exportBtn.classList.remove("hidden");
  } catch (err) {
    el.status.textContent = `Failed: ${err.message}`;
  } finally {
    setLoading(false);
    fetchLogs();
  }
}

function render(data) {
  renderVerdict(data.overall_risk, data.findings);
  renderFindings(data.findings);
  el.empty.classList.add("hidden");
  el.toolbar.classList.remove("hidden");
  renderLegend();
  closeDetail();
  sim = new RadialGraph(el, data);
  sim.start();
}

/* ---------------- verdict + findings ---------------- */

function renderVerdict(overall, findings) {
  if (!overall || overall.score === undefined) { el.verdict.classList.add("hidden"); return; }
  const color = RISK_COLOR[overall.level] || "var(--text-faint)";
  const driver = overall.driver ? overall.driver.split("::").pop() : "—";
  el.verdict.style.setProperty("--verdict-color", color);
  el.verdict.innerHTML = `
    <div class="v-score">${overall.score.toFixed(2)}</div>
    <div class="v-level">${overall.level} risk</div>
    <div class="v-meta">
      Driven by <span class="v-driver">${escapeHtml(driver)}</span>.
      ${overall.elevated || 0} symbol(s) above the review bar.
    </div>`;
  el.verdict.classList.remove("hidden");
}

function renderFindings(findings) {
  el.findings.innerHTML = "";
  if (!findings.length) {
    el.findings.innerHTML = `<p style="color:var(--text-faint);font-size:13px;padding:8px 4px">
      No symbols crossed the review bar.</p>`;
    return;
  }
  findings.forEach((f, i) => {
    const color = RISK_COLOR[f.level] || "var(--text-faint)";
    const card = document.createElement("div");
    card.className = "finding";
    card.style.setProperty("--f-color", color);
    card.style.animationDelay = `${i * 60}ms`;
    card.innerHTML = `
      <div class="finding-head">
        <span class="sev-badge">${f.severity}</span>
        <span class="finding-name" title="${escapeHtml(f.symbol)}">${escapeHtml(f.name)}</span>
        <span class="finding-risk">${f.risk.toFixed(2)}</span>
      </div>
      <div class="finding-loc">${escapeHtml(f.location)}</div>
      <div class="finding-reasons">${escapeHtml((f.reasons || []).join(" · ") || "structural change")}</div>
      <div class="finding-stats">
        <span>impacts <b>${f.impacted}</b> across <b>${f.impacted_files}</b> file(s)</span>
        <span><b>${f.tests}</b> test(s)</span>
        <span><b>${f.callers}</b> caller(s)</span>
      </div>`;
    card.addEventListener("click", () => {
      document.querySelectorAll(".finding").forEach((c) => c.classList.remove("active"));
      card.classList.add("active");
      if (sim) sim.focusSymbol(f.symbol);
    });
    el.findings.appendChild(card);
  });
}

function renderLegend() {
  el.legend.innerHTML = "";
  const items = [
    ["Changed", "var(--text)"], ["File", KIND_COLOR.File], ["Class", KIND_COLOR.Class],
    ["Function", KIND_COLOR.Function], ["Type", KIND_COLOR.Type], ["Test", KIND_COLOR.Test],
  ];
  for (const [label, color] of items) {
    const item = document.createElement("div");
    item.className = "legend-item";
    item.innerHTML = `<span class="legend-swatch" style="background:${color}"></span>${label}`;
    el.legend.appendChild(item);
  }
}

/* ---------------- radial graph ---------------- */

class RadialGraph {
  constructor(dom, data) {
    this.dom = dom;
    this.seeds = new Set(data.graph.seeds);
    this.scores = data.graph.impact_scores || {};
    this.width = dom.svg.clientWidth || 900;
    this.height = dom.svg.clientHeight || 700;
    this.cx = this.width / 2;
    this.cy = this.height / 2;
    this.maxR = Math.min(this.width, this.height) * 0.44;

    // risk lookup per finding symbol, so seeds can be colored by risk
    this.riskBySymbol = {};
    (data.findings || []).forEach((f) => (this.riskBySymbol[f.symbol] = f.level));

    this.buildModel(data.graph);
    this.buildDOM();
    this.raf = null;
    this.alpha = 1;
  }

  riskRank(n) {
    const order = { critical: 4, high: 3, medium: 2, low: 1 };
    return order[this.riskBySymbol[n.id]] || 0;
  }

  // Score -> ring band. Lower score = further out. The band, not the raw
  // score, sets radius, because many nodes can share a score and a ring must
  // be big enough to hold them without colliding.
  bandFor(score) {
    if (score >= 0.5) return 0;   // direct (1 hop, strong edge)
    if (score >= 0.28) return 1;  // ~2 hops
    if (score >= 0.13) return 2;
    return 3;                     // faint reach
  }

  buildModel(graph) {
    this.nodes = graph.nodes.map((n) => {
      const isSeed = this.seeds.has(n.qualified_name);
      const score = this.scores[n.qualified_name] ?? (isSeed ? 1 : 0);
      return {
        id: n.qualified_name, name: n.name, kind: n.kind, file: n.file_path,
        line: n.line_start, isSeed, score,
        band: isSeed ? -1 : this.bandFor(score),
        vx: 0, vy: 0, r: isSeed ? 9 : nodeRadius(score),
      };
    });

    // Group by band, then size each ring so its nodes fit. Rings grow outward,
    // and a crowded ring is pushed out far enough that its circumference holds
    // every node with room to spare — that is what stops the collision solver
    // from exploding a small ring into a lopsided blob.
    const spacing = 26;      // arc length reserved per node
    const minGap = 96;       // minimum radial gap between rings
    const bands = new Map();
    for (const n of this.nodes) {
      if (n.isSeed) continue;
      (bands.get(n.band) || bands.set(n.band, []).get(n.band)).push(n);
    }
    const ringRadii = {};
    let prevR = 74;
    for (const band of [...bands.keys()].sort((a, b) => a - b)) {
      const ring = bands.get(band);
      const needed = (ring.length * spacing) / (2 * Math.PI);
      const r = Math.max(prevR + minGap, needed);
      ringRadii[band] = r;
      prevR = r;
      ring.sort((a, b) => (a.file + a.name).localeCompare(b.file + b.name));
      const step = (2 * Math.PI) / ring.length;
      const offset = band * 0.6; // stagger spokes between rings
      ring.forEach((n, i) => {
        n.angle = i * step + offset;
        n.targetR = r;
        n.x = this.cx + Math.cos(n.angle) * r;
        n.y = this.cy + Math.sin(n.angle) * r;
      });
    }
    this.ringRadii = ringRadii;
    this.maxRingR = prevR;

    // seeds: central rosette sized so dots never overlap, and pushed inside
    // the innermost ring. Finding-driver seeds sort first so they land at the
    // top of the rosette where their labels have clear air.
    const seeds = this.nodes.filter((n) => n.isSeed);
    seeds.sort((a, b) => (this.riskRank(b) - this.riskRank(a)) || (b.score - a.score));
    const innerRing = ringRadii[Math.min(...Object.keys(ringRadii).map(Number))] || 140;
    const seedR = seeds.length > 1
      ? Math.min(innerRing - 40, Math.max(30, (seeds.length * 22) / (2 * Math.PI)))
      : 0;
    seeds.forEach((n, i) => {
      const a = -Math.PI / 2 + (i / Math.max(1, seeds.length)) * Math.PI * 2;
      n.angle = a; n.targetR = seedR;
      n.x = this.cx + Math.cos(a) * seedR;
      n.y = this.cy + Math.sin(a) * seedR;
    });
    this.seedR = seedR;

    // Label budget: finding-driver seeds always (they are the review's point),
    // plus the highest-impact few. Everything else stays quiet until hovered.
    const findingSeeds = seeds.filter((n) => this.riskBySymbol[n.id]);
    const topImpact = this.nodes.filter((n) => !n.isSeed)
      .sort((a, b) => b.score - a.score).slice(0, 8);
    const labelled = new Set([...findingSeeds, ...topImpact].map((n) => n.id));
    for (const n of this.nodes) n.labelled = labelled.has(n.id);

    this.byId = new Map(this.nodes.map((n) => [n.id, n]));
    this.links = graph.edges
      .map((e) => ({ s: this.byId.get(e.source), t: this.byId.get(e.target), kind: e.kind }))
      .filter((l) => l.s && l.t);

    // adjacency for hover highlighting
    this.adj = new Map();
    for (const n of this.nodes) this.adj.set(n.id, new Set());
    for (const l of this.links) { this.adj.get(l.s.id).add(l.t.id); this.adj.get(l.t.id).add(l.s.id); }
  }

  buildDOM() {
    const { rings, edges, nodes } = this.dom;
    rings.innerHTML = ""; edges.innerHTML = ""; nodes.innerHTML = "";

    // one guide circle per populated band, labeled by what the ring means
    const bandName = { 0: "direct", 1: "~2 hops", 2: "~3 hops", 3: "faint" };
    if (this.seedR > 4) {
      rings.appendChild(mk("circle", { cx: this.cx, cy: this.cy, r: this.seedR, class: "ring-guide" }));
      const sl = mk("text", { x: this.cx, y: this.cy - this.seedR - 5, class: "ring-label", "text-anchor": "middle" });
      sl.textContent = "changed";
      rings.appendChild(sl);
    }
    for (const band of Object.keys(this.ringRadii)) {
      const r = this.ringRadii[band];
      rings.appendChild(mk("circle", { cx: this.cx, cy: this.cy, r, class: "ring-guide" }));
      const label = mk("text", { x: this.cx, y: this.cy - r - 5, class: "ring-label", "text-anchor": "middle" });
      label.textContent = bandName[band] || "";
      rings.appendChild(label);
    }

    this.linkEls = this.links.map((l) => {
      const p = mk("path", { class: "edge" });
      edges.appendChild(p);
      l.elp = p;
      return p;
    });

    this.nodeEls = this.nodes.map((n) => {
      const g = mk("g", { class: "node" + (n.isSeed ? " seed" : "") });
      const risk = this.riskBySymbol[n.id];
      const color = n.isSeed && risk ? RISK_COLOR[risk] : (KIND_COLOR[n.kind] || "var(--text-faint)");
      if (n.isSeed) {
        const halo = mk("circle", { class: "node-halo", r: n.r + 8, fill: color });
        g.appendChild(halo);
      }
      const dot = mk("circle", { class: "node-dot", r: n.r, fill: color });
      g.appendChild(dot);
      if (n.labelled) {
        const label = mk("text", { class: "node-label", dy: -n.r - 5 });
        label.textContent = truncate(n.name, n.isSeed ? 22 : 16);
        g.appendChild(label);
      }
      g.addEventListener("mouseenter", () => this.hover(n));
      g.addEventListener("mouseleave", () => this.unhover());
      g.addEventListener("click", (e) => { e.stopPropagation(); this.select(n); });
      n.g = g; n.dot = dot;
      this.dom.nodes.appendChild(g);
      return g;
    });
  }

  /* velocity-Verlet-ish relaxation: angular spread + collision, radius pinned */
  tick() {
    const nodes = this.nodes;
    // pairwise repulsion (only meaningful for nearby nodes; O(n^2) but n<=500)
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = a.x - b.x, dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 > 9000) continue;
        if (d2 < 0.01) { d2 = 0.01; dx = Math.random() - 0.5; dy = Math.random() - 0.5; }
        const d = Math.sqrt(d2);
        const minD = a.r + b.r + 6;
        if (d < minD) {
          const push = ((minD - d) / d) * 0.35 * this.alpha;
          a.vx += dx * push; a.vy += dy * push;
          b.vx -= dx * push; b.vy -= dy * push;
        }
      }
    }
    // link springs — barely there; a whisper of angular attraction so a
    // symbol's callers drift near it on the ring, but not enough to pull the
    // ring into a spiral. Rings stay circular; collision does the spreading.
    for (const l of this.links) {
      if (l.s.band !== l.t.band) continue; // only same-ring pairs, keeps circles
      const dx = l.t.x - l.s.x, dy = l.t.y - l.s.y;
      const k = 0.0015 * this.alpha;
      l.s.vx += dx * k; l.s.vy += dy * k;
      l.t.vx -= dx * k; l.t.vy -= dy * k;
    }
    // integrate, then project back onto the score radius (the hard constraint)
    for (const n of nodes) {
      n.x += n.vx; n.y += n.vy;
      n.vx *= 0.82; n.vy *= 0.82;
      let dx = n.x - this.cx, dy = n.y - this.cy;
      let dist = Math.hypot(dx, dy) || 0.001;
      const targetR = n.pinned ? dist : n.targetR;
      if (!n.pinned) {
        const ratio = targetR / dist;
        n.x = this.cx + dx * ratio;
        n.y = this.cy + dy * ratio;
      }
    }
    this.draw();
    this.alpha *= 0.985;
    if (this.alpha > 0.02) this.raf = requestAnimationFrame(() => this.tick());
    else this.fit();
  }

  draw() {
    for (const n of this.nodes) n.g.setAttribute("transform", `translate(${n.x.toFixed(1)},${n.y.toFixed(1)})`);
    for (const l of this.links) {
      const mx = (l.s.x + l.t.x) / 2, my = (l.s.y + l.t.y) / 2;
      // bow edges slightly toward center for an organic, non-crossing feel
      const cx = mx + (this.cx - mx) * 0.12, cy = my + (this.cy - my) * 0.12;
      l.elp.setAttribute("d", `M${l.s.x.toFixed(1)},${l.s.y.toFixed(1)} Q${cx.toFixed(1)},${cy.toFixed(1)} ${l.t.x.toFixed(1)},${l.t.y.toFixed(1)}`);
    }
  }

  start() { cancelAnimationFrame(this.raf); this.alpha = 0.5; this.fit(); this.tick(); }

  hover(n) {
    const neighbors = this.adj.get(n.id) || new Set();
    for (const m of this.nodes)
      m.g.classList.toggle("dim", m.id !== n.id && !neighbors.has(m.id));
    for (const l of this.links) {
      const on = l.s.id === n.id || l.t.id === n.id;
      l.elp.classList.toggle("hot", on);
      l.elp.classList.toggle("dim", !on);
    }
    this.showTooltip(n);
  }

  unhover() {
    for (const m of this.nodes) m.g.classList.remove("dim");
    for (const l of this.links) l.elp.classList.remove("hot", "dim");
    this.dom.tooltip.classList.remove("show");
  }

  showTooltip(n) {
    const tip = this.dom.tooltip;
    const scoreTxt = n.isSeed ? "changed" : `impact <span class="tt-score">${(n.score || 0).toFixed(3)}</span>`;
    tip.innerHTML = `${escapeHtml(n.name)} · ${n.kind}<br>${scoreTxt}`;
    const rect = this.dom.svg.getBoundingClientRect();
    const pt = this.toScreen(n.x, n.y);
    tip.style.left = `${pt.x + 14}px`;
    tip.style.top = `${pt.y + 8}px`;
    tip.classList.add("show");
  }

  select(n) {
    for (const m of this.nodes) m.g.classList.toggle("active", m.id === n.id);
    this.showDetail(n);
    this.hover(n);
  }

  focusSymbol(qn) {
    const n = this.byId.get(qn);
    if (n) { this.select(n); this.centerOn(n); }
  }

  showDetail(n) {
    const panel = this.dom.detail;
    const callers = [], callees = [];
    for (const l of this.links) {
      if (l.t.id === n.id) callers.push(l.s);
      if (l.s.id === n.id) callees.push(l.t);
    }
    const list = (arr) => arr.slice(0, 12).map((m) =>
      `<li data-id="${escapeHtml(m.id)}"><span class="li-score">${(this.scores[m.id] || 0).toFixed(2)}</span>${escapeHtml(m.name)}</li>`).join("") || `<li style="color:var(--text-faint);cursor:default">none in radius</li>`;
    const color = n.isSeed && this.riskBySymbol[n.id]
      ? RISK_COLOR[this.riskBySymbol[n.id]] : (KIND_COLOR[n.kind] || "var(--text-faint)");
    panel.innerHTML = `
      <button class="dp-close" aria-label="Close">×</button>
      <div class="dp-kind" style="color:${color}">${n.isSeed ? "CHANGED · " : ""}${n.kind}</div>
      <div class="dp-name">${escapeHtml(n.name)}</div>
      <div class="dp-loc">${escapeHtml(n.file)}:${n.line}</div>
      <div class="dp-metric-row">
        <div class="dp-metric"><div class="m-val">${n.isSeed ? "—" : (n.score || 0).toFixed(2)}</div><div class="m-lbl">impact</div></div>
        <div class="dp-metric"><div class="m-val">${callers.length}</div><div class="m-lbl">callers</div></div>
        <div class="dp-metric"><div class="m-val">${callees.length}</div><div class="m-lbl">callees</div></div>
      </div>
      <div class="dp-section-title">Called by</div>
      <ul class="dp-list">${list(callers)}</ul>
      <div class="dp-section-title">Calls</div>
      <ul class="dp-list">${list(callees)}</ul>`;
    panel.classList.remove("hidden");
    panel.querySelector(".dp-close").addEventListener("click", () => closeDetail());
    panel.querySelectorAll(".dp-list li[data-id]").forEach((li) =>
      li.addEventListener("click", () => this.focusSymbol(li.dataset.id)));
  }

  /* view transform helpers (shared with pan/zoom state on window) */
  toScreen(x, y) {
    const t = window.__view;
    const rect = this.dom.svg.getBoundingClientRect();
    return { x: rect.left + t.x + x * t.k, y: rect.top + t.y + y * t.k };
  }

  centerOn(n) {
    const t = window.__view;
    t.k = Math.max(t.k, 1.1);
    t.x = this.width / 2 - n.x * t.k;
    t.y = this.height / 2 - n.y * t.k;
    applyView();
  }

  fit() {
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const n of this.nodes) {
      minX = Math.min(minX, n.x - n.r); minY = Math.min(minY, n.y - n.r);
      maxX = Math.max(maxX, n.x + n.r); maxY = Math.max(maxY, n.y + n.r);
    }
    const pad = 60;
    const w = maxX - minX + pad * 2, h = maxY - minY + pad * 2;
    const t = window.__view;
    t.k = Math.min(this.width / w, this.height / h, 1.6);
    t.x = this.width / 2 - ((minX + maxX) / 2) * t.k;
    t.y = this.height / 2 - ((minY + maxY) / 2) * t.k;
    applyView();
  }
}

function nodeRadius(score) { return 4 + Math.min(1, score / 0.6) * 4; }

/* ---------------- pan / zoom ---------------- */

function initPanZoom() {
  window.__view = { x: 0, y: 0, k: 1 };
  const svg = el.svg;
  let dragging = false, lx = 0, ly = 0;
  svg.addEventListener("mousedown", (e) => { if (e.target === svg || e.target.closest("#rings, #edges")) { dragging = true; lx = e.clientX; ly = e.clientY; } });
  window.addEventListener("mouseup", () => (dragging = false));
  window.addEventListener("mousemove", (e) => {
    if (!dragging) return;
    const t = window.__view; t.x += e.clientX - lx; t.y += e.clientY - ly;
    lx = e.clientX; ly = e.clientY; applyView();
  });
  svg.addEventListener("wheel", (e) => {
    e.preventDefault();
    const t = window.__view;
    const rect = svg.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
    const nk = Math.min(6, Math.max(0.15, t.k * factor));
    t.x = mx - (mx - t.x) * (nk / t.k);
    t.y = my - (my - t.y) * (nk / t.k);
    t.k = nk; applyView();
  }, { passive: false });
}

function applyView() {
  const t = window.__view;
  el.viewport.setAttribute("transform", `translate(${t.x},${t.y}) scale(${t.k})`);
}

/* ---------------- misc ---------------- */

function closeDetail() {
  el.detail.classList.add("hidden");
  document.querySelectorAll(".node.active").forEach((n) => n.classList.remove("active"));
}

function setLoading(on) {
  el.reviewBtn.classList.toggle("loading", on);
  el.reviewBtn.disabled = on;
}

function toggleTheme() {
  const root = document.documentElement;
  root.dataset.theme = root.dataset.theme === "light" ? "dark" : "light";
}

async function exportReport() {
  const last = window.__lastReview;
  if (!last) return;
  const btn = $("export-btn");
  const label = btn.textContent;
  btn.textContent = "Building…";
  btn.disabled = true;
  try {
    const res = await fetch("/api/export", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ payload: last }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const html = await res.text();
    const blob = new Blob([html], { type: "text/html" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    const stamp = new Date().toISOString().slice(0, 10);
    a.href = url;
    a.download = `prgraf-review-${stamp}.html`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    el.status.textContent = `Export failed: ${err.message}`;
  } finally {
    btn.textContent = label;
    btn.disabled = false;
  }
}

async function fetchLogs() {
  try {
    const res = await fetch("/api/logs");
    const data = await res.json();
    el.logArea.textContent = (data.logs || []).slice().reverse()
      .map((l) => `[${l.timestamp}] ${l.message}`).join("\n");
  } catch { /* logs are best-effort */ }
}

function mk(tag, attrs) {
  const node = document.createElementNS(SVGNS, tag);
  for (const k in attrs) node.setAttribute(k, attrs[k]);
  return node;
}
function truncate(s, n) { return s.length > n ? s.slice(0, n - 1) + "…" : s; }
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
