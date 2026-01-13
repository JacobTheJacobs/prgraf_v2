"""
Semantic Sentinel: Zero-LLM Code Pattern Analyzer
Uses Vector DB (Qdrant) to detect inconsistencies and patterns
without invoking any language model.
"""
from pathlib import Path
from loguru import logger
from typing import Dict, List, Any, Optional

# Confidence thresholds
CONSISTENCY_THRESHOLD = 0.85  # 85% similarity = potential inconsistency
DUPLICATE_THRESHOLD = 0.92   # 92% similarity = potential duplicate

class SemanticSignal:
    """A single semantic finding."""
    def __init__(self, signal_type: str, message: str, confidence: float, file: str = "", details: str = ""):
        self.signal_type = signal_type  # BUG, MISSING_TEST, RISK, INFO
        self.message = message
        self.confidence = confidence
        self.file = file
        self.details = details
    
    def to_dict(self):
        return {
            "type": self.signal_type,
            "message": self.message,
            "confidence": self.confidence,
            "file": self.file,
            "details": self.details
        }


class SemanticSentinel:
    """
    Analyzes code changes using Vector DB patterns.
    Returns structured findings without using LLM.
    """
    
    
    def __init__(self, repo_root: Path = None, project_name: str = None):
        self.repo_root = repo_root
        self.project_name = project_name
        self._qdrant_available = self._check_qdrant()
    
    def _check_qdrant(self) -> bool:
        try:
            from codebase_rag.vector_store import get_qdrant_client
            from codebase_rag.config import settings
            client = get_qdrant_client()
            return client.collection_exists(settings.QDRANT_COLLECTION_NAME)
        except Exception as e:
            logger.debug(f"Qdrant not available: {e}")
            return False
    
    def analyze_changes(self, changes: List[Dict], triage_report: Dict) -> List[SemanticSignal]:
        """
        Analyze PR changes and return semantic signals.
        
        Returns at most 3 high-priority signals for public output.
        """
        signals = []
        
        # Fast path: Check if there's any new_content to analyze
        has_analyzable_content = any(
            change.get('type') == 'LOGIC_CORE' and len(change.get('new_content', '')) > 50
            for change in changes
        )
        
        # 1. Check for Semantic Consistency (Vector-based) - only if we have content
        if self._qdrant_available and has_analyzable_content:
            logger.debug("Running vector consistency check...")
            consistency_signals = self._check_consistency(changes)
            signals.extend(consistency_signals)
        else:
            logger.debug(f"Skipping vector check: qdrant={self._qdrant_available}, content={has_analyzable_content}")
        
        # 2. Check for Missing Tests (AST-based) - always runs, fast
        test_signal = self._check_missing_tests(changes, triage_report)
        if test_signal:
            signals.append(test_signal)
        
        # 3. Sort by confidence and return top 3
        signals.sort(key=lambda s: s.confidence, reverse=True)
        return signals[:3]
    
    def _check_consistency(self, changes: List[Dict]) -> List[SemanticSignal]:
        """
        Compare new code against existing patterns in Qdrant.
        Flag if return type or error handling differs from similar functions.
        """
        signals = []
        
        try:
            from codebase_rag.embedder import embed_code
            from codebase_rag.vector_store import search_embeddings, get_qdrant_client
            from codebase_rag.config import settings
            
            client = get_qdrant_client()
            
            for change in changes:
                if change.get('type') != 'LOGIC_CORE':
                    continue
                
                new_content = change.get('new_content', '')
                file_path = change.get('file', '')
                
                if not new_content or len(new_content) < 50:
                    continue
                
                # Extract function snippets from new content
                functions = self._extract_function_snippets(new_content)
                
                for func_name, func_code in functions.items():
                    # Embed the new function
                    try:
                        embedding = embed_code(func_code)
                    except Exception:
                        continue
                    
                    # Search for similar functions
                    results = search_embeddings(embedding, top_k=5, project_name=self.project_name)
                    
                    if not results:
                        continue
                    
                    # Analyze patterns of similar functions
                    pattern_signal = self._analyze_patterns(
                        func_name, func_code, results, client, settings.QDRANT_COLLECTION_NAME
                    )
                    
                    if pattern_signal:
                        pattern_signal.file = file_path
                        signals.append(pattern_signal)
                        
        except Exception as e:
            logger.debug(f"Consistency check error: {e}")
        
        return signals
    
    def _extract_function_snippets(self, code: str) -> Dict[str, str]:
        """Extract function definitions from code."""
        functions = {}
        try:
            from codebase_rag.parser_loader import load_parsers
            parsers, _ = load_parsers()
            python_parser = parsers.get("python")
            
            if not python_parser:
                return functions
            
            tree = python_parser.parse(code.encode('utf8'))
            
            def find_functions(node):
                if node.type == 'function_definition':
                    name_node = node.child_by_field_name('name')
                    if name_node:
                        func_name = code[name_node.start_byte:name_node.end_byte]
                        func_code = code[node.start_byte:node.end_byte]
                        functions[func_name] = func_code
                for child in node.children:
                    find_functions(child)
            
            find_functions(tree.root_node)
        except Exception as e:
            logger.debug(f"Function extraction error: {e}")
        
        return functions
    
    def _analyze_patterns(self, func_name: str, func_code: str, 
                         similar_results: List, client, collection_name: str) -> Optional[SemanticSignal]:
        """
        Compare patterns between new function and similar existing functions.
        """
        try:
            # Get payloads for similar functions
            similar_ids = [r[0] for r in similar_results[:5]]
            similar_scores = [r[1] for r in similar_results[:5]]
            
            # Check for return pattern consistency
            new_has_return_none = 'return None' in func_code or 'return\n' in func_code
            new_raises_error = 'raise ' in func_code
            
            # Fetch similar function names
            points = client.retrieve(collection_name=collection_name, ids=similar_ids)
            similar_names = [p.payload.get('qualified_name', '') for p in points if p.payload]
            
            # If this function returns None but similar ones raise errors
            if new_has_return_none and not new_raises_error:
                # Check if similar functions follow different pattern
                # (We can't see their code, but we can flag based on naming)
                similar_analyzers = [n for n in similar_names if 'analyze' in n.lower() or 'process' in n.lower()]
                
                if len(similar_analyzers) >= 2:
                    avg_similarity = sum(similar_scores) / len(similar_scores) if similar_scores else 0
                    
                    if avg_similarity > CONSISTENCY_THRESHOLD:
                        return SemanticSignal(
                            signal_type="BUG",
                            message=f"`{func_name}` uses `return None`, but {len(similar_analyzers)} similar functions may use `raise Error`.",
                            confidence=avg_similarity,
                            details=f"Similar: {', '.join(similar_names[:3])}"
                        )
            
            # Check for high similarity (potential duplicate)
            if similar_scores and similar_scores[0] > DUPLICATE_THRESHOLD:
                # This might be duplicated code
                return SemanticSignal(
                    signal_type="INFO",
                    message=f"`{func_name}` is {int(similar_scores[0]*100)}% similar to existing code. Consider reuse.",
                    confidence=similar_scores[0],
                    details=f"Similar to: {similar_names[0] if similar_names else 'unknown'}"
                )
                
        except Exception as e:
            logger.debug(f"Pattern analysis error: {e}")
        
        return None
    
    def _check_missing_tests(self, changes: List[Dict], triage_report: Dict) -> Optional[SemanticSignal]:
        """
        Check if new logic was added without corresponding test changes.
        """
        logic_files = triage_report.get('high_priority_review', [])
        test_files = triage_report.get('test_code_updates', [])
        
        if not logic_files:
            return None
        
        # Check if any logic file has a corresponding test file in the PR
        logic_basenames = set()
        for f in logic_files:
            file_path = f.get('file', '')
            basename = Path(file_path).stem
            logic_basenames.add(basename)
        
        test_covers = set()
        for f in test_files:
            file_path = f.get('file', '')
            # test_foo.py covers foo.py
            basename = Path(file_path).stem
            if basename.startswith('test_'):
                test_covers.add(basename[5:])  # Remove 'test_' prefix
        
        uncovered = logic_basenames - test_covers
        
        if uncovered and len(logic_files) > 0:
            # Check if any new functions were added (not just modified)
            new_functions_added = False
            for f in logic_files:
                fingerprint = f.get('fingerprint', {})
                diff_hint = fingerprint.get('diff_hint', '')
                if '+1 function' in diff_hint.lower() or 'added' in diff_hint.lower():
                    new_functions_added = True
                    break
            
            if new_functions_added or len(uncovered) > 0:
                return SemanticSignal(
                    signal_type="MISSING_TEST",
                    message=f"Logic changed in `{list(uncovered)[0]}.py` but no test updates detected.",
                    confidence=0.90,
                    file=list(logic_files)[0].get('file', ''),
                    details="Consider adding tests for new branches."
                )
        
        return None
