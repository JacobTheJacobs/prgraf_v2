import logging
import requests
import json
from pathlib import Path
from poc_v2.pr_step_1 import StructuralTriage
from poc_v2.pr_step_2 import BlastRadiusDetector
from poc_v2.pr_step_3_review import PRReviewPromptGenerator
from poc_v2.services.graph_service import MemgraphIngestor
from poc_v2.services.processors import ProcessorFactory

class PRService:
    def __init__(self, github_token, project_name="TUTORIAL_RAG"):
        self.github_token = github_token
        self.project_name = project_name
        self.triage_engine = StructuralTriage() 
        self.blast_detector = BlastRadiusDetector(project_name=project_name)
        self.prompt_gen = PRReviewPromptGenerator(project_name=project_name)
        
        # Ingestor for Lazy Loading
        self.ingestor = MemgraphIngestor("localhost", 7687)

    def fetch_pr_data(self, pr_url):
        """
        Fetches PR file changes and content.
        """
        try:
            # Parse URL
            parts = pr_url.strip('/').split('/')
            owner = parts[-4]
            repo = parts[-3]
            pull_number = parts[-1]
            
            api_base = f"https://api.github.com/repos/{owner}/{repo}"
            
            # 1. Get PR Details
            pr_resp = requests.get(f"{api_base}/pulls/{pull_number}", headers=self._headers())
            pr_resp.raise_for_status()
            pr_info = pr_resp.json()
            
            # 2. Get Files
            files_resp = requests.get(f"{api_base}/pulls/{pull_number}/files", headers=self._headers())
            files_resp.raise_for_status()
            files = files_resp.json()
            
            changes = []
            for f in files:
                raw_url = f.get('raw_url')
                new_content = ""
                if raw_url:
                    r = requests.get(raw_url, headers=self._headers())
                    if r.status_code == 200:
                        new_content = r.text
                
                changes.append({
                    'file': f['filename'],
                    'filename': f['filename'],
                    'status': f['status'],
                    'new_content': new_content,
                    'old_content': '', 
                    'patch': f.get('patch', '')
                })
                
            return {
                "owner": owner,
                "repo": repo,
                "number": pull_number,
                "title": pr_info.get('title'),
                "changes": changes
            }
        except Exception as e:
            logging.error(f"Error fetching PR: {e}")
            return None

    def ensure_project_ingested(self, owner, repo):
        # ...
        project_name = f"{owner}_{repo}"
        logging.info(f"Checking ingestion status for {project_name}...")
        
        try:
            self.ingestor.connect()
            res = self.ingestor._execute(f"MATCH (n) WHERE n.project_name = '{project_name}' RETURN count(n) as c")
            count = res[0]['c'] if res else 0
            
            if count > 50:
                logging.info(f"✅ Project {project_name} ready ({count} nodes).")
                return project_name
                
            logging.info(f"⚠️ Project {project_name} missing/empty. Starting Lazy Ingestion...")
            
            repo_dir = Path(f"poc_v2/temp_repos/{project_name}")
            if not repo_dir.exists():
                clone_url = f"https://github.com/{owner}/{repo}.git"
                logging.info(f"🔄 Cloning {clone_url}...")
                import subprocess
                subprocess.check_call(["git", "clone", clone_url, str(repo_dir)])
            else:
                 logging.info(f"📂 Repo dir exists at {repo_dir}.")

            logging.info("🚀 Running Ingestion Pipeline (This may take a minute)...")
            self.ingestor.ensure_node("Project", {"name": project_name}, "name")
            self.ingestor.flush_all()
            
            factory = ProcessorFactory(self.ingestor, str(repo_dir), project_name)
            
            logging.info("Parsing Structure...")
            factory.structure_processor.identify_structure()
            logging.info("Parsing Definitions...")
            factory.definition_processor.process_files()
            logging.info("Parsing Imports & Calls...")
            factory.import_processor.process_imports()
            self.ingestor.flush_all()
            
            logging.info("✅ Lazy Ingestion Complete.")
            return project_name

        except Exception as e:
            logging.error(f"❌ Lazy Ingestion Failed: {e}")
            return project_name

    def analyze_pr(self, pr_url):
        """
        Orchestrates Step 1 & 2 + Lazy Ingestion.
        """
        pr_data = self.fetch_pr_data(pr_url)
        if not pr_data:
            return {"error": "Failed to fetch PR"}
            
        # 0. Lazy Ingestion Trigger
        # Update project name based on PR
        real_project_name = self.ensure_project_ingested(pr_data['owner'], pr_data['repo'])
        
        # Update detectors with real project name
        self.blast_detector.project_name = real_project_name
            
        # Step 1: Triage
        triage_results = []
        logic_core_changes = []
        
        for c in pr_data['changes']:
            # triage_file(file_path, old_content, new_content)
            category = self.triage_engine.triage_file(c['file'], c.get('old_content', ''), c['new_content'])
            triage_results.append({
                "file": c['file'],
                "category": category
            })
            if "LOGIC_CORE" in category:
                logic_core_changes.append(c)
        
        # Step 2: Blast Radius
        impact_report = []
        if logic_core_changes:
            impact_report = self.blast_detector.analyze_impact(logic_core_changes)
        
        return {
            "pr_info": {k:v for k,v in pr_data.items() if k != 'changes'},
            "triage": triage_results,
            "blast_radius": impact_report,
            "raw_changes": pr_data['changes'] # Pass implementation details for Step 3
        }

    async def generate_review(self, pr_data_dict):
        """
        Step 3: LLM Review
        Expects the full dictionary returned by analyze_pr
        """
        # Reconstruct input for PRReviewPromptGenerator
        # It expects {'pr_number': ..., 'changes': ...}
        # We stored changes in 'raw_changes'
        
        input_data = {
            'pr_number': pr_data_dict['pr_info']['number'],
            'changes': pr_data_dict['raw_changes']
        }
        
        review = await self.prompt_gen.execute_review(input_data)
        return review

    def post_pr_comment(self, pr_url, comment_body):
        """
        Posts a comment to the GitHub PR.
        """
        try:
            # Re-parse URL to extract owner/repo/number
            # (Ideally we reuse parsed info but pr_url is what we get)
            parts = pr_url.strip('/').split('/')
            owner = parts[-4]
            repo = parts[-3]
            issue_number = parts[-1]
            
            url = f"https://api.github.com/repos/{owner}/{repo}/issues/{issue_number}/comments"
            
            payload = {"body": comment_body}
            
            logging.info(f"Posting comment to {url}...")
            resp = requests.post(url, headers=self._headers(), json=payload)
            resp.raise_for_status()
            
            return {"status": "success", "url": resp.json().get("html_url")}
            
        except Exception as e:
            logging.error(f"Failed to post comment: {e}")
            return {"status": "error", "message": str(e)}

    def _headers(self):
        return {
            "Authorization": f"token {self.github_token}",
            "Accept": "application/vnd.github.v3+json"
        }
