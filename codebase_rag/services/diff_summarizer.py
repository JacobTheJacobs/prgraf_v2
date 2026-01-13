"""
HyDE Diff Summarizer - Pre-analyzes PR diff before orchestrator runs.

Uses Ollama gpt-oss to extract:
- PR intent (what is this PR trying to accomplish)
- Key changes (main code modifications)
- Search queries (suggested queries for graph/vector DB)
- Risk areas (potential blast radius concerns)
"""

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from loguru import logger


class PRIntentSummary(BaseModel):
    """Output schema for HyDE pre-analysis."""
    intent: str = Field(description="1-2 sentence summary of what this PR accomplishes")
    key_changes: list[str] = Field(description="List of 3-5 main code changes visible in the diff")
    search_queries: list[str] = Field(description="3-5 specific queries to find related code in the codebase")
    risk_areas: list[str] = Field(description="2-3 areas that might be affected by these changes")
    is_refactor: bool = Field(description="True if this is a refactor/migration, False if adding new features or fixing bugs")


# Configure Ollama provider
provider = OpenAIProvider(api_key='ollama', base_url='http://localhost:11434/v1')
model = OpenAIChatModel('gpt-oss:latest', provider=provider)

summarizer_agent = Agent(
    model,
    output_type=PRIntentSummary,
    system_prompt="""
You are a **Senior Staff Engineer** analyzing a PR diff BEFORE a code review.

Your job is to understand the PR's PURPOSE and generate intelligent search queries
that will help find related code in the codebase.

## Your Analysis Process:
1. Read the diff carefully - focus on lines starting with + (added) and - (removed)
2. Identify the CORE change (what is being modified/added/removed)
3. Generate search queries that would find:
   - Functions that CALL the modified code
   - Files that IMPORT modified modules
   - Similar patterns elsewhere in the codebase
   - Configuration that might need updating

## Query Generation Tips:
- Use function names, class names, and variable names from the diff
- Think about what a Senior Engineer would search for to understand impact
- Include queries for both direct and indirect dependencies

Be SPECIFIC and ACTIONABLE. Avoid generic queries like "find all files".
""",
)


async def summarize_pr_intent(diff_text: str, metadata: dict) -> PRIntentSummary:
    """
    Pre-analyzes a PR diff to extract intent and generate search queries.
    
    Args:
        diff_text: The unified diff content
        metadata: PR metadata (author, title, etc.)
        
    Returns:
        PRIntentSummary with intent, key changes, search queries, and risk areas
    """
    prompt = f"""
# PR to Analyze

**Author:** {metadata.get('author', 'Unknown')}
**Title:** {metadata.get('message', 'No title')}
**Files Changed:** {metadata.get('files_changed', 'Unknown')}

## Diff Content:
```diff
{diff_text[:15000]}  
```
*(Truncated to first 15k chars)*

## Your Task:
1. What is this PR trying to accomplish? (intent)
2. What are the 3-5 main code changes? (key_changes)
3. What should we search for to understand the impact? (search_queries)
4. What areas might be affected? (risk_areas)
5. Is this a refactor/migration or new feature/bug fix? (is_refactor)
"""
    
    try:
        logger.info("🔍 HyDE: Analyzing PR intent...")
        result = await summarizer_agent.run(prompt)
        summary = result.output
        
        logger.info(f"📋 HyDE Intent: {summary.intent}")
        logger.info(f"🔎 HyDE Queries: {summary.search_queries}")
        
        return summary
        
    except Exception as e:
        logger.error(f"❌ HyDE summarization failed: {e}")
        # Return a fallback summary
        return PRIntentSummary(
            intent="Unable to analyze - will proceed with standard review",
            key_changes=["Analysis failed"],
            search_queries=["modified functions", "changed files"],
            risk_areas=["unknown"],
            is_refactor=False
        )
