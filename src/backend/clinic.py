"""Clinic booking backend: every Lambda handler of the application is in this file."""

import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone

import boto3

ddb = boto3.client("dynamodb")
sqs = boto3.client("sqs")
ses = boto3.client("ses")

CLINIC_TZ = timezone(timedelta(hours=5, minutes=30))  # India Standard Time
SERVICES = {"checkup": "General check-up", "dental": "Dental visit", "eye": "Eye test"}
# Appointments start every 30 minutes from 09:00 until 16:30, with a 13:00–14:00 break.
OPENING_TIME, CLOSING_TIME = "09:00", "17:00"
LUNCH_START, LUNCH_END = "13:00", "14:00"
OPEN_WEEKDAYS = (0, 1, 2, 3, 4)  # Monday to Friday; 5=Saturday, 6=Sunday
SLOT_TIMES = [
    f"{hour:02d}:{minute:02d}"
    for hour in range(9, 17)
    for minute in (0, 30)
    if not (13 <= hour < 14)
]
# Extra full-day closures supplied as ISO dates in CLINIC_HOLIDAYS=YYYY-MM-DD,YYYY-MM-DD.
HOLIDAYS = frozenset(d.strip() for d in os.environ.get("CLINIC_HOLIDAYS", "").split(",") if d.strip())
MAX_DAYS_AHEAD = 60
KEEP_DAYS = 90  # DynamoDB deletes a booking this long after the appointment
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
CODE = re.compile(r"^[0-9A-F]{10}$")
CONFLICT = "ConditionalCheckFailedException"


# ---------- small helpers ----------
def _table() -> str:
    return os.environ["TABLE_NAME_BOOKINGS"]


def _reply(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _now():
    return datetime.now(CLINIC_TZ)


def _error_code(exc):
    return getattr(exc, "response", {}).get("Error", {}).get("Code", "")


def _plain(item):
    """{'a': {'S': 'x'}} -> {'a': 'x'}"""
    return {key: next(iter(value.values())) for key, value in item.items()}


def _text(data, key):
    value = data.get(key)
    return value.strip() if isinstance(value, str) else ""


def _slot_start(day, slot):
    return datetime.strptime(f"{day} {slot}", "%Y-%m-%d %H:%M").replace(
        tzinfo=CLINIC_TZ
    )


def _check_day(text):
    """Returns (date, "") or (None, reason)."""
    try:
        day = datetime.strptime(text, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None, "date must look like 2026-12-31"
    today = _now().date()
    if day < today or day > today + timedelta(days=MAX_DAYS_AHEAD):
        return None, f"date must be today or up to {MAX_DAYS_AHEAD} days ahead"
    return day, ""


def _closed_reason(day):
    if day.weekday() not in OPEN_WEEKDAYS:
        return "Closed on Saturdays and Sundays. Please choose a weekday."
    if day.isoformat() in HOLIDAYS:
        return "The clinic is closed for a scheduled holiday. Please choose another date."
    return ""


def _schedule_data():
    return {
        "open": OPENING_TIME,
        "close": CLOSING_TIME,
        "lunch_start": LUNCH_START,
        "lunch_end": LUNCH_END,
        "timezone": "Asia/Kolkata",
        "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri"],
        "slot_minutes": 30,
    }


def _day_items(day_text):
    found = ddb.query(
        TableName=_table(),
        KeyConditionExpression="booking_date = :d",
        ExpressionAttributeValues={":d": {"S": day_text}},
    )
    return [_plain(item) for item in found.get("Items", [])]


def _find_by_code(code):
    if not CODE.match(code):
        return None
    found = ddb.query(
        TableName=_table(),
        IndexName="code-index",
        KeyConditionExpression="booking_code = :c",
        ExpressionAttributeValues={":c": {"S": code}},
        Limit=1,
    )
    items = found.get("Items", [])
    return _plain(items[0]) if items else None


def _key(booking):
    return {
        "booking_date": {"S": booking["booking_date"]},
        "slot_time": {"S": booking["slot_time"]},
    }


def _notify(kind, booking):
    """Queue an e-mail. The booking is already saved, so a queue problem is only logged."""
    queue_url = os.environ.get("QUEUE_URL_BOOKING_EVENTS")
    if not queue_url:
        return
    message = {
        "type": kind,
        "date": booking["booking_date"],
        "time": booking["slot_time"],
        "code": booking["booking_code"],
        "name": booking["patient_name"],
        "email": booking["patient_email"],
        "service": booking["service"],
    }
    try:
        sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(message))
    except Exception:  # noqa: BLE001
        print(f"could not queue the {kind} notification")


