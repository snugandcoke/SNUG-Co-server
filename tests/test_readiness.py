"""/api/health (process alive) and /api/ready (database answers)."""

from types import SimpleNamespace

from sqlalchemy.exc import OperationalError

import app.routes.public as public_routes


def broken_database(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OperationalError("SELECT 1", {}, Exception("password authentication failed for user secret"))

    fake = SimpleNamespace(session=SimpleNamespace(execute=refuse, rollback=lambda: None))
    monkeypatch.setattr(public_routes, "db", fake)


def test_health_is_unchanged_and_does_not_touch_the_database(client, monkeypatch):
    broken_database(monkeypatch)
    res = client.get("/api/health")
    assert res.status_code == 200
    assert res.get_json() == {"status": "ok"}


def test_ready_confirms_the_database_answers(client):
    res = client.get("/api/ready")
    assert res.status_code == 200
    assert res.get_json() == {"status": "ready", "database": "ok"}
    assert res.headers["Cache-Control"] == "no-store"


def test_ready_reports_a_database_outage_without_leaking_details(client, monkeypatch):
    broken_database(monkeypatch)
    res = client.get("/api/ready")
    assert res.status_code == 503
    assert res.get_json() == {"status": "unavailable", "database": "unavailable"}
    assert "secret" not in res.get_data(as_text=True)
    assert res.headers["Cache-Control"] == "no-store"
