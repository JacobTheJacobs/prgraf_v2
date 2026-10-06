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
const { SidebarProvider } = require("./sidebarView");
const agents = require("./agents");

let sidebar = null;
/** Last successful review, so "Open graph" and "Explain" need no re-run. */
let last = { payload: null, root: "", base: "", head: "" };

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

/** Sentinel the engine reads as "the working tree", matching diff.WORKTREE. */
const WORKTREE = "WORKTREE";

/** Same pattern the engine validates refs with (diff._SAFE_GIT_REF). */
const SAFE_REF = /^[A-Za-z0-9_.~^/@{}\-]+$/;

/** Problem with a base/head pair, or "" when both are safe to use. */
function refError(base, head) {
  if (!SAFE_REF.test(base || "")) return `Unsafe base ref: ${JSON.stringify(base)}`;
  if (head !== WORKTREE && !SAFE_REF.test(head || "")) return `Unsafe head ref: ${JSON.stringify(head)}`;
  return "";
}

/** Run bookkeeping. Each review takes a token; only the newest token may
 *  publish to `last` and the sidebar, so a slow older run (or one started
 *  before the repo changed) cannot overwrite fresher results. */
let runSeq = 0;
let activeRuns = 0;

function beginRun() {
  activeRuns += 1;
  return ++runSeq;
}

function isCurrent(token) {
  return token === runSeq;
}

/** Busy means "any run in flight", not "the run that just ended". */
function endRun() {
  activeRuns = Math.max(0, activeRuns - 1);
  if (sidebar) sidebar.update({ busy: activeRuns > 0 });
}

/** Panels the user closed. Writing to a disposed webview throws. */
const disposedPanels = new WeakSet();

function trackPanel(panel) {
  panel.onDidDispose(() => disposedPanels.add(panel));
  return panel;
}

function alive(panel) {
  return !disposedPanels.has(panel);
}

function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand("prgraf.review", () => runReview(context, false)),
    vscode.commands.registerCommand("prgraf.reviewPick", () => runReview(context, true)),
    vscode.commands.registerCommand("prgraf.reviewInChat", () => reviewInChat(context)),
    vscode.commands.registerCommand("prgraf.chooseModel", () => chooseModel()),
    vscode.commands.registerCommand("prgraf.chooseAgent", () => chooseAgent()),
    vscode.commands.registerCommand("prgraf.writeMcpConfig", () => writeMcpConfig(context))
  );

  // Activity-bar view. It owns the whole loop — choose what to review, run it,
  // read the findings — so the extension is usable without the palette.
  sidebar = new SidebarProvider(context.extensionUri, {
    pickRepo: () => pickRepo(context),
    pickAgent: () => pickAgent(),
    pickBase: () => pickBase(context),
    review: () => sidebarReview(context),
    openGraph: () => openGraphPanel(context),
    askAgent: () => vscode.commands.executeCommand("prgraf.reviewInChat"),
    widen: () => sidebarReview(context, defaultBranchOf(currentRoot(context) || "")),
    open: (file, line) => openInEditor(currentRoot(context), file, line),
  });
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("prgraf.findings", sidebar)
  );
  syncSidebar(context);

  registerMcpServer(context);
}

/** Push host-side selections (repo, agent, base) into the view. */
function syncSidebar(context) {
  if (!sidebar) return;
  const cfg = vscode.workspace.getConfiguration("prgraf");
  const agent = agents.resolve(cfg.get("agent", "auto"));
  sidebar.update({
    repo: currentRoot(context) || "",
    agentLabel: agent ? agent.label : "none found",
    base: baseLabel(cfg),
  });
}

