from pydantic import BaseModel, Field
from pydantic_ai import Agent
import os

from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from ..config import settings
from loguru import logger

# Define the score output structure
class ReviewScore(BaseModel):
    score: int = Field(description="Score from 1-10 where 10 is Master GOAT Staff Engineer level")
    reasoning: str = Field(description="Detailed explanation of the score")
    missing_context: list[str] = Field(description="List of specific things the reviewer missed from the diff")
    noise_level: str = Field(description="LOW, MEDIUM, or HIGH")
    is_staff_level: bool = Field(description="True if score >= 7 and meets Master GOAT standard")
    improvement_tips: list[str] = Field(description="Specific, actionable ways to improve - be very concrete")
    what_was_good: list[str] = Field(description="What the reviewer did well - reinforce good behavior", default=[])

# Define the judge agent with explicit API key for local Ollama
provider = OpenAIProvider(api_key='ollama', base_url='http://localhost:11434/v1')
model = OpenAIChatModel('gpt-oss:latest', provider=provider)

judge_agent = Agent(
    model,
    output_type=ReviewScore,
    retries=3,
    system_prompt="""
You are the **Ultimate PR Review Critic** - a Distinguished Engineer with 20+ years experience.

## What Makes a "Master GOAT" PR Review (9-10/10):
1. ✅ **Accurately describes what changed** - matches the actual diff content
2. ✅ **Identifies blast radius** - finds callers, dependencies, affected modules  
3. ✅ **Distinguishes intent** - knows if it's a refactor, bugfix, or new feature
4. ✅ **Concise** - no repeated content, no verbose explanations
5. ✅ **Actionable** - provides specific suggestions, not generic advice

## Scoring Scale:
- **9-10**: Staff Engineer level. Ready to merge. No hallucinations.
- **7-8**: Good but missing 1-2 key insights. Minor improvements needed.
- **4-6**: Acceptable but noisy OR incomplete. Missed important changes.
- **1-3**: FAILED. Contains hallucinations OR completely missed the core change.

## Critical Failures (automatic score ≤3):
- ❌ Describing changes NOT in the diff (hallucination)
- ❌ Missing the PRIMARY change (e.g., ignoring a class removal)
- ❌ Inventing files or functions that don't exist
- ❌ Saying "No changes detected" when diff shows clear changes

## Your Feedback Must Be:
1. **Specific**: "You missed the removal of ChatLlama class on line 42"
2. **Actionable**: "Query the graph for 'ChatLlama callers' to find impact"
3. **Prioritized**: Most important issue first

Be HARSH but FAIR. We want Master GOAT level, not "good enough".
"""
)

async def judge_pr_review(diff_text: str, review_text: str, pr_metadata: dict) -> ReviewScore:
    """
    Evaluates a PR review against the actual diff using an LLM.
    """
    
    prompt = f"""
    # 🕵️‍♀️ JUDGEMENT DAY
    
    ## PR Context
    - **Author:** {pr_metadata.get('author')}
    - **Description:** {pr_metadata.get('message')}
    
    ## 📄 The Diff (Truth)
    ```text
    {diff_text[:10000]} 
    ```
    *(Diff truncated to first 10k chars)*
    
    ## 🤖 The AI Review (Candidate)
    ```markdown
    {review_text}
    ```
    
    ## YOUR TASK
    Evaluate the Candidate's review. 
    Did they catch the main changes? 
    Did they identify the impact (blast radius)? 
    Was it concise?
    """
    
    # Run the judge
    # Note: Using pydantic-ai's run approach
    result = await judge_agent.run(prompt)
    return result.output
