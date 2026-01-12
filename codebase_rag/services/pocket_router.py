import asyncio
import os
import requests
from pathlib import Path
from loguru import logger
from typing import Dict, List, Any

# Import existing components
from codebase_rag.services.pr_review.analyzer import StructuralTriage, BlastRadiusDetector, PRReviewPromptGenerator
from codebase_rag.services.pr_review.fetcher import RepoFetcher

# --- Constants ---
MAX_CALLERS_TO_FETCH = 20  # Don't fetch more than 20 caller snippets

class PocketStrategyRouter:
    """
    The Decision Logic (The "Pocket")
    Routes PRs to one of 5 Tiers based on risk and budget.
    
    Tiers:
    - Tier 0: The Mechanic ($0.00)
    - Tier 1: The Summarizer (1 Call)
    - Tier 2: The Reviewer (2 Calls - Hybrid)
    - Tier 3: The Council (5 Calls)
    - Tier 4: The Deep Dive (10+ Calls)
    """
    def __init__(self, project_name="TUTORIAL_RAG"):
        self.project_name = project_name
        self.triage = StructuralTriage()
        self.blast_detector = BlastRadiusDetector(project_name=project_name)
        self.synthesis = PRReviewPromptGenerator(project_name=project_name)
        
        # State for interactive loop (Tier 4)
        self.repo_root = None

    async def route_and_review(self, pr_data: Dict[str, Any], repo_root=None) -> str:
        """
        Main entry point for the router.
        Analyze -> Decide -> Execute.
        """
        self.repo_root = repo_root
        self.synthesis.repo_root = repo_root
        self.blast_detector.repo_root = repo_root

        logger.info(f"--- 🚦 Pocket Strategy Router: Analyzing PR ---")

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

        # 4. Execution
        if tier == "TIER_0":
            return await self._execute_tier_0_mechanic(triage_report)
        elif tier == "TIER_1":
            return await self._execute_tier_1_summarizer(pr_data, logic_files, fingerprints)
        elif tier == "TIER_2":
            return await self._execute_tier_2_reviewer(pr_data, impact_report, fingerprints)
        elif tier == "TIER_3":
            return await self._execute_tier_3_council(pr_data, impact_report, changes)
        elif tier == "TIER_4":
            return await self._execute_tier_4_deep_dive(pr_data, impact_report)
        
        return "Error: No valid strategy selected."

    def _decide_tier(self, logic_count, blast_score, is_critical, pr_data):
        # Explicit User Override
        mode = pr_data.get('mode')
        if mode == 'deep_debug': return "TIER_4"
        if mode == 'security': return "TIER_3"
        if mode == 'standard': return "TIER_2"
        if mode == 'fast': return "TIER_1"

        # Auto-routing based on analysis
        if logic_count == 0:
            return "TIER_0"
        
        if logic_count > 0 and blast_score == 0:
            return "TIER_1"
            
        if is_critical:
            return "TIER_3"
            
        if logic_count > 0 and blast_score < 5:
            return "TIER_2"
            
        # Default for moderate PRs (5+ callers)
        return "TIER_2"

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
    # TIER 0: THE MECHANIC (Cost: $0.00)
    # ---------------------------------------------------------
    async def _execute_tier_0_mechanic(self, triage_report):
        logger.info("💰 Estimated Cost: $0.00 (Tier 0)")
        
        breakdown = []
        breakdown.append("## ✅ Tier 0: The Mechanic (No LLM Required)")
        breakdown.append("")
        breakdown.append("**Verdict**: Safe to merge. No structural logic changes detected.")
        breakdown.append("")
        breakdown.append("### 📊 Triage Breakdown (via AST Analysis)")
        
        mechanical = triage_report.get('automated_verified_refactors', [])
        config = triage_report.get('configuration_changes', [])
        docs = triage_report.get('documentation_updates', [])
        tests = triage_report.get('test_code_updates', [])
        logic = triage_report.get('high_priority_review', [])
        
        if mechanical:
            breakdown.append(f"- **Mechanical/Refactor**: {len(mechanical)} files (AST unchanged)")
            for f in mechanical[:5]:
                breakdown.append(f"  - `{f['file']}`")
        if config:
            breakdown.append(f"- **Configuration**: {len(config)} files")
        if docs:
            breakdown.append(f"- **Documentation**: {len(docs)} files")
        if tests:
            breakdown.append(f"- **Test Code**: {len(tests)} files")
        if logic:
            breakdown.append(f"- **Logic Core**: {len(logic)} files (SHOULD BE 0 for Tier 0)")
        
        breakdown.append("")
        breakdown.append("### 🔬 Why No LLM?")
        breakdown.append("Tree-sitter parsed the AST of old vs new code. The S-expressions matched,")
        breakdown.append("proving no structural changes (only formatting, comments, or renames).")
        breakdown.append("")
        breakdown.append("**Cost**: $0.00 | **Confidence**: 100% (Deterministic)")
        
        return "\n".join(breakdown)

    # ---------------------------------------------------------
    # TIER 1: THE SUMMARIZER (Cost: 1 Call)
    # ---------------------------------------------------------
    async def _execute_tier_1_summarizer(self, pr_data, files, fingerprints=None):
        logger.info("💰 Estimated Cost: Low (1 Call) - Tier 1")
        
        # Build Logic Impact Header (no blast radius for Tier 1)
        header = self._build_logic_impact_header(files, fingerprints, "Tier 1 (The Summarizer)", blast_radius=0, impact_report=[])
        
        review = await self.synthesis.execute_review(pr_data, impact_report=[], mode="FAST", repo_root=self.repo_root)
        return f"{header}\n\n---\n\n{review}"

    # ---------------------------------------------------------
    # TIER 2: THE REVIEWER (Cost: 2 Calls - HYBRID)
    # Step A: Local LLM (Ollama) compresses context
    # Step B: Cloud LLM reviews Diff + Compressed Context
    # ---------------------------------------------------------
    async def _execute_tier_2_reviewer(self, pr_data, impact_report, fingerprints=None):
        logger.info("💰 Estimated Cost: Medium (2 Calls - Hybrid) - Tier 2")
        
        # Calculate blast radius
        blast_radius = self._calculate_blast_score(impact_report)
        
        # Build Logic Impact Header with blast radius
        logic_files = [item for item in impact_report]
        header = self._build_logic_impact_header(
            logic_files, 
            fingerprints, 
            "Tier 2 (The Reviewer)", 
            blast_radius=blast_radius,
            impact_report=impact_report
        )
        
        # Step A: Local Compression with fallback
        compressed_impact = []
        for item in impact_report[:MAX_CALLERS_TO_FETCH]:
            context_text = self._fetch_context_text(item)
            if context_text:
                summary = self._local_llm_compress(context_text)
                item['compressed_context'] = summary
            compressed_impact.append(item)

        # Step B: Cloud Review
        review = await self.synthesis.execute_review(pr_data, compressed_impact, mode="STANDARD", repo_root=self.repo_root)
        return f"{header}\n\n---\n\n{review}"

    def _build_logic_impact_header(self, files, fingerprints, route_name, blast_radius=0, impact_report=None):
        """
        Build the Logic Impact report header with blast radius and Low-Noise format.
        Max 3 actionable items per the "Low-Noise Risk Reviewer" approach.
        """
        header = []
        
        # Risk Level Calculation
        if blast_radius >= 50:
            risk_level = "🔴 HIGH"
            risk_emoji = "🚨"
        elif blast_radius >= 10:
            risk_level = "🟡 MEDIUM"
            risk_emoji = "⚠️"
        else:
            risk_level = "🟢 LOW"
            risk_emoji = "✅"
        
        # Header
        header.append(f"# {risk_emoji} Cognit PR Analysis")
        header.append("")
        header.append(f"| Metric | Value |")
        header.append(f"|--------|-------|")
        header.append(f"| **Risk Level** | {risk_level} |")
        header.append(f"| **Blast Radius** | {blast_radius} callers affected |")
        header.append(f"| **Files Changed** | {len(set(f.get('file', f.get('function', '')) for f in files))} |")
        header.append(f"| **Route** | {route_name} |")
        header.append("")
        
        # Structural Changes Summary
        header.append("## 🔍 What Changed")
        header.append("")
        
        seen_files = set()
        for f in files[:5]:
            file_path = f.get('file', f.get('function', 'Unknown'))
            if file_path in seen_files:
                continue
            seen_files.add(file_path)
            
            file_name = Path(file_path).name if '/' in file_path or '\\' in file_path else file_path
            fp = fingerprints.get(file_path, {}) if fingerprints else {}
            diff_hint = fp.get('diff_hint', 'Logic changed')
            
            header.append(f"- **`{file_name}`**: {diff_hint}")
        
        # Blast Radius Impact (Full list, grouped by file)
        if impact_report and len(impact_report) > 0:
            header.append("")
            header.append("## 📊 Blast Radius - Affected Files")
            header.append("")
            
            # Get all callers and group by file
            file_callers = {}
            for item in impact_report:
                for caller_qn in item.get('callers', []):
                    parts = caller_qn.split('.')
                    # Format: PROJECT.path.file.function OR path.file.function OR file.function
                    if len(parts) >= 2:
                        func_name = parts[-1]
                        # Work backwards to find file name (ends with common file indicators)
                        file_name = None
                        for i in range(len(parts) - 2, -1, -1):
                            part = parts[i]
                            # Check if this looks like a module/file name
                            if not part.startswith('_') or part == '__init__':
                                file_name = part + ".py"
                                break
                        
                        if not file_name:
                            file_name = parts[-2] + ".py" if len(parts) > 1 else "module.py"
                        
                        if file_name not in file_callers:
                            file_callers[file_name] = set()
                        file_callers[file_name].add(func_name)
            
            if file_callers:
                header.append("| File | Functions Affected |")
                header.append("|------|-------------------|")
                
                # Sort by number of affected functions
                sorted_files = sorted(file_callers.items(), key=lambda x: -len(x[1]))
                
                for file_name, functions in sorted_files:
                    func_list = ", ".join(sorted(list(functions))[:5])
                    if len(functions) > 5:
                        func_list += f", +{len(functions) - 5} more"
                    header.append(f"| `{file_name}` | {func_list} |")
                
                header.append("")
                header.append(f"> **Total**: {len(file_callers)} files, {blast_radius} function calls affected")
            else:
                # Fallback if no callers parsed - show raw count
                header.append(f"> {blast_radius} function calls in the dependency graph")
        
        # Confidence & Next Steps
        header.append("")
        header.append("## 🎯 Analysis Confidence")
        header.append("")
        header.append(f"- **Structural Detection**: 100% (Tree-sitter AST)")
        header.append(f"- **Blast Radius**: 100% (Memgraph Query)")
        header.append(f"- **Semantic Review**: Pending (Gemini API)")
        
        return "\n".join(header)

    # ---------------------------------------------------------
    # TIER 3: THE COUNCIL (Cost: 5 Calls)
    # Parallel execution: DB Specialist + Security Agent + Synthesizer
    # ---------------------------------------------------------
    async def _execute_tier_3_council(self, pr_data, impact_report, all_changes):
        logger.info("💰 Estimated Cost: High (5 Calls) - Tier 3")
        
        db_keywords = ['db', 'sql', 'database', 'query', 'model', 'migration', 'orm']
        sec_keywords = ['auth', 'security', 'password', 'token', 'secret', 'permission', 'crypto', 'key']
        
        # Filter impact report for each specialist
        db_impact = [
            item for item in impact_report 
            if any(kw in item['file'].lower() or kw in item['function'].lower() for kw in db_keywords)
        ]
        
        sec_impact = [
            item for item in impact_report
            if any(kw in item['file'].lower() or kw in item['function'].lower() for kw in sec_keywords)
        ]
        
        db_changes = [c for c in all_changes if any(kw in c['file'].lower() for kw in db_keywords)]
        sec_changes = [c for c in all_changes if any(kw in c['file'].lower() for kw in sec_keywords)]
        
        tasks = []
        results_labels = []
        
        # DB Specialist Agent
        if db_impact or db_changes:
            db_pr_data = {**pr_data, 'changes': db_changes or pr_data.get('changes', [])}
            tasks.append(self.synthesis.execute_review(db_pr_data, db_impact, mode="DB_FOCUS", repo_root=self.repo_root))
            results_labels.append("Database Warden")
            logger.info(f"  📊 DB Agent: {len(db_impact)} functions, {len(db_changes)} files")
        
        # Security Specialist Agent
        if sec_impact or sec_changes:
            sec_pr_data = {**pr_data, 'changes': sec_changes or pr_data.get('changes', [])}
            tasks.append(self.synthesis.execute_review(sec_pr_data, sec_impact, mode="SECURITY_FOCUS", repo_root=self.repo_root))
            results_labels.append("Security Warden")
            logger.info(f"  🔐 Security Agent: {len(sec_impact)} functions, {len(sec_changes)} files")
        
        if not tasks:
            logger.info("  ℹ️ No DB/Security files detected. Running standard review.")
            return await self.synthesis.execute_review(pr_data, impact_report[:10], mode="STANDARD", repo_root=self.repo_root)
        
        # Parallel execution
        results = await asyncio.gather(*tasks)
        
        # Synthesizer: Merge results
        output = ["### 🏛️ The Council Has Spoken\n"]
        for label, result in zip(results_labels, results):
            output.append(f"**{label}**:\n{result}\n")
        
        return "\n".join(output)

    # ---------------------------------------------------------
    # TIER 4: THE DEEP DIVE (Cost: 10+ Calls)
    # Interactive Loop: Agent asks for more files until satisfied
    # ---------------------------------------------------------
    async def _execute_tier_4_deep_dive(self, pr_data, impact_report):
        logger.info("💰 Estimated Cost: Very High (10+ Calls) - Tier 4")
        
        MAX_ITERATIONS = 3
        limited_impact = impact_report[:10]
        
        # Initial review
        current_review = await self.synthesis.execute_review(
            pr_data, limited_impact, mode="DEEP", repo_root=self.repo_root
        )
        
        # Iterative refinement loop
        additional_context = []
        iteration = 0
        for iteration in range(MAX_ITERATIONS):
            logger.info(f"🔄 Deep Dive Iteration {iteration + 1}/{MAX_ITERATIONS}")
            
            # Check if review requests more files
            requested_files = self._parse_file_requests(current_review)
            
            if not requested_files:
                logger.info("✅ Deep Dive: No more files requested. Analysis complete.")
                break
            
            # Fetch requested files
            for file_path in requested_files[:3]:
                content = self._fetch_file_content(file_path)
                if content:
                    additional_context.append(f"### Requested: `{file_path}`\n```python\n{content[:500]}\n```")
                    logger.info(f"  📄 Fetched: {file_path} ({len(content)} chars)")
            
            if not additional_context:
                break
                
            # Refine review with additional context
            refined_data = {
                **pr_data,
                'additional_context': '\n'.join(additional_context)
            }
            current_review = await self.synthesis.execute_review(
                refined_data, limited_impact, mode="DEEP", repo_root=self.repo_root
            )
        
        return f"### 🌊 Tier 4: Deep Dive Analysis\n\n{current_review}\n\n**Iterations**: {iteration + 1} | **Files Fetched**: {len(additional_context)}"

    def _parse_file_requests(self, review_text):
        """Parse LLM response for file requests."""
        import re
        patterns = [
            r"need to see ['\"]?([\w/._-]+\.py)['\"]?",
            r"provide ['\"]?([\w/._-]+\.py)['\"]?",
            r"check ['\"]?([\w/._-]+\.py)['\"]?",
            r"verify ['\"]?([\w/._-]+\.py)['\"]?",
        ]
        
        requested = []
        for pattern in patterns:
            matches = re.findall(pattern, review_text.lower())
            requested.extend(matches)
        
        return list(set(requested))[:5]

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
