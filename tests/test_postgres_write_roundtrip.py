"""Exercise the real HTTP write path against an isolated PostgreSQL schema."""
import os
from contextlib import closing
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


def test_postgres_transaction_crud_and_sync():
    url = os.getenv("BEECOUNT_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set BEECOUNT_TEST_POSTGRES_URL to a disposable PostgreSQL database")

    from fastapi.testclient import TestClient

    from src.database import Base, get_db
    from src.main import app

    schema = "test_write_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    factory = sessionmaker(engine)
    previous = dict(app.dependency_overrides)

    def session_override():
        with factory() as db:
            yield db

    try:
        Base.metadata.create_all(engine)
        app.dependency_overrides[get_db] = session_override
        with closing(TestClient(app)) as client:
            registered = client.post("/api/v1/auth/register", json={
                "email": "postgres-regression@example.com", "password": "test-password-123",
                "client_type": "web", "device_name": "pytest", "platform": "web",
            })
            assert registered.status_code == 200, registered.text
            token = registered.json()["access_token"]
            headers = {"Authorization": f"Bearer {token}", "X-Device-ID": "test-writer"}
            ledger = client.post("/api/v1/write/ledgers", headers=headers,
                                 json={"ledger_name": "Regression", "currency": "CNY"})
            assert ledger.status_code == 200, ledger.text
            ledger_id = ledger.json()["entity_id"]
            path = f"/api/v1/write/ledgers/{ledger_id}/transactions"
            created = client.post(path, headers=headers, json={
                "base_change_id": 0, "tx_type": "expense", "amount": 12.5,
                "happened_at": "2026-05-01T00:00:00+00:00", "note": "regression",
            })
            assert created.status_code == 200, created.text
            tx_id = created.json()["entity_id"]
            updated = client.patch(f"{path}/{tx_id}", headers=headers,
                                   json={"base_change_id": created.json()["new_change_id"],
                                         "amount": 18.5})
            assert updated.status_code == 200, updated.text
            read_path = f"/api/v1/read/ledgers/{ledger_id}/transactions"
            rows = client.get(read_path, headers=headers)
            assert rows.status_code == 200, rows.text
            assert len(rows.json()) == 1
            assert rows.json()[0]["amount"] == 18.5
            pull = client.get("/api/v1/sync/pull?since=0&limit=500", headers={
                "Authorization": f"Bearer {token}", "X-Device-ID": "test-reader",
            })
            assert pull.status_code == 200, pull.text
            assert any(c.get("entity_sync_id") == tx_id for c in pull.json()["changes"])
            deleted = client.request("DELETE", f"{path}/{tx_id}", headers=headers,
                                     json={"base_change_id": updated.json()["new_change_id"]})
            assert deleted.status_code == 200, deleted.text
            assert client.get(read_path, headers=headers).json() == []
    finally:
        app.dependency_overrides.clear()
        app.dependency_overrides.update(previous)
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
