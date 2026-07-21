"use strict";
/* Sidebar markup tests — plain Node, no DOM, no build step.
 *
 *     node tests/test_sidebar_render.js
 *
 * These assert on rendered text rather than on shapes, because the last UI bug
 * in this family passed a /~\d/ assertion while displaying "0K" to the user.
 */

const assert = require("assert");
const path = require("path");

const sidebar = require(
  path.join(__dirname, "..", "vscode-extension", "media", "sidebar.js")
);

const FINDING = {
  name: "startSession",
  symbol: "src/extension/sidebar-provider.ts::GrokSidebar.startSession",
  severity: "P2",
  level: "medium",
  risk: 0.7,
  location: "src/extension/sidebar-provider.ts:731",
  impacted: 132,
  impacted_files: 27,
  tests: 0,
  callers: 9,
  top_impacted: ["src/acp/client.ts::AcpClient.start"],
};

const PAYLOAD = {
  overall_risk: { score: 0.695, level: "medium" },
  findings: [FINDING],
  changed_files: ["a.ts", "b.ts"],
};

const tests = {
  "setup shows the three choices and the run button"() {
    const html = sidebar.render({ repo: "C:/work/grok-build-vscode", agentLabel: "Claude Code", base: "HEAD~1" });
    assert.ok(html.includes(">Repo<"), "no Repo field");
    assert.ok(html.includes(">Agent<"), "no Agent field");
    assert.ok(html.includes(">Base<"), "no Base field");
    // The repo reads as its own name, not a wall of path.
    assert.ok(html.includes("grok-build-vscode"), "repo name missing");
    assert.ok(!html.includes("C:/work/"), "full path leaked into a narrow sidebar");
    assert.ok(html.includes("Claude Code"), "agent missing");
    assert.ok(html.includes("Review blast radius"), "no run button");
  },

  "an unset repo prompts instead of showing an empty slot"() {
    const html = sidebar.render({ repo: "", agentLabel: "auto", base: "HEAD~1" });
    assert.ok(html.includes("choose a repository"), "no prompt for an unset repo");
    assert.ok(html.includes('class="value dim"'), "prompt not styled as a placeholder");
  },

  "running disables the button and shows dots, not a spinner"() {
    const html = sidebar.render({ busy: true, verb: "Scoring risk" });
    assert.ok(html.includes("disabled"), "button still clickable while running");
    assert.ok(html.includes("Scoring risk"), "verb missing");
    assert.ok(
      (html.match(/<span><\/span>/g) || []).length === 3,
      "expected exactly three dots"
    );
  },

  "risk always renders two decimals"() {
    const html = sidebar.render({ payload: PAYLOAD });
    assert.ok(html.includes("0.70"), `risk 0.7 must read as 0.70, got: ${excerpt(html, "sev")}`);
    assert.ok(html.includes("0.70</span>") || html.includes("· 0.70"), "risk not attached to severity");
    assert.ok(html.includes("0.69"), "overall score missing");
    assert.ok(!/\b0\.7\b/.test(html.replace(/0\.70/g, "")), "a bare 0.7 slipped through");
  },

  "a finding shows what it is, where, and what it reaches"() {
    const html = sidebar.render({ payload: PAYLOAD });
    assert.ok(html.includes("startSession"), "symbol name missing");
    assert.ok(html.includes("P2"), "severity missing");
    assert.ok(html.includes("src/extension/sidebar-provider.ts:731"), "location missing");
    assert.ok(html.includes("no direct test coverage"), "coverage gap not stated");
    assert.ok(html.includes("9 caller(s)"), "caller count missing");
    assert.ok(html.includes("132 symbol(s)"), "reach missing");
    assert.ok(html.includes("27 file(s)"), "file spread missing");
    assert.ok(html.includes('data-level="medium"'), "severity not exposed for styling");
  },

  "every finding carries a dot, and the rail is the only decoration"() {
    const html = sidebar.render({ payload: PAYLOAD });
    assert.ok(html.includes('class="dot"'), "no dot marker");
    assert.ok(!/[▸▾►▼]|codicon-chevron/.test(html), "chevrons are the old look");
  },

  "check-first targets are clickable"() {
    const html = sidebar.render({ payload: PAYLOAD });
    assert.ok(html.includes("AcpClient.start"), "top-impacted chip missing");
    assert.ok(html.includes('data-open="src/acp/client.ts"'), "chip cannot open its file");
  },

  "no findings says so plainly and offers a wider range"() {
    const html = sidebar.render({ payload: { findings: [], changed_files: ["a.ts"] } });
    assert.ok(html.includes("Nothing above the review bar"), "empty state missing");
    assert.ok(html.includes("1 changed file(s)"), "did not say what was checked");
    assert.ok(html.includes('data-act="widen"'), "no way out of a dead-end range");
  },

  "a stale graph warns where the findings are"() {
    const html = sidebar.render({
      payload: Object.assign({}, PAYLOAD, {
        stale: { graph_built: "2026-07-21T18:31:57", code_changed: "2026-07-21T22:20:07" },
      }),
    });
    assert.ok(html.includes("2026-07-21T18:31:57"), "build time missing");
    assert.ok(html.includes("Line numbers may be off"), "consequence not stated");
  },

  "symbol names cannot inject markup"() {
    const html = sidebar.render({
      payload: {
        overall_risk: { score: 0.5, level: "low" },
        findings: [
          Object.assign({}, FINDING, { name: '<img src=x onerror="alert(1)">' }),
        ],
        changed_files: [],
      },
    });
    assert.ok(!html.includes("<img src=x"), "unescaped markup from a symbol name");
    assert.ok(html.includes("&lt;img"), "name not escaped");
  },

  "errors are shown instead of findings, never alongside"() {
    const html = sidebar.render({ error: "Engine failed.\n\nboom", payload: PAYLOAD });
    assert.ok(html.includes("Engine failed."), "error not shown");
    assert.ok(!html.includes("startSession"), "stale findings shown under an error");
  },
};

function excerpt(html, cls) {
  const at = html.indexOf(cls);
  return at < 0 ? "(not found)" : html.slice(at, at + 80);
}

let passed = 0;
const names = Object.keys(tests);
for (const name of names) {
  try {
    tests[name]();
    console.log(`  PASS: ${name}`);
    passed += 1;
  } catch (err) {
    console.log(`  FAIL: ${name}\n        ${err.message}`);
  }
}
console.log(`\n${passed}/${names.length} passed`);
process.exit(passed === names.length ? 0 : 1);
