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

    async def route_and_review(self, pr_data, on_progress=None):
        async def report(pct, msg):
            if on_progress: await on_progress(pct, msg)
            logger.info(f"🚀 [Progress {pct}%] {msg}")

        pr_number = pr_data.get('pr_number', 'UNKNOWN')
        await report(10, f"Routing PR #{pr_number}")
        
        # 1. Fetch Repo (Ingestion/Update)
        await report(20, "Fetching Remote Repository...")
        repo_url = pr_data.get('repo_url', 'https://github.com/The-Pocket/PocketFlow-Tutorial-Codebase-Knowledge.git')
        repo_path = self.fetcher.fetch_repo(repo_url)
        
        # Derive project name to match what GraphUpdater uses (repo_path.resolve().name)
        # GraphUpdater uses the folder name, NOT uppercase URL parsing
        derived_project_name = repo_path.resolve().name
        logger.info(f"📂 Project Name (from path): {derived_project_name}")

        # 2. Get Changes (Early) needed for incremental detection
        changes = pr_data.get('changes')
        if not changes:
            if 'pr_number' in pr_data:
                await report(30, "Fetching PR Diff...")
                changes = self.fetcher.fetch_pr_diff(repo_path, pr_data['pr_number'])
                pr_data['changes'] = changes
            else:
                pass # Will be caught later or is empty

        # 3. Check for Ingestion / Graph Existence
        await report(40, "Verifying Graph Knowledge Base...")
        from codebase_rag.config import settings
        with MemgraphIngestor(
            settings.MEMGRAPH_HOST, 
            settings.MEMGRAPH_PORT, 
            username=settings.MEMGRAPH_USER, 
            password=settings.MEMGRAPH_PASSWORD
        ) as ingestor:
            project_check = ingestor.fetch_all("MATCH (p:Project {name: $name}) RETURN p", {"name": derived_project_name})
            if project_check:
                logger.info(f"🔄 Project '{derived_project_name}' exists. Performing Incremental Update.")
                
                # Handle Deletions
                if changes:
                    deleted = [c['file'] for c in changes if not c.get('new_content') and c.get('old_content')]
                    if deleted:
                        logger.info(f"🗑️ Cleanup: removing {len(deleted)} deleted files from graph...")
                        for dfile in deleted:
                            try:
                                abs_path = str(repo_path / dfile)
                                # Delete File and all contained nodes (Classes/Functions)
                                q_del = "MATCH (f:File {path: $path}) OPTIONAL MATCH (f)-[:CONTAINS*]->(c) DETACH DELETE f, c"
                                ingestor._execute_query(q_del, {"path": abs_path})
                            except Exception as e:
                                logger.warning(f"Failed to delete node for {dfile}: {e}")
            else:
                logger.info(f"📉 Project '{derived_project_name}' not found. Starting Fresh Ingestion...")

            # Run GraphUpdater
            parsers, queries = load_parsers()
            updater = GraphUpdater(
                ingestor=ingestor, 
                repo_path=repo_path, 
                parsers=parsers, 
                queries=queries
            )
            
            # If project exists, use incremental mode (only process changed files)
            if project_check and changes:
                # Get list of changed file paths (new + modified, exclude deleted)
                changed_files = [
                    repo_path / c['file'] 
                    for c in changes 
                    if c.get('new_content')  # Has new content = not deleted
                ]
                if changed_files:
                    logger.info(f"⚡ Incremental: Processing only {len(changed_files)} changed files...")
                    updater.run(files_to_process=changed_files)
                else:
                    logger.info("ℹ️ No files to update (all deleted). Skipping GraphUpdater.")
            else:
                # Fresh project or no changes info: full scan
                updater.run()
                
            logger.success(f"✅ Graph synced for '{derived_project_name}'.")

        # 4. Changes already fetched above
        if not changes:
             raise ValueError("No changes provided.")
        
        # 4. Fetch PR Metadata (author, commits, date, etc.)
        if 'pr_number' in pr_data and 'metadata' not in pr_data:
            metadata = self.fetcher.fetch_pr_metadata(repo_path, pr_data['pr_number'])
            pr_data['metadata'] = metadata
            logger.info(f"📋 PR Metadata: {metadata.get('author')} | {metadata.get('commits')} commits")

        # 5. Delegate to Pocket Strategy Router
        return await self.router.route_and_review(pr_data, repo_root=repo_path, project_name=derived_project_name, on_progress=report)
    
    def get_graph_data(self, project_name=None):
        """
        Exports the entire project graph for visualization.
        TODO: Use project_name to filter export if needed.
        """
        from codebase_rag.config import settings
        try:
            with MemgraphIngestor(
                settings.MEMGRAPH_HOST, 
                settings.MEMGRAPH_PORT, 
                username=settings.MEMGRAPH_USER, 
                password=settings.MEMGRAPH_PASSWORD
            ) as ingestor:
                return ingestor.export_graph_to_dict(project_name=project_name)
        except Exception as e:
            logger.error(f"Graph Export Failed: {e}")
            return {"error": str(e)}

    def list_projects(self):
        """
        List all ingested projects from Memgraph.
        """
        from codebase_rag.config import settings
        from codebase_rag.services.graph_service import MemgraphIngestor
        
        try:
            with MemgraphIngestor(
                settings.MEMGRAPH_HOST, 
                settings.MEMGRAPH_PORT, 
                username=settings.MEMGRAPH_USER, 
                password=settings.MEMGRAPH_PASSWORD
            ) as ingestor:
                results = ingestor.fetch_all("MATCH (p:Project) RETURN p.name AS name ORDER BY p.name")
                return [row['name'] for row in results]
        except Exception as e:
            logger.error(f"Failed to list projects: {e}")
            return []
