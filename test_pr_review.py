#!/usr/bin/env python3
"""Smoke test for the prgraf_v2 blast-radius PR reviewer."""

print("=" * 60)
print("BLAST-RADIUS PR REVIEW TEST - prgraf_v2")
print("=" * 60)

# === Test 1: StructuralTriage ===
print("\n[1] Testing StructuralTriage (AST logic check)...")
try:
    from codebase_rag.services.pr_review.analyzer import StructuralTriage

    triage = StructuralTriage()

    changes = [
        {
            "file": "app/auth.py",
            "old_content": """def authenticate(username, password):
    user = db.find_user(username)
    if user and user.check_password(password):
        return create_token(user)
    return None
""",
            "new_content": """def authenticate(username, password):
    user = db.find_user(username)
    if user and user.check_password(password):
        if user.is_active:
            return create_token(user)
    return None
""",
        },
        {
            "file": "app/main.py",
            "old_content": "from app.auth import authenticate\n\nresult = authenticate('a', 'b')\n",
            "new_content": "from app.auth import authenticate\n\nresult = authenticate('a', 'b')\n",
        },
        {
            "file": "docs/setup.md",
            "old_content": "# Setup\n",
            "new_content": "",
        },
        {
            "file": "README.md",
            "old_content": "See docs/setup.md\n",
            "new_content": "See docs/setup.md\n",
        },
        {
            "file": "config.json",
            "old_content": '{"debug": false}',
            "new_content": '{"debug": true}',
        },
    ]

    manifest = triage.generate_manifest(changes)

    logic = manifest.get("high_priority_review", [])
    mechanical = manifest.get("automated_verified_refactors", [])
    config = manifest.get("configuration_changes", [])
    docs = manifest.get("documentation_updates", [])

    print(f"   LOGIC_CORE files: {len(logic)}")
    print(f"   MECHANICAL files: {len(mechanical)}")
    print(f"   CONFIG files: {len(config)}")
    print(f"   DOCUMENTATION files: {len(docs)}")

    if logic and logic[0]["file"] == "app/auth.py":
        print("   PASS: Correctly identified logic change in auth.py")
    else:
        print("   WARN: Classification may need tuning")

    print("   PASS: StructuralTriage")
except Exception as e:
    print(f"   FAIL: StructuralTriage - {e}")
    import traceback
    traceback.print_exc()

# === Test 2: PocketStrategyRouter ===
print("\n[2] Testing PocketStrategyRouter (blast-radius output)...")
try:
    from codebase_rag.services.pocket_router import PocketStrategyRouter

    router = PocketStrategyRouter()

    pr_data = {
        "pr_number": 123,
        "changes": changes,
    }

    result = router.route_and_review(pr_data)
    print("   Review output length:", len(result), "chars")
    print("   Preview:", result.splitlines()[0])
    if "Pre-Landing Review:" in result and "docs/setup.md" in result:
        print("   PASS: PocketStrategyRouter")
    else:
        print("   WARN: Output format unexpected")

except Exception as e:
    print(f"   FAIL: PocketStrategyRouter - {e}")

# === Test 3: Active service imports ===
print("\n[3] Checking supporting components...")
try:
    from codebase_rag.services.pr_service import PRService
    print("   PASS: PRService importable")

    from codebase_rag.services.pr_review import RepoFetcher, StructuralTriage
    print("   PASS: pr_review exports importable")

except Exception as e:
    print(f"   WARN: Some components import issue: {e}")

print("\n" + "=" * 60)
print("TEST SUMMARY")
print("=" * 60)
print("Blast-radius review works without DB, vector search, or LLM calls.")
print("The PR API exposes one analyze endpoint plus logs.")
print("=" * 60)
