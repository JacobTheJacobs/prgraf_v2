import argparse
import subprocess
import sys
from pathlib import Path

# Ensure parenet dir is in path for module imports if running as script
sys.path.append(".") 

from poc_v2.pr_step_1 import StructuralTriage

def run_command(cmd, cwd):
    result = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error running {cmd}: {result.stderr}")
        return None
    return result.stdout.strip()

def analyze_pr(repo_path, pr_id):
    repo_path = Path(repo_path).resolve()
    print(f"--- Analyzing PR #{pr_id} in {repo_path} ---")
    
    # 1. Fetch PR
    print("Fetching PR...")
    branch_name = f"pr-{pr_id}"
    # Check if branch exists, if not fetch
    run_command(f"git fetch origin pull/{pr_id}/head:{branch_name}", repo_path)
    
    # 2. Add safe directory to avoid git errors if necessary
    run_command(f"git config --global --add safe.directory {str(repo_path).replace(os.sep, '/')}", repo_path)
    
    # 3. Get Changed Files
    # Assuming base is 'main'. In real app, we'd detect base branch.
    base_branch = "main" # or master
    
    # Verify base branch exists locally
    run_command(f"git fetch origin {base_branch}:{base_branch}", repo_path)
    
    diff_cmd = f"git diff --name-only {base_branch}...{branch_name}"
    changed_files = run_command(diff_cmd, repo_path)
    
    if not changed_files:
        print("No changed files found or error in diff.")
        return

    files = changed_files.split('\n')
    print(f"Found {len(files)} changed files.")
    
    triage = StructuralTriage()
    results = []

    for f in files:
        if not f.strip(): continue
        
        # Read Old Content
        old_content = run_command(f"git show {base_branch}:{f}", repo_path) or ""
        
        # Read New Content
        new_content = run_command(f"git show {branch_name}:{f}", repo_path) or ""
        
        # Triage
        classification = triage.triage_file(f, old_content, new_content)
        
        results.append({"file": f, "type": classification})
        print(f"  {f}: {classification}")

    manifest = triage.generate_manifest(results)
    print("\n--- Manifest Summary ---")
    print(f"Logic Core (To Review): {len(manifest['high_priority_review'])}")
    print(f"Mechanical (Auto-Verified): {len(manifest['automated_verified_refactors'])}")
    print(f"Configuration (Ops Review): {len(manifest['configuration_changes'])}")
    print(f"Documentation: {len(manifest['documentation_updates'])}")
    
    if manifest['high_priority_review']:
        print("\n[LOGIC CORE FILES]")
        for f in manifest['high_priority_review']:
            print(f" - {f['file']}")

    if manifest['configuration_changes']:
        print("\n[CONFIGURATION FILES]")
        for f in manifest['configuration_changes']:
            print(f" - {f['file']}")

if __name__ == "__main__":
    import os
    if len(sys.argv) < 3:
        print("Usage: python poc_v2/run_pr_triage.py <repo_path> <pr_id>")
        sys.exit(1)
    
    analyze_pr(sys.argv[1], sys.argv[2])
