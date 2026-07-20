"""SQLite in WAL mode (amendment E3). One file, one writer at a time —
the pipeline and the bot daemon share it safely because WAL readers
never block the writer.
"""

import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from jobpilot.models import Base

# Loaded here, not in each entry point: DB_PATH is read at import time, and any
# module that imports db before calling load_dotenv() would otherwise silently
# get the default path instead of the one configured in .env.
load_dotenv()

DB_PATH = os.environ.get("JOBPILOT_DB", "jobpilot.db")

engine = create_engine(f"sqlite:///{DB_PATH}")


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    cursor.close()


SessionLocal = sessionmaker(bind=engine)


def init_db() -> None:
    Base.metadata.create_all(engine)


def get_session() -> Session:
    return SessionLocal()
