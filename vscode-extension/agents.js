"use strict";
/** Which agent reviews the findings.
 *
 *  Every CLI here reads project MCP config, so once .mcp.json exists they all
 *  get prgraf's graph tools — the handoff is just "start this agent in this
 *  repo with this prompt". No vendor is special-cased. */

const cp = require("child_process");
const fs = require("fs");
const path = require("path");

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

/** Shell script for POSIX terminals: run the agent from its positional args
 *  ("$@" — never parsed as shell text), then keep the terminal open, so a
 *  one-shot agent (gemini -p, opencode run) leaves its output readable. */
const POSIX_RUNNER =
  '"$@"; status=$?; echo; echo "[agent exited $status]"; exec "${SHELL:-/bin/sh}"';

/**
 * Terminal options that start `agent` with `prompt` as one argument.
 *
 * No text is ever typed into a shell. On POSIX /bin/sh runs a fixed script
 * and the agent plus its args arrive as positional parameters. On Windows the
 * CLIs are usually npm shims, so PowerShell runs a quoted call sent as
 * -EncodedCommand (base64 UTF-16LE), which nothing re-parses on the way in.
 */
function terminalOptions(agent, prompt, cwd) {
  const name = `prgraf · ${agent.label}`;
  // Newlines would end the command early in PowerShell; double quotes get
  // re-parsed when PowerShell hands args to a .cmd shim.
  const text = String(prompt).replace(/\r?\n/g, " ").replace(/"/g, "'");
  const args = agent.args(text);
  if (process.platform === "win32") {
    const script = `& ${[windowsCommand(agent.bin), ...args].map(psQuote).join(" ")}`;
    return {
      name, cwd,
      shellPath: "powershell.exe",
      // Bypass covers a .ps1 we could not avoid; process scope only, and a
      // Group Policy setting still wins — hence preferring the .cmd shim.
      shellArgs: [
        "-NoLogo", "-NoExit", "-ExecutionPolicy", "Bypass",
        "-EncodedCommand", Buffer.from(script, "utf16le").toString("base64"),
      ],
    };
  }
  return {
    name, cwd,
    shellPath: "/bin/sh",
    // "prgraf" is $0; the agent binary and its args are $1.. and run as "$@".
    shellArgs: ["-c", POSIX_RUNNER, "prgraf", locate(agent.bin), ...args],
  };
}

/** What PowerShell should invoke for `bin`. A bare name resolves npm's
 *  claude.ps1 first, which the default ExecutionPolicy blocks, so use the
 *  .cmd/.exe `where` finds, or the .cmd shim sitting next to the sh shim. */
function windowsCommand(bin) {
  let hits;
  try {
    hits = cp.execFileSync("where", [bin], { encoding: "utf8", timeout: 5000 })
      .split(/\r?\n/).map((l) => l.trim()).filter(Boolean);
  } catch (_) {
    return bin;
  }
  if (!hits.length) return bin;
  const runnable = hits.find((h) => /\.(cmd|bat|exe)$/i.test(h));
  if (runnable) return runnable;
  const parsed = path.win32.parse(hits[0]);
  const shim = path.win32.join(parsed.dir, `${parsed.name}.cmd`);
  return fs.existsSync(shim) ? shim : bin;
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

module.exports = { AGENTS, available, byId, resolve, isOnPath, terminalOptions, psQuote, windowsCommand };
