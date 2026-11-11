import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest
from local_server import make_server

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture
def base(monkeypatch):
    # Unit tests are isolated: no Docker dependency and no persistent bookings touched.
    monkeypatch.setenv("USE_DYNAMODB_LOCAL", "0")
    httpd = make_server(0, "127.0.0.1")
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()


def call(base, method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base + path, data=data, method=method, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def state(base):
    return json.loads(call(base, "GET", "/debug/state")[1])


def booking_body(**override):
    day_obj = (datetime.now(IST) + timedelta(days=3)).date()
    while day_obj.weekday() > 4:
        day_obj += timedelta(days=1)
    day = day_obj.isoformat()
    return {
        "date": day,
        "time": "10:00",
        "service": "checkup",
        "name": "Priya Sharma",
        "email": "priya@example.com",
        **override,
    }


def test_page_and_dashboard_are_served(base) -> None:
    assert "CareSlot" in call(base, "GET", "/")[1]
    assert "Timeline" in call(base, "GET", "/debug")[1]


def test_a_booking_reaches_every_service(base) -> None:
    status, _ = call(base, "POST", "/api/bookings", booking_body())
    assert status == 201
    now = state(base)
    assert len(now["items"]) == 1
    assert [m["to"] for m in now["emails"]] == ["priya@example.com"]
    log = "\n".join(now["timeline"])
    for service in ("CloudFront", "API Gateway", "DynamoDB", "SQS", "SES"):
        assert service in log


def test_cancelling_updates_table_and_sends_email(base) -> None:
    code = json.loads(call(base, "POST", "/api/bookings", booking_body())[1])["code"]
    assert call(base, "DELETE", f"/api/bookings/{code}")[0] == 200
    now = state(base)
    assert now["items"] == []
    assert len(now["emails"]) == 2


def test_double_booking_is_refused(base) -> None:
    assert call(base, "POST", "/api/bookings", booking_body())[0] == 201
    assert call(base, "POST", "/api/bookings", booking_body())[0] == 409


def test_seed_then_reminder(base) -> None:
    assert call(base, "POST", "/debug/seed")[0] == 201
    assert len(state(base)["emails"]) == 1
    result = json.loads(call(base, "POST", "/debug/run-reminder")[1])
    seeded = state(base)["items"][0]
    tomorrow = (datetime.now(IST) + timedelta(days=1)).date().isoformat()
    expected = int(seeded["booking_date"] == tomorrow)
    assert result["reminders"] == expected
    assert len(state(base)["emails"]) == 1 + expected


def test_reset_clears_everything(base) -> None:
    call(base, "POST", "/api/bookings", booking_body())
    call(base, "POST", "/debug/reset")
    now = state(base)
    assert now["items"] == [] and now["emails"] == []


def test_unknown_api_route_is_404(base) -> None:
    assert call(base, "GET", "/api/nothing")[0] == 404


def test_frontend_assets_and_favicon_are_served(base) -> None:
    for path, snippet in (
        ("/assets/styles.css", ".container"),
        ("/assets/app.js", "loadSlots"),
        ("/favicon.svg", "<svg"),
    ):
        status, content = call(base, "GET", path)
        assert status == 200
        assert snippet in content