/** Repo picker — the same candidates the auto-detector considers, plus Browse. */
async function pickRepo(context) {
  const folders = vscode.workspace.workspaceFolders || [];
  const candidates = [];
  for (const folder of folders) {
    const root = folder.uri.fsPath;
    if (isProject(root)) candidates.push(root);
    candidates.push(...childProjects(root));
  }

  const current = currentRoot(context);
  const items = [...new Set(candidates)]
    .sort((a, b) => path.basename(a).localeCompare(path.basename(b)))
    .map((dir) => ({
      label: path.basename(dir),
      description: dir === current ? "current" : "",
      detail: dir,
      dir,
    }));
  items.push({ label: "$(folder-opened) Browse…", detail: "pick any folder on disk", dir: null });

  const pick = await vscode.window.showQuickPick(items, {
    placeHolder: "Which repository should prgraf review?",
    matchOnDetail: true,
  });
  if (!pick) return;

  let dir = pick.dir;
  if (!dir) {
    const chosen = await vscode.window.showOpenDialog({
      canSelectFolders: true,
      canSelectFiles: false,
      openLabel: "Review this repository",
    });
    if (!chosen || !chosen.length) return;
    dir = chosen[0].fsPath;
  }

  await context.workspaceState.update(LAST_ROOT_KEY, dir);
  if (mcpDidChange) mcpDidChange.fire(); // the agent's tools follow the repo
  // A new repo invalidates findings from the old one — showing them under a
  // different repo name is worse than showing nothing. Bumping the sequence
  // makes any run still in flight for the old repo stale.
  runSeq += 1;
  last = { payload: null, root: "", base: "", head: "" };
  sidebar.update({ payload: null, error: "" });
  syncSidebar(context);
}

/** Provider picker, from the sidebar. Delegates to the same command the
 *  palette uses so there is one implementation of "choose an agent". */
async function pickAgent() {
  await chooseAgent();
  syncSidebarFromConfig();
}

function syncSidebarFromConfig() {
  if (!sidebar) return;
  const cfg = vscode.workspace.getConfiguration("prgraf");
  const agent = agents.resolve(cfg.get("agent", "auto"));
  sidebar.update({
    agentLabel: agent ? agent.label : "none found",
    base: baseLabel(cfg),
  });
}

/** How the range reads in the sidebar: a ref, or the word for the worktree. */
function baseLabel(cfg) {
  return cfg.get("head", "HEAD") === WORKTREE ? "Uncommitted" : cfg.get("base", "HEAD~1");
}

/** What to diff against. Presets cover the cases that actually come up.
 *  "Uncommitted" is base=HEAD with the WORKTREE sentinel as head — the same
 *  pair the CLI's --uncommitted builds, so both surfaces mean one thing. */
async function pickBase(context) {
  const root = currentRoot(context);
  const items = [
    { label: "HEAD~1", description: "the last commit", base: "HEAD~1", head: "HEAD" },
    { label: "HEAD~3", description: "the last three commits", base: "HEAD~3", head: "HEAD" },
    {
      label: "Uncommitted",
      description: "working tree, nothing committed yet",
      base: "HEAD",
      head: WORKTREE,
    },
  ];
  if (root) {
    const branch = defaultBranchOf(root);
    items.push({
      label: branch,
      description: "the whole branch, as a PR would see it",
      base: branch,
      head: "HEAD",
    });
  }
  items.push({ label: "$(edit) Custom…", description: "any git ref", base: null, head: "HEAD" });

  const pick = await vscode.window.showQuickPick(items, {
    placeHolder: "Review changes since…",
  });
  if (!pick) return;

  let base = pick.base;
  if (base === null) {
    base = await vscode.window.showInputBox({
      prompt: "Base ref to diff against",
      value: vscode.workspace.getConfiguration("prgraf").get("base", "HEAD~1"),
      validateInput: (v) => (SAFE_REF.test(v) ? "" : "Not a valid git ref"),
    });
    if (!base) return;
  }
  const cfg = vscode.workspace.getConfiguration("prgraf");
  await cfg.update("base", base, vscode.ConfigurationTarget.Global);
  await cfg.update("head", pick.head, vscode.ConfigurationTarget.Global);
  syncSidebarFromConfig();
}

function openInEditor(root, file, line) {
  if (!root || !file) return;
  const uri = vscode.Uri.file(path.join(root, file));
  const at = Math.max(0, (line || 1) - 1);
  vscode.window
    .showTextDocument(uri, { selection: new vscode.Range(at, 0, at, 0) })
    .then(undefined, () => {
      vscode.window.setStatusBarMessage(`prgraf: could not open ${file}`, 3000);
    });
}

