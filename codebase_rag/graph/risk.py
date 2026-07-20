"""Risk scoring for changed symbols.

Adapted from upstream code-review-graph (changes.py:312) with two deliberate
divergences, both aimed at noise:

1. Security keywords are split by strength. Upstream gives the full +0.20 to
   a 27-word set that includes `query`, `request`, `connect`, `validate` and
   `execute` — so a function named `validate_input` reaches 0.50 on its name
   plus a missing test, before any structural signal. Generic terms now score
   +0.05 and only reach +0.20 when the file path corroborates them.

2. Flow participation and community crossing are replaced by blast breadth
   and cross-file reach, which we can measure without flows or Leiden.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .store import GraphNode, GraphStore

# Unambiguous: appearing in a symbol name is itself the signal.
STRONG_SECURITY_TERMS: frozenset[str] = frozenset({
    "auth", "login", "logout", "password", "passwd", "token", "credential",
    "secret", "crypt", "encrypt", "decrypt", "permission", "privilege",
    "admin", "session", "oauth", "jwt", "signin", "signup", "billing",
    "payment", "charge", "refund", "invoice",
})

# Common in ordinary code. Only meaningful alongside a sensitive path.
WEAK_SECURITY_TERMS: frozenset[str] = frozenset({
    "query", "request", "connect", "validate", "execute", "http", "socket",
    "sanitize", "hash", "sign", "verify", "escape", "serialize", "upload",
})

# Directory/path markers that corroborate a weak term.
SENSITIVE_PATH_TERMS: frozenset[str] = frozenset({
    "auth", "security", "session", "account", "user", "payment", "billing",
    "admin", "crypto", "middleware", "permission", "migration", "db",
    "database", "sql", "api",
})

RISK_THRESHOLDS = {"critical": 0.85, "high": 0.70, "medium": 0.40}


def risk_level(score: float) -> str:
    if score >= RISK_THRESHOLDS["critical"]:
        return "critical"
    if score >= RISK_THRESHOLDS["high"]:
        return "high"
    if score >= RISK_THRESHOLDS["medium"]:
        return "medium"
    return "low"


def severity_for(score: float) -> str:
    return {"critical": "P0", "high": "P1", "medium": "P2", "low": "P3"}[risk_level(score)]


@dataclass
class RiskFactors:
    """Per-factor breakdown, so a score can always be explained."""

    untested: float = 0.0
    security: float = 0.0
    callers: float = 0.0
    breadth: float = 0.0
    cross_file: float = 0.0

    def total(self) -> float:
        return min(
            1.0,
            self.untested + self.security + self.callers + self.breadth + self.cross_file,
        )

    def reasons(self) -> list[str]:
        out = []
        if self.untested >= 0.25:
            out.append("no direct test coverage")
        elif self.untested > 0.05:
            out.append("thin test coverage")
        if self.security >= 0.20:
            out.append("security- or money-sensitive surface")
        elif self.security > 0:
            out.append("sensitive path")
        if self.callers >= 0.05:
            out.append("many callers")
        if self.breadth >= 0.10:
            out.append("wide blast radius")
        if self.cross_file >= 0.10:
            out.append("spans many files")
        return out


@dataclass
class ChangedSymbolRisk:
    node: GraphNode
    score: float
    level: str
    severity: str
    factors: RiskFactors
    test_count: int
    caller_count: int
    impacted_count: int
    impacted_files: int
    top_impacted: list[str] = field(default_factory=list)


def _in_test_file(file_path: str) -> bool:
    lowered = file_path.lower()
    name = Path(lowered).name
    return (
        name.startswith("test_")
        or name.endswith(("_test.py", "_test.go", ".test.ts", ".test.js",
                          ".spec.ts", ".spec.js"))
        or "/tests/" in f"/{lowered}"
        or "/__tests__/" in f"/{lowered}"
    )


def _security_component(node: GraphNode) -> float:
    name = node.name.lower()
    path = node.file_path.lower()

    if any(term in name for term in STRONG_SECURITY_TERMS):
        return 0.20
    if any(term in path for term in STRONG_SECURITY_TERMS):
        return 0.15

    if any(term in name for term in WEAK_SECURITY_TERMS):
        # Generic on its own; meaningful if the path agrees.
        path_parts = set(Path(path).parts)
        corroborated = any(
            term in part for part in path_parts for term in SENSITIVE_PATH_TERMS
        )
        return 0.20 if corroborated else 0.05

    return 0.0


def score_symbol(
    store: GraphStore,
    node: GraphNode,
    impact: dict | None = None,
) -> ChangedSymbolRisk:
    """Score one changed symbol. `impact` is its own blast-radius result."""
    if node.kind == "File":
        # Tests attach to symbols, never to files, so asking a File node for
        # its coverage always answers "none". Roll up its symbols instead.
        covered: set[str] = set()
        for contained in store.get_nodes_by_file(node.file_path):
            if contained.kind == "File":
                continue
            covered.update(t.qualified_name for t in store.tests_for(contained.qualified_name))
        tests = list(covered)
        callers = store.callers_of(node.qualified_name)
    else:
        tests = store.tests_for(node.qualified_name)
        callers = store.callers_of(node.qualified_name)

    impacted_nodes = impact.get("impacted_nodes", []) if impact else []
    impacted_files = impact.get("impacted_files", []) if impact else []
    scores = impact.get("impact_scores", {}) if impact else {}

    # A File seed reaches every symbol it contains, then their callers, so its
    # breadth is large by construction rather than by evidence. Left undamped
    # it makes every module-level edit outrank every specific symbol change.
    reach_damping = 0.4 if node.kind == "File" else 1.0

    factors = RiskFactors(
        # 0.30 untested, decaying to 0.05 at five or more tests.
        untested=0.30 - min(len(tests) / 5.0, 1.0) * 0.25,
        security=_security_component(node),
        callers=min(len(callers) / 20.0, 1.0) * 0.10,
        breadth=min(len(impacted_nodes) / 50.0, 1.0) * 0.15 * reach_damping,
        cross_file=min(len(impacted_files) / 10.0, 1.0) * 0.15 * reach_damping,
    )
    total = factors.total()

    # Test code changes still matter — a deleted test is lost coverage — but
    # they are not blast-radius findings. A test helper with many callers is
    # widely *used*, not widely *risky*; production breakage outranks it.
    # Keyed on the file, not the symbol name: a helper like `_extract_scripts`
    # living in test_foo.py is test code even though its name says otherwise.
    if node.is_test or _in_test_file(node.file_path):
        total = round(total * 0.5, 3)

    top = sorted(
        (n for n in impacted_nodes if not n.is_test),
        key=lambda n: -scores.get(n.qualified_name, 0.0),
    )[:5]

    return ChangedSymbolRisk(
        node=node,
        score=round(total, 3),
        level=risk_level(total),
        severity=severity_for(total),
        factors=factors,
        test_count=len(tests),
        caller_count=len(callers),
        impacted_count=impact.get("total_impacted", 0) if impact else 0,
        impacted_files=len(impacted_files),
        top_impacted=[n.qualified_name for n in top],
    )


def overall_risk(risks: list[ChangedSymbolRisk]) -> dict:
    """PR-level verdict.

    Upstream returns a bare `max()` over nodes, which is defensible but hides
    who caused it and says nothing about breadth. Same headline number, plus
    attribution and a count of how many symbols are actually elevated.
    """
    if not risks:
        return {"score": 0.0, "level": "low", "driver": None, "elevated": 0}

    worst = max(risks, key=lambda r: r.score)
    return {
        "score": worst.score,
        "level": worst.level,
        "driver": worst.node.qualified_name,
        "elevated": sum(1 for r in risks if r.score >= RISK_THRESHOLDS["medium"]),
    }
