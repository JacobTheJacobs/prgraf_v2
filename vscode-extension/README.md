# prgraf — VS Code extension

Blast-radius review inside the editor. On command it runs the prgraf engine
over your working diff and opens an Obsidian-style graph: changed symbols in
the center, everything they might break ringing outward, closest first.

It is a thin webview over the **same** renderer the web app uses
(`codebase_rag/web/{style.css, app.js}`), so there is exactly one UI codebase.
No bundled Python, no second renderer.

## How it works

```
prgraf.review command
  -> spawn:  python -m codebase_rag.graph.export_cli --repo <root> --base HEAD~1
  -> engine: build graph (incremental) -> review diff -> JSON on stdout
  -> webview: inject JSON, render with app.js in embedded mode
```

Clicking a node opens that symbol's file in the editor.

## Prerequisites

The engine must be importable by the interpreter the extension calls:

```bash
cd /path/to/prgraf_v2
pip install -e .          # installs the codebase_rag package
```

## Run it from source (Extension Development Host)

1. Open the `prgraf_v2` folder in VS Code.
2. Point the extension at the right Python if `python` on PATH is not the one
   with the package: Settings → **prgraf: Python Path** (`prgraf.pythonPath`).
3. Press **F5** (Run → Start Debugging) — this launches a second VS Code window
   (the Extension Development Host) with the extension loaded.
4. In that window, open any git repo, then run **prgraf: Review blast radius**
   from the Command Palette (Ctrl+Shift+P) or the Source Control title bar.

## Package as a .vsix (optional)

```bash
npm install -g @vscode/vsce
cd vscode-extension
vsce package        # produces prgraf-0.1.0.vsix
code --install-extension prgraf-0.1.0.vsix
```

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `prgraf.pythonPath` | `python` | Interpreter with the prgraf package installed |
| `prgraf.base` | `HEAD~1` | Base ref to diff the working tree against |
| `prgraf.head` | `HEAD` | Head ref to review |
