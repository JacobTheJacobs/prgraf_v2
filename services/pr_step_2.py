import sys
import os
from pathlib import Path
from tree_sitter import Language, Parser

# Ensuring the POC directory is in the path for service imports
try:
    from poc_v2.services.parser_service import load_parsers
    from poc_v2.services.graph_service import MemgraphIngestor
except ImportError:
    sys.path.append(os.getcwd())
    from poc_v2.services.parser_service import load_parsers
    from poc_v2.services.graph_service import MemgraphIngestor

class BlastRadiusDetector:
    """
    Step 2: Logic Graph Analysis
    Identifies the 'Ripple Effect' of changes by mapping LOGIC_CORE symbols
    to their dependents in the Memgraph Base Graph.
    """
    
    def __init__(self, project_name="POC_V2"):
        self.parsers, self.queries = load_parsers()
        self.python_parser = self.parsers.get("python")
        self.project_name = project_name
        self.ingestor = MemgraphIngestor("localhost", 7687)
        
    def analyze_impact(self, changed_files_with_content):
        """
        Input: List of dicts {file, old_content, new_content}
        Output: List of impact objects for LLM synthesis
        """
        impact_report = []
        
        try:
            self.ingestor.connect()
        except Exception as e:
            print(f"Connection Error: {e}")
            return []

        for item in changed_files_with_content:
            file_path = item['file']
            
            # 1. Extract function definitions from the new content
            # This identifies the 'Anchor Nodes' that were potentially modified
            defined_functions = self._extract_functions(item['new_content'])
            
            for func_name in defined_functions:
                # 2. Resolve Qualified Name (QN)
                # Matches the pattern used during ingestion: Project.Module.Function
                full_qn = self._resolve_qn(file_path, func_name)
                
                # 3. Query Graph (Partial Match Logic)
                callers = self._find_callers(func_name, file_path)
                
                if callers:
                     # Calculate risk based on connectivity and keyword triggers
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
        """Converts file path to dot-notation Qualified Name."""
        relative_path = Path(file_path)
        parts = list(relative_path.with_suffix('').parts)
        module_qn = ".".join(parts)
        return f"{self.project_name}.{module_qn}.{func_name}"

    def _extract_functions(self, content):
        """Uses Tree-sitter to find function definitions in the changed file."""
        if not self.python_parser: return []
        
        funcs = []
        tree = self.python_parser.parse(content.encode('utf8'))
        cursor = tree.walk()
        
        # Iterative tree walk to collect function names
        reached_root = False
        while not reached_root:
            if cursor.node.type == 'function_definition':
                name_node = cursor.node.child_by_field_name('name')
                if name_node:
                    funcs.append(content[name_node.start_byte:name_node.end_byte])
            
            if cursor.goto_first_child():
                continue
            if cursor.goto_next_sibling():
                continue
            
            retracing = True
            while retracing:
                if not cursor.goto_parent():
                    retracing = False
                    reached_root = True
                elif cursor.goto_next_sibling():
                    retracing = False
        return funcs

    def _find_callers(self, func_name, file_path):
        """
        Hybrid Approach: Graph Query + Text Search fallback.
        """
        # 1. Graph Query
        partial_path = str(Path(file_path).name)
        print(f"[BlastRadius] Querying Graph for {func_name} in {partial_path}...")
        
        query = f"""
        MATCH (caller)-[:CALLS]->(target:Function)
        WHERE target.name = '{func_name}' 
          AND (target.qualified_name CONTAINS '{partial_path}' OR target.qualified_name CONTAINS '{partial_path[:-3]}')
        RETURN caller.qualified_name as caller_id
        LIMIT 20
        """
        results = self.ingestor._execute(query)
        graph_callers = [r['caller_id'] for r in results]
        
        if graph_callers:
            print(f"[BlastRadius] Graph found {len(graph_callers)} callers.")
            return graph_callers
            
        # 2. Text Search Fallback (If Graph Misses)
        print(f"[BlastRadius] Graph found 0. Falling back to Text Search...")
        return self._find_text_usages(func_name, file_path)

    def _find_text_usages(self, func_name, origin_file):
        """
        Greps the codebase for usage of `func_name`.
        Excludes the origin file itself.
        """
        # Walk the directory of the project (Assuming we are in root or have access)
        # We need a root path. In this POC, 'poc_v2/temp_repos/PocketFlow-Tutorial-Codebase-Knowledge'
        # Ideally passed in __init__. For now, hardcoded or relative search.
        
        search_root = Path("poc_v2/temp_repos/PocketFlow-Tutorial-Codebase-Knowledge")
        if not search_root.exists():
             search_root = Path(".") # Fallback to current dir if testing local
             
        callers = []
        try:
            # Recursive search in .py files
            import os
            for root, dirs, files in os.walk(search_root):
                for file in files:
                    if file.endswith(".py"):
                        full_path = Path(root) / file
                        # Skip origin file
                        if str(full_path.name) == Path(origin_file).name:
                            continue
                            
                        try:
                            with open(full_path, "r", encoding="utf-8", errors="ignore") as f:
                                content = f.read()
                                if func_name in content:
                                    # Very basic check: "func_name(" or "func_name (" ?
                                    # Or just strict string match is enough for "Potential Blast Radius"
                                    if f"{func_name}(" in content or f"import {func_name}" in content or f"from {func_name}" in content or f".{func_name}" in content:
                                        rel_path = full_path.relative_to(search_root) if search_root != Path(".") else full_path
                                        callers.append(f"[TextMatch] {rel_path}")
                        except Exception as e:
                            pass
        except Exception as e:
            print(f"Text search error: {e}")
            
        return callers

    def _check_criticality(self, qn, callers):
        """Heuristic to determine if the impact reaches sensitive modules."""
        critical_keywords = ['auth', 'billing', 'payment', 'security', 'db', 'main']
        combined_context = (qn + "".join(callers)).lower()
        return any(key in combined_context for key in critical_keywords)

if __name__ == "__main__":
    # Mock Run with 'TUTORIAL_RAG' (The one user cares about)
    detector = BlastRadiusDetector(project_name="TUTORIAL_RAG")
    
    mock_change = [{
        'file': 'utils/call_llm.py',
        'new_content': 'def call_llm(prompt): ...',
        'old_content': ''
    }]
    
    print("--- Running Hybrid Blast Radius Analysis ---")
    report = detector.analyze_impact(mock_change)
    
    for item in report:
        print(f"Function: {item['function']}")
        print(f"  Called By: {item['callers']}")