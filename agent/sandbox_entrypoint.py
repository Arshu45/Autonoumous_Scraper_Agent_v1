# agent/sandbox_entrypoint.py
# ──────────────────────────────────────────────────────────────────────────────
# This module runs INSIDE the Docker sandbox container.
# It is copied into the image at build time (see docker/Dockerfile.sandbox).
#
# Environment variables consumed:
#   CONFIG_JSON       — JSON string of the full scraper config dict (required)
#   SCRAPER_CODE_B64  — base64-encoded custom Python scraper code (optional)
#
# Output (to stdout):
#   {"offers": [...], "count": N}
#
# On error:
#   Writes error detail to stderr and exits with a non-zero code.
# ──────────────────────────────────────────────────────────────────────────────

from __future__ import annotations

import base64
import json
import os
import sys
import traceback


def main() -> None:
    # ── Read config from environment ──────────────────────────────────────────
    raw_config = os.environ.get("CONFIG_JSON")
    if not raw_config:
        print("ERROR: CONFIG_JSON environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    try:
        config: dict = json.loads(raw_config)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Could not parse CONFIG_JSON as JSON: {exc}", file=sys.stderr)
        sys.exit(1)

    custom_code_b64 = os.environ.get("SCRAPER_CODE_B64")
    offers: list = []

    # ── Branch: custom scraper code supplied ──────────────────────────────────
    if custom_code_b64:
        try:
            code_bytes = base64.b64decode(custom_code_b64)
        except Exception as exc:
            print(f"ERROR: Could not base64-decode SCRAPER_CODE_B64: {exc}", file=sys.stderr)
            sys.exit(1)

        try:
            code_str = code_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            print(f"ERROR: Custom scraper code is not valid UTF-8: {exc}", file=sys.stderr)
            sys.exit(1)

        exec_globals: dict = {"config": config}
        try:
            exec(code_str, exec_globals)  # noqa: S102
        except Exception as exc:
            print(f"ERROR: Custom scraper code raised an exception: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            sys.exit(1)

        raw_offers = exec_globals.get("offers")
        if raw_offers is None:
            print(
                "WARNING: Custom scraper code did not set an 'offers' variable — "
                "returning empty list.",
                file=sys.stderr,
            )
            offers = []
        elif not isinstance(raw_offers, list):
            print(
                f"WARNING: 'offers' variable has unexpected type {type(raw_offers).__name__} "
                "— returning empty list.",
                file=sys.stderr,
            )
            offers = []
        else:
            offers = raw_offers

    # ── Branch: standard HybridPromoExtractor path ────────────────────────────
    else:
        try:
            from promo_scraper.hybrid_promo_extractor import HybridPromoExtractor
        except ImportError as exc:
            print(
                f"ERROR: Could not import HybridPromoExtractor: {exc}\n"
                "Ensure promo_scraper/ is present at /app/promo_scraper inside the container.",
                file=sys.stderr,
            )
            sys.exit(1)

        try:
            extractor = HybridPromoExtractor(config)
            summary = extractor.run()
            offers = summary.get("offer_items", [])
        except Exception as exc:
            print(
                f"ERROR: HybridPromoExtractor failed: {exc}",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)
            sys.exit(1)

    # ── Emit result to stdout ─────────────────────────────────────────────────
    # sandbox_runner.py reads this JSON from container logs.
    result = {"offers": offers, "count": len(offers)}
    try:
        print(json.dumps(result))
    except (TypeError, ValueError) as exc:
        # Offers contain non-serialisable objects — degrade gracefully
        print(
            f"WARNING: Could not serialise all offers ({exc}); returning count only.",
            file=sys.stderr,
        )
        print(json.dumps({"offers": [], "count": 0, "serialisation_error": str(exc)}))


if __name__ == "__main__":
    main()
