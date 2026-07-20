"use strict";
/**
 * prgraf VS Code extension — a thin webview over the same renderer the web
 * app uses. On command it:
 *   1. resolves which project to graph (auto when obvious, asks when not)
 *   2. spawns the Python engine to review that repo's diff -> JSON
 *   3. renders it with codebase_rag/web/app.js in embedded mode
 *
 * There is no second renderer and no bundled Python. One codebase, two hosts.
 */

const vscode = require("vscode");
const cp = require("child_process");
const path = require("path");
const fs = require("fs");
const { renderHtml, loadingHtml, errorHtml } = require("./webview");
const { callSummary, listModels } = require("./summary");
const { FindingsProvider } = require("./findingsView");

let findingsProvider = null;

/** Files that mark a directory as "a project you'd review", not a container.
 *  `.git` is checked as a direct child on purpose: walking up finds the
 *  enclosing repo, which on some machines is the whole drive. */
const PROJECT_MARKERS = [
  ".git", "pyproject.toml", "setup.py", "package.json",
  "go.mod", "Cargo.toml", "pom.xml", "build.gradle", "requirements.txt",
];

const SKIP_DIRS = new Set([
  "node_modules", "venv", ".venv", "dist", "build", "target",
  "vendor", "__pycache__", "temp_repos", "archive", "tmp",
]);

const LAST_ROOT_KEY = "prgraf.lastRoot";

function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand("prgraf.review", () => runReview(context, false)),
    vscode.commands.registerCommand("prgraf.reviewPick", () => runReview(context, true)),
    vscode.commands.registerCommand("prgraf.reviewInChat", () => reviewInChat(context)),
    vscode.commands.registerCommand("prgraf.chooseModel", () => chooseModel()),
    vscode.commands.registerCommand("prgraf.writeMcpConfig", () => writeMcpConfig(context))
  );

  // Activity-bar view: findings survive after the graph panel is closed, and
  // give the extension a home you can find without the command palette.
  findingsProvider = new FindingsProvider();
  context.subscriptions.push(
    vscode.window.registerTreeDataProvider("prgraf.findings", findingsProvider)
  );

  registerMcpServer(context);
}

/** Let the user pick which chat model narrates — any vendor the editor has. */
async function chooseModel() {
  const models = await listModels(vscode);
  if (!models.length) {
    vscode.window.showWarningMessage(
      "prgraf: no chat models are available to this editor. The graph works without one."
    );
    return;
  }
  const items = models.map((m) => ({
    label: m.name || `${m.vendor}/${m.family}`,
    description: `${m.vendor}/${m.family}`,
    detail: m.maxInputTokens ? `${m.maxInputTokens} max input tokens` : "",
    value: `${m.vendor}/${m.family}`,
  }));
  items.unshift({ label: "Auto", description: "first available", detail: "", value: "" });

  const pick = await vscode.window.showQuickPick(items, {
    placeHolder: "Which model should write the impact summary?",
  });
  if (!pick) return;
  await vscode.workspace
    .getConfiguration("prgraf")
    .update("model", pick.value, vscode.ConfigurationTarget.Global);
  vscode.window.showInformationMessage(`prgraf summary model: ${pick.label}`);
}

/**
 * Write .mcp.json so agents that read project MCP config (Claude Code,
 * Cursor, others) can use the same prgraf server. The VS Code provider
 * only covers VS Code's own agent.
 */
async function writeMcpConfig(context) {
  const root = await resolveProjectRoot(context, false);
  if (!root) return;
  const python = vscode.workspace.getConfiguration("prgraf").get("pythonPath", "python");
  const target = path.join(root, ".mcp.json");

  let existing = {};
  if (fs.existsSync(target)) {
    try {
      existing = JSON.parse(fs.readFileSync(target, "utf8"));
    } catch (_) {
      const overwrite = await vscode.window.showWarningMessage(
        `${target} exists but is not valid JSON. Overwrite?`, "Overwrite", "Cancel"
      );
      if (overwrite !== "Overwrite") return;
    }
  }
  const merged = {
    ...existing,
    mcpServers: {
      ...(existing.mcpServers || {}),
      prgraf: {
        command: python,
        args: ["-m", "codebase_rag.graph.mcp_server"],
        env: { PRGRAF_REPO: root },
      },
    },
  };
  fs.writeFileSync(target, JSON.stringify(merged, null, 2) + "\n", "utf8");
  const open = await vscode.window.showInformationMessage(
    `prgraf: wrote ${path.basename(target)} — agents reading project MCP config can now use the graph tools.`,
    "Open"
  );
  if (open === "Open") {
    vscode.window.showTextDocument(vscode.Uri.file(target));
  }
}

/**
 * Expose prgraf's MCP server to the editor's agent.
 *
 * This is the primary LLM path: the agent calls the graph tools itself
 * (minimal_context -> review_diff -> trace_symbol) under the token discipline
 * baked into the server's prompt, rather than us shipping it one pre-built
 * blob. Same server Claude Code and Cursor use — one implementation.
 */
