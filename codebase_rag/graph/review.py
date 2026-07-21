"""Orchestration: diff -> seeds -> blast radius -> risk -> findings.

One result model feeds all three surfaces (PR comment, MCP tool, webview),
so they cannot disagree about what the review found.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from datetime import datetime

from .constants import MAX_IMPACT_DEPTH, MAX_IMPACT_NODES
from .diff import WORKTREE, _git, changed_symbols
from .risk import ChangedSymbolRisk, overall_risk, score_symbol
from .store import GraphStore, default_db_path

MAX_FINDINGS = 3


class EmptyGraphError(RuntimeError):
    """The graph exists but holds no symbols.

    Raised rather than returned, because the failure mode it prevents is the
    worst one this tool has: an empty graph produces zero changed symbols,
    which renders as "nothing to review" — a clean bill of health that is
    indistinguishable from a real one. Silence must not read as a pass.
    """


def graph_staleness(store: GraphStore, repo_root: Path, head: str) -> dict | None:
    """Was the graph built before the code it is about to review?

    Returns None when fresh. A stale graph still produces confident output;
    it just maps diff line numbers onto symbol ranges that have since moved,
    so findings can land on the wrong symbol entirely.
    """
    built_raw = store.get_meta("last_build")
    if not built_raw:
        return None
    try:
        built = datetime.fromisoformat(built_raw)
    except ValueError:
        return None

    if head == WORKTREE:
        # Uncommitted review: the working tree itself is the head.
        newest = _git(["diff", "--name-only"], repo_root).split("\n")
        mtimes = [
            datetime.fromtimestamp((repo_root / f).stat().st_mtime)
            for f in (n.strip() for n in newest)
            if f and (repo_root / f).exists()
        ]
        if not mtimes or max(mtimes) <= built:
            return None
        return {"graph_built": built_raw, "code_changed": max(mtimes).isoformat(timespec="seconds")}

    stamp = _git(["log", "-1", "--format=%cI", head], repo_root).strip()
    if not stamp:
        return None
    try:
        committed = datetime.fromisoformat(stamp).replace(tzinfo=None)
    except ValueError:
        return None
    if committed <= built:
        return None
    return {"graph_built": built_raw, "code_changed": committed.isoformat(timespec="seconds")}


@dataclass
class ReviewResult:
    findings: list[ChangedSymbolRisk] = field(default_factory=list)
    all_risks: list[ChangedSymbolRisk] = field(default_factory=list)
    overall: dict = field(default_factory=dict)
    changed_files: list[str] = field(default_factory=list)
    subgraph: dict = field(default_factory=dict)
    truncated: bool = False
    stale: dict | None = None

    def to_dict(self, detail: str = "minimal") -> dict[str, Any]:
        """Serialize at a given detail level.

        `minimal` is the default on purpose — it's what an agent should pay
        for on a first call, and it's enough to decide whether to escalate.
        """
        base = {
            "overall_risk": self.overall,
            "changed_files": len(self.changed_files),
            "changed_symbols": len(self.all_risks),
            "findings": [
                {
                    "severity": f.severity,
                    "symbol": f.node.qualified_name,
                    "location": f"{f.node.file_path}:{f.node.line_start}",
                    "risk": f.score,
                    "impacted": f.impacted_count,
                }
                for f in self.findings
            ],
        }
        # Surfaced at every level including minimal: an agent that stops after
        # the cheap call is exactly the one that must not be told stale
        # findings without knowing they are stale.
        if self.stale:
            base["warning"] = (
                f"Graph built {self.stale['graph_built']} but code changed "
                f"{self.stale['code_changed']}. Findings may point at symbols "
                f"that have since moved. Call `rebuild` and review again."
            )
        if detail == "minimal":
            return base

        for finding, payload in zip(self.findings, base["findings"], strict=True):
            payload.update({
                "level": finding.level,
                "reasons": finding.factors.reasons(),
                "tests": finding.test_count,
                "callers": finding.caller_count,
                "impacted_files": finding.impacted_files,
                "top_impacted": finding.top_impacted,
            })

        if detail == "verbose":
            base["factors"] = {
                f.node.qualified_name: asdict(f.factors) for f in self.findings
            }
            base["truncated"] = self.truncated
            # Deliberately NOT the raw subgraph — on a multi-file diff that is
            # hundreds of KB and would blow any token budget. Only its shape
            # travels over MCP; the full subgraph is for the webview, via
            # write_subgraph() to disk.
            base["subgraph_summary"] = {
                "nodes": len(self.subgraph.get("nodes", [])),
                "edges": len(self.subgraph.get("edges", [])),
                "seeds": len(self.subgraph.get("seeds", [])),
            }
        return base

    def write_subgraph(self, path: str | Path) -> Path:
        """Persist the full impact subgraph as JSON for the webview.

        This is the render contract: nodes, edges, seeds, impact_scores. The
        VSCode extension reads this instead of pulling it through MCP.
        """
        import json

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "overall_risk": self.overall,
            "findings": [
                {
                    "symbol": f.node.qualified_name,
                    "severity": f.severity,
                    "risk": f.score,
                    "level": f.level,
                    "reasons": f.factors.reasons(),
                }
                for f in self.findings
            ],
            "seeds": self.subgraph.get("seeds", []),
            "nodes": self.subgraph.get("nodes", []),
            "edges": self.subgraph.get("edges", []),
            "impact_scores": self.subgraph.get("impact_scores", {}),
            "truncated": self.truncated,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path


def _rank_key(risk: ChangedSymbolRisk) -> tuple:
    """Rank on both axes: risk (how much it matters) x reach (how far).

    Risk alone ignores a wide-but-safe refactor. Reach alone ranks a
    thoroughly tested hub above an untested auth change. The product
    separates them; ties fall back to name for stable output.
    """
    reach = min(risk.impacted_count / 50.0, 1.0)
    return (-(risk.score * (0.5 + 0.5 * reach)), -risk.score, risk.node.qualified_name)


def review_range(
    repo_root: str | Path,
    base: str,
    head: str = "HEAD",
    db_path: str | Path | None = None,
    max_findings: int = MAX_FINDINGS,
    max_depth: int = MAX_IMPACT_DEPTH,
    max_nodes: int = MAX_IMPACT_NODES,
) -> ReviewResult:
    """Review a git range against an already-built graph."""
    repo_root = Path(repo_root).resolve()
    store = GraphStore(db_path or default_db_path(repo_root))

    try:
        if store.node_count() == 0:
            raise EmptyGraphError(
                f"The graph at {store.db_path} holds no symbols, so this review "
                f"would find nothing regardless of what changed. Check that the "
                f"repo root is right (currently {repo_root}) and run `rebuild`."
            )

        stale = graph_staleness(store, repo_root, head)
        if stale:
            logger.warning("Graph is older than the reviewed code: {}", stale)

        seeds, ranges = changed_symbols(store, repo_root, base, head)
        if not seeds:
            logger.info("No changed symbols found in the graph for this range.")
            return ReviewResult(changed_files=sorted(ranges), stale=stale)

        risks: list[ChangedSymbolRisk] = []
        union_scores: dict[str, float] = {}
        truncated = False

        for seed in seeds:
            impact = store.get_impact_radius(
                seed_qualified_names={seed.qualified_name},
                max_depth=max_depth,
                max_nodes=max_nodes,
            )
            truncated = truncated or impact["truncated"]
            for qn, score in impact["impact_scores"].items():
                if score > union_scores.get(qn, 0.0):
                    union_scores[qn] = score
            risks.append(score_symbol(store, seed, impact))

        risks.sort(key=_rank_key)
        findings = [r for r in risks if r.score >= 0.40][:max_findings]
        if not findings:
            # Nothing crossed the bar; still surface the single riskiest
            # symbol so the reviewer knows what was actually looked at.
            findings = risks[:1] if risks else []

        seed_qns = {s.qualified_name for s in seeds}
        scope = seed_qns | set(union_scores)
        subgraph = {
            "seeds": sorted(seed_qns),
            "nodes": [n.to_dict() for n in store._batch_get_nodes(scope)],  # noqa: SLF001
            "edges": [e.to_dict() for e in store.edges_between(scope)],
            "impact_scores": union_scores,
        }

        return ReviewResult(
            findings=findings,
            all_risks=risks,
            overall=overall_risk(risks),
            changed_files=sorted(ranges),
            subgraph=subgraph,
            truncated=truncated,
            stale=stale,
        )
    finally:
        store.close()


def render_graph(result: ReviewResult, cap: int = 160) -> dict:
    """Trim the subgraph to what reads well: all seeds + top-N by impact.

    The full radius can be hundreds of nodes; showing every one is a hairball
    and makes the O(n^2) layout expensive. Seeds are always kept (they are the
    change); the rest are the highest-impact nodes — what to look at first.
    """
    sub = result.subgraph
    seeds = set(sub.get("seeds", []))
    scores = sub.get("impact_scores", {})
    nodes = sub.get("nodes", [])

    ranked = sorted(
        (n for n in nodes if n["qualified_name"] not in seeds),
        key=lambda n: -scores.get(n["qualified_name"], 0.0),
    )
    keep = seeds | {n["qualified_name"] for n in ranked[: max(0, cap)]}
    kept_nodes = [n for n in nodes if n["qualified_name"] in keep]
    kept_edges = [
        e for e in sub.get("edges", [])
        if e["source"] in keep and e["target"] in keep
    ]
    return {
        "seeds": sorted(seeds),
        "nodes": kept_nodes,
        "edges": kept_edges,
        "impact_scores": {qn: s for qn, s in scores.items() if qn in keep},
        "shown": len(kept_nodes),
        "total": len(nodes),
    }


def web_payload(result: ReviewResult, cap: int = 160) -> dict:
    """The render contract shared by the web app and the VSCode extension."""
    return {
        "status": "ok",
        "report": format_report(result),
        "overall_risk": result.overall,
        "findings": [
            {
                "symbol": f.node.qualified_name,
                "name": f.node.name,
                "kind": f.node.kind,
                "severity": f.severity,
                "level": f.level,
                "risk": f.score,
                "location": f"{f.node.file_path}:{f.node.line_start}",
                "reasons": f.factors.reasons(),
                "impacted": f.impacted_count,
                "impacted_files": f.impacted_files,
                "tests": f.test_count,
                "callers": f.caller_count,
                "top_impacted": f.top_impacted,
            }
            for f in result.findings
        ],
        "changed_files": result.changed_files,
        "graph": render_graph(result, cap),
        "truncated": result.truncated,
        "stale": result.stale,
    }


def _stale_line(result: ReviewResult) -> list[str]:
    if not result.stale:
        return []
    return [
        f"! Graph built {result.stale['graph_built']}, code changed "
        f"{result.stale['code_changed']} — rebuild before trusting line numbers."
    ]


def format_report(result: ReviewResult) -> str:
    """Terse text report — the PR-comment surface."""
    if not result.all_risks:
        files = len(result.changed_files)
        return "\n".join([
            "Pre-Landing Review: no reviewable symbols changed.",
            f"Checked {files} changed file(s); nothing mapped to a graph symbol.",
            *_stale_line(result),
        ])

    overall = result.overall
    lines = [
        f"Pre-Landing Review: {len(result.findings)} finding(s) · "
        f"overall risk {overall['score']:.2f} ({overall['level']})",
        *_stale_line(result),
    ]

    for finding in result.findings:
        node = finding.node
        reasons = ", ".join(finding.factors.reasons()) or "structural change"
        lines.append(
            f"- [{finding.severity}] (risk {finding.score:.2f}) "
            f"`{node.file_path}:{node.line_start}` - `{node.name}` "
            f"impacts {finding.impacted_count} symbol(s) "
            f"across {finding.impacted_files} file(s)."
        )
        lines.append(f"  {reasons}.")
        if finding.top_impacted:
            preview = ", ".join(qn.split("::")[-1] for qn in finding.top_impacted[:3])
            lines.append(f"  Check first: {preview}")

    if result.truncated:
        lines.append("  (blast radius truncated at the node cap)")
    return "\n".join(lines)
