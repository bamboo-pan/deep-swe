import json

import pytest

from app.database import SessionLocal, init_db
from app.models import Setting
from app.provider_proxy import (
    REQUEST_SCHEDULE_KEY, _target_url, provider_queue_status,
    reserve_provider_request,
)


def test_target_url_handles_openai_and_anthropic_paths():
    assert _target_url("http://provider.example/v1", "responses", "") == (
        "http://provider.example/v1/responses"
    )
    assert _target_url("http://provider.example/v1", "v1/messages", "beta=1") == (
        "http://provider.example/v1/messages?beta=1"
    )


def test_provider_rpm_reserves_every_http_request():
    init_db()
    keys = ("provider_rpm", REQUEST_SCHEDULE_KEY)
    with SessionLocal() as db:
        original = {
            key: row.value
            for key in keys
            if (row := db.get(Setting, key)) is not None
        }
        for key in keys:
            row = db.get(Setting, key)
            if row:
                db.delete(row)
        db.add(Setting(key="provider_rpm", value=json.dumps(30)))
        db.commit()
    try:
        for _ in range(30):
            assert reserve_provider_request(now=1000.0) == 0
        status = provider_queue_status(now=1000.0)
        assert status == {
            "enabled": True,
            "rpm": 30,
            "sent_last_60_seconds": 30,
            "queued_requests": 0,
            "available_now": 0,
            "next_release_seconds": 60.0,
        }
        assert reserve_provider_request(now=1000.0) == pytest.approx(60.0)
        status = provider_queue_status(now=1030.0)
        assert status["queued_requests"] == 1
        assert status["next_release_seconds"] == 30.0
        assert reserve_provider_request(now=1030.0) == pytest.approx(30.0)
        assert reserve_provider_request(now=1060.0) == 0
    finally:
        with SessionLocal() as db:
            for key in keys:
                row = db.get(Setting, key)
                if row:
                    db.delete(row)
            for key, value in original.items():
                db.add(Setting(key=key, value=value))
            db.commit()


def test_provider_rpm_zero_disables_waiting():
    init_db()
    with SessionLocal() as db:
        row = db.get(Setting, "provider_rpm")
        original = row.value if row else None
        if row:
            row.value = "0"
        else:
            db.add(Setting(key="provider_rpm", value="0"))
        db.commit()
    try:
        assert reserve_provider_request(now=1000.0) == 0
        assert reserve_provider_request(now=1000.0) == 0
    finally:
        with SessionLocal() as db:
            row = db.get(Setting, "provider_rpm")
            if original is None:
                if row:
                    db.delete(row)
            elif row:
                row.value = original
            else:
                db.add(Setting(key="provider_rpm", value=original))
            db.commit()
