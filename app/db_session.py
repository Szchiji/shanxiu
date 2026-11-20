"""Return idle SQLAlchemy connections to the pool.

Bot handlers and scheduled jobs often keep a Flask app context open while
``await``-ing Telegram I/O. A read starts a transaction and, by default, holds
the pool connection as ``idle in transaction`` until the context exits. With
pool_size 5 + overflow 10, a burst of in-flight handlers exhausts the pool and
the admin site plus later message handling time out.

This module auto-commits a *clean* (no pending ORM writes) session after a
SELECT so the connection is released immediately. Row-lock queries
(``FOR UPDATE``), INSERT/UPDATE/DELETE, and dirty or already-open
transactions are left alone so existing locking and write transactions stay
atomic.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager

from flask_sqlalchemy.session import Session as FSASession

logger = logging.getLogger(__name__)

_RELEASING = "_sx_releasing"


def _statement_holds_row_lock(statement) -> bool:
    for_update = getattr(statement, "_for_update_arg", None)
    if for_update is not None:
        return True
    try:
        text = str(statement.compile(compile_kwargs={"literal_binds": False}))
    except Exception:
        try:
            text = str(statement)
        except Exception:
            return False
    upper = text.upper()
    return "FOR UPDATE" in upper or "FOR SHARE" in upper or "LOCK IN SHARE" in upper


def _is_auto_releasable_select(statement) -> bool:
    """True only for a plain SELECT that does not take a row lock."""
    if statement is None or _statement_holds_row_lock(statement):
        return False
    if getattr(statement, "is_select", False):
        return True
    inner = getattr(statement, "element", None) or getattr(statement, "selectable", None)
    return bool(getattr(inner, "is_select", False))


def _orm_session(session):
    """Return the real Session even if a scoped_session proxy was passed."""
    in_tx = getattr(session, "in_transaction", None)
    if callable(in_tx):
        return session
    if callable(session):
        return session()
    return session


def session_has_pending_writes(session) -> bool:
    session = _orm_session(session)
    return bool(session.new or session.dirty or session.deleted)


def release_idle_db_session(session=None) -> bool:
    """Commit a clean read transaction so the pool connection is returned.

    Returns True if a transaction was released. Does not commit or roll back a
    dirty session or a write/lock transaction; the caller still owns those.
    """
    if session is None:
        try:
            from app import db
            session = db.session
        except Exception:
            return False
    session = _orm_session(session)
    info = getattr(session, "info", None)
    if info is None or info.get(_RELEASING):
        return False
    try:
        if not session.is_active or not session.in_transaction():
            return False
        if session.in_nested_transaction():
            return False
        if session_has_pending_writes(session):
            return False
    except Exception:
        return False
    info[_RELEASING] = True
    try:
        session.commit()
        return True
    except Exception:
        logger.exception("Failed to release idle DB session")
        try:
            session.rollback()
        except Exception:
            pass
        return False
    finally:
        info[_RELEASING] = False


class ReleasingSession(FSASession):
    """Session that returns the pool connection after a clean SELECT."""

    def execute(self, statement, *args, **kwargs):
        # Only auto-release when this statement started a fresh read
        # transaction. If a transaction is already open (explicit begin,
        # earlier writes, flush, or FOR UPDATE), committing here would break
        # atomicity. Also capture pending writes *before* autoflush.
        already_in_transaction = self.in_transaction()
        had_pending_writes = session_has_pending_writes(self)
        result = super().execute(statement, *args, **kwargs)
        if (
            not already_in_transaction
            and not had_pending_writes
            and _is_auto_releasable_select(statement)
        ):
            # ORM results may lazily fetch rows from a psycopg cursor. COMMIT
            # closes that cursor: buffer first, then return a replayable Result.
            # Streaming callers own the transaction until they consume/close it.
            options = dict(getattr(statement, 'get_execution_options', lambda: {})())
            options.update(kwargs.get('execution_options', {}))
            orm_options = options.get('_sa_orm_load_options')
            streaming = (options.get('stream_results') or options.get('yield_per')
                         or getattr(orm_options, '_yield_per', None))
            if not streaming:
                result = result.freeze()()
                # The rows are now ORM instances. Expiring them here can erase
                # values still being populated by a deferred attribute loader.
                expires = self.expire_on_commit
                self.expire_on_commit = False
                try:
                    release_idle_db_session(self)
                finally:
                    self.expire_on_commit = expires
        return result


@contextmanager
def db_app_context(app):
    """App context that always removes the session, including on exceptions."""
    from app import db

    with app.app_context():
        try:
            yield db.session
        except Exception:
            db.session.rollback()
            raise
        finally:
            db.session.remove()
