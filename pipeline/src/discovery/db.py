from __future__ import annotations

import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


def get_database_url() -> str:
    try:
        return os.environ["DATABASE_URL"]
    except KeyError as exc:
        raise RuntimeError(
            "DATABASE_URL is not set. It should be the Postgres connection string "
            "(see the db-connection-string secret in Key Vault)."
        ) from exc


def get_engine(database_url: str | None = None) -> Engine:
    return create_engine(database_url or get_database_url(), pool_pre_ping=True)


def get_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine or get_engine(), expire_on_commit=False)
