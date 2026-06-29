import subprocess
from pathlib import Path

from loguru import logger


class RepoFetcher:
    def __init__(self, base_dir: str = "temp_repos"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def fetch_repo(self, repo_url: str) -> Path:
        repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
        repo_path = self.base_dir / repo_name

        if repo_path.exists():
            logger.info(f"Updating {repo_name}")
            self._git(["git", "pull"], cwd=repo_path, check=False)
        else:
            logger.info(f"Cloning {repo_name}")
            self._git(["git", "clone", repo_url, str(repo_path)])

        return repo_path

    def fetch_pr_diff(self, repo_path: Path, pr_number: int) -> list[dict]:
        branch = f"pr_review_{pr_number}"
        self._git(["git", "branch", "-D", branch], cwd=repo_path, check=False)
        self._git(["git", "fetch", "origin", f"pull/{pr_number}/head:{branch}"], cwd=repo_path)

        base = self._detect_base(repo_path, branch)
        files = self._git(["git", "diff", "--name-only", f"{base}...{branch}"], cwd=repo_path).stdout.splitlines()

        changes = []
        for file_path in [file.strip() for file in files if file.strip()]:
            merge_base = self._git(["git", "merge-base", base, branch], cwd=repo_path).stdout.strip()
            changes.append(
                {
                    "file": file_path,
                    "old_content": self._show(repo_path, merge_base, file_path),
                    "new_content": self._show(repo_path, branch, file_path),
                }
            )

        logger.info(f"Detected {len(changes)} changed file(s)")
        return changes

    def _detect_base(self, repo_path: Path, branch: str) -> str:
        for base in ("main", "master"):
            result = self._git(["git", "diff", "--name-only", f"{base}...{branch}"], cwd=repo_path, check=False)
            if result.returncode == 0:
                return base
        return "HEAD"

    def _show(self, repo_path: Path, ref: str, file_path: str) -> str:
        return self._git(["git", "show", f"{ref}:{file_path}"], cwd=repo_path, check=False).stdout

    def _git(self, cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=check,
        )
