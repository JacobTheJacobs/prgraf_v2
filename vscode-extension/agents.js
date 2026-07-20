"use strict";
/** Which agent reviews the findings.
 *
 *  Every CLI here reads project MCP config, so once .mcp.json exists they all
 *  get prgraf's graph tools — the handoff is just "start this agent in this
 *  repo with this prompt". No vendor is special-cased. */

const cp = require("child_process");

const AGENTS = [
  { id: "vscode", label: "VS Code chat", bin: null, build: null },
  { id: "claude", label: "Claude Code", bin: "claude", build: (p) => ["claude", q(p)] },
  { id: "codex", label: "Codex", bin: "codex", build: (p) => ["codex", q(p)] },
  { id: "gemini", label: "Gemini CLI", bin: "gemini", build: (p) => ["gemini", "-p", q(p)] },
  { id: "grok", label: "Grok CLI", bin: "grok", build: (p) => ["grok", q(p)] },
  { id: "opencode", label: "opencode", bin: "opencode", build: (p) => ["opencode", "run", q(p)] },
];

/** Quote for a shell without mangling the prompt. */
function q(text) {
  return `"${String(text).replace(/"/g, "'").replace(/\r?\n/g, " ")}"`;
}

function isOnPath(bin) {
  if (!bin) return true; // vscode chat needs no binary
  try {
    const probe = process.platform === "win32" ? "where" : "which";
    cp.execFileSync(probe, [bin], { stdio: "ignore" });
    return true;
  } catch (_) {
    return false;
  }
}

/** Agents actually available on this machine. */
function available() {
  return AGENTS.filter((a) => isOnPath(a.bin));
}

function byId(id) {
  return AGENTS.find((a) => a.id === id) || null;
}

/**
 * Resolve which agent to use: an explicit setting, else the first CLI found,
 * else VS Code chat. Returns null only if nothing at all is usable.
 */
function resolve(preferred) {
  const found = available();
  if (preferred && preferred !== "auto") {
    const want = found.find((a) => a.id === preferred);
    if (want) return want;
  }
  return found.find((a) => a.bin) || found[0] || null;
}

module.exports = { AGENTS, available, byId, resolve, isOnPath };
