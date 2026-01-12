import subprocess
import os
from pathlib import Path
import shutil
from loguru import logger

# Force UTF-8 encoding on Windows
if os.name == 'nt':
    os.environ['PYTHONIOENCODING'] = 'utf-8'

class RepoFetcher:
    """Handles cloning and updating of git repositories."""

    def __init__(self, base_dir: str = "temp_repos"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _run_git(self, cmd, cwd=None, check=True):
        """Run git command with proper encoding handling."""
        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                text=True,
                encoding='utf-8',
                errors='replace',  # Replace bad chars instead of crashing
                check=check
            )
            return result
        except subprocess.CalledProcessError as e:
            logger.warning(f"Git command failed: {' '.join(cmd)}: {e}")
            raise

    def _get_file_content(self, cmd, cwd):
        """Get file content with encoding fallback."""
        try:
            result = subprocess.run(
                cmd,
                cwd=cwd,
                capture_output=True,
                encoding='utf-8',
                errors='replace'
            )
            return result.stdout if result.returncode == 0 else ""
        except Exception:
            return ""

    def fetch_repo(self, repo_url: str) -> Path:
        """Clones a repo and returns its local path."""
        repo_name = repo_url.rstrip("/").split("/")[-1].replace(".git", "")
        target_path = self.base_dir / repo_name

        if target_path.exists():
            logger.info(f"🔄 Repo {repo_name} already exists. Pulling latest...")
            try:
                self._run_git(["git", "pull"], cwd=target_path, check=False)
            except Exception as e:
                 logger.warning(f"⚠️ Failed to pull {repo_name}: {e}. Continuing with existing version.")
        else:
            logger.info(f"⬇️ Cloning {repo_name}...")
            subprocess.run(["git", "clone", repo_url, str(target_path)], check=True)
        
        return target_path

    def fetch_pr_diff(self, repo_path: Path, pr_number: int) -> list[dict]:
        """
        Fetches a PR to a temporary branch and calculates the diff.
        Returns a list of changes: [{'file': str, 'old_content': str, 'new_content': str}]
        """
        try:
            # Delete old branch if it exists (to force fresh fetch)
            self._run_git(["git", "branch", "-D", f"pr_review_{pr_number}"], cwd=repo_path, check=False)
            
            # Fetch PR ref (force)
            logger.info(f"⬇️ Fetching PR #{pr_number} (fresh)...")
            self._run_git(["git", "fetch", "origin", f"pull/{pr_number}/head:pr_review_{pr_number}"], cwd=repo_path)
            
            # Get changed files - try master first, then main
            result = self._run_git(
                ["git", "diff", "--name-only", f"master...pr_review_{pr_number}"], 
                cwd=repo_path, 
                check=False
            )
            
            if result.returncode != 0 or not result.stdout.strip():
                result = self._run_git(
                    ["git", "diff", "--name-only", f"main...pr_review_{pr_number}"],
                    cwd=repo_path,
                    check=False
                )

            files = [f for f in result.stdout.splitlines() if f.strip()]
            
            if not files:
                logger.warning("⚠️ No files detected in PR diff.")
                return []
            
            changes = []
            
            for file in files:
                # Get merge base
                try:
                    merge_base_result = self._run_git(
                        ["git", "merge-base", "HEAD", f"pr_review_{pr_number}"],
                        cwd=repo_path,
                        check=False
                    )
                    merge_base = merge_base_result.stdout.strip() if merge_base_result.returncode == 0 else "HEAD"
                except Exception:
                    merge_base = "HEAD"
                
                # Read old content
                old_content = self._get_file_content(
                    ["git", "show", f"{merge_base}:{file}"],
                    cwd=repo_path
                )
                
                # Read new content
                new_content = self._get_file_content(
                    ["git", "show", f"pr_review_{pr_number}:{file}"],
                    cwd=repo_path
                )

                changes.append({
                    "file": file,
                    "old_content": old_content,
                    "new_content": new_content
                })
                logger.info(f"  📄 Detected change: {file} (old: {len(old_content)} chars, new: {len(new_content)} chars)")
            
            logger.info(f"📊 Total files changed in PR: {len(changes)}")
            return changes

        except Exception as e:
            logger.error(f"⚠️ Failed to fetch PR changes: {e}")
            return []

    def clear_cache(self):
        """Removes the temp_repos directory."""
        if self.base_dir.exists():
            import shutil
            shutil.rmtree(self.base_dir)

