import asyncio
import os
import requests
from pathlib import Path
from loguru import logger
from typing import Dict, List, Any

# Import existing components
from codebase_rag.services.pr_review.analyzer import StructuralTriage, BlastRadiusDetector, PRReviewPromptGenerator
from codebase_rag.services.pr_review.fetcher import RepoFetcher
from codebase_rag.services.semantic_sentinel import SemanticSentinel

# --- Constants ---
MAX_CALLERS_TO_FETCH = 20  # Don't fetch more than 20 caller snippets

class PocketStrategyRouter:
    """
    The Decision Logic (The "Pocket")
    Routes PRs to one of 5 Tiers based on risk and budget.
    
    Tiers:
    - Tier 0: The Sentinel (Zero-LLM)
    """
    def __init__(self, project_name="TUTORIAL_RAG"):
        self.project_name = project_name
        self.triage = StructuralTriage()
        self.blast_detector = BlastRadiusDetector(project_name=project_name)
        self.synthesis = PRReviewPromptGenerator(project_name=project_name)
        self.sentinel = SemanticSentinel()  # NEW: Vector-based analyzer
        
        # State for interactive loop (Tier 4)
        self.repo_root = None

    async def route_and_review(self, pr_data: Dict[str, Any], repo_root=None, project_name=None, on_progress=None) -> str:
        """
        Main entry point for the router.
        Analyze -> Decide -> Execute.
        """
        async def report(pct, msg):
             if on_progress: await on_progress(pct, msg)
        if project_name:
            self.project_name = project_name
            self.blast_detector.project_name = project_name
            self.synthesis.project_name = project_name
            self.sentinel.project_name = project_name
            
        self.repo_root = repo_root
        self.synthesis.repo_root = repo_root
        self.blast_detector.repo_root = repo_root

        logger.info(f"--- 🚦 Pocket Strategy Router: Analyzing PR for '{self.project_name}' ---")

        # 1. Triage (Step 1)
        changes = pr_data.get('changes', [])
        triage_report = self.triage.generate_manifest(changes)
        
        logic_files = triage_report.get('high_priority_review', [])
        logic_file_count = len(logic_files)
        
        # 2. Blast Radius (Step 2)
        impact_report = []
        if logic_files:
            impact_report = self.blast_detector.analyze_impact(logic_files, repo_root=repo_root)
        
        blast_radius_score = self._calculate_blast_score(impact_report)
        has_critical_impact = self._check_critical_keywords(impact_report)

        # 3. Decision Logic
        tier = self._decide_tier(logic_file_count, blast_radius_score, has_critical_impact, pr_data)
        
        logger.info(f"📊 Analysis: Logic Files={logic_file_count}, Blast Radius={blast_radius_score}, Critical={has_critical_impact}")
        logger.info(f"🎯 Selected Strategy: {tier}")

        # Get fingerprints for reporting
        fingerprints = triage_report.get('fingerprints', {})

        # 4. Execution based on Tier
        if tier == "TIER_2":
            return await self._execute_tier_2_project_map(
                pr_data, triage_report, impact_report, blast_radius_score, on_progress
            )
        elif tier == "TIER_1":
            return await self._execute_tier_1_context_builder(
                pr_data, triage_report, impact_report, blast_radius_score, on_progress
            )
        else:
            # Default to Tier 0 (Sentinel)
            return await self._execute_tier_0_mechanic(
                triage_report, 
                changes=changes, 
                impact_report=impact_report, 
                blast_radius=blast_radius_score,
                pr_data=pr_data
            )

    def _decide_tier(self, logic_count, blast_score, is_critical, pr_data):
        """
        Decide which tier to use based on mode or auto-detection.
        Modes: 'sentinel'/'tier0' -> TIER_0, 'tier1' -> TIER_1, 'tier2' -> TIER_2, 'auto' -> auto-detect
        """
        mode = pr_data.get('mode', 'auto').lower()
        
        if mode in ['sentinel', 'tier0']:
            return "TIER_0"
        if mode == 'tier1':
            return "TIER_1"
        if mode == 'tier2':
            return "TIER_2"
        
        # Auto mode: Use Tier 1 if there's ANY logic change
        if logic_count > 0:
            return "TIER_1"
        
        # Default to Tier 0 (Zero-cost Sentinel)
        return "TIER_0"


    def _calculate_blast_score(self, impact_report):
        score = 0
        for item in impact_report:
            score += len(item.get('callers', []))
        return score

    def _check_critical_keywords(self, impact_report):
        critical_keywords = ['auth', 'billing', 'crypto', 'security', 'permission', 'secret', 'key']
        for item in impact_report:
            context = (item['function'] + item['file']).lower()
            if any(k in context for k in critical_keywords):
                return True
        return False

    # ---------------------------------------------------------
    # TIER 0: THE SENTINEL (Cost: $0.00, Low-Noise)
    # At most 3 public comments: Bug, Missing Test, Risky Area
    # Everything else goes into private report.
    # ---------------------------------------------------------
    async def _execute_tier_0_mechanic(self, triage_report, changes=None, impact_report=None, blast_radius=0, pr_data=None):
        logger.info("💰 Estimated Cost: $0.00 (Tier 0 - Semantic Sentinel)")
        
        if changes is None:
            changes = []
        if impact_report is None:
            impact_report = []
        if pr_data is None:
            pr_data = {}
        
        # Extract metadata
        metadata = pr_data.get('metadata', {})
        pr_number = pr_data.get('pr_number', 'N/A')
        
        # Run Semantic Sentinel analysis
        self.sentinel.repo_root = self.repo_root
        signals = self.sentinel.analyze_changes(changes, triage_report)
        
        # Get triage categories
        mechanical = triage_report.get('automated_verified_refactors', [])
        config = triage_report.get('configuration_changes', [])
        docs = triage_report.get('documentation_updates', [])
        tests = triage_report.get('test_code_updates', [])
        logic = triage_report.get('high_priority_review', [])
        fingerprints = triage_report.get('fingerprints', {})
        
        # Risk Level Calculation
        if blast_radius >= 50:
            risk_level = "🔴 HIGH"
            risk_emoji = "🚨"
        elif blast_radius >= 10:
            risk_level = "🟡 MEDIUM"
            risk_emoji = "⚠️"
        elif len(logic) > 0:
            risk_level = "🟡 MEDIUM"
            risk_emoji = "⚠️"
        else:
            risk_level = "🟢 LOW"
            risk_emoji = "✅"
        
        # --- BUILD OUTPUT ---
        output = []
        
        # Header
        output.append(f"# {risk_emoji} Cognit Sentinel Report")
        output.append("")
        output.append("| Metric | Value |")
        output.append("|--------|-------|")
        output.append(f"| **Risk Level** | {risk_level} |")
        output.append(f"| **Blast Radius** | {blast_radius} callers |")
        output.append(f"| **Logic Files** | {len(logic)} |")
        output.append(f"| **Signals** | {len(signals)} |")
        output.append(f"| **Cost** | $0.00 |")
        output.append("")
        
        # What Changed
        if logic:
            output.append("## 🔍 What Changed (Logic Core)")
            output.append("")
            for f in logic[:5]:
                file_path = f.get('file', 'Unknown')
                file_name = Path(file_path).name
                fp = fingerprints.get(file_path, {})
                diff_hint = fp.get('diff_hint', 'Logic changed')
                output.append(f"- **`{file_name}`**: {diff_hint}")
            output.append("")
        
        # KEY FINDINGS (PUBLIC - Max 3)
        if signals:
            output.append("## 🎯 Key Findings")
            output.append("")
            for i, signal in enumerate(signals[:3], 1):
                emoji = "🐛" if signal.signal_type == "BUG" else "🧪" if signal.signal_type == "MISSING_TEST" else "⚠️"
                conf_pct = int(signal.confidence * 100)
                output.append(f"### {i}. {emoji} {signal.signal_type} ({conf_pct}%)")
                output.append(f"> {signal.message}")
                if signal.file:
                    output.append(f"> *File: `{Path(signal.file).name}`*")
                if signal.details:
                    output.append(f"> *{signal.details}*")
                output.append("")
        else:
            output.append("## ✅ No Issues Detected")
            output.append("")
            output.append("> All checks passed. Safe to merge.")
            output.append("")
        
        # BLAST RADIUS (if significant)
        if impact_report and blast_radius > 0:
            output.append("## 📊 Blast Radius")
            output.append("")
            
            # Group callers by file
            file_callers = {}
            for item in impact_report:
                for caller_qn in item.get('callers', [])[:20]:
                    parts = caller_qn.split('.')
                    if len(parts) >= 2:
                        func_name = parts[-1]
                        file_name = parts[-2] + ".py" if len(parts) > 1 else "module.py"
                        if file_name not in file_callers:
                            file_callers[file_name] = set()
                        file_callers[file_name].add(func_name)
            
            if file_callers:
                output.append("| File | Functions |")
                output.append("|------|-----------|")
                for file_name, funcs in sorted(file_callers.items(), key=lambda x: -len(x[1]))[:10]:
                    func_list = ", ".join(sorted(list(funcs))[:3])
                    if len(funcs) > 3:
                        func_list += f" (+{len(funcs) - 3})"
                    output.append(f"| `{file_name}` | {func_list} |")
                output.append("")
        
        # PRIVATE BREAKDOWN (Collapsible)
        output.append("---")
        output.append("")
        output.append("<details>")
        output.append("<summary>📋 Full Breakdown (Private)</summary>")
        output.append("")
        
        # PR Metadata
        if metadata:
            output.append(f"**Author**: {metadata.get('author', 'Unknown')}")
            if metadata.get('date'):
                output.append(f"**Date**: {metadata.get('date', '')[:10]}")
            if metadata.get('commits'):
                output.append(f"**Commits**: {metadata.get('commits', 0)}")
            if metadata.get('message'):
                output.append(f"**Message**: {metadata.get('message', '')[:80]}")
            output.append("")
        
        # File categories
        if mechanical:
            output.append(f"- **Mechanical/Refactor**: {len(mechanical)} files")
        if config:
            output.append(f"- **Configuration**: {len(config)} files")
        if docs:
            output.append(f"- **Documentation**: {len(docs)} files")
        if tests:
            output.append(f"- **Test Code**: {len(tests)} files")
        if not (mechanical or config or docs or tests or metadata):
            output.append("*No additional items.*")
        output.append("")
        output.append("</details>")
        output.append("")
        
        # Footer
        output.append("---")
        output.append("*Analysis: Tree-sitter AST + Qdrant Vectors + Memgraph Dependencies*")
        
        return "\n".join(output)

    # ---------------------------------------------------------
    # TIER 1: RAG ORCHESTRATOR (Cost: 2-5 LLM Calls)
    # True Agentic approach using pydantic-ai
    # ---------------------------------------------------------
    async def _execute_tier_1_context_builder(self, pr_data, triage_report, impact_report, blast_radius, on_progress=None):
        """
        Tier 1: Agentic RAG Orchestrator
        Uses the pydantic-ai Agent with real tools:
        - semantic_code_search
        - query_codebase_knowledge_graph
        - read_file_content
        """
        import time
        from codebase_rag.services.llm import create_rag_orchestrator, CypherGenerator
        from codebase_rag.tools.semantic_search import create_semantic_search_tool
        from codebase_rag.tools.codebase_query import create_query_tool
        from codebase_rag.tools.file_reader import create_file_reader_tool, FileReader
        from codebase_rag.services.graph_service import MemgraphIngestor
        from codebase_rag.config import settings
        from pydantic_ai import DeferredToolRequests
        
        async def report(pct, msg):
             if on_progress: await on_progress(pct, msg)

        start_time = time.time()
        logger.info("=" * 60)
        logger.info("🚀 TIER 1: RAG ORCHESTRATOR - Starting Autonoumous Agent")
        logger.info("=" * 60)
        
        # 1. Initialize Components
        logger.info("🛠️ Initializing Tools & Agent...")
        try:
            # Force settings to use correct Ollama model
            settings.set_orchestrator(
                provider="ollama",
                model="gpt-oss:latest",
                endpoint="http://localhost:11434/v1"
            )
            # Ensure Cypher Generator uses the same model (fixing 404 error)
            settings.set_cypher(
                provider="ollama",
                model="gpt-oss:latest",
                endpoint="http://localhost:11434/v1"
            )
            
            # Cypher Generator for Graph Tool
            cypher_gen = CypherGenerator()
            ingestor = MemgraphIngestor(
                host=settings.MEMGRAPH_HOST, 
                port=settings.MEMGRAPH_PORT,
                username=settings.MEMGRAPH_USER,
                password=settings.MEMGRAPH_PASSWORD
            )
            
            # Create Tools
            # IMPORTANT: Use the actual cloned repo root, otherwise we look in the wrong directory
            root_path = self.repo_root if self.repo_root else "."
            logger.info(f"📂 FileReader Root: {root_path} (Project: {self.project_name})")
            
            file_reader = FileReader(project_root=root_path)
            tools = [
                create_semantic_search_tool(project_name=self.project_name),
                create_query_tool(ingestor, cypher_gen, project_name=self.project_name),
                create_file_reader_tool(file_reader)
            ]
            
            # Create Agent
            agent = create_rag_orchestrator(tools)
            logger.info(f"🤖 Agent initialized with model: {agent.model.model_name}")
            
        except Exception as e:
            logger.error(f"❌ Failed to init agent: {e}")
            return f"Error initializing RAG Agent: {e}"

        # 2. Construct Prompt
        changes = pr_data.get('changes', [])
        metadata = pr_data.get('metadata', {})
        
        # Import HyDE summarizer and Judge for iterative critique
        from codebase_rag.services.diff_summarizer import summarize_pr_intent
        from codebase_rag.services.judge import judge_pr_review
        
        # Create a summary of changes for the initial prompt
        change_summary = []
        diff_content = []
        
        import difflib
        
        for c in changes[:3]:  # Limit to first 3 files
            file_path = c.get('file', '')
            old = c.get('old_content', '')
            new = c.get('new_content', '')
            change_summary.append(f"- {file_path}")
            
            # Generate ACTUAL unified diff - this is what really changed
            if old and new:
                old_lines = old.splitlines(keepends=True)
                new_lines = new.splitlines(keepends=True)
                diff = list(difflib.unified_diff(old_lines, new_lines, fromfile='before', tofile='after', lineterm=''))
                
                # Only show the changed lines (skip the header and context)
                diff_lines = [l for l in diff if l.startswith('+') or l.startswith('-')]
                
                if diff_lines:
                    diff_content.append(f"### {file_path}\n```diff\n{''.join(diff_lines)}\n```")
                else:
                    diff_content.append(f"### {file_path}\n*(No line changes detected)*")
            elif new:
                # New file - show first 10 lines
                preview = '\n'.join(new.splitlines()[:10])
                diff_content.append(f"### {file_path} (NEW FILE)\n```\n{preview}\n```")
            elif old:
                diff_content.append(f"### {file_path} (DELETED)")
        
        # Get blast radius info from function parameters (calculated earlier)
        # Also format impact report for agent context
        callers_info = []
        for item in impact_report[:5]:  # Top 5 impacted functions
            callers_info.append(f"- {item.get('function', '?')} called by: {', '.join(item.get('callers', [])[:3])}")
        
        # 2.5 HyDE Pre-Analysis: Understand PR intent before querying
        diff_text_for_hyde = chr(10).join(diff_content) if diff_content else "No diff available"
        await report(45, "Running HyDE Pre-Analysis...")
        logger.info("🔍 Running HyDE Pre-Analysis...")
        try:
            hyde_summary = await summarize_pr_intent(diff_text_for_hyde, metadata)
            hyde_context = f"""
## 🧠 PR Intent (Pre-Analyzed by HyDE)
**Purpose:** {hyde_summary.intent}
**Is Refactor:** {hyde_summary.is_refactor}

### Key Changes Detected:
{chr(10).join([f'- {c}' for c in hyde_summary.key_changes])}

### Suggested Search Queries:
{chr(10).join([f'- `{q}`' for q in hyde_summary.search_queries])}

### Risk Areas to Investigate:
{chr(10).join([f'- {r}' for r in hyde_summary.risk_areas])}
"""
            logger.info(f"✅ HyDE found intent: {hyde_summary.intent[:100]}...")
        except Exception as e:
            logger.warning(f"⚠️ HyDE pre-analysis failed: {e}")
            hyde_context = "\n*(HyDE pre-analysis unavailable)*\n"
            
        # Check for breadcrumb context from Tier 2
        breadcrumb_section = ""
        if pr_data.get('breadcrumb_context'):
            breadcrumb_section = f"""
## 🗺️ Project Map (Module Summaries):
{pr_data.get('breadcrumb_context')}
"""
            
        initial_prompt = f"""
# PR Review Request

**Author:** {metadata.get('author', 'Unknown')}
**Files:** {len(changes)} changed
**Blast Radius:** {blast_radius} functions potentially affected

## Changed Files:
{chr(10).join(change_summary)}

## Known Callers (from Graph):
{chr(10).join(callers_info) if callers_info else "No callers found in graph."}
{breadcrumb_section}
## Diff:
{chr(10).join(diff_content) if diff_content else "⚠️ No diff content available - files may be new or empty."}

---

{hyde_context}

# 🐐 STAFF ENGINEER CODE REVIEW

You are reviewing this PR as if you wrote this entire codebase. Provide a **thorough, low-noise review**.

**CRITICAL: Use the HyDE search queries above to find relevant code in the graph and vector DB.**

## YOUR ANALYSIS STEPS:

### Step 1: Read the Diff Carefully
- Look at what ACTUALLY changed (lines starting with + or -)
- Do NOT assume changes that are not in the diff
- If the diff shows "No diff" or is empty, state that clearly

### Step 2: Check Blast Radius  
- Use `query_codebase_knowledge_graph` to find callers of modified functions
- If graph returns no results, note that the function may be new or isolated

### Step 3: Assess Risk
- New files = LOW risk (no existing code depends on them)
- Modified core functions with many callers = HIGHER risk
- Documentation/README changes = NONE risk

## OUTPUT FORMAT (REQUIRED):

```markdown
## ✅ APPROVE | ⚠️ REQUEST CHANGES | ❌ REJECT

**Summary:** [1-2 sentence summary of what this PR actually does based on the diff]

### 🎯 Blast Radius Analysis

| Function | What Changed | Callers | Impact |
|----------|-------------|---------|--------|
| [function name] | [actual change from diff] | [callers from graph] | [LOW/MEDIUM/HIGH] |

### 📋 Changes by File

| File | Changes | Risk |
|------|---------|------|
| [filename] | [what changed] | [risk level] |

### 🚨 Issues Found

[List actual bugs or concerns, or state "No issues found"]

### Risk: [LOW/MEDIUM/HIGH]
```

## RULES:
1. **ONLY describe changes visible in the diff** - do NOT hallucinate
2. **If diff is empty**, say "No changes detected" - do NOT invent changes
3. **Read the actual file content** - use read_file_content tool if needed
4. **Use the graph** - query for callers and dependencies
5. **Be concise** - avoid repeating the diff verbatim

NOW: Analyze the diff above and output your review.
"""
        
        # Debug: Log the actual diff content
        logger.debug(f"📝 Diff content being sent to LLM:\n{chr(10).join(diff_content)}")
        logger.info(f"📝 Initial Prompt: {initial_prompt[:100]}...")
        
        from pydantic_ai import DeferredToolRequests, DeferredToolResults
        
        # 3. Interactive Loop (Tier 4 style)
        current_prompt = initial_prompt
        message_history = []
        
        # Add tool names to trace
        tool_log = []
        llm_call_count = 0
        MAX_TURNS = 10  # Increased limit
        
        await report(50, "Agent Starting Analysis Loop...")
        
        deferred_results = None

        for turn in range(MAX_TURNS):
            if llm_call_count > 0 and deferred_results is None:
                # Add continuation prompt context if loop continues without tools (rare)
                current_prompt = "Result processed. Continue analysis or finalize."
            elif deferred_results:
                # If we have results, the prompt is implicitly handled by pydantic-ai matching?
                # or we just pass the same prompt?
                # PydanticAI usually expects the same prompt or empty if continuing?
                # We'll keep current_prompt
                pass
            
            try:
                llm_call_count += 1
                progress_step = 50 + min(35, (turn * 4)) # Increment slowly up to 85%
                await report(progress_step, f"Agent Thinking (Turn {turn+1})...")
                logger.info(f"🔄 Step {turn+1}: Thinking... (Call #{llm_call_count})")
                
                result = await agent.run(
                    current_prompt, 
                    message_history=message_history,
                    deferred_tool_results=deferred_results
                )
                
                # Update history
                if hasattr(result, 'new_messages'):
                    message_history.extend(result.new_messages())
                
                # Reset deferred results for next turn (unless we generate new ones)
                deferred_results = None
                
                logger.debug(f"DEBUG RESULT ATTRIBUTES: {dir(result)}")
                
                # Check output - accessing .output (matching llm.py usage)
                if isinstance(result.output, str):
                    logger.success("✅ Agent finalized answer!")
                    final_answer = result.output
                    break
                
                elif isinstance(result.output, DeferredToolRequests):
                    logger.info("🛠️  Agent requested tools...")
                    
                    # Create container for results
                    deferred_results = DeferredToolResults()
                    
                    # Inspect the requests
                    # Handle both .calls (newer) and .approvals (older) just in case
                    calls = getattr(result.output, 'calls', getattr(result.output, 'approvals', []))
                    
                    for call in calls:
                        t_name = call.tool_name
                        t_args = call.args_as_dict()
                        t_id = call.tool_call_id
                        
                        logger.info(f"   🔧 Executing: {t_name}")
                        logger.debug(f"      Args: {str(t_args)[:100]}...")
                        tool_log.append(f"{t_name}")
                        
                        # Auto-approve
                        if deferred_results.approvals is not None:
                             deferred_results.approvals[t_id] = True
                    
                    # Continue loop to feed results back
                    continue
                    
            except Exception as e:
                logger.error(f"❌ Agent loop failed: {e}")
                final_answer = f"Agent failed: {e}"
                import traceback
                logger.error(traceback.format_exc())
                break

        elapsed = time.time() - start_time
        
        # Count tools from message history (pydantic-ai auto-executes them)
        tool_count = 0
        for msg in message_history:
            if hasattr(msg, 'parts'):
                for part in msg.parts:
                    if hasattr(part, 'tool_name'):  # ToolReturnPart has tool_name
                        tool_count += 1
        
        # Log Summary
        logger.info("=" * 60)
        logger.info("✅ TIER 1 COMPLETE - Agent Summary:")
        logger.info(f"   🤖 LLM Calls: {llm_call_count}")
        logger.info(f"   🛠️ Tool Calls: {tool_count}")
        logger.info(f"   ⏱️ Total Time: {elapsed:.1f}s")
        logger.info("=" * 60)
        
        # ========================================================
        # 5. ITERATIVE CRITIQUE LOOP
        # Judge evaluates review, Agent improves until score >= 7
        # ========================================================
        PASSING_SCORE = 7
        MAX_CRITIQUE_ITERATIONS = 2  # Max 2 additional attempts
        
        diff_text_for_judge = chr(10).join(diff_content) if diff_content else "No diff"
        best_answer = final_answer
        best_score = 0
        
        for critique_round in range(MAX_CRITIQUE_ITERATIONS + 1):
            if critique_round == 0:
                await report(85, "Judge Evaluating Initial Review...")
                logger.info("⚖️  Judge is evaluating initial review...")
            else:
                prog = 85 + (critique_round * 5)
                await report(prog, f"Judge Evaluating Revision #{critique_round}...")
                logger.info(f"⚖️  Judge evaluating revision #{critique_round}...")
            
            try:
                score_card = await judge_pr_review(
                    diff_text=diff_text_for_judge,
                    review_text=best_answer,
                    pr_metadata=metadata
                )
                
                current_score = score_card.score
                logger.info(f"   📊 Score: {current_score}/10 | Noise: {score_card.noise_level}")
                
                if current_score >= PASSING_SCORE:
                    logger.success(f"✅ Review PASSED Judge (Score: {current_score}/10)")
                    best_answer = best_answer
                    best_score = current_score
                    break
                    
                if current_score > best_score:
                    best_score = current_score
                    
                # Last iteration? Use best effort
                if critique_round == MAX_CRITIQUE_ITERATIONS:
                    logger.warning(f"⚠️ Max iterations reached. Best score: {best_score}/10")
                    break
                
                # Inject critique and retry
                logger.info(f"🔄 Score {current_score}/10 < {PASSING_SCORE}. Refining with Judge feedback...")
                
                critique_prompt = f"""
# 🔄 REVISION REQUEST

Your previous review scored **{current_score}/10**. The Judge found these issues:

## Judge's Critique:
{score_card.reasoning}

## Specific Improvements Needed:
{chr(10).join([f'- {tip}' for tip in score_card.improvement_tips])}

## What You Missed:
{chr(10).join([f'- {miss}' for miss in score_card.missing_context])}

---

**REVISE YOUR REVIEW** addressing all the above issues. Be more specific, accurate, and concise.

## Original PR Context:
{diff_text_for_judge[:5000]}

Output ONLY the revised review (no explanations).
"""
                
                # Run agent again with critique feedback
                revision_result = await agent.run(critique_prompt)
                if hasattr(revision_result, 'output') and isinstance(revision_result.output, str):
                    best_answer = revision_result.output
                    llm_call_count += 1
                    
            except Exception as e:
                logger.error(f"❌ Judge evaluation failed: {e}")
                break
        
        final_answer = best_answer

        # 6. Format Output - Keep it clean, just show the agent's answer
        output = []
        output.append(final_answer)
        output.append("")
        output.append(f"---")
        output.append(f"*⏱️ {elapsed:.1f}s | 🤖 {llm_call_count} calls | 🛠️ {tool_count} tools | 📊 Score: {best_score}/10*")
        output.append("")
        
        output.append("<details>")
        output.append("<summary>📊 Agent Execution Log</summary>")
        output.append("")
        for i, t in enumerate(tool_log):
             output.append(f"{i+1}. Used tool: `{t}`")
        output.append("")
        output.append("</details>")
        
        return "\n".join(output)
    
    def _build_graph_context(self, impact_report) -> str:
        """Build context from Memgraph graph relationships."""
        lines = []
        for item in impact_report[:5]:
            func_name = item.get('function', 'Unknown')
            callers = item.get('callers', [])[:5]
            if callers:
                lines.append(f"- `{func_name}` is called by: {', '.join(callers[:3])}")
        return "\n".join(lines) if lines else ""
    
    # ---------------------------------------------------------
    # TIER 2: PROJECT MAP (Summary-Enhanced Review)
    # Uses pre-generated summaries for context navigation
    # ---------------------------------------------------------
    async def _execute_tier_2_project_map(self, pr_data, triage_report, impact_report, blast_radius, on_progress=None):
        """
        Tier 2: Enhanced review using project summaries.
        
        1. Query summaries from Memgraph
        2. Build breadcrumb context (Location + Intent + Parent + Dependencies)
        3. Run Tier 1 agent with enhanced context
        """
        import time
        from pathlib import Path
        from codebase_rag.services.graph_service import MemgraphIngestor
        from codebase_rag.config import settings
        
        async def report(pct, msg):
            if on_progress: await on_progress(pct, msg)
        
        start_time = time.time()
        logger.info("=" * 60)
        logger.info("🗺️ TIER 2: PROJECT MAP - Summary-Enhanced Review")
        logger.info("=" * 60)
        
        await report(45, "Fetching project summaries...")
        
        changes = pr_data.get('changes', [])
        metadata = pr_data.get('metadata', {})
        
        # 1. Query summaries for changed files
        breadcrumb_context = []
        
        with MemgraphIngestor(
            settings.MEMGRAPH_HOST,
            settings.MEMGRAPH_PORT,
            username=settings.MEMGRAPH_USER,
            password=settings.MEMGRAPH_PASSWORD
        ) as ingestor:
            for change in changes[:5]:  # Limit to 5 files
                filepath = change.get('file', '')
                filename = Path(filepath).name
                dirpath = str(Path(filepath).parent)
                
                # Query file summary
                file_result = ingestor.fetch_all("""
                    MATCH (f:File)
                    WHERE f.path ENDS WITH $filepath OR f.name = $filename
                    RETURN f.summary AS summary, f.path AS path
                    LIMIT 1
                """, {"filepath": filepath, "filename": filename})
                
                file_summary = file_result[0]['summary'] if file_result and file_result[0].get('summary') else "No summary available"
                
                # Query directory summary
                dir_result = ingestor.fetch_all("""
                    MATCH (d:Directory)
                    WHERE d.path ENDS WITH $dirpath
                    RETURN d.summary AS summary
                    LIMIT 1
                """, {"dirpath": dirpath})
                
                dir_summary = dir_result[0]['summary'] if dir_result and dir_result[0].get('summary') else "No directory summary"
                
                # Query callers from impact report
                callers = []
                for item in impact_report:
                    if filepath in item.get('file', ''):
                        callers = item.get('callers', [])[:3]
                        break
                
                # Build breadcrumb
                breadcrumb = f"""
## 📍 {filepath}
- **Module Intent:** {file_summary}
- **Parent Context:** {dir_summary}
- **Callers:** {', '.join(callers) if callers else 'No callers found'}
"""
                breadcrumb_context.append(breadcrumb)
        
        logger.info(f"📋 Built {len(breadcrumb_context)} breadcrumb contexts")
        
        await report(50, "Running enhanced agent with project map...")
        
        # 2. Inject breadcrumb context into Tier 1 prompt
        # Store original context and add summaries
        original_pr_data = pr_data.copy()
        
        # Add breadcrumb context to metadata for prompt construction
        if 'breadcrumb_context' not in original_pr_data:
            original_pr_data['breadcrumb_context'] = chr(10).join(breadcrumb_context)
        
        # 3. Run Tier 1 with enhanced context
        # We modify the prompt in Tier 1 to include breadcrumbs
        result = await self._execute_tier_1_context_builder(
            original_pr_data, triage_report, impact_report, blast_radius, on_progress
        )
        
        elapsed = time.time() - start_time
        
        # Append Tier 2 marker
        tier_marker = f"\n\n---\n*🗺️ Tier 2 Project Map | {elapsed:.1f}s total*"
        
        return result + tier_marker
    
    async def _build_vector_context(self, changes) -> str:
        """Find semantically similar code using Qdrant."""
        lines = []
        try:
            from codebase_rag.vector_store import search_embeddings, get_qdrant_client
            from codebase_rag.embedder import embed_code
            from codebase_rag.config import settings
            
            client = get_qdrant_client()
            
            for change in changes[:3]:
                new_content = change.get('new_content', '')
                if len(new_content) < 100:
                    continue
                
                # Take first 500 chars for embedding
                snippet = new_content[:500]
                try:
                    logger.debug(f"🔮 Embedding {len(snippet)} chars from {change.get('file', 'unknown')}")
                    embedding = embed_code(snippet)
                    results = search_embeddings(embedding, top_k=3)
                    
                    if results:
                        # Get qualified names (Contextual Chunk Headers)
                        similar_ids = [r[0] for r in results]
                        points = client.retrieve(collection_name=settings.QDRANT_COLLECTION_NAME, ids=similar_ids)
                        similar_names = [p.payload.get('qualified_name', '') for p in points if p.payload]
                        
                        file_name = Path(change.get('file', '')).name
                        if similar_names:
                            lines.append(f"- `{file_name}` is similar to: {', '.join(similar_names[:3])}")
                            logger.info(f"🔗 Contextual Headers: {similar_names[:3]}")
                except Exception as e:
                    logger.debug(f"Vector search failed for {change.get('file')}: {e}")
                    continue
                    
        except Exception as e:
            logger.warning(f"⚠️ Vector context error: {e}")
        
        return "\n".join(lines) if lines else ""
    
    def _build_changes_summary(self, logic_files, fingerprints) -> str:
        """Summarize code changes from triage."""
        lines = []
        for f in logic_files[:5]:
            file_path = f.get('file', 'Unknown')
            file_name = Path(file_path).name
            fp = fingerprints.get(file_path, {})
            diff_hint = fp.get('diff_hint', 'Modified')
            lines.append(f"- `{file_name}`: {diff_hint}")
        return "\n".join(lines) if lines else ""
    
    async def _call_llm(self, prompt: str) -> str:
        """Call LLM for review. Tries Ollama first (local), falls back to Gemini."""
        import os
        import time
        
        prompt_preview = prompt[:200].replace('\n', ' ')
        logger.info(f"🤖 LLM Request: '{prompt_preview}...' ({len(prompt)} chars)")
        
        start_time = time.time()
        
        # Try Ollama first (local, no cost)
        try:
            logger.info("📡 Calling Ollama (gpt-oss:latest) - 20.9B model, may take 2-5 min...")
            resp = requests.post(
                "http://localhost:11434/api/generate",
                json={"model": "gpt-oss:latest", "prompt": prompt, "stream": False},
                timeout=300  # 5 min timeout for 20.9B model
            )
            if resp.status_code == 200:
                response_text = resp.json().get('response', '').strip()
                elapsed = time.time() - start_time
                logger.success(f"✅ LLM Response: {len(response_text)} chars in {elapsed:.1f}s")
                logger.debug(f"📝 Response preview: {response_text[:200]}...")
                if response_text:
                    return response_text
                else:
                    logger.warning("⚠️ Ollama returned empty response")
            else:
                logger.warning(f"⚠️ Ollama returned status {resp.status_code}: {resp.text[:200]}")
        except Exception as e:
            logger.warning(f"⚠️ Ollama failed: {e}")
        
        # Fallback to Gemini
        gemini_key = os.environ.get('GEMINI_API_KEY')
        if gemini_key:
            try:
                logger.info("📡 Falling back to Gemini...")
                import google.generativeai as genai
                genai.configure(api_key=gemini_key)
                model = genai.GenerativeModel('gemini-2.0-flash')
                response = model.generate_content(prompt)
                elapsed = time.time() - start_time
                logger.success(f"✅ Gemini Response: {len(response.text)} chars in {elapsed:.1f}s")
                return response.text
            except Exception as e:
                logger.warning(f"⚠️ Gemini failed: {e}")
        
        logger.error("❌ All LLMs unavailable")
        return "*LLM unavailable*"


    # ---------------------------------------------------------
    # HELPERS
    # ---------------------------------------------------------
    def _local_llm_compress(self, text):
        """
        Calls Ollama to compress context.
        Fallback: Skip compression if Ollama is offline.
        """
        try:
            url = "http://localhost:11434/api/generate"
            payload = {
                "model": "qwen2.5-coder:1.5b",
                "prompt": f"Summarize this code context for a reviewer:\n{text[:2000]}",
                "stream": False
            }
            resp = requests.post(url, json=payload, timeout=5)
            if resp.status_code == 200:
                result = resp.json().get('response', '').strip()
                if result:
                    return result
        except Exception as e:
            logger.warning(f"⚠️ Ollama offline: {e}. Skipping compression.")
        
        # Fallback: Return truncated original
        return text[:500] + "..." if len(text) > 500 else text

    def _fetch_context_text(self, item):
        """Fetch caller snippets."""
        context_text = ""
        callers = item.get('callers', [])[:MAX_CALLERS_TO_FETCH]
        
        for caller_qn in callers:
            path_str = self.synthesis._resolve_path_from_qn(caller_qn)
            if path_str:
                snippet = self.synthesis._read_snippet(path_str, item['function'].split('.')[-1])
                if snippet:
                    context_text += f"\nCaller {caller_qn}:\n{snippet}\n"
        return context_text

    def _fetch_file_content(self, relative_path):
        if not self.repo_root: return ""
        target = self.repo_root / relative_path
        if target.exists():
            return target.read_text(encoding='utf-8')
        return ""
