import sys
import os
import difflib
import time
import json
import requests
import dotenv
from pathlib import Path

# Load env vars
dotenv.load_dotenv()

# Add project root to path
sys.path.append(os.getcwd())

from poc_v2.pr_step_2 import BlastRadiusDetector

class PRReviewPromptGenerator:
    """
    Step 3: The Architect & Detective
    Synthesizes the Triage (Step 1) and Blast Radius (Step 2) into a 
    high-accuracy, one-shot prompt for the LLM and executes the review.
    """
    
    def __init__(self, project_name="TUTORIAL_RAG"):
        self.project_name = project_name
        self.blast_detector = BlastRadiusDetector(project_name=project_name)
        # Root path for reading actual file content for context
        self.repo_root = Path("poc_v2/temp_repos/PocketFlow-Tutorial-Codebase-Knowledge")

    def generate_review_prompt(self, pr_data):
        """
        Combines Diff + Graph Insights + Caller Context.
        """
        impact_report = self.blast_detector.analyze_impact(pr_data['changes'])
        
        # Determine overall risk based on Graph
        has_impact = len(impact_report) > 0
        risk_label = "HIGH" if any(r['risk_level'] == "HIGH" for r in impact_report) else ("LOW" if not has_impact else "MEDIUM")

        prompt = []
        prompt.append(f"role: Senior Software Architect and Security Reviewer")
        prompt.append(f"context: Reviewing PR #{pr_data.get('pr_number', '126')} in a 10M line codebase.")
        
        # 1. GRAPH INSIGHTS SECTION (The 'Detective' work)
        prompt.append("\n## 🧭 GRAPH ANALYSIS INSIGHTS")
        if not has_impact:
            prompt.append("> ℹ️ **Zero External Impact Detected:** Static analysis of the 10M line codebase shows NO external modules calling the modified functions. This change is isolated (Leaf Node).")
        else:
            prompt.append(f"> ⚠️ **Impact Alert:** This change affects symbols used by {len(impact_report)} other modules. Risk Level: {risk_label}.")
            prompt.append(self._build_context_section(impact_report))

        # 2. CODE CHANGES SECTION (The 'Diff')
        prompt.append("\n## 📝 PROPOSED CHANGES (Unified Diff)")
        for change in pr_data['changes']:
            diff = self._generate_diff(change['old_content'], change['new_content'], change['file'])
            prompt.append(f"### File: {change['file']}")
            prompt.append(f"```diff\n{diff}\n```")

        # 3. TASK INSTRUCTIONS (The 'Architect's' orders)
        prompt.append("\n## 🛠️ REVIEW INSTRUCTIONS")
        prompt.append(f"1. **Primary Goal:** Verify the logic of the changes in the diff.")
        if has_impact:
            prompt.append(f"2. **Regression Check:** Focus heavily on the 'Impact Analysis' section. Ensure the new behavior doesn't break the identified Callers.")
        else:
            prompt.append(f"2. **Decoupled Review:** Since the Blast Radius is 0, focus on internal code quality, documentation, and the validity of the new Bedrock integration logic.")
        prompt.append("3. **Security:** Check for hardcoded credentials or insecure AWS configurations in the Bedrock client.")
        prompt.append("4. **Verdict:** Provide a final verdict: [APPROVED, REQUEST_CHANGES, or BLOCK].")

        return "\n".join(prompt)

    async def execute_review(self, pr_data):
        """
        Generates the prompt and calls the Gemini API with exponential backoff.
        """
        prompt = self.generate_review_prompt(pr_data)
        
        api_key = os.getenv("GEMINI_API_KEY") # Key is provided by the execution environment
        model = "gemini-2.5-flash-preview-09-2025"
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        
        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "systemInstruction": {
                "parts": [{"text": "You are an elite Senior Software Architect. Your reviews are concise, technically deep, and focus on architectural integrity and security. Use markdown for your final response."}]
            }
        }

        # Exponential backoff: 1s, 2s, 4s, 8s, 16s
        for attempt in range(5):
            try:
                response = requests.post(url, json=payload, timeout=30)
                if response.status_code == 200:
                    result = response.json()
                    return result.get('candidates', [{}])[0].get('content', {}).get('parts', [{}])[0].get('text', "Error: No text generated.")
                
                # If rate limited or server error, back off
                if response.status_code in [429, 500, 503]:
                    time.sleep(2 ** attempt)
                    continue
                else:
                    return f"API Error: {response.status_code} - {response.text}"
            except Exception as e:
                time.sleep(2 ** attempt)
                if attempt == 4:
                    return f"Failed to connect to API after 5 attempts: {str(e)}"
        
        return "Review failed due to persistent API errors."

    def _generate_diff(self, old, new, filename):
        """Generates a real unified diff for better LLM readability."""
        old_lines = old.splitlines(keepends=True)
        new_lines = new.splitlines(keepends=True)
        diff = difflib.unified_diff(old_lines, new_lines, fromfile=f"a/{filename}", tofile=f"b/{filename}")
        return "".join(diff) or "No changes detected (Identity match)."

    def _build_context_section(self, impact_report):
        out = []
        for item in impact_report:
            out.append(f"\n### Impacted Symbol: `{item['function']}`")
            for caller_qn in item['callers']:
                path = self._resolve_path_from_qn(caller_qn)
                out.append(f"- **Caller:** `{caller_qn}` (File: `{path or 'Unknown'}`)")
                if path:
                    snippet = self._read_snippet(path, item['function'].split('.')[-1])
                    if snippet:
                        out.append(f"  ```python\n  # Context from {path}\n{snippet}\n  ```")
        return "\n".join(out)

    def _resolve_path_from_qn(self, qn):
        """
        Reverse Engineer QN: 'TUTORIAL_RAG.utils.call_llm.call_llm' 
        to 'utils/call_llm.py'
        """
        parts = qn.split('.')
        if len(parts) < 2: return None
        # Remove project name (parts[0]) and function name (parts[-1])
        module_parts = parts[1:-1]
        path_str = "/".join(module_parts) + ".py"
        return path_str

    def _read_snippet(self, path_str, search_term):
        """Reads the caller file and finds the line where the function is called."""
        target = self.repo_root / path_str
        if not target.exists(): return None
        try:
            lines = target.read_text().splitlines()
            for i, line in enumerate(lines):
                if search_term in line:
                    start = max(0, i - 5)
                    end = min(len(lines), i + 5)
                    return "\n".join(lines[start:end])
            return "\n".join(lines[:10]) # Fallback to start of file
        except:
            return None

if __name__ == "__main__":
    # Simulate a full run for PR #126
    import asyncio
    
    gen = PRReviewPromptGenerator()
    mock_pr = {
        'pr_number': 126,
        'changes': [{
            'file': 'utils/call_llm.py',
            'old_content': 'def call_llm(p): pass',
            'new_content': 'import boto3\nimport os\n\n# Added Bedrock support\ndef call_llm(prompt):\n    # New Logic: Potential security risk with default credentials\n    client = boto3.client("bedrock-runtime", region_name=os.getenv("AWS_REGION", "us-east-1"))\n    return "Bedrock Response"'
        }]
    }
    
    print("--- Executing Graph-Aware Review ---")
    async def run():
        review = await gen.execute_review(mock_pr)
        # Safe print for Windows consoles
        sys.stdout.buffer.write(review.encode('utf-8'))
        sys.stdout.buffer.write(b'\n')
        
    asyncio.run(run())