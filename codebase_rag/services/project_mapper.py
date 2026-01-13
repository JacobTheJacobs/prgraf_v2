"""
Project Mapper for Tier 2 Summaries

Creates a "mental map" of the codebase by generating LLM summaries
for files and directories, then storing them as node properties.

Key features:
- Bottom-up aggregation: File summaries → Directory summaries
- AST fingerprint: Skip unchanged files via content hash
- Incremental updates: Only summarize changed files
"""

import asyncio
import hashlib
from pathlib import Path
from collections import defaultdict
from typing import Optional
from loguru import logger

from codebase_rag.config import settings
from codebase_rag.services.gemini_client import get_gemini_client, GeminiClient
from codebase_rag.services.graph_service import MemgraphIngestor


# File extensions we summarize
SUMMARIZABLE_EXTENSIONS = {'.py', '.js', '.ts', '.tsx', '.jsx', '.java', '.go', '.rs', '.cpp', '.c', '.h'}

# Directories to skip
SKIP_DIRS = {'__pycache__', '.git', 'node_modules', '.venv', 'venv', 'dist', 'build', '.next'}


class ProjectMapper:
    """
    Generates and stores business-intent summaries for a codebase.
    """
    
    def __init__(self, gemini_client: Optional[GeminiClient] = None):
        self.gemini = gemini_client or get_gemini_client()
        self.file_summaries: dict[str, str] = {}  # {filepath: summary}
        self.dir_summaries: dict[str, str] = {}   # {dirpath: summary}
    
    async def generate_summaries(
        self, 
        repo_path: Path, 
        changed_files: Optional[list[Path]] = None,
        on_progress: Optional[callable] = None
    ) -> dict[str, str]:
        """
        Generate summaries for files in the repo.
        
        Args:
            repo_path: Root of the repository
            changed_files: If provided, only summarize these files (incremental mode)
            on_progress: Optional callback(pct, msg) for progress updates
        
        Returns:
            Dict of {filepath: summary}
        """
        if not self.gemini.enabled:
            logger.warning("⚠️ Gemini not enabled. Skipping summary generation.")
            return {}
        
        async def report(pct, msg):
            if on_progress:
                await on_progress(pct, msg)
        
        # Collect files to summarize
        if changed_files:
            files = [f for f in changed_files if f.suffix in SUMMARIZABLE_EXTENSIONS]
            logger.info(f"📝 Incremental mode: Summarizing {len(files)} changed files")
        else:
            files = self._collect_files(repo_path)
            logger.info(f"📝 Full mode: Summarizing {len(files)} files")
        
        if not files:
            return {}
        
        # Summarize files
        await report(0, f"Summarizing {len(files)} files...")
        
        for i, filepath in enumerate(files):
            try:
                relative_path = filepath.relative_to(repo_path)
                code = filepath.read_text(encoding='utf-8', errors='ignore')
                
                # Skip empty files
                if len(code.strip()) < 50:
                    continue
                
                summary = await self.gemini.summarize_intent(code, str(relative_path))
                if summary:
                    self.file_summaries[str(relative_path)] = summary
                
                # Progress update every 10 files
                if (i + 1) % 10 == 0:
                    pct = int((i + 1) / len(files) * 50)  # Files = 0-50%
                    await report(pct, f"Summarized {i+1}/{len(files)} files")
                    
            except Exception as e:
                logger.warning(f"Failed to summarize {filepath}: {e}")
        
        logger.info(f"✅ Generated {len(self.file_summaries)} file summaries")
        
        # Bottom-up directory aggregation
        await report(50, "Aggregating directory summaries...")
        await self._aggregate_directories(repo_path, on_progress)
        
        return self.file_summaries
    
    async def _aggregate_directories(
        self, 
        repo_path: Path,
        on_progress: Optional[callable] = None
    ):
        """
        Bottom-up aggregation: Combine file summaries into directory summaries.
        """
        # Group file summaries by directory
        dir_files: dict[str, list[str]] = defaultdict(list)
        
        for filepath, summary in self.file_summaries.items():
            dir_path = str(Path(filepath).parent)
            if dir_path == '.':
                dir_path = '/'
            dir_files[dir_path].append(summary)
        
        # Sort directories by depth (deepest first for bottom-up)
        sorted_dirs = sorted(dir_files.keys(), key=lambda d: d.count('/'), reverse=True)
        
        async def report(pct, msg):
            if on_progress:
                await on_progress(pct, msg)
        
        for i, dir_path in enumerate(sorted_dirs):
            file_summaries = dir_files[dir_path]
            
            if len(file_summaries) >= 2:  # Only aggregate if multiple files
                summary = await self.gemini.aggregate_summaries(dir_path, file_summaries)
                if summary:
                    self.dir_summaries[dir_path] = summary
            
            # Progress update
            if (i + 1) % 5 == 0:
                pct = 50 + int((i + 1) / len(sorted_dirs) * 40)  # Dirs = 50-90%
                await report(pct, f"Aggregated {i+1}/{len(sorted_dirs)} directories")
        
        logger.info(f"✅ Generated {len(self.dir_summaries)} directory summaries")
    
    async def update_graph(self, project_name: str):
        """
        Update Memgraph nodes with summary and summary_hash properties.
        """
        logger.info(f"📊 Updating graph with summaries for project: {project_name}")
        
        with MemgraphIngestor(
            settings.MEMGRAPH_HOST,
            settings.MEMGRAPH_PORT,
            username=settings.MEMGRAPH_USER,
            password=settings.MEMGRAPH_PASSWORD
        ) as ingestor:
            # Update File nodes
            for filepath, summary in self.file_summaries.items():
                content_hash = GeminiClient.compute_content_hash(summary)
                query = """
                MATCH (p:Project {name: $project})-[:CONTAINS_FILE]->(f:File)
                WHERE f.path ENDS WITH $filepath OR f.name = $filename
                SET f.summary = $summary, f.summary_hash = $hash
                """
                ingestor._execute_query(query, {
                    "project": project_name,
                    "filepath": filepath,
                    "filename": Path(filepath).name,
                    "summary": summary,
                    "hash": content_hash
                })
            
            # Update Directory nodes (if they exist)
            for dirpath, summary in self.dir_summaries.items():
                query = """
                MATCH (p:Project {name: $project})-[:CONTAINS_FOLDER*]->(d:Directory)
                WHERE d.path ENDS WITH $dirpath
                SET d.summary = $summary
                """
                ingestor._execute_query(query, {
                    "project": project_name,
                    "dirpath": dirpath,
                    "summary": summary
                })
        
        logger.success(f"✅ Graph updated with {len(self.file_summaries)} file summaries")
    
    def _collect_files(self, repo_path: Path) -> list[Path]:
        """Collect all summarizable files in the repo."""
        files = []
        
        for item in repo_path.rglob('*'):
            if item.is_file() and item.suffix in SUMMARIZABLE_EXTENSIONS:
                # Skip unwanted directories
                if any(skip in item.parts for skip in SKIP_DIRS):
                    continue
                files.append(item)
        
        return files[:500]  # Limit to 500 files for cost control
    
    @staticmethod
    def should_resummarize(current_hash: str, stored_hash: Optional[str]) -> bool:
        """Check if file needs re-summarization based on hash."""
        return stored_hash is None or current_hash != stored_hash


async def run_project_mapping(
    repo_path: Path,
    project_name: str,
    changed_files: Optional[list[Path]] = None,
    on_progress: Optional[callable] = None
) -> dict[str, str]:
    """
    High-level function to run the full project mapping pipeline.
    
    1. Generate file summaries (with Gemini)
    2. Aggregate directory summaries (bottom-up)
    3. Update Memgraph with summary properties
    """
    if not settings.ENABLE_SUMMARIES:
        logger.info("ℹ️ Summaries disabled in config. Skipping.")
        return {}
    
    mapper = ProjectMapper()
    
    summaries = await mapper.generate_summaries(
        repo_path,
        changed_files=changed_files,
        on_progress=on_progress
    )
    
    if summaries:
        await mapper.update_graph(project_name)
    
    return summaries
