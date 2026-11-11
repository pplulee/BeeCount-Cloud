"""Regression coverage for PostgreSQL writes; see upstream PR #97.

Set BEECOUNT_TEST_POSTGRES_URL to a disposable database for real writes.
Only uniquely named test tables are created and dropped.
"""
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import Column, MetaData, String, Table, create_engine, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from src.projection import _upsert


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect()])
@pytest.mark.parametrize("values", [{"id": "one"}, {"id": "one", "value": "new"}])
def test_upsert_compiles_for_bound_dialect(dialect, values):
    table = Table("projection_test", MetaData(), Column("id", String, primary_key=True),
                  Column("value", String))
    statements = []
    db = SimpleNamespace(get_bind=lambda: SimpleNamespace(dialect=dialect),
                         execute=statements.append)
    _upsert(db, table, ("id",), values)
    sql = str(statements[0].compile(dialect=dialect))
    assert "ON CONFLICT" in sql
    assert ("DO UPDATE" if "value" in values else "DO NOTHING") in sql


@pytest.fixture(params=["sqlite", "postgresql"])
def database(request):
    url = "sqlite://"
    if request.param == "postgresql":
        url = os.getenv("BEECOUNT_TEST_POSTGRES_URL")
        if not url:
            pytest.skip("Set BEECOUNT_TEST_POSTGRES_URL for real PostgreSQL writes")
    engine = create_engine(url)
    metadata = MetaData()
    table = Table("test_upsert_" + uuid4().hex, metadata,
                  Column("id", String, primary_key=True), Column("value", String))
    metadata.create_all(engine)
    try:
        yield engine, table
    finally:
        metadata.drop_all(engine)
        engine.dispose()


def test_insert_update_do_nothing_and_rollback(database):
    engine, table = database
    with Session(engine) as db:
        _upsert(db, table, ("id",), {"id": "one", "value": "first"})
        db.commit()
        assert db.scalar(select(table.c.value)) == "first"
        _upsert(db, table, ("id",), {"id": "one", "value": "updated"})
        db.commit()
        assert db.scalar(select(table.c.value)) == "updated"
        _upsert(db, table, ("id",), {"id": "one"})
        db.commit()
        assert db.scalar(select(table.c.value)) == "updated"
        _upsert(db, table, ("id",), {"id": "two", "value": "discard"})
        db.rollback()
        assert db.execute(select(table.c.id)).scalars().all() == ["one"]
