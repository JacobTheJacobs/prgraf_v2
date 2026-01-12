import os
import sys
import difflib
import time
import requests
from pathlib import Path
from loguru import logger
import dotenv

# Load env vars
dotenv.load_dotenv()

from codebase_rag.parser_loader import load_parsers
from codebase_rag.services.graph_service import MemgraphIngestor
from codebase_rag.config import AppConfig

# Initialize Config
try:
    config = AppConfig()
except:
    config = None # Fallback

class StructuralTriage:
    """
    Analyzes the 'Blast Radius' of a PR by categorizing changes 
    before they reach the LLM.
    
    Key Feature: AST Fingerprinting
    - Generates S-expression fingerprints from Tree-sitter AST
    - Compares old vs new fingerprints to detect LOGIC_CORE vs MECHANICAL
    """
    
    def __init__(self):
        self.parsers, _ = load_parsers()
        self.python_parser = self.parsers.get("python")
        self.fingerprints = {}  # Store fingerprints for reporting

    def triage_file(self, file_path, old_content, new_content):
        """
        The core logic: If S-expression matches, it's MECHANICAL.
        If S-expression differs, it's LOGIC_CORE.
        """
        ext = Path(file_path).suffix
        
        # 1. Quick Filter: Config & Docs
        if ext in ['.json', '.yaml', '.yml', '.toml', '.lock', 'requirements.txt']:
            return "CONFIGURATION"
            
        if ext in ['.md', '.txt']:
             if 'requirements' in Path(file_path).name:
                 return "CONFIGURATION"
             return "DOCUMENTATION"

        # 2. Tests
        if 'test' in file_path.lower() or 'conftest.py' in file_path:
             return "TEST_CODE"

        # 3. Generated / Minified
        if 'min.js' in file_path or 'generated' in file_path:
             return "GENERATED"
             
        # 4. Structural Analysis (e.g., Python)
        if ext == '.py':
            is_logic, detail = self._check_ast_structural_change_with_fingerprint(old_content, new_content, file_path)
            return "LOGIC_CORE" if is_logic else "MECHANICAL"
            
        # 5. Default Fallback
        if ext in ['.js', '.ts', '.jsx', '.tsx', '.java', '.go', '.rs', '.cpp', '.sql']:
             return "LOGIC_CORE"
            
        return "UNKNOWN"

    def _check_ast_structural_change(self, old_code, new_code):
        if not self.python_parser:
            return True # Fail safe

        try:
           old_tree = self.python_parser.parse(old_code.encode('utf8'))
           new_tree = self.python_parser.parse(new_code.encode('utf8'))
           
           old_sexp = str(old_tree.root_node)
           new_sexp = str(new_tree.root_node)
           
           return old_sexp != new_sexp
        except Exception as e:
            logger.error(f"Error parsing structural change: {e}")
            return True

    def _check_ast_structural_change_with_fingerprint(self, old_code, new_code, file_path):
        """
        Enhanced version that stores fingerprints for reporting.
        Returns: (is_logic_change, detail_dict)
        """
        if not self.python_parser:
            return True, {"error": "Parser not available"}

        # DEBUG: Log content lengths
        logger.debug(f"🔬 AST Check: {file_path}")
        logger.debug(f"   Old content length: {len(old_code)} chars")
        logger.debug(f"   New content length: {len(new_code)} chars")
        
        # If both are empty or very short, something is wrong
        if len(old_code) < 10 and len(new_code) < 10:
            logger.warning(f"⚠️ Both old and new content are empty/too short for {file_path}")
            return True, {"error": "Content not fetched"}

        try:
            old_tree = self.python_parser.parse(old_code.encode('utf8'))
            new_tree = self.python_parser.parse(new_code.encode('utf8'))
            
            # Use the FULL S-expression for comparison (not the compact fingerprint)
            old_sexp = str(old_tree.root_node)
            new_sexp = str(new_tree.root_node)
            
            is_changed = old_sexp != new_sexp
            
            # Generate compact fingerprints for display only
            old_fingerprint = self._generate_compact_fingerprint(old_tree.root_node)
            new_fingerprint = self._generate_compact_fingerprint(new_tree.root_node)
            
            logger.debug(f"   Old fingerprint: {old_fingerprint[:80]}...")
            logger.debug(f"   New fingerprint: {new_fingerprint[:80]}...")
            logger.debug(f"   AST Changed: {is_changed}")
            
            # Store for reporting
            self.fingerprints[file_path] = {
                "old": old_fingerprint[:100] + "..." if len(old_fingerprint) > 100 else old_fingerprint,
                "new": new_fingerprint[:100] + "..." if len(new_fingerprint) > 100 else new_fingerprint,
                "changed": is_changed,
                "diff_hint": self._get_structure_diff_hint(old_tree.root_node, new_tree.root_node) if is_changed else "No structural changes"
            }
            
            return is_changed, self.fingerprints[file_path]
        except Exception as e:
            logger.error(f"Error parsing structural change: {e}")
            return True, {"error": str(e)}

    def _generate_compact_fingerprint(self, node, max_depth=6, current_depth=0):
        """
        Generate a compact fingerprint showing the structure.
        e.g., (func(if(call)(return))(return))
        Depth 6 captures: module -> class -> func -> if -> nested_if -> call
        """
        if current_depth >= max_depth:
            return ""
        
        # Get meaningful node types
        meaningful_types = ['function_definition', 'if_statement', 'for_statement', 
                          'while_statement', 'try_statement', 'call', 'return_statement',
                          'class_definition', 'with_statement', 'raise_statement']
        
        node_type = node.type
        if node_type == 'function_definition':
            node_type = 'func'
        elif node_type == 'if_statement':
            node_type = 'if'
        elif node_type == 'return_statement':
            node_type = 'return'
        elif node_type == 'call':
            node_type = 'call'
        elif node_type == 'class_definition':
            node_type = 'class'
        
        children_fp = ""
        for child in node.children:
            child_fp = self._generate_compact_fingerprint(child, max_depth, current_depth + 1)
            if child_fp:
                children_fp += child_fp
        
        if node.type in meaningful_types or children_fp:
            if node.type in meaningful_types:
                return f"({node_type}{children_fp})"
            return children_fp
        
        return ""

    def _get_structure_diff_hint(self, old_node, new_node):
        """Provide a HUMAN-READABLE hint about what changed structurally."""
        old_funcs = self._count_node_types(old_node)
        new_funcs = self._count_node_types(new_node)
        
        hints = []
        
        # Map node types to human-readable names
        readable_names = {
            'if_statement': 'if statement',
            'for_statement': 'for loop',
            'while_statement': 'while loop',
            'try_statement': 'try/except block',
            'function_definition': 'function',
            'return_statement': 'return statement',
            'raise_statement': 'raise statement',
            'class_definition': 'class'
        }
        
        for node_type, readable in readable_names.items():
            old_count = old_funcs.get(node_type, 0)
            new_count = new_funcs.get(node_type, 0)
            if new_count > old_count:
                hints.append(f"+{new_count - old_count} {readable}")
            elif new_count < old_count:
                hints.append(f"-{old_count - new_count} {readable}")
        
        if hints:
            return ", ".join(hints)
        
        # If no node count difference, check for modifications
        return "Logic modified (same structure, different content)"

    def _count_node_types(self, node, counts=None):
        if counts is None:
            counts = {}
        
        if node.type not in counts:
            counts[node.type] = 0
        counts[node.type] += 1
        
        for child in node.children:
            self._count_node_types(child, counts)
        
        return counts

    def generate_manifest(self, file_list):
        # Reset fingerprints for new manifest
        self.fingerprints = {}
        
        # First pass: Ensure all files have a type
        for f in file_list:
            if 'type' not in f:
                f['type'] = self.triage_file(f['file'], f.get('old_content', ''), f.get('new_content', ''))
                # Attach fingerprint if available
                if f['file'] in self.fingerprints:
                    f['fingerprint'] = self.fingerprints[f['file']]
                logger.info(f"  🏷️ Triage: {f['file']} -> {f['type']}")

        return {
            "high_priority_review": [f for f in file_list if f['type'] == "LOGIC_CORE"],
            "automated_verified_refactors": [f for f in file_list if f['type'] == "MECHANICAL"],
            "configuration_changes": [f for f in file_list if f['type'] == "CONFIGURATION"],
            "documentation_updates": [f for f in file_list if f['type'] == "DOCUMENTATION"],
            "test_code_updates": [f for f in file_list if f['type'] == "TEST_CODE"],
            "generated_code": [f for f in file_list if f['type'] == "GENERATED"],
            "fingerprints": self.fingerprints  # Include for Tier 0 report
        }


