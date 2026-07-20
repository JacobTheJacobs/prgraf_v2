"use strict";
/** Pure webview HTML assembly — no vscode dependency, so it is unit-testable.
 *  extension.js supplies the payload and web asset dir; this builds the page
 *  that runs the same app.js in embedded mode. */

const path = require("path");
const fs = require("fs");

function nonce() {
  let s = "";
  const chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
  for (let i = 0; i < 24; i++) s += chars[Math.floor(Math.random() * chars.length)];
  return s;
}

function renderHtml(webDir, payload) {
  const css = fs.readFileSync(path.join(webDir, "style.css"), "utf8");
  const js = fs.readFileSync(path.join(webDir, "app.js"), "utf8");
  const body = fs.readFileSync(path.join(webDir, "index.html"), "utf8")
    .replace(/[\s\S]*<body>/i, "")
    .replace(/<\/body>[\s\S]*/i, "")
    .replace('<script src="app.js"></script>', "");
  const n = nonce();
  const data = JSON.stringify(payload).replace(/</g, "\\u003c");
  // Scripts are nonce-locked (the injection boundary that matters). Styles are
  // 'unsafe-inline' because app.js sets style attributes via innerHTML; a nonce
  // would block those. Data is local; symbol names are HTML-escaped in app.js.
  const csp = `default-src 'none'; style-src 'unsafe-inline'; script-src 'nonce-${n}';`;
  return `<!DOCTYPE html><html lang="en" data-theme="dark"><head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<style>${css}</style></head>
<body>${body}
<script nonce="${n}">
  window.__PRGRAF_DATA__ = ${data};
  const vscode = acquireVsCodeApi();
  window.__prgrafOpen = (file, line) => vscode.postMessage({ type: "open", file, line });
  window.__prgrafPost = (type, payload) => vscode.postMessage(Object.assign({ type }, payload || {}));
  window.__prgrafHosted = true;
</script>
<script nonce="${n}">${js}</script>
</body></html>`;
}

function shell(inner) {
  return `<!DOCTYPE html><html><head><meta charset="UTF-8"><style>
    body{font-family:system-ui,sans-serif;background:#0e1117;color:#e6edf3;
      display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
    .box{max-width:460px;padding:24px;text-align:center}
    .spin{width:26px;height:26px;border:3px solid #232a34;border-top-color:#6ea8fe;
      border-radius:50%;margin:0 auto 14px;animation:s 0.8s linear infinite}
    pre{white-space:pre-wrap;text-align:left;color:#f0883e;font-size:12px;
      background:#0a0d12;border:1px solid #232a34;border-radius:8px;padding:12px;margin-top:12px}
    .detail{margin-top:10px;font-size:11.5px;color:#64707d;
      font-family:ui-monospace,monospace;word-break:break-word}
    @keyframes s{to{transform:rotate(360deg)}}
  </style></head><body><div class="box">${inner}</div></body></html>`;
}

const esc = (s) =>
  String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

function loadingHtml(project, detail) {
  const who = project ? ` <b>${esc(project)}</b>` : "";
  return shell(
    `<div class="spin"></div><div>Building graph for${who} …</div>` +
    (detail ? `<div class="detail">${esc(detail)}</div>` : "")
  );
}

function errorHtml(msg) {
  return shell(`<div>prgraf couldn't produce a review.</div><pre>${esc(msg)}</pre>`);
}

module.exports = { renderHtml, loadingHtml, errorHtml, nonce };
