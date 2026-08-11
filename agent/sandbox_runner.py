# agent/sandbox_runner.py
from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

SANDBOX_IMAGE = "promo-scraper-sandbox:latest"
NETWORK_NAME = "scraper-egress-only"

# Docker Desktop on Linux uses a non-standard socket path.
# Try that first, then fall back to the standard location.
_DOCKER_DESKTOP_SOCK = Path.home() / ".docker" / "desktop" / "docker.sock"
_STANDARD_SOCK = Path("/var/run/docker.sock")


def _get_docker_client(docker_sdk, timeout: int = 300):
    """
    Return a connected Docker client, auto-detecting Docker Desktop's socket.

    Docker Desktop on Linux uses ~/.docker/desktop/docker.sock instead of
    /var/run/docker.sock. If DOCKER_HOST is already set in the environment
    (e.g. by the user's shell), honour it; otherwise probe both paths.
    """
    # 1. Honour explicit DOCKER_HOST if set
    if os.environ.get("DOCKER_HOST"):
        return docker_sdk.from_env(timeout=timeout)

    # 2. Try Docker Desktop socket (Linux Docker Desktop)
    if _DOCKER_DESKTOP_SOCK.exists():
        logger.debug("Connecting via Docker Desktop socket: %s", _DOCKER_DESKTOP_SOCK)
        return docker_sdk.DockerClient(base_url=f"unix://{_DOCKER_DESKTOP_SOCK}", timeout=timeout)

    # 3. Fall back to standard socket (Docker Engine via apt/snap)
    return docker_sdk.from_env(timeout=timeout)


def detect_violations(logs: str, result: dict) -> list[str]:
    """
    Inspect container exit result and log output for security/resource violations.

    Parameters
    ----------
    logs   : Combined stdout + stderr text from the container.
    result : The dict returned by container.wait(), e.g. {"StatusCode": N}.

    Returns
    -------
    A list of violation strings. Empty list = clean run.

    Violation strings:
      "oom_kill"             — exit code 137 (OOM kill)
      "network_violation"    — blocked network access detected in logs
      "filesystem_violation" — write to read-only FS detected in logs
      "nonzero_exit"         — generic non-zero exit (not categorised above)
      "timeout_or_crash"     — set by run_scraper_in_sandbox on timeout/API errors
    """
    violations: list[str] = []
    exit_code: Optional[int] = result.get("StatusCode")
    logs_lower = logs.lower()

    # OOM kill — kernel delivers SIGKILL (exit 137 = 128 + 9)
    if exit_code == 137:
        violations.append("oom_kill")
        logger.warning("Sandbox violation: OOM kill (exit 137)")
        return violations  # Definitive; don't stack further violations

    if exit_code is not None and exit_code != 0:
        # Read-only filesystem violation
        fs_markers = [
            "read-only file system",
            "erofs",
            "read only filesystem",
        ]
        if any(m in logs_lower for m in fs_markers):
            violations.append("filesystem_violation")
            logger.warning("Sandbox violation: write to read-only filesystem")

        # Network violation — blocked egress manifests as connection errors
        network_markers = [
            "connectionerror",
            "connection refused",
            "network is unreachable",
            "name or service not known",
            "errno 101",
            "errno 111",
            "failed to establish a new connection",
            "max retries exceeded",
            "remotedisconnected",
            "nodename nor servname provided",
        ]
        if any(m in logs_lower for m in network_markers):
            violations.append("network_violation")
            logger.warning("Sandbox violation: network access to non-allowlisted host")

        # Generic non-zero exit (not already categorised)
        if not violations:
            violations.append("nonzero_exit")
            logger.warning("Sandbox: non-zero exit code %d (uncategorised)", exit_code)

    return violations