class BlastRadiusDetector:
    """
    Step 2: Logic Graph Analysis
    Identifies the 'Ripple Effect' of changes.
    """
    
    def __init__(self, project_name="TUTORIAL_RAG", memgraph_host="localhost", memgraph_port=7687):
        self.parsers, _ = load_parsers()
        self.python_parser = self.parsers.get("python")
        self.project_name = project_name
        self.ingestor = MemgraphIngestor(memgraph_host, memgraph_port)
        self.repo_root = None # Set by PRService

    def analyze_impact(self, changed_files_with_content, repo_root: Path = None):
        """
        Input: List of dicts {file, new_content}
        Output: List of impact objects
        """
        if repo_root:
            self.repo_root = repo_root

        impact_report = []
        
        try:
            # MemgraphIngestor uses context manager, but we can also manually connect/close if needed
            # Or use the _execute_query method which uses _get_cursor() context manager
            # The original code called self.ingestor.connect(), but MemgraphIngestor in code-graph-rag doesn't have connect() exposed publicly like that usually 
            # Looking at codebase_rag impl: __enter__ does connect. 
            # We should wrap calls or manually set self.ingestor.conn if we want persistence, 
            # but _execute_query checks for self.conn.
            
            # We will use the proper context manager pattern in the loop or assume connection is managed externally?
            # actually MemgraphIngestor 'connect' method in Step 166 was a poc_v2 method. 
            # codebase_rag MemgraphIngestor uses __enter__. 
            pass 
        except Exception:
            pass

        # We need to explicitly connect for the class instance if we want to reuse it across calls without 'with' block
        # codebase_rag's MemgraphIngestor is designed for 'with' usage.
        # Let's use a context manager for the scope of analysis.
        
        with self.ingestor as ingestor:
            for item in changed_files_with_content:
                file_path = item['file']
                item_content = item.get('new_content', '')
                if not item_content: continue

                defined_functions = self._extract_functions(item_content)
                
                for func_name in defined_functions:
                    full_qn = self._resolve_qn(file_path, func_name)
                    callers = self._find_callers(ingestor, func_name, file_path)
                    
                    if callers:
                         is_high_risk = self._check_criticality(full_qn, callers)
                         
                         impact_report.append({
                             "function": full_qn,
                             "file": file_path,
                             "callers": callers,
                             "caller_count": len(callers),
                             "risk_level": "HIGH" if is_high_risk else "MEDIUM",
                             "reason": f"Modified symbol is a dependency for {len(callers)} modules."
                         })
        
        return impact_report

    def _resolve_qn(self, file_path, func_name):
        relative_path = Path(file_path)
        parts = list(relative_path.with_suffix('').parts)
        module_qn = ".".join(parts)
        return f"{self.project_name}.{module_qn}.{func_name}"

    def _extract_functions(self, content):
        if not self.python_parser: return []
        funcs = []
        try:
            tree = self.python_parser.parse(content.encode('utf8'))
            cursor = tree.walk()
            
            reached_root = False
            while not reached_root:
                if cursor.node.type == 'function_definition':
                    name_node = cursor.node.child_by_field_name('name')
                    if name_node:
                        funcs.append(content[name_node.start_byte:name_node.end_byte])
                
                if cursor.goto_first_child(): continue
                if cursor.goto_next_sibling(): continue
                
                retracing = True
                while retracing:
                    if not cursor.goto_parent():
                        retracing = False
                        reached_root = True
                    elif cursor.goto_next_sibling():
                        retracing = False
        except Exception as e:
            logger.warning(f"Failed to extract functions: {e}")
        return funcs

    def _find_callers(self, ingestor: MemgraphIngestor, func_name, file_path):
        # 1. Graph Query
        partial_path = str(Path(file_path).name)
        
        query = f"""
        MATCH (caller)-[:CALLS]->(target:Function)
        WHERE target.name = '{func_name}' 
          AND (target.qualified_name CONTAINS '{partial_path}' OR target.qualified_name CONTAINS '{partial_path[:-3]}')
        RETURN caller.qualified_name as caller_id
        LIMIT 20
        """
        try:
            results = ingestor.fetch_all(query) # Using fetch_all from codebase_rag implementation
            graph_callers = [r['caller_id'] for r in results]
            
            if graph_callers:
                return graph_callers
        except Exception as e:
            logger.error(f"Graph query failed: {e}")

        # 2. Fallback
        return self._find_text_usages(func_name, file_path)

    def _find_text_usages(self, func_name, origin_file):
        if not self.repo_root or not self.repo_root.exists():
            return []

        callers = []
        try:
            for root, dirs, files in os.walk(self.repo_root):
                for file in files:
                    if file.endswith(".py"):
                        full_path = Path(root) / file
                        if str(full_path.name) == Path(origin_file).name:
                            continue
                        try:
                            content = full_path.read_text(encoding='utf-8', errors='ignore')
                            if func_name in content:
                                if f"{func_name}(" in content or f"import {func_name}" in content or f".{func_name}" in content:
                                    rel_path = full_path.relative_to(self.repo_root)
                                    callers.append(f"[TextMatch] {rel_path}")
                        except:
                            pass
        except Exception as e:
            logger.error(f"Text search error: {e}")
        return callers

    def _check_criticality(self, qn, callers):
        critical_keywords = ['auth', 'billing', 'payment', 'security', 'db', 'main']
        combined_context = (qn + "".join(callers)).lower()
        return any(key in combined_context for key in critical_keywords)