def _send_email(to, subject, text):
    ses.send_email(
        Source=os.environ["SENDER_EMAIL"],
        Destination={"ToAddresses": [to]},
        Message={"Subject": {"Data": subject}, "Body": {"Text": {"Data": text}}},
    )


def _short_name(name):
    parts = name.split()
    return parts[0] if len(parts) < 2 else f"{parts[0]} {parts[-1][0]}."


def _label(service):
    return SERVICES.get(service, service)


# ---------- API handlers ----------
def list_slots(event, context):
    params = event.get("queryStringParameters") or {}
    day, problem = _check_day(params.get("date"))
    if day is None:
        return _reply(400, {"error": problem})
    reason = _closed_reason(day)
    if reason:
        return _reply(200, {
            "date": day.isoformat(), "closed": True, "reason": reason,
            "slots": [], "schedule": _schedule_data(),
        })
    taken = {item["slot_time"] for item in _day_items(day.isoformat())}
    now = _now()
    slots = [
        {"time": s, "free": s not in taken and _slot_start(day, s) > now}
        for s in SLOT_TIMES
    ]
    return _reply(200, {
        "date": day.isoformat(), "closed": False, "slots": slots,
        "schedule": _schedule_data(),
    })


def book_slot(event, context):
    raw = event.get("body") or "{}"
    if len(raw) > 2000:
        return _reply(413, {"error": "request too large"})
    try:
        data = json.loads(raw)
    except ValueError:
        return _reply(400, {"error": "invalid JSON"})
    if not isinstance(data, dict):
        return _reply(400, {"error": "invalid request"})

    day, problem = _check_day(data.get("date"))
    slot, service = _text(data, "time"), _text(data, "service")
    name, email = _text(data, "name"), _text(data, "email")
    if day is None:
        return _reply(400, {"error": problem})
    reason = _closed_reason(day)
    if reason:
        return _reply(400, {"error": reason})
    if slot not in SLOT_TIMES:
        return _reply(400, {"error": "time is not one of the clinic slots"})
    if service not in SERVICES:
        return _reply(400, {"error": "choose a service from the list"})
    if not 2 <= len(name) <= 60:
        return _reply(400, {"error": "name must be 2 to 60 characters"})
    if len(email) > 120 or not EMAIL.match(email):
        return _reply(400, {"error": "enter a valid e-mail address"})
    start = _slot_start(day, slot)
    if start <= _now():
        return _reply(400, {"error": "that time has already passed"})

    code = secrets.token_hex(5).upper()
    item = {
        "booking_date": {"S": day.isoformat()},
        "slot_time": {"S": slot},
        "booking_code": {"S": code},
        "patient_name": {"S": name},
        "patient_email": {"S": email},
        "service": {"S": service},
        "created_at": {"N": str(int(_now().timestamp()))},
        "expires_at": {"N": str(int((start + timedelta(days=KEEP_DAYS)).timestamp()))},
    }
    try:
        # Succeeds only if nobody booked this slot yet. This is what stops double booking.
        ddb.put_item(
            TableName=_table(),
            Item=item,
            ConditionExpression="attribute_not_exists(booking_date)",
        )
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) == CONFLICT:
            return _reply(409, {"error": "That slot was just taken. Pick another."})
        raise
    _notify("confirmed", _plain(item))
    return _reply(
        201,
        {
            "code": code,
            "date": day.isoformat(),
            "time": slot,
            "service": _label(service),
        },
    )


