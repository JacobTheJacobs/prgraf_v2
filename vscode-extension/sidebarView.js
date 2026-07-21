"use strict";
/** The activity-bar sidebar: pick a repo and an agent, run a review, read the
 *  findings. A webview rather than a TreeView because the tree could not show
 *  what gets reviewed — only what a review found, after the fact.
 *
 *  All state lives on the host side; the webview is a pure function of the
 *  state message it is sent. */

const vscode = require("vscode");
const path = require("path");
const fs = require("fs");
const { nonce } = require("./webview");

class SidebarProvider {
  /** @param {vscode.Uri} extensionUri @param {object} handlers */
  constructor(extensionUri, handlers) {
    this.extensionUri = extensionUri;
    this.handlers = handlers;
    this.view = null;
    this.state = {
      repo: "",
      agentLabel: "",
      base: "HEAD~1",
      busy: false,
      payload: null,
      error: "",
    };
  }

  resolveWebviewView(view) {
    this.view = view;
    view.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.extensionUri, "media")],
    };
    view.webview.html = this.html(view.webview);
    view.webview.onDidReceiveMessage((msg) => this.onMessage(msg));
  }

  onMessage(msg) {
    if (!msg) return;
    const h = this.handlers;
    switch (msg.type) {
      case "ready":
        this.post();
        break;
      case "pick":
        if (msg.what === "repo") h.pickRepo();
        else if (msg.what === "agent") h.pickAgent();
        else if (msg.what === "base") h.pickBase();
        break;
      case "review":
        h.review();
        break;
      case "graph":
        h.openGraph();
        break;
      case "askAgent":
        h.askAgent();
        break;
      case "widen":
        h.widen();
        break;
      case "open":
        h.open(msg.file, msg.line);
        break;
    }
  }

  /** Merge a partial state and repaint. */
  update(patch) {
    Object.assign(this.state, patch);
    this.post();
  }

  post() {
    if (this.view) this.view.webview.postMessage({ type: "state", state: this.state });
  }

  html(webview) {
    const mediaDir = vscode.Uri.joinPath(this.extensionUri, "media");
    const css = fs.readFileSync(path.join(mediaDir.fsPath, "sidebar.css"), "utf8");
    const script = webview.asWebviewUri(vscode.Uri.joinPath(mediaDir, "sidebar.js"));
    const n = nonce();
    // Same split as the graph panel: scripts are nonce-locked, styles are
    // inlined from disk. Nothing loads from the network.
    const csp =
      `default-src 'none'; style-src 'unsafe-inline'; ` +
      `script-src 'nonce-${n}' ${webview.cspSource};`;
    return `<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<style>${css}</style></head>
<body><div id="root"></div>
<script nonce="${n}" src="${script}"></script>
</body></html>`;
  }
}

module.exports = { SidebarProvider };
