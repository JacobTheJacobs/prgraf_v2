"use strict";
/** Sidebar tree of the last review's findings.
 *
 *  Top level = findings (severity, name, risk). Expanding one shows why it
 *  scored and what it reaches, so the sidebar answers "what should I look at"
 *  without opening the graph. Clicking any row jumps to the code. */

const vscode = require("vscode");
const path = require("path");

const LEVEL_ICON = {
  critical: new vscode.ThemeIcon("flame", new vscode.ThemeColor("errorForeground")),
  high: new vscode.ThemeIcon("flame", new vscode.ThemeColor("editorWarning.foreground")),
  medium: new vscode.ThemeIcon("warning", new vscode.ThemeColor("editorWarning.foreground")),
  low: new vscode.ThemeIcon("info", new vscode.ThemeColor("editorInfo.foreground")),
};

class FindingsProvider {
  constructor() {
    this._emitter = new vscode.EventEmitter();
    this.onDidChangeTreeData = this._emitter.event;
    this.payload = null;
    this.root = null;
  }

  /** Called after each review. */
  update(payload, root) {
    this.payload = payload;
    this.root = root;
    this._emitter.fire();
  }

  clear() {
    this.payload = null;
    this._emitter.fire();
  }

  getTreeItem(item) {
    return item;
  }

  getChildren(element) {
    if (!this.payload) return [];

    if (!element) {
      const findings = this.payload.findings || [];
      if (!findings.length) {
        const none = new vscode.TreeItem("No findings above the review bar");
        none.iconPath = new vscode.ThemeIcon("pass", new vscode.ThemeColor("testing.iconPassed"));
        none.description = `${(this.payload.changed_files || []).length} file(s) changed`;
        return [none];
      }
      return findings.map((f) => this._findingNode(f));
    }

    if (element.finding) return this._detailNodes(element.finding);
    return [];
  }

  _findingNode(f) {
    const item = new vscode.TreeItem(
      f.name,
      vscode.TreeItemCollapsibleState.Collapsed
    );
    item.description = `${f.severity} · ${Number(f.risk).toFixed(2)}`;
    item.iconPath = LEVEL_ICON[f.level] || LEVEL_ICON.low;
    item.tooltip = `${f.symbol}\n${f.location}\n${(f.reasons || []).join("; ")}`;
    item.finding = f;
    item.command = this._openCommand(f.location);
    item.contextValue = "prgrafFinding";
    return item;
  }

  _detailNodes(f) {
    const rows = [];
    const add = (label, icon, description, location) => {
      const item = new vscode.TreeItem(label);
      item.iconPath = new vscode.ThemeIcon(icon);
      if (description) item.description = description;
      if (location) item.command = this._openCommand(location);
      rows.push(item);
    };

    add(
      `impacts ${f.impacted} symbol(s)`,
      "broadcast",
      `across ${f.impacted_files} file(s)`
    );
    add(
      f.tests ? `${f.tests} test(s)` : "no direct test coverage",
      f.tests ? "beaker" : "warning"
    );
    add(`${f.callers} caller(s)`, "references");

    // Coverage and caller count already have their own rows above; showing the
    // matching reason again just repeats the line back at the reader.
    const covered = /test coverage|caller/i;
    for (const reason of f.reasons || []) {
      if (!covered.test(reason)) add(reason, "circle-small-filled");
    }

    const top = (f.top_impacted || []).slice(0, 8);
    for (const qn of top) {
      const short = String(qn).split("::").pop();
      const file = String(qn).split("::")[0];
      add(short, "symbol-method", "check first", `${file}:1`);
    }
    return rows;
  }

  /** location is "path/to/file.py:LINE" (repo-relative). */
  _openCommand(location) {
    if (!this.root || !location) return undefined;
    const idx = String(location).lastIndexOf(":");
    const file = idx > 1 ? location.slice(0, idx) : location;
    const line = idx > 1 ? parseInt(location.slice(idx + 1), 10) || 1 : 1;
    const uri = vscode.Uri.file(path.join(this.root, file));
    return {
      command: "vscode.open",
      title: "Open",
      arguments: [uri, { selection: new vscode.Range(line - 1, 0, line - 1, 0) }],
    };
  }
}

module.exports = { FindingsProvider };
