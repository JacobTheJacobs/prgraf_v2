import subprocess
from pathlib import Path
import shutil
import os

class RepoFetcher:
    """Handles cloning and updating of git repositories."""

    def __init__(self, base_dir: str = "temp_repos"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(exist_ok=True)

    def fetch_repo(self, repo_url: str) -> Path:
        """Clones a repo and returns its local path."""
        repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
        target_path = self.base_dir / repo_name

        if target_path.exists():
            print(f"🔄 Repo {repo_name} already exists. Pulling latest...")
            try:
                subprocess.run(["git", "pull"], cwd=target_path, check=True, capture_output=True)
            except Exception as e:
                 print(f"⚠️ Failed to pull {repo_name}: {e}. Continuing with existing version.")
        else:
            print(f"⬇️ Cloning {repo_name}...")
            subprocess.run(["git", "clone", repo_url, str(target_path)], check=True)
        
        return target_path

    def clear_cache(self):
        """Removes the temp_repos directory."""
        if self.base_dir.exists():
            shutil.rmtree(self.base_dir)