/** Review driven from the sidebar: no panel, findings land in the view. */
async function sidebarReview(context, baseOverride) {
  const root = currentRoot(context) || (await resolveProjectRoot(context, false));
  if (!root) {
    sidebar.update({ error: "Open a folder, or pick a repository above." });
    return;
  }
  await context.workspaceState.update(LAST_ROOT_KEY, root);
  if (mcpDidChange) mcpDidChange.fire();

  const cfg = vscode.workspace.getConfiguration("prgraf");
  const base = baseOverride || cfg.get("base", "HEAD~1");
  const head = cfg.get("head", "HEAD");

  const token = beginRun();
  sidebar.update({ repo: root, busy: true, error: "", payload: null });
  try {
    const result = await runEngine(cfg.get("pythonPath", "python"), root, base, head);
    if (!isCurrent(token)) return; // superseded; the newer run owns the view
    if (result.error) {
      sidebar.update({ error: result.error });
      return;
    }
    last = { payload: result.payload, root, base, head };
    sidebar.update({ payload: result.payload, error: "" });
  } finally {
    endRun();
  }
}

/** Render the last review's graph in a panel, without recomputing it. */
function openGraphPanel(context) {
  if (!last.payload) {
    // Review the repo the sidebar shows, not whatever detection would pick.
    runReview(context, false, undefined, currentRoot(context));
    return;
  }
  const { payload, root } = last;
  const python = vscode.workspace.getConfiguration("prgraf").get("pythonPath", "python");
  const webDir = locateWebDir(python, root);
  const panel = trackPanel(vscode.window.createWebviewPanel(
    "prgraf.graph",
    `prgraf · ${path.basename(root)}`,
    vscode.ViewColumn.Beside,
    { enableScripts: true, retainContextWhenHidden: true }
  ));
  if (!webDir) {
    panel.webview.html = errorHtml(
      "prgraf package not found for the configured interpreter.\n" +
      "Run: pip install -e .   (or set prgraf.pythonPath)"
    );
    return;
  }
  panel.webview.html = renderHtml(webDir, payload);
  wirePanelMessages(context, panel, root, payload);
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

/** Hand the review to whichever agent the user picked. */
async function reviewInChat(context) {
  // The sidebar's pick wins; detection only runs when nothing is picked yet
  // (and remembers its own answer), so this never overwrites the selection.
  const root = currentRoot(context) || (await resolveProjectRoot(context, false));
  if (!root) return;
  if (mcpDidChange) mcpDidChange.fire(); // re-resolve the server for this root

  const cfg = vscode.workspace.getConfiguration("prgraf");
  const agent = agents.resolve(cfg.get("agent", "auto"));
  if (!agent) {
    vscode.window.showWarningMessage(
      "prgraf: no agent available. Install one (claude, codex, gemini, grok, opencode) " +
      "or use VS Code chat."
    );
    return;
  }

  const base = cfg.get("base", "HEAD~1");
  const head = cfg.get("head", "HEAD");
  // The prompt reaches a terminal, so refuse anything the engine would too.
  const bad = refError(base, head);
  if (bad) {
    vscode.window.showErrorMessage(`prgraf: ${bad}. Fix prgraf.base / prgraf.head.`);
    return;
  }
  const range = head === WORKTREE
    ? `the uncommitted changes (working tree vs ${base})`
    : `${base}..${head}`;
  const prompt =
    `Review the blast radius of ${range} in this repo using the prgraf MCP ` +
    `tools. Start with minimal_context; if the risk is not low, call review_diff ` +
    `with detail "standard", then trace_symbol on each finding. Report what could ` +
    `break and what to check first. Keep it under 5 tool calls.`;

  if (agent.id === "vscode") {
    try {
      await vscode.commands.executeCommand("workbench.action.chat.open", {
        query: prompt, mode: "agent",
      });
    } catch (_) {
      await vscode.commands.executeCommand("workbench.action.chat.open", prompt);
    }
    return;
  }

  // CLI agents read project MCP config, so make sure prgraf's tools are there.
  await ensureMcpConfig(root);
  // The agent is launched directly with an argument array — never typed into
  // a shell — so nothing in the prompt is interpreted.
  const terminal = vscode.window.createTerminal(agents.terminalOptions(agent, prompt, root));
  terminal.show();
}

/** Choose which agent handles the handoff — only offers what is installed. */
async function chooseAgent() {
  const found = agents.available();
  const items = found.map((a) => ({
    label: a.label,
    description: a.bin ? a.bin : "built in",
    value: a.id,
  }));
  items.unshift({ label: "Auto", description: "first CLI found", value: "auto" });

  const pick = await vscode.window.showQuickPick(items, {
    placeHolder: "Which agent should review the findings?",
  });
  if (!pick) return;
  await vscode.workspace
    .getConfiguration("prgraf")
    .update("agent", pick.value, vscode.ConfigurationTarget.Global);
  vscode.window.showInformationMessage(`prgraf agent: ${pick.label}`);
}

/** Write .mcp.json if absent so CLI agents can see the graph tools. */
async function ensureMcpConfig(root) {
  const target = path.join(root, ".mcp.json");
  let existing = {};
  if (fs.existsSync(target)) {
    try {
      existing = JSON.parse(fs.readFileSync(target, "utf8"));
    } catch (_) {
      return; // malformed and not ours to fix silently
    }
    if (existing.mcpServers && existing.mcpServers.prgraf) return;
  }
  const python = vscode.workspace.getConfiguration("prgraf").get("pythonPath", "python");
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
      // An interpreter that hangs on import must not freeze the extension host.
      { cwd, encoding: "utf8", timeout: 10 * 1000 }
    ).trim();
    if (out && fs.existsSync(out)) return out;
  } catch (err) {
    console.warn("prgraf: could not locate web assets", err && err.message);
  }
  return null;
}

