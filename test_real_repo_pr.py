#!/usr/bin/env python3
"""Test the blast-radius reviewer against real prgraf_v2 source files."""

from pathlib import Path

from codebase_rag.services.pr_review.analyzer import StructuralTriage
from codebase_rag.services.pocket_router import PocketStrategyRouter

print("=== PR Review Test on Real Project Source ===\n")

project_root = Path(".")
print(f"Simulating PR against files under: {project_root.resolve()}")

# Pick real files that exist
file1 = Path("codebase_rag/services/pr_review/analyzer.py")
file2 = Path("codebase_rag/services/pocket_router.py")

old1 = file1.read_text(encoding="utf-8")
# Realistic logic change: add a new early security/config check inside triage.
new1 = old1.replace(
    'if ext == ".py":\n            return "LOGIC_CORE" if self._python_logic_changed(old_content, new_content) else "MECHANICAL"',
    'if ext == ".py":\n            if "security" in lowered:\n                return "LOGIC_CORE"\n            return "LOGIC_CORE" if self._python_logic_changed(old_content, new_content) else "MECHANICAL"',
)

old2 = file2.read_text(encoding="utf-8") if file2.exists() else "def dummy(): pass"
# Mechanical change: comment-only update should not become a finding.
new2 = old2 + "\n# smoke-test comment\n"

changes = [
    {"file": str(file1), "old_content": old1, "new_content": new1},
    {"file": str(file2), "old_content": old2, "new_content": new2},
]

# 1. Triage
triage = StructuralTriage()
manifest = triage.generate_manifest(changes)

print("1. Structural Triage (AST logic check):")
logic_files = manifest.get("high_priority_review", [])
mech_files = manifest.get("automated_verified_refactors", [])
config_files = manifest.get("configuration_changes", [])

for f in logic_files:
    print(f"   LOGIC_CORE: {f['file']}")
for f in mech_files:
    print(f"   MECHANICAL: {f['file']}")
for f in config_files:
    print(f"   CONFIG: {f['file']}")

print(f"\n   Total logic files: {len(logic_files)}")

# 2. Router execution
router = PocketStrategyRouter()
report = router.route_and_review(
    {"pr_number": 77, "changes": changes},
    repo_root=project_root,
)

print("\n2. Review Report (excerpt):")
print(report[:1200])
print("... [truncated]")

print("\n" + "=" * 50)
print("PASS: PR review pipeline ran successfully on real source files")
print("   (no graph database, vector search, or LLM required).")
print("=" * 50)
