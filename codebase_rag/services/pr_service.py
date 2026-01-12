import os
import requests
from pathlib import Path
from loguru import logger
from codebase_rag.services.pr_review.fetcher import RepoFetcher
from codebase_rag.services.pr_review.analyzer import StructuralTriage, BlastRadiusDetector, PRReviewPromptGenerator
from codebase_rag.graph_updater import GraphUpdater
from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor
import codebase_rag.constants as cs

# --- Local LLM Helper (Ollama) ---
def local_llm_call(prompt, model="qwen2.5-coder:1.5b"):
    """
    Calls localhost:11434 (Ollama). Cost: $0.
    """
    try:
        url = "http://localhost:11434/api/generate"
        payload = {
            "model": model,
            "prompt": prompt,
            "stream": False
        }
        # Fallback to a common model if specific one fails? 
        # For this implementation we stick to the requested model default or allow override.
        resp = requests.post(url, json=payload, timeout=30)
        if resp.status_code == 200:
            return resp.json().get('response', '').strip()
    except Exception as e:
        logger.warning(f"Local LLM call failed: {e}")
    return ""

from codebase_rag.services.pocket_router import PocketStrategyRouter

class PRService:
    def __init__(self, project_name="TUTORIAL_RAG"):
        self.project_name = project_name
        self.fetcher = RepoFetcher()
        self.router = PocketStrategyRouter(project_name=project_name)

    async def route_and_review(self, pr_data):
        pr_number = pr_data.get('pr_number', 'UNKNOWN')
        logger.info(f"--- 🚦 Cognit Protocol: Routing PR #{pr_number} ---")
        
        # 1. Fetch Repo (Ingestion/Update)
        repo_url = pr_data.get('repo_url', 'https://github.com/The-Pocket/PocketFlow-Tutorial-Codebase-Knowledge.git')
        repo_path = self.fetcher.fetch_repo(repo_url)
        
        # Derive project name to match what GraphUpdater uses (repo_path.resolve().name)
        # GraphUpdater uses the folder name, NOT uppercase URL parsing
        derived_project_name = repo_path.resolve().name
        logger.info(f"📂 Project Name (from path): {derived_project_name}")
        
        # 2. Check for Ingestion / Graph Existence
        from codebase_rag.config import settings
        with MemgraphIngestor(
            settings.MEMGRAPH_HOST, 
            settings.MEMGRAPH_PORT, 
            username=settings.MEMGRAPH_USER, 
            password=settings.MEMGRAPH_PASSWORD
        ) as ingestor:
            project_check = ingestor.fetch_all("MATCH (p:Project {name: $name}) RETURN p", {"name": derived_project_name})
            if not project_check:
                logger.info(f"📉 Project '{derived_project_name}' not found. Starting Auto-Ingestion...")
                parsers, queries = load_parsers()
                # Initialize GraphUpdater with all required arguments
                updater = GraphUpdater(
                    ingestor=ingestor, 
                    repo_path=repo_path, 
                    parsers=parsers, 
                    queries=queries
                )
                updater.run()
                logger.success("✅ Auto-Ingestion Complete.")
            else:
                logger.info(f"✅ Project '{derived_project_name}' found in Graph. Skipping Auto-Ingestion.")

        # 3. Get Changes
        changes = pr_data.get('changes')
        if not changes:
            if 'pr_number' in pr_data:
                changes = self.fetcher.fetch_pr_diff(repo_path, pr_data['pr_number'])
                pr_data['changes'] = changes # Ensure router sees changes
            else:
                raise ValueError("No changes provided.")

        # 4. Delegate to Pocket Strategy Router
        return await self.router.route_and_review(pr_data, repo_root=repo_path)
