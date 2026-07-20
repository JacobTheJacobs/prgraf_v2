"use strict";
/**
 * LLM narration of an already-computed review.
 *
 * Design rule, and the whole reason this stays cheap: the model NEVER sees the
 * diff or any file contents. It only receives facts the graph already derived
 * — which symbols changed, what they reach, coverage, and why each scored the
 * way it did — and turns them into prose. That means:
 *   - one small call per review, regardless of PR size
 *   - a 5000-line PR costs the same as a 5-line one
 *   - the model cannot invent findings; it can only explain given ones
 *
 * Kept vscode-free below the callSummary boundary so the prompt builder is
 * unit-testable without an editor.
 */

const MAX_FINDINGS = 5;
const MAX_IMPACTED_PER_FINDING = 6;

/** Compact, factual brief. This is the entire model input. */
function buildPrompt(payload) {
  const overall = payload.overall_risk || {};
  const findings = (payload.findings || []).slice(0, MAX_FINDINGS);
  const graph = payload.graph || {};
  const scores = graph.impact_scores || {};

  const lines = [];
  lines.push(`Repository: ${shortName(payload.repo)}`);
  lines.push(`Range: ${payload.base || "?"}..${payload.head || "HEAD"}`);
  lines.push(`Changed files: ${(payload.changed_files || []).length}`);
  lines.push(
    `Overall risk: ${fmt(overall.score)} (${overall.level || "unknown"}), ` +
    `driven by ${shortSymbol(overall.driver)}`
  );
  lines.push("");
  lines.push("Changed symbols and what they reach:");

  for (const f of findings) {
    lines.push(
      `- ${shortSymbol(f.symbol)} (${f.kind || "symbol"}) at ${f.location}` +
      ` | severity ${f.severity}, risk ${fmt(f.risk)}` +
      ` | reaches ${f.impacted} symbol(s) across ${f.impacted_files} file(s)` +
      ` | ${f.tests} test(s), ${f.callers} caller(s)`
    );
    if (f.reasons && f.reasons.length) {
      lines.push(`    scored for: ${f.reasons.join("; ")}`);
    }
    const top = (f.top_impacted || []).slice(0, MAX_IMPACTED_PER_FINDING);
    if (top.length) {
      // Only annotate with a score when we actually have one: the graph map is
      // capped, so some listed symbols fall outside it. Emitting "(?)" would
      // hand the model a token it might try to interpret.
      const rendered = top.map((t) => {
        const s = scores[t];
        return typeof s === "number" ? `${shortSymbol(t)} (${s.toFixed(2)})` : shortSymbol(t);
      });
      lines.push(`    most-affected: ${rendered.join(", ")}`);
    }
  }

  if (payload.truncated) {
    lines.push("");
    lines.push("Note: the blast radius was truncated at the node cap; reach may be larger.");
  }

  return lines.join("\n");
}

const SYSTEM = `You explain the blast radius of a code change to a reviewer.

You are given ONLY pre-computed graph facts — never the diff. Do not invent
findings, file contents, or behaviour you were not given. If something is not
in the facts, do not claim it.

"Impact" numbers are graph reach scores: 1.0 = the changed symbol, ~0.6 = a
direct caller/callee, ~0.36 = two hops. Higher means more tightly coupled.

Write for someone deciding what to check before merging:
1. One sentence on what this change touches and how far it spreads.
2. What is most likely to break, and why — name the specific symbols.
3. Any coverage gap worth noting (untested symbol with wide reach).
4. What to check first.

Be concrete and brief: under 150 words, plain prose, no headings, no bullet
padding. If the risk is low and reach is small, say so plainly in one or two
sentences rather than manufacturing concern.`;

function fmt(n) {
  return typeof n === "number" ? n.toFixed(2) : "?";
}

function shortSymbol(qn) {
  if (!qn) return "unknown";
  const tail = String(qn).split("::").pop();
  return tail || String(qn);
}

function shortName(p) {
  if (!p) return "repo";
  return String(p).replace(/\\/g, "/").split("/").filter(Boolean).pop() || "repo";
}

/**
 * Run the narration through VS Code's language model API.
 * Returns { text } or { error } — never throws.
 */
async function listModels(vscode) {
  try {
    // No vendor filter on purpose: return whatever this editor actually has —
    // Copilot, Claude, or anything another extension registered.
    return (await vscode.lm.selectChatModels()) || [];
  } catch (_) {
    return [];
  }
}

/** Match a saved preference against id, family, or "vendor/family". */
function matchModel(models, preferred) {
  if (!preferred) return null;
  const want = String(preferred).toLowerCase();
  return (
    models.find((m) => String(m.id).toLowerCase() === want) ||
    models.find((m) => `${m.vendor}/${m.family}`.toLowerCase() === want) ||
    models.find((m) => String(m.family).toLowerCase() === want) ||
    models.find((m) => String(m.vendor).toLowerCase() === want) ||
    null
  );
}

async function callSummary(vscode, payload, token, preferred) {
  const models = await listModels(vscode);
  if (!models.length) {
    return {
      error:
        "No language model is available to this editor. prgraf uses whichever " +
        "chat model VS Code exposes — it does not require any particular vendor. " +
        "Either sign in to a chat provider, or use the MCP path instead " +
        "('prgraf: Review with the agent (MCP)'), or set prgraf.summary = false. " +
        "The graph itself needs no model at all.",
    };
  }

  const model = matchModel(models, preferred) || models[0];
  const messages = [
    vscode.LanguageModelChatMessage.User(`${SYSTEM}\n\n---\n\n${buildPrompt(payload)}`),
  ];

  try {
    const response = await model.sendRequest(messages, {}, token);
    let text = "";
    for await (const chunk of response.text) text += chunk;
    return { text: text.trim(), model: `${model.vendor}/${model.family}` };
  } catch (err) {
    // Consent refusal and quota both land here; surface, never crash the panel.
    return { error: `Summary failed via ${model.vendor}/${model.family}: ${err && err.message ? err.message : err}` };
  }
}

module.exports = { buildPrompt, callSummary, listModels, matchModel, SYSTEM };