/** The branch a PR would target: origin's default, else main/master. */
function defaultBranchOf(root) {
  const run = (args) => {
    try {
      return cp.execFileSync("git", args, { cwd: root, encoding: "utf8", timeout: 10 * 1000 }).trim();
    } catch (_) {
      return "";
    }
  };
  const head = run(["symbolic-ref", "refs/remotes/origin/HEAD"]);
  if (head) return `origin/${head.split("/").pop()}`;
  for (const candidate of ["origin/main", "origin/master", "main", "master"]) {
    if (run(["rev-parse", "--verify", candidate])) return candidate;
  }
  return "HEAD~5";
}

/**
 * Run the engine once. Resolves to {payload} or {error} — never throws, and
 * never touches a view, so the sidebar and the graph panel share exactly one
 * implementation of "review this range".
 */
function runEngine(python, root, base, head, onProgress) {
  return new Promise((resolve) => {
    const args = [
      "-m", "codebase_rag.graph.export_cli",
      "--repo", root, "--base", base, "--head", head,
    ];
    const opts = {
      cwd: root,
      maxBuffer: 64 * 1024 * 1024,
      // Never hang forever: a runaway index should surface, not spin.
      timeout: 5 * 60 * 1000,
    };
    const child = cp.execFile(python, args, opts, (err, stdout, stderr) => {
      if (err && err.killed) {
        resolve({
          error:
            "Timed out after 5 minutes.\n\nThis folder may be larger than expected. " +
            "Pick a specific project with the Repo field.",
        });
        return;
      }
      if (err && !stdout) {
        resolve({ error: `Engine failed.\n\n${stderr || err.message}` });
        return;
      }
      let payload;
      try {
        payload = JSON.parse(stdout);
      } catch (e) {
        resolve({ error: `Could not parse engine output.\n\n${stderr || String(e)}` });
        return;
      }
      if (payload.status === "error") {
        resolve({ error: payload.message || "Review failed." });
        return;
      }
      payload.repo = root;
      payload.base = base;
      payload.head = head;
      resolve({ payload });
    });

    // The engine logs progress to stderr; echo the latest line so a long
    // build reads as working, not frozen.
    if (child.stderr && onProgress) {
      child.stderr.on("data", (chunk) => {
        const line = String(chunk).trim().split("\n").pop();
        if (line) onProgress(line.slice(0, 160));
      });
    }
  });
}

