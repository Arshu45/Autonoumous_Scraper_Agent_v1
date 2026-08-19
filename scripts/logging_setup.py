"""
logging_setup.py
================
Central helper that attaches a timestamped rotating file handler to the
root logger for every scraper run.

Log files are written to:
    <project_root>/logs/scraper_<YYYYMMDD_HHMMSS>_<source>.log

Where <source> is:
    - "pipeline"   → triggered via master_pipeline.py / Prefect
    - "cli"        → triggered via run_hybrid_promo_scraper.py directly
    - "ui"         → triggered via the Streamlit Scraper Runner page

Usage
-----
    from scripts.logging_setup import attach_file_logger, detach_file_logger

    handler = attach_file_logger(source="cli")
    # ... run the scraper ...
    detach_file_logger(handler)

The returned handler must be passed to detach_file_logger() so it is
cleanly removed from the root logger after the run completes.
"""

import logging
import logging.handlers
import os
from datetime import datetime, timezone

# ── Config ────────────────────────────────────────────────────────────────────
LOGS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "logs",
)

LOG_FORMAT  = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Rotation limits — each log file grows up to 10 MB, keeping 5 backups.
# Total max disk usage per source type: ~60 MB (10 MB × 6 files).
MAX_LOG_BYTES   = 10 * 1024 * 1024   # 10 MB
BACKUP_COUNT    = 5

from contextlib import contextmanager

# ── Public API ────────────────────────────────────────────────────────────────

def build_log_path(source: str = "run") -> str:
    """Return a unique log file path for this run."""
    os.makedirs(LOGS_DIR, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return os.path.join(LOGS_DIR, f"scraper_{ts}_{source}.log")


def attach_file_logger(source: str = "run") -> logging.handlers.RotatingFileHandler:
    """
    Create a RotatingFileHandler for this run, attach it to the root logger,
    and return it. Call detach_file_logger(handler) when the run finishes.

    Each log file is capped at 10 MB with 5 rotated backups to prevent
    unbounded disk growth from frequent pipeline or agent runs.

    Args:
        source: Short label appended to the filename (cli | pipeline | ui | agent).

    Returns:
        The RotatingFileHandler so the caller can detach it later.
    """
    log_path = build_log_path(source)

    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=MAX_LOG_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))

    # Attach to root so every named logger in the project writes here too
    root = logging.getLogger()
    root.addHandler(handler)

    # First line acts as a header
    root.info("=" * 70)
    root.info("SCRAPER RUN STARTED  |  source=%-10s  |  log=%s", source, log_path)
    root.info("=" * 70)

    return handler


def detach_file_logger(handler: logging.Handler | None) -> str:
    """
    Write a closing footer, flush, and safely remove the handler from the root logger.

    Returns:
        The absolute path of the log file that was written (or empty string if handler is None).
    """
    if handler is None:
        return ""

    log_path = getattr(handler, "baseFilename", "")
    root = logging.getLogger()

    if handler in root.handlers:
        try:
            root.info("=" * 70)
            root.info("SCRAPER RUN FINISHED  |  log=%s", log_path)
            root.info("=" * 70)
        except Exception:
            pass
        try:
            root.removeHandler(handler)
        except Exception:
            pass

    try:
        handler.flush()
        handler.close()
    except Exception:
        pass

    return log_path


@contextmanager
def scoped_file_logger(source: str = "run"):
    """
    Context manager that attaches a FileHandler for a run and guarantees it is
    detached and closed even if an exception is raised.

    Usage:
        with scoped_file_logger("pipeline") as log_path:
            ...
    """
    handler = attach_file_logger(source)
    try:
        yield handler.baseFilename
    finally:
        detach_file_logger(handler)