function registerMcpServer(context) {
  if (!vscode.lm || typeof vscode.lm.registerMcpServerDefinitionProvider !== "function") {
    return; // older VS Code: the panel still works, just no agent integration
  }
  const emitter = new vscode.EventEmitter();
  context.subscriptions.push(emitter);
  mcpDidChange = emitter;

  try {
    context.subscriptions.push(
      vscode.lm.registerMcpServerDefinitionProvider("prgraf.mcpProvider", {
        onDidChangeMcpServerDefinitions: emitter.event,
        provideMcpServerDefinitions: () => {
          const root = currentRoot(context);
          if (!root) return [];
          const python = vscode.workspace.getConfiguration("prgraf").get("pythonPath", "python");
          return [
            new vscode.McpStdioServerDefinition(
              "prgraf",
              python,
              ["-m", "codebase_rag.graph.mcp_server"],
              // The server reads the repo from PRGRAF_REPO, so the agent's
              // tools operate on the project the user actually picked.
              { PRGRAF_REPO: root },
              undefined
            ),
          ];
        },
      })
    );
  } catch (err) {
    console.warn("prgraf: MCP registration failed", err);
  }
}

let mcpDidChange = null;

/** Best known project root without prompting. */
function currentRoot(context) {
  const remembered = context.workspaceState.get(LAST_ROOT_KEY);
  if (remembered && fs.existsSync(remembered)) return remembered;
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || !folders.length) return null;
  const first = folders[0].uri.fsPath;
  if (isProject(first)) return first;
  const kids = childProjects(first);
  return kids.length === 1 ? kids[0] : null;
}

/** Hand the review to the agent, which uses the MCP tools. */
async function reviewInChat(context) {
  const root = await resolveProjectRoot(context, false);
  if (!root) return;
  await context.workspaceState.update(LAST_ROOT_KEY, root);
  if (mcpDidChange) mcpDidChange.fire(); // re-resolve the server for this root

  const cfg = vscode.workspace.getConfiguration("prgraf");
  const base = cfg.get("base", "HEAD~1");
  const query =
    `Review the blast radius of ${base}..HEAD in ${path.basename(root)} using the ` +
    `prgraf MCP tools. Start with minimal_context; if the risk is not low, call ` +
    `review_diff with detail "standard", then trace_symbol on each finding. ` +
    `Report what could break and what to check first. Keep it under 5 tool calls.`;

  try {
    await vscode.commands.executeCommand("workbench.action.chat.open", {
      query,
      mode: "agent",
    });
  } catch (_) {
    await vscode.commands.executeCommand("workbench.action.chat.open", query);
  }
}

function isProject(dir) {
  try {
    return PROJECT_MARKERS.some((m) => fs.existsSync(path.join(dir, m)));
  } catch (_) {
    return false;
  }
}

/** Immediate subdirectories that look like projects. Two levels deep, because
 *  people commonly nest as work/<org>/<repo>. */
function childProjects(dir, depth = 2) {
  const found = [];
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch (_) {
    return found;
  }
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    if (entry.name.startsWith(".") || SKIP_DIRS.has(entry.name)) continue;
    const child = path.join(dir, entry.name);
    if (isProject(child)) {
      found.push(child);
    } else if (depth > 1) {
      found.push(...childProjects(child, depth - 1));
    }
  }
  return found;
}

/**
 * Which folder should we graph?
 *   - an explicit prgraf.projectRoot setting always wins
 *   - a workspace folder that is itself a project is used silently
 *   - a container of projects prompts once, then remembers
 */
async function resolveProjectRoot(context, forcePick) {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || !folders.length) {
    vscode.window.showErrorMessage("prgraf: open a folder or repository first.");
    return null;
  }

  const configured = vscode.workspace.getConfiguration("prgraf").get("projectRoot", "");
  if (configured && !forcePick) {
    const abs = path.isAbsolute(configured)
      ? configured
      : path.join(folders[0].uri.fsPath, configured);
    if (fs.existsSync(abs)) return abs;
    vscode.window.showWarningMessage(`prgraf: projectRoot "${configured}" not found; detecting instead.`);
  }

  const roots = folders.map((f) => f.uri.fsPath);

  // Common case: one folder, and it is a project. No prompt.
  if (!forcePick && roots.length === 1 && isProject(roots[0])) return roots[0];

  const candidates = [];
  for (const root of roots) {
    if (isProject(root)) candidates.push(root);
    else candidates.push(...childProjects(root));
  }

  if (!candidates.length) {
    // Nothing detected — use the folder and let the engine's scope guard
    // produce a specific error rather than guessing further.
    return roots[0];
  }
  if (candidates.length === 1 && !forcePick) return candidates[0];

  const last = context.workspaceState.get(LAST_ROOT_KEY);
  const ordered = candidates.slice().sort((a, b) => {
    if (a === last) return -1;
    if (b === last) return 1;
    return path.basename(a).localeCompare(path.basename(b));
  });

  const items = ordered.map((dir) => ({
    label: path.basename(dir),
    description: dir === last ? "last used" : "",
    detail: dir,
    dir,
  }));
  const pick = await vscode.window.showQuickPick(items, {
    placeHolder: "Which project should prgraf graph?",
    matchOnDetail: true,
  });
  if (!pick) return null;
  await context.workspaceState.update(LAST_ROOT_KEY, pick.dir);
  return pick.dir;
}