def run_scraper_in_sandbox(
    scraper_code: Optional[str],
    config: dict,
    timeout_seconds: int = 240,
) -> dict:
    """
    Run the scraper inside a locked-down, single-use Docker container.

    Parameters
    ----------
    scraper_code     : Custom Python code string to exec() inside the sandbox.
                       If None, the sandbox runs HybridPromoExtractor directly.
    config           : Full config dict — JSON-serialised as CONFIG_JSON env var.
    timeout_seconds  : Maximum wall-clock seconds to wait for the container.

    Returns
    -------
    {
        "exit_code"  : int | None,   # None = timeout/crash
        "logs"       : str,          # combined stdout + stderr
        "violations" : list[str],    # empty = clean; non-empty = auto-reject
    }

    Prerequisites
    -------------
    1. Docker daemon running  (docker info succeeds)
    2. Image built:  docker build -f docker/Dockerfile.sandbox -t promo-scraper-sandbox:latest .
    3. Network exists:  ./docker/setup_egress_network.sh create <target-domain>
    """
    try:
        import docker as docker_sdk
    except ImportError:
        logger.error("docker package not installed. Run: pip install docker")
        return {
            "exit_code": None,
            "logs": "docker package not installed",
            "violations": ["timeout_or_crash"],
        }

    env_vars: dict[str, str] = {
        "CONFIG_JSON": json.dumps(config),
        "LITELLM_LOCAL_MODEL_COST_MAP": "True",
        "LITELLM_TELEMETRY": "False",
    }
    if scraper_code:
        env_vars["SCRAPER_CODE_B64"] = base64.b64encode(scraper_code.encode()).decode()

    # ── Forward host env vars required by HybridPromoExtractor ─────────────
    # The container is fully isolated and does NOT inherit the host environment.
    # We explicitly pass the minimum set of variables the scraper needs so it
    # can initialise correctly (PROMO_CATEGORIES, API keys, tuning knobs).
    # Do NOT forward DATABASE_URL or other secrets unrelated to scraping.
    _PASSTHROUGH_ENV_VARS = [
        # Required by HybridPromoExtractor.__init__ (raises EnvironmentError if missing)
        "PROMO_CATEGORIES",
        # Vision API credentials — used by _load_allowed_categories + _client init
        "GEMINI_API_KEY",
        "LITELLM_API_KEY",
        "LITELLM_API_BASE",
        "VISION_LLM_MODEL",
        "LLM_MODEL",
        # Tuning knobs read at import time
        "VISION_API_MIN_DELAY",
        "VISION_COST_PER_MILLION_TOKENS_USD",
    ]
    for var in _PASSTHROUGH_ENV_VARS:
        val = os.environ.get(var)
        if val is not None:
            env_vars[var] = val
    logger.debug("Sandbox env vars forwarded: %s", list(env_vars.keys()))

    try:
        client = _get_docker_client(docker_sdk, timeout=timeout_seconds + 30)
    except Exception as exc:
        logger.error("Cannot connect to Docker daemon: %s", exc)
        return {
            "exit_code": None,
            "logs": f"Cannot connect to Docker daemon: {exc}",
            "violations": ["timeout_or_crash"],
        }

    logger.info(
        "Launching sandbox: image=%s network=%s timeout=%ds",
        SANDBOX_IMAGE, NETWORK_NAME, timeout_seconds,
    )

    try:
        container = client.containers.run(
            SANDBOX_IMAGE,
            command=["python", "-m", "sandbox_entrypoint"],
            detach=True,
            network_mode=NETWORK_NAME,
            mem_limit="768m",         # Raised: Chromium + Vision API responses need headroom
            nano_cpus=1_000_000_000,
            pids_limit=128,           # Raised: Chromium spawns broker/GPU/renderer subprocesses
            read_only=True,
            tmpfs={
                "/tmp": "size=256m",  # Raised: Chromium uses /tmp when --disable-dev-shm-usage
                "/dev/shm": "size=256m",  # Critical: Chromium IPC; Docker default 64MB causes "Target crashed"
            },
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            environment=env_vars,
        )
    except Exception as exc:
        logger.error("Failed to start sandbox container: %s", exc)
        return {
            "exit_code": None,
            "logs": f"Failed to start container: {exc}",
            "violations": ["timeout_or_crash"],
        }

    try:
        result = container.wait(timeout=timeout_seconds)
        logs = container.logs().decode(errors="replace")
        violations = detect_violations(logs, result)
        exit_code = result.get("StatusCode")
        logger.info("Sandbox finished: exit_code=%s violations=%s", exit_code, violations)
        if exit_code != 0 and logs.strip():
            # Surface the container's stderr/stdout so failures are visible
            # in the agent log without needing a separate `docker run` debug session.
            logger.warning("Sandbox container output (exit=%s):\n%s", exit_code, logs.strip())
        return {"exit_code": exit_code, "logs": logs, "violations": violations}

    except Exception as exc:
        logger.warning("Sandbox wait/log error: %s", exc)
        try:
            partial_logs = container.logs().decode(errors="replace")
            if partial_logs.strip():
                logger.warning("Partial sandbox container output before error:\n%s", partial_logs.strip())
        except Exception:
            partial_logs = ""
        return {
            "exit_code": None,
            "logs": f"{partial_logs}\n[runner error]: {exc}",
            "violations": ["timeout_or_crash"],
        }

    finally:
        try:
            container.remove(force=True)
            logger.debug("Sandbox container removed.")
        except Exception as rm_exc:
            logger.warning("Could not remove sandbox container: %s", rm_exc)
