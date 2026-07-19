from pathlib import Path

from loguru import logger

from codebase_rag.services.pocket_router import PocketStrategyRouter
from codebase_rag.services.pr_review.fetcher import RepoFetcher


class PRService:
    def __init__(self):
        self.fetcher = RepoFetcher()
        self.router = PocketStrategyRouter()

    def route_and_review(self, pr_data):
        changes = pr_data.get("changes")
        repo_path: Path | None = None

        if not changes:
            # Local / CI range: already-checked-out repo + base...head
            local_repo = pr_data.get("repo_path") or pr_data.get("repo")
            base = pr_data.get("base")
            head = pr_data.get("head", "HEAD")
            if local_repo and base:
                repo_path = Path(local_repo)
                logger.info(f"Reviewing local range {base}...{head} in {repo_path}")
                changes = self.fetcher.fetch_range_diff(repo_path, base=base, head=head)
                pr_data["changes"] = changes
            else:
                repo_url = pr_data.get("repo_url")
                pr_number = pr_data.get("pr_number")
                if not repo_url or not pr_number:
                    raise ValueError(
                        "PR review needs changes, repo_path+base, or repo_url+pr_number."
                    )

                logger.info("Fetching repository")
                repo_path = self.fetcher.fetch_repo(repo_url)
                logger.info("Fetching PR diff")
                changes = self.fetcher.fetch_pr_diff(repo_path, pr_number)
                pr_data["changes"] = changes

        if not changes:
            raise ValueError("No changes provided.")

        return self.router.route_and_review(pr_data, repo_root=repo_path)
