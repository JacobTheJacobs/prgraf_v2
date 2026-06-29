from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codebase_rag.services.pr_review.analyzer import StructuralTriage

MAX_FINDINGS = 3
MAX_REFERENCE_FILES = 5

REVIEWABLE_EXTENSIONS = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".go",
    ".rs",
    ".cpp",
    ".c",
    ".h",
    ".hpp",
    ".sql",
}

REFERENCE_EXTENSIONS = {
    ".css",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".ps1",
    ".py",
    ".rst",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}

CRITICAL_TERMS = {
    "auth",
    "billing",
    "database",
    "db",
    "login",
    "migration",
    "payment",
    "permission",
    "security",
    "secret",
    "session",
    "token",
}


@dataclass(frozen=True)
class SourceHit:
    file: str
    line: int
    text: str


@dataclass(frozen=True)
class BlastFinding:
    severity: str
    confidence: int
    file: str
    line: int | None
    message: str
    reason: str


class PocketStrategyRouter:
    def __init__(self):
        self.repo_root: Path | None = None
        self.triage = StructuralTriage()

    def route_and_review(
        self,
        pr_data: dict[str, Any],
        repo_root: Path | None = None,
    ) -> str:
        self.repo_root = Path(repo_root) if repo_root else None

        changes = pr_data.get("changes", [])
        if not changes:
            return "Pre-Landing Review: no changed files to review."

        triage_report = self.triage.generate_manifest(changes)
        findings = self._find_blast_radius_findings(changes, triage_report)
        return self._format_report(findings, changes, triage_report)

    def _find_blast_radius_findings(
        self,
        changes: list[dict[str, Any]],
        triage_report: dict[str, Any],
    ) -> list[BlastFinding]:
        findings = []
        findings.extend(self._deleted_reference_findings(changes))
        findings.extend(
            self._logic_reference_findings(
                triage_report.get("high_priority_review", []),
                changes,
            )
        )
        return self._dedupe_findings(findings)[:MAX_FINDINGS]

    def _deleted_reference_findings(
        self,
        changes: list[dict[str, Any]],
    ) -> list[BlastFinding]:
        references_by_file: dict[str, dict[str, Any]] = {}
        deleted_files = [
            change
            for change in changes
            if change.get("old_content") and not change.get("new_content")
        ]

        for deleted in deleted_files:
            deleted_path = self._normalize_path(deleted.get("file", ""))
            if not deleted_path:
                continue

            hits = self._find_path_references(deleted_path, changes)
            for hit in hits[:MAX_REFERENCE_FILES]:
                bucket = references_by_file.setdefault(
                    hit.file,
                    {
                        "line": hit.line,
                        "paths": set(),
                        "severity": self._reference_severity(hit.file),
                    },
                )
                bucket["line"] = min(bucket["line"], hit.line)
                bucket["paths"].add(deleted_path)

        findings = []
        for file_path, bucket in references_by_file.items():
            paths = sorted(bucket["paths"])
            preview = ", ".join(f"`{path}`" for path in paths[:4])
            if len(paths) > 4:
                preview += f", +{len(paths) - 4} more"
            findings.append(
                BlastFinding(
                    severity=bucket["severity"],
                    confidence=10,
                    file=file_path,
                    line=bucket["line"],
                    message=f"Deleted file(s) still referenced: {preview}.",
                    reason="Push will leave dead links/paths on a user-facing or executable surface.",
                )
            )

        return findings

    def _logic_reference_findings(
        self,
        logic_files: list[dict[str, Any]],
        changes: list[dict[str, Any]],
    ) -> list[BlastFinding]:
        findings = []

        for change in logic_files:
            file_path = self._normalize_path(change.get("file", ""))
            old_content = change.get("old_content", "")
            new_content = change.get("new_content", "")
            touched_symbols = self._touched_symbols(old_content, new_content)

            for symbol, line in touched_symbols[:3]:
                if self._is_noisy_symbol(symbol):
                    continue
                callers = self._find_symbol_references(symbol, file_path, changes)
                critical = self._is_critical(file_path, symbol)
                if not callers and not self._is_critical_path(file_path):
                    continue

                if callers:
                    caller_files = ", ".join(hit.file for hit in callers[:3])
                    if len(callers) > 3:
                        caller_files += f", +{len(callers) - 3} more"
                    message = (
                        f"`{symbol}` changed and is referenced by {len(callers)} "
                        f"file(s): {caller_files}."
                    )
                    reason = "Review caller contracts first; this is the real runtime blast radius."
                else:
                    message = f"Critical-area logic changed in `{symbol}`."
                    reason = "No callers were found, but the file path touches auth, security, data, or money."

                findings.append(
                    BlastFinding(
                        severity="P1" if critical else "P2",
                        confidence=8 if callers else 7,
                        file=file_path,
                        line=line,
                        message=message,
                        reason=reason,
                    )
                )

        return findings

    def _find_path_references(
        self,
        deleted_path: str,
        changes: list[dict[str, Any]],
    ) -> list[SourceHit]:
        variants = {
            deleted_path,
            f"./{deleted_path}",
            deleted_path.replace("/", "\\"),
        }
        hits = []
        for source in self._iter_searchable_sources(changes, exclude_file=deleted_path):
            if any(self._contains_path_reference(source.text, variant) for variant in variants):
                hits.append(source)
        return self._unique_hits(hits)

    def _find_symbol_references(
        self,
        symbol: str,
        origin_file: str,
        changes: list[dict[str, Any]],
    ) -> list[SourceHit]:
        if len(symbol) < 3 or self._is_noisy_symbol(symbol):
            return []

        pattern = re.compile(rf"\b{re.escape(symbol)}\b")
        hits = []
        for hit in self._iter_searchable_sources(changes, exclude_file=origin_file):
            if Path(hit.file).suffix.lower() not in REVIEWABLE_EXTENSIONS:
                continue
            if not pattern.search(hit.text):
                continue
            if f"def {symbol}" in hit.text or f"class {symbol}" in hit.text:
                continue
            hits.append(hit)
        return self._unique_file_hits(hits)

    def _iter_searchable_sources(
        self,
        changes: list[dict[str, Any]],
        exclude_file: str,
    ) -> list[SourceHit]:
        hits = []
        seen_files = set()

        for change in changes:
            file_path = self._normalize_path(change.get("file", ""))
            if not file_path or file_path == exclude_file or file_path in seen_files:
                continue
            content = change.get("new_content", "")
            if not content:
                continue
            seen_files.add(file_path)
            hits.extend(self._hits_for_content(file_path, content))

        if not self.repo_root or not self.repo_root.exists():
            return hits

        for path in self.repo_root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in REFERENCE_EXTENSIONS:
                continue
            if any(part in {".git", ".venv", "__pycache__", "node_modules"} for part in path.parts):
                continue
            rel_path = self._normalize_path(str(path.relative_to(self.repo_root)))
            if rel_path == exclude_file or rel_path in seen_files:
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            hits.extend(self._hits_for_content(rel_path, content))

        return hits

    def _hits_for_content(self, file_path: str, content: str) -> list[SourceHit]:
        if Path(file_path).suffix.lower() not in REFERENCE_EXTENSIONS:
            return []
        return [
            SourceHit(file=file_path, line=index, text=line)
            for index, line in enumerate(content.splitlines(), start=1)
        ]

    def _contains_path_reference(self, text: str, path: str) -> bool:
        if not path:
            return False
        pattern = rf"(?<![\w./\\-]){re.escape(path)}(?![\w./\\-])"
        return re.search(pattern, text) is not None

    def _touched_symbols(self, old_content: str, new_content: str) -> list[tuple[str, int]]:
        new_symbols = self._symbol_lines(new_content)
        if not new_symbols:
            fallback = self._first_changed_line(old_content, new_content)
            return [("<module>", fallback)] if fallback else []

        changed_lines = self._changed_new_lines(old_content, new_content)
        if not changed_lines:
            return []

        touched = []
        for changed_line in changed_lines:
            symbol = self._nearest_symbol(new_symbols, changed_line)
            if symbol and symbol not in touched:
                touched.append(symbol)
        return touched or [new_symbols[0]]

    def _symbol_lines(self, content: str) -> list[tuple[str, int]]:
        symbols = []
        patterns = [
            re.compile(r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)"),
            re.compile(r"^\s*(?:export\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)"),
            re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*="),
        ]
        for line_no, line in enumerate(content.splitlines(), start=1):
            indent = len(line) - len(line.lstrip())
            for index, pattern in enumerate(patterns):
                match = pattern.search(line)
                if match:
                    if index == 0 and indent > 4:
                        continue
                    symbols.append((match.group(1), line_no))
                    break
        return symbols

    def _changed_new_lines(self, old_content: str, new_content: str) -> list[int]:
        old_lines = old_content.splitlines()
        new_lines = new_content.splitlines()
        changed = []
        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines)
        for tag, _old_start, _old_end, new_start, new_end in matcher.get_opcodes():
            if tag == "equal":
                continue
            changed.extend(range(new_start + 1, max(new_start + 1, new_end + 1)))
        return changed

    def _first_changed_line(self, old_content: str, new_content: str) -> int | None:
        changed = self._changed_new_lines(old_content, new_content)
        return changed[0] if changed else None

    def _nearest_symbol(
        self,
        symbols: list[tuple[str, int]],
        line_no: int,
    ) -> tuple[str, int] | None:
        nearest = None
        for symbol in symbols:
            if symbol[1] <= line_no:
                nearest = symbol
            else:
                break
        return nearest

    def _format_report(
        self,
        findings: list[BlastFinding],
        changes: list[dict[str, Any]],
        triage_report: dict[str, Any],
    ) -> str:
        if not findings:
            deleted_count = sum(
                1 for change in changes if change.get("old_content") and not change.get("new_content")
            )
            logic_count = len(triage_report.get("high_priority_review", []))
            return (
                "Pre-Landing Review: no blast-radius findings.\n"
                f"Checked {len(changes)} file(s): {logic_count} logic, {deleted_count} deleted. "
                "No surviving references or caller contracts found."
            )

        lines = [f"Pre-Landing Review: {len(findings)} blast-radius finding(s)"]
        for finding in findings:
            location = finding.file
            if finding.line:
                location = f"{location}:{finding.line}"
            lines.append(
                f"- [{finding.severity}] (confidence: {finding.confidence}/10) "
                f"`{location}` - {finding.message}"
            )
            lines.append(f"  {finding.reason}")
        return "\n".join(lines)

    def _dedupe_findings(self, findings: list[BlastFinding]) -> list[BlastFinding]:
        severity_rank = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        unique = {}
        for finding in findings:
            key = (finding.file, finding.line, finding.message)
            unique.setdefault(key, finding)
        return sorted(
            unique.values(),
            key=lambda finding: (
                severity_rank.get(finding.severity, 9),
                -finding.confidence,
                finding.file,
                finding.line or 0,
            ),
        )

    def _reference_severity(self, file_path: str) -> str:
        suffix = Path(file_path).suffix.lower()
        if suffix in REVIEWABLE_EXTENSIONS or suffix in {".json", ".yaml", ".yml", ".toml", ".sh"}:
            return "P1"
        return "P2"

    def _is_critical(self, file_path: str, symbol: str) -> bool:
        text = f"{file_path} {symbol}".lower()
        return any(term in text for term in CRITICAL_TERMS)

    def _is_critical_path(self, file_path: str) -> bool:
        text = file_path.lower()
        return any(term in text for term in CRITICAL_TERMS)

    def _is_noisy_symbol(self, symbol: str) -> bool:
        return symbol == "<module>" or (symbol.startswith("__") and symbol.endswith("__"))

    def _normalize_path(self, path: str) -> str:
        return path.replace("\\", "/").strip()

    def _unique_hits(self, hits: list[SourceHit]) -> list[SourceHit]:
        unique = {}
        for hit in hits:
            key = (hit.file, hit.line)
            unique.setdefault(key, hit)
        return list(unique.values())

    def _unique_file_hits(self, hits: list[SourceHit]) -> list[SourceHit]:
        unique = {}
        for hit in hits:
            unique.setdefault(hit.file, hit)
        return list(unique.values())
