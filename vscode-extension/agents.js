"use strict";
/** Which agent reviews the findings.
 *
 *  Every CLI here reads project MCP config, so once .mcp.json exists they all
 *  get prgraf's graph tools — the handoff is just "start this agent in this
 *  repo with this prompt". No vendor is special-cased. */

const cp = require("child_process");

const AGENTS = [
  { id: "vscode", label: "VS Code chat", bin: null, args: null },
  { id: "claude", label: "Claude Code", bin: "claude", args: (p) => [p] },
  { id: "codex", label: "Codex", bin: "codex", args: (p) => [p] },
  { id: "gemini", label: "Gemini CLI", bin: "gemini", args: (p) => ["-p", p] },
  { id: "grok", label: "Grok CLI", bin: "grok", args: (p) => [p] },
  { id: "opencode", label: "opencode", bin: "opencode", args: (p) => ["run", p] },
];

/** PowerShell single-quoted literal. PowerShell also treats the typographic
 *  single quotes as quote characters, so those are doubled too. */
function psQuote(text) {
  return `'${String(text).replace(/['\u2018\u2019\u201A\u201B]/g, "$&$&")}'`;
}

/**
 * Terminal options that start `agent` with `prompt` as one argument.
 *
 * No text is ever typed into a shell. On POSIX the agent binary IS the
 * terminal process, args passed as an array. On Windows the CLIs are usually
 * .cmd shims that need a shell, so PowerShell runs a quoted call sent as
 * -EncodedCommand (base64 UTF-16LE), which nothing re-parses on the way in.
 */
function terminalOptions(agent, prompt, cwd) {
  const name = `prgraf · ${agent.label}`;
  // Newlines would end the command early in PowerShell; double quotes get
  // re-parsed when PowerShell hands args to a .cmd shim.
  const text = String(prompt).replace(/\r?\n/g, " ").replace(/"/g, "'");
  const args = agent.args(text);
  if (process.platform === "win32") {
    // Bare name: PowerShell's own lookup honours PATHEXT, whereas `where`
    // lists npm's extensionless sh shim first.
    const script = `& ${[agent.bin, ...args].map(psQuote).join(" ")}`;
    return {
      name, cwd,
      shellPath: "powershell.exe",
      shellArgs: ["-NoLogo", "-NoExit", "-EncodedCommand", Buffer.from(script, "utf16le").toString("base64")],
    };
  }
  return { name, cwd, shellPath: locate(agent.bin), shellArgs: args };
}

/** Absolute path of `bin` when `which` finds one, else the bare name. */
function locate(bin) {
  try {
    const out = cp.execFileSync("which", [bin], { encoding: "utf8", timeout: 5000 });
    return out.split(/\r?\n/)[0].trim() || bin;
  } catch (_) {
    return bin;
  }
}

function isOnPath(bin) {
  if (!bin) return true; // vscode chat needs no binary
  try {
    const probe = process.platform === "win32" ? "where" : "which";
    cp.execFileSync(probe, [bin], { stdio: "ignore", timeout: 5000 });
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

module.exports = { AGENTS, available, byId, resolve, isOnPath, terminalOptions, psQuote };