class PRReviewPromptGenerator:
    """
    Step 3: The Architect & Detective (Review Generation)
    """
    
    def __init__(self, project_name="TUTORIAL_RAG"):
        self.project_name = project_name
        self.blast_detector = BlastRadiusDetector(project_name=project_name)
        self.repo_root = None

    async def execute_review(self, pr_data, impact_report, mode="STANDARD", repo_root=None):
        """
        Executes the review using Gemini with the appropriate prompt mode.
        """
        self.repo_root = repo_root or self.repo_root
        
        # Build Context
        context_section = self._build_context_section(impact_report)
        diff_section = self._build_diff_section(pr_data.get('changes', []))
        
        # Select System Instruction based on Mode
        if mode == "FAST":
            system_instruction = (
                f"You are 'The Sniper' (Tier 1). Review the following PR diff for {self.project_name}. "
                "Focus ONLY on critical regressions or major logic flaws. "
                "Be extremely concise. If safe, say 'LGTM'."
            )
        elif mode == "SECURITY_FOCUS":
            system_instruction = (
                f"You are 'The Security Warden' (Tier 3). Review the code for security vulnerabilities ONLY. "
                "Check for SQL injection, XSS, auth bypass, and broken access control. "
                "Ignore style or minor bugs."
            )
        elif mode == "DEEP":
            system_instruction = (
                f"You are 'The Architect' (Tier 4). Perform a Deep Dive analysis. "
                "Consider architectural consistency, performance at scale, and edge cases. "
                "Provide a detailed report."
            )
        else: # STANDARD / Tier 2
            system_instruction = (
                f"You are a Senior Reviewer for {self.project_name}. "
                "Synthesize the provided structural impact analysis and code diffs. "
                "Identify bugs, blocking issues, and improvements."
            )

        prompt = f'''
        {system_instruction}

        CONTEXT:
        {context_section}

        CHANGES:
        {diff_section}
        '''
        
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
             return "Error: GEMINI_API_KEY not found."

        model = "gemini-2.0-flash-exp" # Using a valid model name
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "systemInstruction": {
                "parts": [{"text": "You are an elite Senior Software Architect. Your reviews are concise, technically deep, and focus on architectural integrity and security. Use markdown for your final response."}]
            }
        }

        for attempt in range(3):
            try:
                response = requests.post(url, json=payload, timeout=60)
                if response.status_code == 200:
                    result = response.json()
                    candidates = result.get('candidates', [])
                    if not candidates:
                         return "Error: No candidates returned from Gemini."
                    return candidates[0].get('content', {}).get('parts', [{}])[0].get('text', "Error: No text generated.")
                
                if response.status_code in [429, 500, 503]:
                    time.sleep(2 ** attempt)
                    continue
                else:
                    return f"API Error: {response.status_code} - {response.text}"
            except Exception as e:
                time.sleep(2 ** attempt)
        
        return "Review failed due to persistent API errors."

    def generate_review_prompt(self, pr_data, impact_report, mode):
        has_impact = len(impact_report) > 0
        risk_label = "HIGH" if any(r['risk_level'] == "HIGH" for r in impact_report) else ("LOW" if not has_impact else "MEDIUM")

        prompt = []
        prompt.append(f"role: Senior Software Architect and Security Reviewer")
        prompt.append(f"context: Reviewing PR #{pr_data.get('pr_number', '126')}. Mode: {mode}.")
        
        # 1. GRAPH INSIGHTS
        prompt.append("\n## 🧭 GRAPH ANALYSIS INSIGHTS")
        if not has_impact:
            prompt.append("> ℹ️ **Zero External Impact Detected:** This change is isolated (Leaf Node).")
        else:
            prompt.append(f"> ⚠️ **Impact Alert:** This change affects symbols used by {len(impact_report)} other modules. Risk Level: {risk_label}.")
            prompt.append(self._build_context_section(impact_report))

        # 2. CODE CHANGES
        prompt.append("\n## 📝 PROPOSED CHANGES (Unified Diff)")
        for change in pr_data['changes']:
            diff = self._generate_diff(change['old_content'], change.get('new_content', ''), change['file'])
            prompt.append(f"### File: {change['file']}")
            prompt.append(f"```diff\n{diff}\n```")

        # 3. INSTRUCTIONS
        prompt.append("\n## 🛠️ REVIEW INSTRUCTIONS")
        prompt.append("1. **Logic Verification:** Verify the logic of the changes.")
        if mode == "DEEP" or has_impact:
            prompt.append("2. **Regression Check:** Ensure no existing callers are broken.")
        prompt.append("3. **Security:** Check for credentials and vulnerabilities.")
        prompt.append("4. **Verdict:** [APPROVED, REQUEST_CHANGES, or BLOCK].")

        return "\n".join(prompt)

    def _generate_diff(self, old, new, filename):
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)
        diff = difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{filename}", tofile=f"b/{filename}")
        return "".join(diff) or "No changes detected."

    def _build_diff_section(self, changes):
        """Build unified diff section from list of changes."""
        if not changes:
            return "No changes to display."
        
        diff_parts = []
        for change in changes: 
            diff = self._generate_diff(
                change.get('old_content', ''),
                change.get('new_content', ''),
                change.get('file', 'unknown')
            )
            diff_parts.append(f"### {change.get('file', 'unknown')}\n```diff\n{diff}\n```")
        
        return "\n\n".join(diff_parts)

    def _build_context_section(self, impact_report):
        out = []
        for item in impact_report:
            out.append(f"\n### Impacted Symbol: `{item['function']}`")
            # If compressed context exists (Tier 2), use it
            if 'compressed_context' in item:
                out.append(f"**Context Summary (Compressed):**\n{item['compressed_context']}")
            else:
                for caller_qn in item['callers']:
                    out.append(f"- Caller: `{caller_qn}`")
                    path = self._resolve_path_from_qn(caller_qn)
                    if path:
                        snippet = self._read_snippet(path, item['function'].split('.')[-1])
                        if snippet:
                             out.append(f"  ```python\n{snippet}\n  ```")
        return "\n".join(out)

    def _resolve_path_from_qn(self, qn):
        parts = qn.split('.')
        if len(parts) < 2: return None
        module_parts = parts[1:-1]
        return "/".join(module_parts) + ".py"

    def _read_snippet(self, path_str, search_term):
        if not self.repo_root: return None
        target = self.repo_root / path_str
        if not target.exists(): return None
        try:
            lines = target.read_text(encoding='utf-8').splitlines()
            for i, line in enumerate(lines):
                if search_term in line:
                    start = max(0, i - 5)
                    end = min(len(lines), i + 5)
                    return "\n".join(lines[start:end])
            return "\n".join(lines[:10])
        except:
            return None
