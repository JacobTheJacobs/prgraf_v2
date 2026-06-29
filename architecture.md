# Architecture

`prgraf` is intentionally small.

```text
POST /api/analyze
  -> PRService
     -> RepoFetcher, if repo_url + pr_number were provided
     -> PocketStrategyRouter
        -> StructuralTriage
        -> deleted-file reference scan
        -> touched-symbol reference scan
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