/** Locate the prgraf package's web/ dir via the interpreter, so the webview
 *  always loads the exact assets shipped with the engine it's calling. */
function locateWebDir(python, cwd) {
  try {
    const out = cp.execFileSync(
      python,
      ["-c", "import codebase_rag, os; print(os.path.join(os.path.dirname(codebase_rag.__file__), 'web'))"],
      { cwd, encoding: "utf8" }
    ).trim();
    if (out && fs.existsSync(out)) return out;
  } catch (_) { /* fall through */ }
  return null;
}

async function runReview(context, forcePick) {
  const root = await resolveProjectRoot(context, forcePick);
  if (!root) return;
  // Remember it so the MCP server (and the agent) target the same project.
  await context.workspaceState.update(LAST_ROOT_KEY, root);
  if (mcpDidChange) mcpDidChange.fire();

  const cfg = vscode.workspace.getConfiguration("prgraf");
  const python = cfg.get("pythonPath", "python");
  const base = cfg.get("base", "HEAD~1");
  const head = cfg.get("head", "HEAD");

  const panel = vscode.window.createWebviewPanel(
    "prgraf.graph",
    `prgraf · ${path.basename(root)}`,
    vscode.ViewColumn.Beside,
    { enableScripts: true, retainContextWhenHidden: true }
  );
  panel.webview.html = loadingHtml(path.basename(root));

  await vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: `prgraf: reviewing ${path.basename(root)}…` },
    () =>
      new Promise((resolve) => {
        const args = ["-m", "codebase_rag.graph.export_cli", "--repo", root, "--base", base, "--head", head];
        const opts = {
          cwd: root,
          maxBuffer: 64 * 1024 * 1024,
          // Never hang forever: a runaway index should surface, not spin.
          timeout: 5 * 60 * 1000,
        };
        const child = cp.execFile(python, args, opts, (err, stdout, stderr) => {
          if (err && err.killed) {
            panel.webview.html = errorHtml(
              "Timed out after 5 minutes.\n\nThis folder may be larger than expected. " +
              "Run 'prgraf: Review blast radius in…' to pick a specific project."
            );
            resolve();
            return;
          }
          if (err && !stdout) {
            panel.webview.html = errorHtml(`Engine failed.\n\n${stderr || err.message}`);
            resolve();
            return;
          }
          let payload;
          try {
            payload = JSON.parse(stdout);
          } catch (e) {
            panel.webview.html = errorHtml(`Could not parse engine output.\n\n${stderr || String(e)}`);
            resolve();
            return;
          }
          if (payload.status === "error") {
            panel.webview.html = errorHtml(payload.message || "Review failed.");
            resolve();
            return;
          }
          payload.repo = root;
          payload.base = base;
          payload.head = head;
          const webDir = locateWebDir(python, root);
          if (!webDir) {
            panel.webview.html = errorHtml(
              "prgraf package not found for the configured interpreter.\n" +
              "Run: pip install -e .   (or set prgraf.pythonPath)"
            );
            resolve();
            return;
          }
          panel.webview.html = renderHtml(webDir, payload);
          if (findingsProvider) findingsProvider.update(payload, root);
          // Narration is additive: the graph is already usable without it.
          if (cfg.get("summary", true) && (payload.findings || []).length) {
            narrate(panel, payload, cfg.get("model", ""));
          }
          resolve();
        });

        // The engine logs progress to stderr; echo the latest line into the
        // loading screen so a long build reads as working, not frozen.
        if (child.stderr) {
          child.stderr.on("data", (chunk) => {
            const line = String(chunk).trim().split("\n").pop();
            if (line) panel.webview.html = loadingHtml(path.basename(root), line.slice(0, 160));
          });
        }
      })
  );

  // Let the graph open the clicked symbol's file in the editor.
  panel.webview.onDidReceiveMessage((msg) => {
    if (msg && msg.type === "open" && msg.file) {
      const uri = vscode.Uri.file(path.join(root, msg.file));
      const line = Math.max(0, (msg.line || 1) - 1);
      vscode.window
        .showTextDocument(uri, {
          viewColumn: vscode.ViewColumn.One,
          selection: new vscode.Range(line, 0, line, 0),
        })
        .then(undefined, () => {
          vscode.window.setStatusBarMessage(`prgraf: could not open ${msg.file}`, 3000);
        });
    }
  });
}

/** Ask the language model to explain the already-computed findings, then push
 *  the prose into the panel. Failure is non-fatal — the graph stands alone. */
function narrate(panel, payload, preferred) {
  const cts = new vscode.CancellationTokenSource();
  panel.onDidDispose(() => cts.cancel());
  panel.webview.postMessage({ type: "summary-pending" });
  callSummary(vscode, payload, cts.token, preferred).then((res) => {
    panel.webview.postMessage({
      type: "summary",
      text: res.text || "",
      error: res.error || "",
    });
  });
}

function deactivate() {}

module.exports = { activate, deactivate };
