#!/usr/bin/env python3
# agent/test_sandbox.py
# ──────────────────────────────────────────────────────────────────────────────
# Standalone test for the sandbox infrastructure.
# Tests three deliberately misbehaving scrapers WITHOUT touching the real
# agent pipeline.
#
# Usage (from repo root):
#   ../env/bin/python agent/test_sandbox.py
#
# Prerequisites:
#   1. Docker daemon running
#   2. Sandbox image built:
#        docker build -f docker/Dockerfile.sandbox -t promo-scraper-sandbox:latest .
#   3. Egress network created for any domain (we use example.com for tests):
#        sudo ./docker/setup_egress_network.sh create example.com
#
# Each test asserts that the expected violation appears in the violations list
# returned by run_scraper_in_sandbox().
#
# Exit code: 0 = all 3 tests passed, non-zero = at least one failure.
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import os
import sys
from pathlib import Path

# Ensure project root is on path so `agent` package is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.sandbox_runner import run_scraper_in_sandbox

# ── Minimal dummy config (no real scraping happens in violation tests) ─────────
DUMMY_CONFIG = {
    "brand": "TestBrand",
    "source_url": "https://example.com",
    "spider": "image_promo",
    "extraction_strategy": "text",
    "text_selectors": [],
    "screenshot_selectors": [],
    "enabled": True,
}

PASS = "✓"
FAIL = "✗"

_results: list[bool] = []


def _run_test(name: str, scraper_code: str, expected_violation: str) -> bool:
    """
    Run one sandbox test and print the result.

    Returns True if the expected violation was found, False otherwise.
    """
    print(f"\n── {name} ──")
    print(f"   Scraper code: {scraper_code[:80].strip()!r}...")
    print(f"   Expecting violation containing: {expected_violation!r}")

    result = run_scraper_in_sandbox(
        scraper_code=scraper_code,
        config=DUMMY_CONFIG,
        timeout_seconds=90,
    )

    print(f"   exit_code  : {result['exit_code']}")
    print(f"   violations : {result['violations']}")
    if result["logs"]:
        # Print last 5 lines of logs for diagnostics
        log_lines = result["logs"].strip().splitlines()
        tail = "\n      ".join(log_lines[-5:])
        print(f"   log tail   :\n      {tail}")

    passed = expected_violation in result["violations"]
    mark = PASS if passed else FAIL
    print(f"   {mark}  {'PASSED' if passed else 'FAILED'} — expected {expected_violation!r}")
    _results.append(passed)
    return passed


# ── Test 1: Filesystem violation ───────────────────────────────────────────────
# Tries to write to /etc/pwned — blocked by the read-only root filesystem.
# Expected: filesystem_violation (EROFS from the kernel)
FILESYSTEM_VIOLATION_CODE = """\
offers = []
try:
    with open("/etc/pwned", "w") as f:
        f.write("hacked")
    offers = [{"title": "pwned", "source": "test"}]
except Exception as e:
    import sys
    print(f"Caught expected error: {e}", file=sys.stderr)
    raise
"""

# ── Test 2: Network violation ──────────────────────────────────────────────────
# Tries to GET https://evil.com — blocked by the egress-filtered Docker network.
# Expected: network_violation (connection refused / unreachable)
NETWORK_VIOLATION_CODE = """\
offers = []
import requests
try:
    resp = requests.get("https://evil.com", timeout=10)
    offers = [{"title": f"fetched {resp.status_code}", "source": "test"}]
except Exception as e:
    import sys
    print(f"Network error (expected): {e}", file=sys.stderr)
    raise
"""

# ── Test 3: Memory violation ───────────────────────────────────────────────────
# Allocates a list until OOM — container killed with SIGKILL (exit 137).
# Expected: oom_kill
MEMORY_VIOLATION_CODE = """\
offers = []
import sys
print("Starting memory exhaustion...", file=sys.stderr)
blob = []
while True:
    blob.append(b"x" * (1024 * 1024))  # allocate 1 MB at a time
"""


def _make_docker_client(docker_sdk):
    """Same socket auto-detection as sandbox_runner._get_docker_client."""
    desktop_sock = Path.home() / ".docker" / "desktop" / "docker.sock"
    if os.environ.get("DOCKER_HOST"):
        return docker_sdk.from_env()
    if desktop_sock.exists():
        return docker_sdk.DockerClient(base_url=f"unix://{desktop_sock}")
    return docker_sdk.from_env()


def main() -> None:
    print("=" * 60)
    print("  Sandbox Violation Tests")
    print("  agent/test_sandbox.py")
    print("=" * 60)

    # Verify Docker is reachable before running tests
    try:
        import docker as _docker
        client = _make_docker_client(_docker)
        info = client.info()
        print(f"\nDocker daemon: OK (server version {info.get('ServerVersion', 'unknown')})")
    except Exception as exc:
        print(f"\n{FAIL}  Cannot connect to Docker daemon: {exc}")
        print("    Please ensure the Docker daemon is running and your user has access.")
        sys.exit(1)

    # Verify sandbox image exists
    try:
        client.images.get("promo-scraper-sandbox:latest")
        print("Sandbox image: OK (promo-scraper-sandbox:latest found)")
    except Exception:
        print(f"\n{FAIL}  Sandbox image not found: promo-scraper-sandbox:latest")
        print("    Build it with:")
        print("      docker build -f docker/Dockerfile.sandbox -t promo-scraper-sandbox:latest .")
        sys.exit(1)

    # Verify egress network exists
    try:
        client.networks.get("scraper-egress-only")
        print("Egress network: OK (scraper-egress-only found)")
    except Exception:
        print(f"\n{FAIL}  Egress network not found: scraper-egress-only")
        print("    Create it with:")
        print("      sudo ./docker/setup_egress_network.sh create example.com")
        sys.exit(1)

    print()

    _run_test(
        name="test_filesystem_violation",
        scraper_code=FILESYSTEM_VIOLATION_CODE,
        expected_violation="filesystem_violation",
    )

    _run_test(
        name="test_network_violation",
        scraper_code=NETWORK_VIOLATION_CODE,
        expected_violation="network_violation",
    )

    _run_test(
        name="test_memory_violation",
        scraper_code=MEMORY_VIOLATION_CODE,
        expected_violation="oom_kill",
    )

    # ── Summary ────────────────────────────────────────────────────────────────
    total = len(_results)
    passed = sum(_results)
    failed = total - passed

    print()
    print("=" * 60)
    print(f"  Results: {passed}/{total} passed, {failed}/{total} failed")
    print("=" * 60)

    if failed:
        print(f"\n{FAIL}  {failed} test(s) FAILED. See output above for details.")
        sys.exit(1)
    else:
        print(f"\n{PASS}  All {total} tests PASSED.")
        sys.exit(0)


if __name__ == "__main__":
    main()
