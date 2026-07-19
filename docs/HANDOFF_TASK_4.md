# HANDOFF — Task 4: Sandbox Infrastructure

## Files Delivered

| File | Purpose |
|---|---|
| `docker/Dockerfile.sandbox` | Minimal Python 3.12-slim sandbox image |
| `docker/setup_egress_network.sh` | Creates/tears down the egress-filtered Docker network |
| `agent/sandbox_entrypoint.py` | Runs inside the container; reads config + custom code from env vars |
| `agent/sandbox_runner.py` | Docker container lifecycle manager; consumed by Task 5 |
| `agent/test_sandbox.py` | Standalone tests for all 3 violation scenarios |

**One-line fix also applied:**  
`agent/generation_agent.py` — after `json.dump(...)`, `state.status = "generated"` is now set on success (was staying `"generation"`).

---

## Environment Prerequisites

| Requirement | Version |
|---|---|
| Docker Engine | ≥ 20.10 (tested: **29.6.1**) |
| Docker Python SDK | `docker==7.1.0` (already in `requirements.txt`) |
| iptables | Available for egress network rules (requires root/sudo for `setup_egress_network.sh`) |
| User in `docker` group | `sudo usermod -aG docker $USER` then re-login |

Verify Docker is accessible:
```bash
docker info
```

---

## Step 1 — Build the Sandbox Image

Run from the **repository root** (`myers_competitive_analysis/`):

```bash
docker build \
  -f docker/Dockerfile.sandbox \
  -t promo-scraper-sandbox:latest \
  .
```

> **Important:** The build context must be the repo root, not the `docker/` subdirectory.  
> The `COPY promo_scraper/` and `COPY agent/sandbox_entrypoint.py` instructions resolve relative to the build context root.

Verify the image was built:
```bash
docker images promo-scraper-sandbox:latest
```

---

## Step 2 — Create the Egress Network

The network restricts outbound connections from sandbox containers to a single target domain's HTTPS endpoint only.

```bash
# Replace <target-domain> with the actual domain being scraped, e.g.:
sudo ./docker/setup_egress_network.sh create www.vanheusen.com.au

# Verify network exists:
docker network inspect scraper-egress-only
```

### Tear down the network

```bash
sudo ./docker/setup_egress_network.sh teardown
```

### Usage notes
- Run `create` once per session (or once per target domain if you change domains).
- iptables rules are applied globally — they persist until `teardown` is called or the host reboots.
- DNS (port 53) is always permitted so the container can resolve the target domain.
- If `getent`/`host`/`dig` cannot resolve the domain at setup time, all outbound HTTPS will be blocked (fail-safe).

---

## Step 3 — Run the Sandbox Tests

```bash
# From repo root:
../env/bin/python agent/test_sandbox.py
```

### Expected output (all 3 tests passing)

```
============================================================
  Sandbox Violation Tests
  agent/test_sandbox.py
============================================================

Docker daemon: OK (server version 29.6.1)
Sandbox image: OK (promo-scraper-sandbox:latest found)
Egress network: OK (scraper-egress-only found)

── test_filesystem_violation ──
   Scraper code: 'offers = []\ntry:\n    with open("/etc/pwned", "w") as f:\n  ...'...
   Expecting violation containing: 'filesystem_violation'
   exit_code  : 1
   violations : ['filesystem_violation']
   ✓  PASSED — expected 'filesystem_violation'

── test_network_violation ──
   Scraper code: 'offers = []\nimport requests\ntry:\n    resp = requests.get("https://evil....'...
   Expecting violation containing: 'network_violation'
   exit_code  : 1
   violations : ['network_violation']
   ✓  PASSED — expected 'network_violation'

── test_memory_violation ──
   Scraper code: 'offers = []\nimport sys\nprint("Starting memory exhaustion...", file=sys...'...
   Expecting violation containing: 'oom_kill'
   exit_code  : 137
   violations : ['oom_kill']
   ✓  PASSED — expected 'oom_kill'

============================================================
  Results: 3/3 passed, 0/3 failed
============================================================

✓  All 3 tests PASSED.
```

---

## Step 4 — Verify Existing Tests Still Pass

```bash
../env/bin/python agent/test_orchestrator.py
```

Expected: `🎉 All orchestrator and generation agent tests passed successfully!` (exit 0)

---

## What `sandbox_runner.run_scraper_in_sandbox()` Returns (Task 5 Reference)

```python
from agent.sandbox_runner import run_scraper_in_sandbox

result = run_scraper_in_sandbox(
    scraper_code=None,     # or str with custom Python code
    config=config_dict,
    timeout_seconds=60,
)

# result shape:
{
    "exit_code"  : int | None,   # None = timeout/crash before container exited
    "logs"       : str,          # combined stdout + stderr from the container
    "violations" : list[str],    # empty = clean run
}
```

### Violation strings Task 5 will receive

| Violation string | Trigger condition | Recommended action |
|---|---|---|
| `"oom_kill"` | Container killed by kernel OOM (exit 137) | Auto-reject; flag in ValidationReport.sandbox_violations |
| `"network_violation"` | Blocked outbound connection detected in logs | Auto-reject; indicates scraper tried to reach non-target host |
| `"filesystem_violation"` | EROFS error in logs (write to read-only FS) | Auto-reject; scraper attempted to write outside /tmp |
| `"nonzero_exit"` | Non-zero exit not matching above patterns | Auto-reject; generic failure |
| `"timeout_or_crash"` | `container.wait()` timed out or Docker API error | Auto-reject; sandbox failed to produce a result |

**Any non-empty `violations` list must force `recommendation = "reject"` and `confidence_score = 0` in `ValidationReport`.**

### Reading scraper output from `logs`

When the sandbox runs cleanly (exit 0, no violations), `logs` contains a single-line JSON string on stdout:

```json
{"offers": [...], "count": N}
```

Task 5's validation agent should parse this from `logs` to extract `offer_items` for schema validation and confidence scoring.

---

## Sandbox Container Security Constraints

| Constraint | Value | Purpose |
|---|---|---|
| `mem_limit` | `512m` | Prevent memory exhaustion |
| `nano_cpus` | `1_000_000_000` (1 CPU) | Prevent CPU starvation |
| `pids_limit` | `64` | Prevent fork bombs |
| `read_only` | `True` | Root filesystem read-only |
| `tmpfs /tmp` | `size=64m` | Only writable scratch space |
| `cap_drop` | `["ALL"]` | Drop all Linux capabilities |
| `security_opt` | `["no-new-privileges"]` | Prevent privilege escalation |
| `network_mode` | `scraper-egress-only` | Egress-filtered Docker network |

---

## Notes on the Egress Network

The `scraper-egress-only` Docker network uses iptables rules (managed by `setup_egress_network.sh`) to allow only:
1. HTTPS (port 443) to the resolved IPs of the target domain
2. DNS (port 53) for hostname resolution

All other outbound traffic is dropped. The `network_violation` detection in `sandbox_runner.detect_violations()` catches the Python-level connection errors that result from this blocking.

> **Note:** For the sandbox tests (`test_sandbox.py`), the network can be created with any domain (e.g., `example.com`) since the tests don't make real scraping calls — they only test that the security constraints enforce violations correctly.