/** Panel-side actions. Split out so both entry points wire the same handlers. */
function wirePanelMessages(context, panel, root, payload) {
  panel.webview.onDidReceiveMessage((msg) => {
    if (!msg) return;
    // Explain / hand-off requested from the panel itself, so the LLM review is
    // reachable where the findings are rather than only from the palette.
    // Explain uses this panel's own payload, and always answers: the webview
    // is showing "reading the graph…" until it hears back.
    if (msg.type === "explain") {
      if (payload) {
        const model = vscode.workspace.getConfiguration("prgraf").get("model", "");
        narrate(panel, payload, model);
      } else if (alive(panel)) {
        panel.webview.postMessage({ type: "summary", text: "", error: "No review to explain yet." });
      }
      return;
    }
    if (msg.type === "askAgent") {
      vscode.commands.executeCommand("prgraf.reviewInChat");
      return;
    }
    if (msg.type === "widen") {
      // HEAD~1 often lands on a config-only commit, which correctly finds
      // nothing. Re-run against the default branch so the panel can show the
      // whole branch's blast radius instead of a dead end.
      runReview(context, false, defaultBranchOf(root), root);
      return;
    }
    if (msg.type === "open" && msg.file) {
      openInEditor(root, msg.file, msg.line);
    }
  });
}

async function runReview(context, forcePick, baseOverride, rootOverride) {
  const root = rootOverride || (await resolveProjectRoot(context, forcePick));
  if (!root) return;
  // Remember it so the MCP server (and the agent) target the same project.
  await context.workspaceState.update(LAST_ROOT_KEY, root);
  if (mcpDidChange) mcpDidChange.fire();
  syncSidebar(context);

  const cfg = vscode.workspace.getConfiguration("prgraf");
  const python = cfg.get("pythonPath", "python");
  const base = baseOverride || cfg.get("base", "HEAD~1");
  const head = cfg.get("head", "HEAD");

  const panel = trackPanel(vscode.window.createWebviewPanel(
    "prgraf.graph",
    `prgraf · ${path.basename(root)}`,
    vscode.ViewColumn.Beside,
    { enableScripts: true, retainContextWhenHidden: true }
  ));
  // The user may close the panel mid-review; the sidebar still gets the result.
  const show = (html) => { if (alive(panel)) panel.webview.html = html; };
  show(loadingHtml(path.basename(root)));

  const token = beginRun();
  if (sidebar) sidebar.update({ repo: root, busy: true, error: "", payload: null });
  try {
    const result = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: `prgraf: reviewing ${path.basename(root)}…` },
      () =>
        runEngine(python, root, base, head, (line) => {
          show(loadingHtml(path.basename(root), line));
        })
    );
    // A stale run still fills its own panel, but leaves `last` and the
    // sidebar to the newer one.
    const current = isCurrent(token);

    if (result.error) {
      show(errorHtml(result.error));
      if (sidebar && current) sidebar.update({ error: result.error });
      return;
    }

    const webDir = locateWebDir(python, root);
    if (!webDir) {
      const msg =
        "prgraf package not found for the configured interpreter.\n" +
        "Run: pip install -e .   (or set prgraf.pythonPath)";
      show(errorHtml(msg));
      if (sidebar && current) sidebar.update({ error: msg });
      return;
    }

    if (current) {
      last = { payload: result.payload, root, base, head };
      if (sidebar) sidebar.update({ payload: result.payload, error: "" });
    }
    if (!alive(panel)) return;
    panel.webview.html = renderHtml(webDir, result.payload);
    // Narration is additive: the graph is already usable without it.
    if (cfg.get("summary", true) && (result.payload.findings || []).length) {
      narrate(panel, result.payload, cfg.get("model", ""));
    }
    wirePanelMessages(context, panel, root, result.payload);
  } finally {
    endRun();
  }
}

/** Ask the language model to explain the already-computed findings, then push
 *  the prose into the panel. Failure is non-fatal — the graph stands alone. */
function narrate(panel, payload, preferred) {
  if (!alive(panel)) return;
  const cts = new vscode.CancellationTokenSource();
  const sub = panel.onDidDispose(() => cts.cancel());
  panel.webview.postMessage({ type: "summary-pending" });
  const reply = (text, error) => {
    sub.dispose();
    if (alive(panel)) panel.webview.postMessage({ type: "summary", text, error });
  };
  callSummary(vscode, payload, cts.token, preferred).then(
    (res) => reply(res.text || "", res.error || ""),
    (err) => reply("", (err && err.message) || "Summary failed.")
  );
}

function deactivate() {}

module.exports = { activate, deactivate };
