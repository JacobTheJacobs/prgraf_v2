from __future__ import annotations

import ast
from pathlib import Path


class StructuralTriage:
    """Classify changes so the reviewer only inspects real blast radius."""

    def triage_file(self, file_path, old_content, new_content):
        path = Path(file_path)
        ext = path.suffix.lower()
        name = path.name.lower()
        lowered = file_path.lower()

        if name == "requirements.txt" or ext in {
            ".env",
            ".ini",
            ".json",
            ".lock",
            ".toml",
            ".yaml",
            ".yml",
        }:
            return "CONFIGURATION"

        if ext in {".md", ".rst", ".txt"}:
            return "DOCUMENTATION"

        if "test" in lowered or name == "conftest.py":
            return "TEST_CODE"

        if "generated" in lowered or lowered.endswith(".min.js"):
            return "GENERATED"

        if ext == ".py":
            return "LOGIC_CORE" if self._python_logic_changed(old_content, new_content) else "MECHANICAL"

        if ext in {
            ".c",
            ".cpp",
            ".go",
            ".h",
            ".hpp",
            ".java",
            ".js",
            ".jsx",
            ".rs",
            ".sql",
            ".ts",
            ".tsx",
        }:
            return "LOGIC_CORE"

        return "UNKNOWN"

    def _python_logic_changed(self, old_code, new_code):
        try:
            return ast.dump(ast.parse(old_code), include_attributes=False) != ast.dump(
                ast.parse(new_code),
                include_attributes=False,
            )
        except SyntaxError:
            return self._strip_comment_lines(old_code) != self._strip_comment_lines(new_code)

    def _strip_comment_lines(self, code):
        return "\n".join(
            line for line in code.splitlines() if not line.lstrip().startswith("#")
        )

    def generate_manifest(self, file_list):
        for item in file_list:
            item.setdefault(
                "type",
                self.triage_file(
                    item["file"],
                    item.get("old_content", ""),
                    item.get("new_content", ""),
                ),
            )

        return {
            "high_priority_review": [
                item for item in file_list if item["type"] == "LOGIC_CORE"
            ],
            "automated_verified_refactors": [
                item for item in file_list if item["type"] == "MECHANICAL"
            ],
            "configuration_changes": [
                item for item in file_list if item["type"] == "CONFIGURATION"
            ],
            "documentation_updates": [
                item for item in file_list if item["type"] == "DOCUMENTATION"
            ],
            "test_code_updates": [
                item for item in file_list if item["type"] == "TEST_CODE"
            ],
            "generated_code": [item for item in file_list if item["type"] == "GENERATED"],
        }
