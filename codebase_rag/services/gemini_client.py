"""
Gemini Client for Tier 2 Summaries

Uses Gemini 2.0 Flash Lite for budget-friendly code summarization.
Focuses on BUSINESS INTENT, not structure (Tree-sitter handles that).
"""

import os
import asyncio
import hashlib
from typing import Optional
from loguru import logger

import google.generativeai as genai
from google.generativeai.types import HarmCategory, HarmBlockThreshold

from codebase_rag.config import settings


class GeminiClient:
    """
    Lightweight Gemini client for code summarization.
    Uses gemini-2.0-flash-lite for cost efficiency.
    """
    
    MODEL = "gemini-2.0-flash-lite"
    
    # Safety settings - allow code analysis
    SAFETY_SETTINGS = {
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
    }
    
    def __init__(self, api_key: Optional[str] = None):
        """
        Initialize with API key from param, config, or environment.
        """
        self.api_key = api_key or settings.GEMINI_API_KEY or os.getenv("GEMINI_API_KEY", "")
        
        if not self.api_key:
            logger.warning("⚠️ GEMINI_API_KEY not set. Tier 2 summaries will be disabled.")
            self.enabled = False
            return
            
        genai.configure(api_key=self.api_key)
        self.model = genai.GenerativeModel(self.MODEL)
        self.enabled = True
        logger.info(f"✅ Gemini client initialized with model: {self.MODEL}")
    
    async def summarize_intent(self, code: str, filepath: str) -> str:
        """
        Generate a 1-2 sentence summary of the file's BUSINESS PURPOSE.
        
        Does NOT extract structure (functions, imports) - that's Tree-sitter's job.
        Focuses on: Why does this code exist? Is it critical? Handles PII?
        """
        if not self.enabled:
            return ""
        
        prompt = f"""You are a Senior Engineer documenting a codebase.
For this file: {filepath}

Explain in 1-2 sentences:
1. What is the BUSINESS PURPOSE of this code?
2. Is it critical path? Does it handle sensitive data (PII, auth, payments)?

Code:
```
{code[:8000]}
```

Respond with ONLY the summary, no preamble."""

        try:
            response = await asyncio.to_thread(
                self.model.generate_content,
                prompt,
                safety_settings=self.SAFETY_SETTINGS,
                generation_config=genai.types.GenerationConfig(
                    max_output_tokens=150,
                    temperature=0.2,
                )
            )
            summary = response.text.strip()
            logger.debug(f"📝 Summarized {filepath}: {summary[:80]}...")
            return summary
        except Exception as e:
            logger.warning(f"⚠️ Gemini summarize failed for {filepath}: {e}")
            return ""
    
    async def aggregate_summaries(self, directory: str, file_summaries: list[str]) -> str:
        """
        Aggregate file summaries into a directory-level summary.
        
        This creates a "logical hierarchy" where the folder summary
        is an abstraction of its contents.
        """
        if not self.enabled or not file_summaries:
            return ""
        
        summaries_text = "\n".join([f"- {s}" for s in file_summaries if s])
        
        prompt = f"""You are a Senior Engineer documenting a codebase.

Directory: {directory}

The files in this directory have these purposes:
{summaries_text}

Based on these file summaries, what is the collective purpose of this directory?
Answer in 1-2 sentences. Focus on BUSINESS PURPOSE.

Respond with ONLY the summary, no preamble."""

        try:
            response = await asyncio.to_thread(
                self.model.generate_content,
                prompt,
                safety_settings=self.SAFETY_SETTINGS,
                generation_config=genai.types.GenerationConfig(
                    max_output_tokens=100,
                    temperature=0.2,
                )
            )
            summary = response.text.strip()
            logger.debug(f"📁 Aggregated {directory}: {summary[:80]}...")
            return summary
        except Exception as e:
            logger.warning(f"⚠️ Gemini aggregate failed for {directory}: {e}")
            return ""
    
    @staticmethod
    def compute_content_hash(content: str) -> str:
        """
        Compute a hash of the content for change detection.
        Used to skip re-summarizing unchanged files.
        """
        return hashlib.md5(content.encode('utf-8')).hexdigest()[:16]


# Singleton instance
_gemini_client: Optional[GeminiClient] = None


def get_gemini_client() -> GeminiClient:
    """Get or create the singleton Gemini client."""
    global _gemini_client
    if _gemini_client is None:
        _gemini_client = GeminiClient()
    return _gemini_client