def get_booking(event, context):
    code = ((event.get("pathParameters") or {}).get("code") or "").upper()
    booking = _find_by_code(code)
    if booking is None:
        return _reply(404, {"error": "no booking found for that code"})
    start = _slot_start(booking["booking_date"], booking["slot_time"])
    return _reply(
        200,
        {
            "code": code,
            "date": booking["booking_date"],
            "time": booking["slot_time"],
            "service": _label(booking["service"]),
            "name": booking["patient_name"],
            "status": "upcoming" if start > _now() else "past",
        },
    )


def cancel_booking(event, context):
    code = ((event.get("pathParameters") or {}).get("code") or "").upper()
    booking = _find_by_code(code)
    if booking is None:
        return _reply(404, {"error": "no booking found for that code"})
    if _slot_start(booking["booking_date"], booking["slot_time"]) <= _now():
        return _reply(400, {"error": "a past appointment cannot be cancelled"})
    try:
        ddb.delete_item(
            TableName=_table(),
            Key=_key(booking),
            ConditionExpression="booking_code = :c",
            ExpressionAttributeValues={":c": {"S": code}},
        )
    except Exception as exc:  # noqa: BLE001
        if _error_code(exc) == CONFLICT:
            return _reply(404, {"error": "no booking found for that code"})
        raise
    _notify("cancelled", booking)
    return _reply(200, {"cancelled": True})


def staff_today(event, context):
    # Staff sign-in has not been implemented. Never expose a staff endpoint on AWS.
    if os.environ.get("APP_MODE", "aws") != "local":
        return _reply(403, {"error": "Staff access requires authentication and is disabled in AWS."})
    day = _now().date().isoformat()
    items = _day_items(day)
    return _reply(
        200,
        {
            "date": day,
            "count": len(items),
            "bookings": [
                {
                    "time": item["slot_time"],
                    "name": _short_name(item["patient_name"]),  # staff page has no login
                    "service": _label(item["service"]),
                }
                for item in items
            ],
        },
    )


# ---------- background handlers ----------
def notify_worker(event, context):
    """Reads booking-events from SQS and sends the e-mail. A failure retries, then DLQ."""
    for record in event["Records"]:
        message = json.loads(record["body"])
        if message["type"] == "confirmed":
            key = {
                "booking_date": {"S": message["date"]},
                "slot_time": {"S": message["time"]},
            }
            current = ddb.get_item(TableName=_table(), Key=key).get("Item")
            if current is None or "confirm_sent" in current:
                continue  # cancelled meanwhile, or already e-mailed (retry)
            _send_email(
                message["email"],
                "Your appointment is confirmed",
                f"Hello {message['name']},\n\n"
                f"Your {_label(message['service'])} is booked for "
                f"{message['date']} at {message['time']}.\n"
                f"Booking code: {message['code']}\n"
                "Keep this code. You need it to view or cancel the booking.\n",
            )
            ddb.update_item(
                TableName=_table(),
                Key=key,
                UpdateExpression="SET confirm_sent = :t",
                ExpressionAttributeValues={":t": {"BOOL": True}},
            )
        else:
            _send_email(
                message["email"],
                "Your appointment was cancelled",
                f"Hello {message['name']},\n\n"
                f"Your {_label(message['service'])} on {message['date']} at "
                f"{message['time']} has been cancelled.\n",
            )
    return {"processed": len(event["Records"])}


def daily_reminder(event, context):
    """Runs once a day and e-mails everyone who has an appointment tomorrow."""
    tomorrow = (_now().date() + timedelta(days=1)).isoformat()
    sent = 0
    for item in _day_items(tomorrow):
        if "reminder_sent" in item:
            continue
        _send_email(
            item["patient_email"],
            "Appointment reminder",
            f"Hello {item['patient_name']},\n\n"
            f"Reminder: your {_label(item['service'])} is tomorrow, "
            f"{tomorrow}, at {item['slot_time']}.\n"
            f"Booking code: {item['booking_code']}\n",
        )
        ddb.update_item(
            TableName=_table(),
            Key=_key(item),
            UpdateExpression="SET reminder_sent = :t",
            ExpressionAttributeValues={":t": {"BOOL": True}},
        )
        sent += 1
    return {"date": tomorrow, "reminders": sent}
