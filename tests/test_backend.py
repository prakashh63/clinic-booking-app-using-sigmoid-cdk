import importlib.util
import json
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fake_aws import FakeDdb, FakeSes, FakeSqs

ROOT = Path(__file__).resolve().parents[1]
IST = timezone(timedelta(hours=5, minutes=30))


def day_in(days: int) -> str:
    day = (datetime.now(IST) + timedelta(days=days)).date()
    for _ in range(7):
        if day.weekday() < 5:
            return day.isoformat()
        day += timedelta(days=1)
    raise AssertionError("no business date")


def next_weekend() -> str:
    day = datetime.now(IST).date() + timedelta(days=1)
    while day.weekday() != 5:
        day += timedelta(days=1)
    return day.isoformat()


@pytest.fixture
def aws(monkeypatch):
    ddb, sqs, ses = FakeDdb(), FakeSqs(), FakeSes()
    clients = {"dynamodb": ddb, "sqs": sqs, "ses": ses}
    monkeypatch.setitem(
        sys.modules, "boto3", types.SimpleNamespace(client=lambda name: clients[name])
    )
    monkeypatch.setenv("TABLE_NAME_BOOKINGS", "bookings")
    monkeypatch.setenv("QUEUE_URL_BOOKING_EVENTS", "fake-queue")
    monkeypatch.setenv("SENDER_EMAIL", "clinic@example.com")
    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.delenv("CLINIC_HOLIDAYS", raising=False)
    spec = importlib.util.spec_from_file_location(
        "clinic", ROOT / "src" / "backend" / "clinic.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return types.SimpleNamespace(ddb=ddb, sqs=sqs, ses=ses, app=module)


def book(aws, **override):
    body = {
        "date": day_in(3),
        "time": "10:00",
        "service": "checkup",
        "name": "Priya Sharma",
        "email": "priya@example.com",
        **override,
    }
    return aws.app.book_slot({"body": json.dumps(body)}, None)


def slots(aws, day):
    result = aws.app.list_slots({"queryStringParameters": {"date": day}}, None)
    return result, json.loads(result["body"])


def test_every_configured_handler_exists(aws) -> None:
    from app_config import build_config

    for function in build_config("dev").lambdas:
        module_name, handler_name = function.handler.split(".")
        assert module_name == "clinic"
        assert callable(getattr(aws.app, handler_name))


def test_all_slots_are_free_on_an_empty_day(aws) -> None:
    result, body = slots(aws, day_in(3))
    assert result["statusCode"] == 200
    assert len(body["slots"]) == 14
    assert all(s["free"] for s in body["slots"])


def test_slots_reject_a_bad_date(aws) -> None:
    assert slots(aws, "not-a-date")[0]["statusCode"] == 400
    assert slots(aws, "2000-01-01")[0]["statusCode"] == 400


def test_booking_is_saved_and_queued(aws) -> None:
    result = book(aws)
    assert result["statusCode"] == 201
    body = json.loads(result["body"])
    assert len(body["code"]) == 10
    assert len(aws.ddb.items) == 1
    assert json.loads(aws.sqs.messages[0])["type"] == "confirmed"


def test_a_taken_slot_shows_as_not_free(aws) -> None:
    book(aws)
    _, body = slots(aws, day_in(3))
    ten = next(s for s in body["slots"] if s["time"] == "10:00")
    assert ten["free"] is False


def test_double_booking_is_rejected(aws) -> None:
    assert book(aws)["statusCode"] == 201
    assert book(aws, name="Someone Else")["statusCode"] == 409
    assert len(aws.ddb.items) == 1
    assert len(aws.sqs.messages) == 1


@pytest.mark.parametrize(
    "override",
    [
        {"date": "2000-01-01"},
        {"date": "tomorrow"},
        {"time": "03:00"},
        {"service": "surgery"},
        {"name": " "},
        {"email": "nope"},
    ],
)
def test_bad_input_is_rejected(aws, override) -> None:
    assert book(aws, **override)["statusCode"] == 400
    assert aws.ddb.items == {}


def test_invalid_json_and_huge_body_are_rejected(aws) -> None:
    assert aws.app.book_slot({"body": "{bad"}, None)["statusCode"] == 400
    assert aws.app.book_slot({"body": "x" * 2001}, None)["statusCode"] == 413


def test_get_booking_hides_the_email(aws) -> None:
    code = json.loads(book(aws)["body"])["code"]
    result = aws.app.get_booking({"pathParameters": {"code": code}}, None)
    assert result["statusCode"] == 200
    assert "priya@example.com" not in result["body"]
    assert aws.app.get_booking({"pathParameters": {"code": "ABCDEF1234"}}, None)[
        "statusCode"
    ] == 404


def test_cancel_frees_the_slot_and_queues_an_email(aws) -> None:
    code = json.loads(book(aws)["body"])["code"]
    event = {"pathParameters": {"code": code}}
    assert aws.app.cancel_booking(event, None)["statusCode"] == 200
    assert aws.ddb.items == {}
    _, body = slots(aws, day_in(3))
    assert all(s["free"] for s in body["slots"])
    assert json.loads(aws.sqs.messages[-1])["type"] == "cancelled"
    assert aws.app.cancel_booking(event, None)["statusCode"] == 404


def test_staff_page_shortens_names_and_hides_codes(aws) -> None:
    today = datetime.now(IST).date().isoformat()
    aws.ddb.items[(today, "10:00")] = {
        "booking_date": {"S": today},
        "slot_time": {"S": "10:00"},
        "booking_code": {"S": "AAAAAAAAAA"},
        "patient_name": {"S": "Priya Sharma"},
        "patient_email": {"S": "priya@example.com"},
        "service": {"S": "dental"},
    }
    result = aws.app.staff_today({}, None)
    body = json.loads(result["body"])
    assert body["count"] == 1
    assert body["bookings"][0]["name"] == "Priya S."
    assert "AAAAAAAAAA" not in result["body"]
    assert "priya@example.com" not in result["body"]


def test_confirmation_email_is_sent_once(aws) -> None:
    code = json.loads(book(aws)["body"])["code"]
    event = {"Records": [{"body": aws.sqs.messages[0]}]}
    aws.app.notify_worker(event, None)
    aws.app.notify_worker(event, None)  # SQS retry must not send a second e-mail
    assert len(aws.ses.sent) == 1
    sent = aws.ses.sent[0]
    assert sent["Destination"]["ToAddresses"] == ["priya@example.com"]
    assert code in sent["Message"]["Body"]["Text"]["Data"]


def test_cancellation_email_is_sent(aws) -> None:
    code = json.loads(book(aws)["body"])["code"]
    aws.app.cancel_booking({"pathParameters": {"code": code}}, None)
    aws.app.notify_worker({"Records": [{"body": aws.sqs.messages[-1]}]}, None)
    assert len(aws.ses.sent) == 1
    assert "cancel" in aws.ses.sent[0]["Message"]["Subject"]["Data"].lower()


def test_no_confirmation_for_a_cancelled_booking(aws) -> None:
    code = json.loads(book(aws)["body"])["code"]
    aws.app.cancel_booking({"pathParameters": {"code": code}}, None)
    aws.app.notify_worker({"Records": [{"body": aws.sqs.messages[0]}]}, None)
    assert aws.ses.sent == []


def put_item_for(aws, day: str) -> None:
    aws.ddb.items[(day, "10:00")] = {
        "booking_date": {"S": day},
        "slot_time": {"S": "10:00"},
        "booking_code": {"S": "BBBBBBBBBB"},
        "patient_name": {"S": "Priya Sharma"},
        "patient_email": {"S": "priya@example.com"},
        "service": {"S": "eye"},
    }


def test_daily_reminder_only_covers_tomorrow_and_only_once(aws) -> None:
    put_item_for(aws, day_in(1))
    result = aws.app.daily_reminder({}, None)
    assert result["reminders"] == 1
    assert aws.app.daily_reminder({}, None)["reminders"] == 0
    assert len(aws.ses.sent) == 1


def test_daily_reminder_ignores_other_days(aws) -> None:
    put_item_for(aws, day_in(2))
    assert aws.app.daily_reminder({}, None)["reminders"] == 0
    assert aws.ses.sent == []


def test_weekend_returns_closed_schedule(aws) -> None:
    result, body = slots(aws, next_weekend())
    assert result["statusCode"] == 200
    assert body["closed"] is True
    assert body["slots"] == []
    assert "Saturday" in body["reason"]
    assert book(aws, date=next_weekend())["statusCode"] == 400


def test_clinic_hours_and_lunch_break_are_enforced(aws) -> None:
    _, body = slots(aws, day_in(3))
    times = [slot["time"] for slot in body["slots"]]
    assert len(times) == 14
    assert "09:00" in times and "16:30" in times
    assert "13:00" not in times and "13:30" not in times
    assert "17:00" not in times
    assert body["schedule"]["timezone"] == "Asia/Kolkata"
    assert book(aws, time="13:30")["statusCode"] == 400
    assert book(aws, time="17:00")["statusCode"] == 400


def test_configured_public_holiday_is_closed(aws, monkeypatch) -> None:
    date = day_in(4)
    monkeypatch.setattr(aws.app, "HOLIDAYS", frozenset([date]))
    _, payload = slots(aws, date)
    assert payload["closed"] is True
    assert "holiday" in payload["reason"].lower()
    assert book(aws, date=date)["statusCode"] == 400


def test_staff_endpoint_requires_authentication_on_aws(aws, monkeypatch) -> None:
    monkeypatch.setenv("APP_MODE", "aws")
    result = aws.app.staff_today({}, None)
    assert result["statusCode"] == 403
    assert "booking" not in result["body"].lower()
