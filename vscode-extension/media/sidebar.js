"use strict";
/* prgraf sidebar view.
 *
 * Rendering is a pure string function of state, exported at the bottom, so the
 * markup can be asserted in plain Node without a DOM or a build step. The last
 * UI bug in this family shipped because assertions matched a regex while the
 * pixels said "0K"; strings you can read are cheaper to trust. */

const VERBS = [
  "Indexing",
  "Parsing symbols",
  "Linking edges",
  "Walking the graph",
  "Scoring risk",
  "Ranking findings",
];

function esc(value) {
  return String(value == null ? "" : value).replace(
    /[&<>"']/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

/** Two decimals, always — a risk that renders as "0.7" next to "0.70" reads
 *  like a different kind of number. */
function fmtRisk(n) {
  return typeof n === "number" && isFinite(n) ? n.toFixed(2) : "—";
}

function shortSymbol(qn) {
  const tail = String(qn || "").split("::").pop();
  return tail || String(qn || "");
}

function basename(p) {
  const parts = String(p || "").replace(/\\/g, "/").split("/").filter(Boolean);
  return parts.length ? parts[parts.length - 1] : "";
}

function field(id, label, value, dim) {
  return (
    `<button class="field" data-pick="${esc(id)}" type="button">` +
    `<span class="label">${esc(label)}</span>` +
    `<span class="value${dim ? " dim" : ""}">${esc(value)}</span>` +
    `<span class="chev">›</span>` +
    `</button>`
  );
}

function renderSetup(state) {
  const repo = state.repo ? basename(state.repo) : "choose a repository";
  const agent = state.agentLabel || "auto";
  const base = state.base || "HEAD~1";
  return (
    `<div class="setup">` +
    field("repo", "Repo", repo, !state.repo) +
    field("agent", "Agent", agent, false) +
    field("base", "Base", base, false) +
    `<button class="go" type="button" data-act="review"${state.busy ? " disabled" : ""}>` +
    `${state.busy ? "Reviewing…" : "Review blast radius"}</button>` +
    `</div>`
  );
}

function renderStatus(state) {
  const verb = state.verb || VERBS[0];
  return (
    `<div class="status"><span>${esc(verb)}</span>` +
    `<span class="dots"><span></span><span></span><span></span></span></div>`
  );
}

function renderFinding(f) {
  const facts = [];
  facts.push(f.tests ? `${f.tests} test(s)` : "no direct test coverage");
  facts.push(`${f.callers} caller(s)`);

  const chips = (f.top_impacted || []).slice(0, 4).map((qn) => {
    const file = String(qn).split("::")[0];
    return `<button class="chip" data-open="${esc(file)}" data-line="1" type="button">${esc(shortSymbol(qn))}</button>`;
  });

  return (
    `<div class="finding" data-level="${esc(f.level || "low")}" ` +
    `data-open="${esc(String(f.location || "").split(":")[0])}" ` +
    `data-line="${esc(String(f.location || "").split(":").pop())}">` +
    `<span class="dot"></span>` +
    `<div class="head"><span class="name">${esc(f.name || shortSymbol(f.symbol))}</span>` +
    `<span class="sev">${esc(f.severity || "")} · ${fmtRisk(f.risk)}</span></div>` +
    `<div class="where">${esc(f.location || "")}</div>` +
    `<div class="facts">${facts.map(esc).join('<span class="sep">·</span>')}</div>` +
    `<div class="reach">reaches ${Number(f.impacted || 0)} symbol(s) across ` +
    `${Number(f.impacted_files || 0)} file(s)</div>` +
    (chips.length ? `<div class="checkfirst">${chips.join("")}</div>` : "") +
    `</div>`
  );
}

function renderEmpty(payload) {
  const files = (payload.changed_files || []).length;
  return (
    `<div class="empty"><b>Nothing above the review bar.</b><br>` +
    `Checked ${files} changed file(s); no symbol scored high enough to flag.</div>` +
    `<div class="actions">` +
    `<button class="link" data-act="widen" type="button">Review the whole branch instead</button>` +
    `</div>`
  );
}

function renderResults(state) {
  const payload = state.payload;
  if (!payload) return "";

  const parts = [];
  const overall = payload.overall_risk || {};
  if (payload.findings && payload.findings.length) {
    parts.push(
      `<div class="overall"><span class="score">${fmtRisk(overall.score)}</span>` +
      `<span>${esc(overall.level || "")} · ${payload.findings.length} finding(s)</span></div>`
    );
  }
  // The engine's own staleness warning, surfaced where the findings are —
  // a stale graph produces confident output, so it must travel with it.
  if (payload.stale) {
    parts.push(
      `<div class="warn">Graph built ${esc(payload.stale.graph_built)}, code changed ` +
      `${esc(payload.stale.code_changed)}. Line numbers may be off — review again to rebuild.</div>`
    );
  }
  if (!payload.findings || !payload.findings.length) {
    parts.push(renderEmpty(payload));
    return parts.join("");
  }
  parts.push(`<div class="findings">${payload.findings.map(renderFinding).join("")}</div>`);
  parts.push(
    `<div class="actions">` +
    `<button class="link" data-act="graph" type="button">Open graph</button>` +
    `<button class="link" data-act="askAgent" type="button">Ask ${esc(state.agentLabel || "an agent")}</button>` +
    `</div>`
  );
  return parts.join("");
}

function render(state) {
  const body = [renderSetup(state)];
  if (state.error) {
    body.push(`<div class="err">${esc(state.error)}</div>`);
  } else if (state.busy) {
    body.push(renderStatus(state));
  } else if (state.payload) {
    body.push(renderResults(state));
  } else {
    body.push(
      `<div class="empty">Pick a repository and an agent, then run a review.<br>` +
      `Changed symbols sit at the centre; everything they reach rings outward.</div>`
    );
  }
  return body.join("");
}

/* ---------- host wiring (skipped under Node) ---------- */

if (typeof acquireVsCodeApi === "function") {
  const vscode = acquireVsCodeApi();
  const root = document.getElementById("root");
  let state = { repo: "", agentLabel: "", base: "HEAD~1", busy: false, payload: null, error: "" };
  let ticker = null;

  const paint = () => { root.innerHTML = render(state); };

  const startTicker = () => {
    let i = 0;
    stopTicker();
    ticker = setInterval(() => {
      i = (i + 1) % VERBS.length;
      state.verb = VERBS[i];
      paint();
    }, 1800);
  };
  const stopTicker = () => { if (ticker) clearInterval(ticker); ticker = null; };

  root.addEventListener("click", (event) => {
    const pick = event.target.closest("[data-pick]");
    if (pick) return vscode.postMessage({ type: "pick", what: pick.dataset.pick });

    const act = event.target.closest("[data-act]");
    if (act) return vscode.postMessage({ type: act.dataset.act });

    const open = event.target.closest("[data-open]");
    if (open && open.dataset.open) {
      return vscode.postMessage({
        type: "open",
        file: open.dataset.open,
        line: parseInt(open.dataset.line, 10) || 1,
      });
    }
  });

  window.addEventListener("message", (event) => {
    const msg = event.data || {};
    if (msg.type !== "state") return;
    state = Object.assign(state, msg.state);
    if (state.busy) { state.verb = VERBS[0]; startTicker(); } else { stopTicker(); }
    paint();
  });

  paint();
  vscode.postMessage({ type: "ready" });
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = { render, renderSetup, renderResults, renderFinding, renderEmpty, esc, fmtRisk, VERBS };
}
