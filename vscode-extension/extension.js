"use strict";
/**
 * prgraf VS Code extension — a thin webview over the same renderer the web
 * app uses. On command it:
 *   1. spawns the Python engine to review the working diff -> JSON
 *   2. loads web/{style.css, app.js} from the installed prgraf package
 *   3. injects the JSON and renders it in the webview (app.js embedded mode)
 *
 * There is no second renderer and no bundled Python. One codebase, two hosts.
 */

const vscode = require("vscode");
const cp = require("child_process");
const path = require("path");
const fs = require("fs");
const { renderHtml, loadingHtml, errorHtml } = require("./webview");

function activate(context) {
  context.subscriptions.push(
    vscode.commands.registerCommand("prgraf.review", () => runReview(context))
  );
}

function repoRoot() {
  const folders = vscode.workspace.workspaceFolders;
  if (!folders || !folders.length) return null;
  return folders[0].uri.fsPath;
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

function runReview(context) {
  const root = repoRoot();
  if (!root) {
    vscode.window.showErrorMessage("prgraf: open a folder/repo first.");
    return;
  }
  const cfg = vscode.workspace.getConfiguration("prgraf");
  const python = cfg.get("pythonPath", "python");
  const base = cfg.get("base", "HEAD~1");
  const head = cfg.get("head", "HEAD");

  const panel = vscode.window.createWebviewPanel(
    "prgraf.graph",
    "prgraf · blast radius",
    vscode.ViewColumn.Beside,
    { enableScripts: true, retainContextWhenHidden: true }
  );
  panel.webview.html = loadingHtml();

  vscode.window.withProgress(
    { location: vscode.ProgressLocation.Notification, title: "prgraf: reviewing blast radius…" },
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
              "Timed out after 5 minutes.\n\nThe folder you opened may contain many " +
              "projects rather than one repository. Open the specific project instead."
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
            : errorHtml("prgraf package not found for the configured interpreter.\nRun: pip install -e . (see prgraf.pythonPath).");
          resolve();
        });

        // The engine logs progress to stderr; echo the latest line into the
        // loading screen so a long build reads as working, not frozen.
        if (child.stderr) {
          child.stderr.on("data", (chunk) => {
            const line = String(chunk).trim().split("\n").pop();
            if (line) panel.webview.html = loadingHtml(line.slice(0, 160));
          });
        }
      })
  );

  // Let the graph open the clicked symbol's file in the editor.
  panel.webview.onDidReceiveMessage((msg) => {
    if (msg && msg.type === "open" && msg.file) {
      const uri = vscode.Uri.file(path.join(root, msg.file));
      vscode.window.showTextDocument(uri, {
        viewColumn: vscode.ViewColumn.One,
        selection: msg.line ? new vscode.Range(msg.line - 1, 0, msg.line - 1, 0) : undefined,
      }).then(undefined, () => { /* file may be outside the tree */ });
    }
  });
}

function deactivate() {}

module.exports = { activate, deactivate };
