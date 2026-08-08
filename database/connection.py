import atexit
import logging
import os
import time
from contextlib import contextmanager
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.exc import TimeoutError as SATimeoutError
from sqlalchemy.orm import sessionmaker, Session

logger = logging.getLogger(__name__)

# Load credentials from .env file
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise ValueError("DATABASE_URL not set. Please configure your .env file.")

# Create the engine — pool_pre_ping ensures dead connections are recycled.
#
# Pool sizing: supports up to pool_size + max_overflow = 20 concurrent sessions.
# This comfortably covers 8 parallel Prefect scraper tasks + Prefect's own
# internal DB operations without hitting SQLAlchemy's QueuePool limit.
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    echo=False,       # Set True to log all SQL (useful for debugging)
    pool_size=15,     # Persistent connections kept alive in the pool
    max_overflow=5,   # Extra connections allowed under burst load (total = 20)
    pool_recycle=3600,# Recycle connections after 1h to prevent stale sockets
    pool_timeout=30,  # Raise immediately after 30s instead of hanging forever
)

# Register engine disposal on process shutdown to prevent leaked socket connections
atexit.register(engine.dispose)


def dispose_engine():
    """Explicitly disposes the connection pool (useful for shutdown hooks or testing)."""
    engine.dispose()


# Session factory — use this to get a DB session
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def get_session(max_retries: int = 3, initial_delay: float = 1.0) -> Session:
    """
    Returns a new SQLAlchemy session with exponential backoff retry on pool exhaustion.
    
    Usage:
        session = get_session()
        try:
            # ... do work ...
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
    """
    for attempt in range(max_retries):
        try:
            return SessionLocal()
        except SATimeoutError as err:
            if attempt == max_retries - 1:
                logger.error("DB connection pool exhausted after %d attempts.", max_retries)
                raise err
            delay = initial_delay * (2 ** attempt)
            logger.warning(
                "DB connection pool exhausted. Retrying in %.1fs (attempt %d/%d)...",
                delay,
                attempt + 1,
                max_retries,
            )
            time.sleep(delay)


@contextmanager
def session_scope():
    """
    Provide a transactional scope around a series of database operations.
    
    Usage:
        with session_scope() as session:
            session.add(obj)
    """
    session = get_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db():
    """
    Creates all tables if they don't exist yet.
    Called once on startup. Alembic handles migrations in production.
    """
    from database.models import Base
    Base.metadata.create_all(bind=engine)
