"""pg.connect failure-hint regression (tool-w3 de-machine-binding): hints no longer contain local absolute paths
(/home/<user>/… pg_ctl, /tmp/pgbench_new/data), replaced with generic guidance (instance list/dsn/--dsn).
A mocked psycopg2.connect raises OperationalError; no running PG needed."""
import psycopg2
import pytest

from pgtuner import pg


def _refuse(conninfo, **kw):
    raise psycopg2.OperationalError(
        'connection to server on socket "/tmp/.s.PGSQL.5432" failed: '
        "No such file or directory\n\tIs the server running locally?")


def test_connect_refused_hint_is_portable(monkeypatch):
    monkeypatch.setattr(psycopg2, "connect", _refuse)
    with pytest.raises(ConnectionError) as ei:
        pg.connect(None)
    msg = str(ei.value)
    assert "pgtuner instance list" in msg                      # generic guidance: list registered instances
    assert "pgtuner instance dsn NAME DB" in msg               # generic guidance: get the connection string
    assert "pg_ctl" in msg                                     # generic guidance: start the server bare
    assert "/home/zxx" not in msg   # hygiene:allow (guard assertion itself) machine-bound paths must disappear
    assert "/tmp/pgbench_new" not in msg
    assert "原始错误" in msg                                   # the original error is still attached


def test_connect_missing_db_hint_unchanged(monkeypatch):
    """The missing-database branch's hint is unrelated to this fix; locked here too so it does not regress."""
    def no_db(conninfo, **kw):
        raise psycopg2.OperationalError('FATAL:  database "nope" does not exist')
    monkeypatch.setattr(psycopg2, "connect", no_db)
    with pytest.raises(ConnectionError) as ei:
        pg.connect(None)
    assert "库不存在" in str(ei.value)
