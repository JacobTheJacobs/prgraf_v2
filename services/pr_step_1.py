import os
from pathlib import Path
from tree_sitter import Language, Parser

# Assuming parsers are managed by a service in your POC
# Assuming parsers are managed by a service in your POC
from poc_v2.services.parser_service import load_parsers

class StructuralTriage:
    """
    Analyzes the 'Blast Radius' of a PR by categorizing changes 
    before they reach the LLM.
    """
    
    def __init__(self):
        # In a real setup, we'd use the loaded parsers
        # For this logic, we focus on the strategy of comparison
        self.parsers, _ = load_parsers()
        self.python_parser = self.parsers.get("python")
        self.logic_files = []
        self.mechanical_files = []
        self.boilerplate_files = []

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
             # Special case for requirements.txt handled above if explicit, 
             # but standard .txt is usually docs. 
             if 'requirements' in Path(file_path).name:
                 return "CONFIGURATION"
             return "DOCUMENTATION"

        # 2. Tests
        if 'test' in file_path.lower() or 'conftest.py' in file_path:
             return "TEST_CODE"

        # 3. Generated / Minified (Simple Heuristic for POC)
        if 'min.js' in file_path or 'generated' in file_path:
             return "GENERATED"
             
        # 4. Structural Analysis (e.g., Python)
        if ext == '.py':
            # This is where tree-sitter shines
            # A 'Mechanical' change (rename) preserves the S-Expression tree structure
            # A 'Logic' change (new if, new loop) alters the S-Expression tree structure
            is_logic = self._check_ast_structural_change(old_content, new_content)
            return "LOGIC_CORE" if is_logic else "MECHANICAL"
            
        # 5. Default Fallback for Checkable Code (JS, TS, Java etc) - Assume Logic
        if ext in ['.js', '.ts', '.jsx', '.tsx', '.java', '.go', '.rs', '.cpp', '.sql']:
             return "LOGIC_CORE" # Conservative
            
        return "UNKNOWN"

    def _check_ast_structural_change(self, old_code, new_code):
        """
        Heuristic: Compare S-expressions.
        Tree-sitter S-expressions represent the SYNTAX types, not the values.
        Example: (function_definition name: (identifier) parameters: (parameters))
        If I rename the function, the S-expression stays EXACTLY the same.
        If I add an 'if' statement, the S-expression gets a new '(if_statement)' node.
        """
        if not self.python_parser:
            return True # Fail safe

        try:
           old_tree = self.python_parser.parse(old_code.encode('utf8'))
           new_tree = self.python_parser.parse(new_code.encode('utf8'))
           
           # Use str() to get S-expression
           old_sexp = str(old_tree.root_node)
           new_sexp = str(new_tree.root_node)
           
           if old_sexp != new_sexp:
               print(f"DEBUG: S-Exp Diff:\nOLD: {old_sexp[:100]}...\nNEW: {new_sexp[:100]}...")
               
           return old_sexp != new_sexp
        except Exception as e:
            print(f"Error parsing: {e}")
            return True

    def generate_manifest(self, file_list):
        """
        Creates the final output that will be fed to the LLM/Pipeline.
        """
        return {
            "high_priority_review": [f for f in file_list if f['type'] == "LOGIC_CORE"],
            "automated_verified_refactors": [f for f in file_list if f['type'] == "MECHANICAL"],
            "configuration_changes": [f for f in file_list if f['type'] == "CONFIGURATION"],
            "documentation_updates": [f for f in file_list if f['type'] == "DOCUMENTATION"],
            "test_code_updates": [f for f in file_list if f['type'] == "TEST_CODE"],
            "generated_code": [f for f in file_list if f['type'] == "GENERATED"]
        }

# --- Mock Data for Demonstration ---

def demo_triage():
    triage = StructuralTriage()
    
    print("--- PR Triage Simulation (Step 1) ---\n")

    # Case 1: Logic Change (New if statement)
    file_1 = "core_logic.py"
    code_base_1 = """
def calculate(a, b):
    return a + b
"""
    code_pr_1 = """
def calculate(a, b):
    if b == 0: return 0
    return a + b
"""
    cat_1 = triage.triage_file(file_1, code_base_1, code_pr_1)
    print(f"File: {file_1}\n  -> Classification: {cat_1}")
    
    # Case 2: Mechanical Change (Rename variable)
    file_2 = "refactor.py"
    code_base_2 = """
def greet(name):
    print(f"Hello {name}")
"""
    code_pr_2 = """
def greet(user_name):
    print(f"Hello {user_name}")
"""
    cat_2 = triage.triage_file(file_2, code_base_2, code_pr_2)
    print(f"File: {file_2}\n  -> Classification: {cat_2}")
    
    # Case 3: Boilerplate
    file_3 = "config.json"
    cat_3 = triage.triage_file(file_3, "{}", "{'a':1}")
    print(f"File: {file_3}\n  -> Classification: {cat_3}")

if __name__ == "__main__":
    demo_triage()