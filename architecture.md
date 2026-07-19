# Architecture

`prgraf` is intentionally small. Primary path is **CLI on every PR** via the template workflow.

```text
GitHub pull_request (opened / synchronize / …)
  -> template/.github/workflows/pr-blast-radius.yml
     -> prgraf --base origin/$BASE --head HEAD
        -> RepoFetcher.fetch_range_diff
        -> PocketStrategyRouter
           -> StructuralTriage
           -> deleted-file reference scan
           -> touched-symbol reference scan
        -> prgraf-report.md + PR comment

Optional web:
POST /api/analyze
  -> PRService
     -> RepoFetcher (range | remote PR | supplied changes)
     -> PocketStrategyRouter
  -> concise Pre-Landing Review
```

## Review Rules

- Deleted files: report surviving files that still reference deleted paths.
- Logic changes: report touched functions/classes that are referenced from other files.
- Critical paths: auth, billing, db, migration, payment, permission, security, session, token.
- Output: maximum 3 findings, each with severity, confidence, file:line, and one reason line.

## Non-Goals

- No graph database.
- No vector database.
- No LLM review.
- No full codebase parser.
- No generated multi-tab dashboard.
