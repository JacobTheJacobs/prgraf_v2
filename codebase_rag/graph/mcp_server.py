"""MCP server: hand an agent a token-minimal, risk-scored context packet.

The agent writes the review; this server never calls an LLM. Cost control is
the same lever upstream uses — a `detail_level` on every tool plus a prompt
that says stay minimal and escalate only on the risky few.

Six tools, not thirty. The set covers: orient, review a diff, trace one
symbol, pull a blast radius, read the graph shape. Everything an agent needs
to review a PR in five calls.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .build import build_graph
from .review import review_range
from .store import GraphStore, default_db_path

DetailLevel = Literal["minimal", "standard", "verbose"]

TOKEN_DISCIPLINE = """\
## Token-efficient graph usage
1. Call `minimal_context` FIRST — it costs ~100 tokens and tells you whether
   this diff is worth deeper inspection.
2. Keep `detail="minimal"` unless the minimal output is genuinely insufficient.
3. Escalate to "standard"/"verbose" only for the specific high-risk symbols.
4. Prefer `trace_symbol` on one name over `blast_radius` on everything.
5. Target: <=5 tool calls, <=800 tokens of graph context per review.
"""


def _repo_root() -> Path:
    return Path(os.environ.get("PRGRAF_REPO", ".")).resolve()


def _db(repo: Path) -> Path:
    override = os.environ.get("PRGRAF_DB")
    return Path(override) if override else default_db_path(repo)


def _open_store(repo: Path) -> GraphStore:
    db = _db(repo)
    if not db.exists():
        raise FileNotFoundError(
            f"No graph at {db}. Run `prgraf build` or call the `rebuild` tool first."
        )
    return GraphStore(db)


def _node_brief(node: Any, score: float | None = None) -> dict[str, Any]:
    out = {
        "symbol": node.qualified_name,
        "kind": node.kind,
        "location": f"{node.file_path}:{node.line_start}",
    }
    if score is not None:
        out["impact"] = round(score, 3)
    return out


mcp = FastMCP("prgraf")


@mcp.tool()
def minimal_context(task: str = "review changes", base: str = "HEAD~1") -> dict[str, Any]:
    """START HERE. ~100-token overview: overall risk + the top findings.

    Use this to decide whether a diff needs deeper inspection at all. If it
    comes back "low" with no findings, you are done — report and stop.
    """
    repo = _repo_root()
    try:
        result = review_range(repo, base=base, db_path=_db(repo))
    except FileNotFoundError as exc:
        return {"error": str(exc), "next": "call `rebuild` then retry"}
    packet = result.to_dict(detail="minimal")
    packet["task"] = task
    packet["next_tool_suggestions"] = (
        ["done — low risk, report the summary"]
        if packet["overall_risk"].get("level") == "low"
        else ["review_diff(detail='standard')", "trace_symbol on each finding"]
    )
    return packet


@mcp.tool()
def review_diff(base: str = "HEAD~1", head: str = "HEAD", detail: DetailLevel = "minimal") -> dict[str, Any]:
    """Risk-scored blast-radius review of a git range.

    The primary review tool. `minimal` gives severities and locations;
    `standard` adds reasons, coverage, and what to check first; `verbose`
    adds the full factor breakdown and the impact subgraph.
    """
    repo = _repo_root()
    try:
        result = review_range(repo, base=base, head=head, db_path=_db(repo))
    except FileNotFoundError as exc:
        return {"error": str(exc), "next": "call `rebuild` then retry"}
    return result.to_dict(detail=detail)


@mcp.tool()
def trace_symbol(name: str, detail: DetailLevel = "minimal") -> dict[str, Any]:
    """Callers, callees, and tests for one symbol.

    Cheaper and sharper than a blast radius when you already know the symbol
    you care about. Accepts a bare name or a fully-qualified name.
    """
    repo = _repo_root()
    with _open_store(repo) as store:
        matches = store.find_nodes(name, limit=5)
        if not matches:
            return {"error": f"no symbol matching {name!r}", "matches": []}
        node = matches[0]
        callers = store.callers_of(node.qualified_name)
        callees = store.callees_of(node.qualified_name)
        tests = store.tests_for(node.qualified_name)
        out: dict[str, Any] = {
            "symbol": node.qualified_name,
            "location": f"{node.file_path}:{node.line_start}",
            "caller_count": len(callers),
            "callee_count": len(callees),
            "test_count": len(tests),
            "tested": bool(tests),
        }
        if len(matches) > 1:
            out["ambiguous"] = [m.qualified_name for m in matches[1:]]
        if detail != "minimal":
            out["callers"] = [_node_brief(n) for n in callers[:20]]
            out["callees"] = [_node_brief(n) for n in callees[:20]]
            out["tests"] = [_node_brief(n) for n in tests[:20]]
        return out


@mcp.tool()
def blast_radius(name: str, max_depth: int = 2, detail: DetailLevel = "minimal") -> dict[str, Any]:
    """What breaks if this symbol changes — bidirectional, score-ranked.

    Scores decay per hop, so the ordering is meaningful: the top entries are
    what a reviewer should check first. Use `trace_symbol` instead if you
    only need direct callers/tests.
    """
    repo = _repo_root()
    with _open_store(repo) as store:
        matches = store.find_nodes(name, limit=3)
        if not matches:
            return {"error": f"no symbol matching {name!r}"}
        node = matches[0]
        impact = store.get_impact_radius(
            seed_qualified_names={node.qualified_name}, max_depth=max_depth
        )
        scores = impact["impact_scores"]
        top = impact["impacted_nodes"][: (10 if detail == "minimal" else 50)]
        out = {
            "symbol": node.qualified_name,
            "total_impacted": impact["total_impacted"],
            "files_touched": len(impact["impacted_files"]),
            "truncated": impact["truncated"],
            "top": [_node_brief(n, scores.get(n.qualified_name)) for n in top],
        }
        if detail == "verbose":
            # Bounded — a hub symbol's radius can carry thousands of edges.
            # The full graph goes to the webview via a subgraph export, not here.
            out["edges"] = [e.to_dict() for e in impact["edges"][:200]]
            out["edges_truncated"] = len(impact["edges"]) > 200
        return out


@mcp.tool()
def find_symbols(query: str, limit: int = 15) -> dict[str, Any]:
    """Locate symbols by name — a graph-aware substitute for grep.

    Returns qualified names and locations. Follow up with `trace_symbol` or
    `blast_radius` on whichever match is relevant.
    """
    repo = _repo_root()
    with _open_store(repo) as store:
        matches = store.find_nodes(query, limit=limit)
        return {
            "query": query,
            "count": len(matches),
            "matches": [_node_brief(n) for n in matches],
        }


@mcp.tool()
def graph_status() -> dict[str, Any]:
    """Graph size, languages, and freshness. Use to confirm it's built."""
    repo = _repo_root()
    db = _db(repo)
    if not db.exists():
        return {"built": False, "next": "call `rebuild`"}
    with GraphStore(db) as store:
        stats = store.stats()
        stats["built"] = True
        return stats


@mcp.tool()
def rebuild(full: bool = False) -> dict[str, Any]:
    """(Re)build the graph from the working tree. Run once before reviewing."""
    repo = _repo_root()
    stats = build_graph(repo, db_path=_db(repo), full=full)
    return {"built": True, **{k: stats[k] for k in ("total_nodes", "total_edges", "files", "elapsed_sec")}}


@mcp.prompt()
def review_pr(base: str = "HEAD~1") -> str:
    """Guided, token-efficient PR review workflow."""
    return (
        f"{TOKEN_DISCIPLINE}\n"
        f"## Review workflow\n"
        f'1. `minimal_context(base="{base}")` — read overall risk.\n'
        f'2. If risk is "low": report the summary and STOP.\n'
        f'3. Otherwise `review_diff(detail="standard")` for the finding list.\n'
        f"4. For each finding, `trace_symbol` to confirm callers/coverage; use "
        f"`blast_radius` only when reach is unclear.\n"
        f"5. Report: overall risk, each finding with what to check first, and "
        f"any missing test coverage.\n"
    )


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
