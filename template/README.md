# prgraf PR template

Drop-in GitHub Action that runs blast-radius review **every time you open or update a PR**.

## One-time setup (per repo)

1. Copy the workflow into the target repo:

```text
target-repo/
  .github/workflows/pr-blast-radius.yml   ← from template/.github/workflows/
  tools/prgraf/                           ← copy entire prgraf_v2 tree here
```

PowerShell (from this `prgraf_v2` folder):

```powershell
$target = "C:\path\to\your\repo"
New-Item -ItemType Directory -Force -Path "$target\.github\workflows" | Out-Null
New-Item -ItemType Directory -Force -Path "$target\tools" | Out-Null
Copy-Item ".\template\.github\workflows\pr-blast-radius.yml" "$target\.github\workflows\"
Copy-Item -Recurse -Force ".\" "$target\tools\prgraf"
# Optional: don't nest template twice
Remove-Item -Recurse -Force "$target\tools\prgraf\template" -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$target\tools\prgraf\temp_repos" -ErrorAction SilentlyContinue
```

2. Commit and push. Next PR open/sync will post a **Blast-Radius PR Review** comment.

Every run also writes the report to the job summary and uploads it as the
`prgraf-report` artifact. PRs from forks get a read-only token, so they skip the
comment and rely on those two. Comments longer than GitHub's limit are truncated
with a link to the artifact.

## Local dry-run (before push)

From the repo under review (with `prgraf` installed or `PYTHONPATH` set):

```bash
pip install -e path/to/prgraf_v2
prgraf --base origin/main --head HEAD --output prgraf-report.md
```

## Optional knobs

| Knob | Where | Meaning |
|------|--------|---------|
| `PRGRAF_PATH` repo variable | GitHub → Settings → Variables | Alternate path to the package (default `tools/prgraf`) |
| `--fail-on p1` | workflow step | Fail the check on P0/P1 findings |
| draft PR skip | workflow `if:` | Remove the draft filter to review drafts too |

## What it checks

- Deleted files still referenced elsewhere
- Touched functions/classes still called from other files
- Critical paths (auth, billing, db, payment, security, …)

Max 3 findings. No LLM, no vector DB, no graph DB.
