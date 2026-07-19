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
    vscode.commands.registerCommand("prgraf.reviewPick", () => runReview(context, true))
  );
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
          panel.webview.html = webDir
            ? renderHtml(webDir, payload)
            : errorHtml(
                "prgraf package not found for the configured interpreter.\n" +
                "Run: pip install -e .   (or set prgraf.pythonPath)"
              );
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

function deactivate() {}

module.exports = { activate, deactivate };
