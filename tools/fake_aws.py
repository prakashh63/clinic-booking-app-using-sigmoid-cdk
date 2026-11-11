"""In-memory stand-ins for DynamoDB, SQS and SES.

Used by the tests and by tools/local_server.py, so running or testing the application needs
no AWS account, no Docker and no DynamoDB Local.
"""

import json
from collections.abc import Callable
from typing import Any

Log = Callable[[str, str], None]


def _quiet(service: str, message: str) -> None:
    return None


class ConditionFailed(Exception):
    """What DynamoDB raises when a ConditionExpression is not met."""

    response = {"Error": {"Code": "ConditionalCheckFailedException"}}


def _key(source: dict[str, Any]) -> tuple[str, str]:
    return source["booking_date"]["S"], source["slot_time"]["S"]


class FakeDdb:
    def __init__(self, log: Log = _quiet) -> None:
        self.items: dict[tuple[str, str], dict[str, Any]] = {}
        self.log = log

    def put_item(self, **kw: Any) -> None:
        item = kw["Item"]
        key = _key(item)
        if "ConditionExpression" in kw and key in self.items:
            self.log("DynamoDB", f"slot {key[0]} {key[1]} already booked: write refused")
            raise ConditionFailed()
        self.items[key] = item
        code = item["booking_code"]["S"]
        self.log("DynamoDB", f"saved booking {key[0]} {key[1]} (code {code})")

    def query(self, **kw: Any) -> dict[str, Any]:
        values = kw["ExpressionAttributeValues"]
        if kw.get("IndexName"):
            code = values[":c"]["S"]
            found = [i for i in self.items.values() if i["booking_code"]["S"] == code]
            self.log("DynamoDB", f"looked up booking code {code} (code-index)")
        else:
            day = values[":d"]["S"]
            found = sorted(
                (i for k, i in self.items.items() if k[0] == day),
                key=lambda i: i["slot_time"]["S"],
            )
            self.log("DynamoDB", f"read the bookings of {day}: {len(found)} found")
        return {"Items": found[: kw.get("Limit", len(found))]}

    def get_item(self, **kw: Any) -> dict[str, Any]:
        key = _key(kw["Key"])
        return {"Item": self.items[key]} if key in self.items else {}

    def delete_item(self, **kw: Any) -> None:
        key = _key(kw["Key"])
        wanted = kw["ExpressionAttributeValues"][":c"]["S"]
        if key not in self.items or self.items[key]["booking_code"]["S"] != wanted:
            raise ConditionFailed()
        del self.items[key]
        self.log("DynamoDB", f"deleted booking {key[0]} {key[1]}")

    def update_item(self, **kw: Any) -> None:
        key = _key(kw["Key"])
        attribute = kw["UpdateExpression"].split()[1]
        self.items[key][attribute] = kw["ExpressionAttributeValues"][":t"]
        self.log("DynamoDB", f"marked {attribute} on {key[0]} {key[1]}")


class FakeSqs:
    """Keeps messages. If `deliver` is given, the message is handed to the worker at once."""

    def __init__(
        self, log: Log = _quiet, deliver: Callable[[str], None] | None = None
    ) -> None:
        self.messages: list[str] = []
        self.log = log
        self.deliver = deliver

    def send_message(self, **kw: Any) -> None:
        body = kw["MessageBody"]
        self.messages.append(body)
        kind = json.loads(body).get("type", "?")
        self.log("SQS", f"message added to booking-events ({kind})")
        if self.deliver is not None:
            self.log("Lambda", "notify-worker picked up the message")
            self.deliver(body)
            self.log("Lambda", "notify-worker finished")


class FakeSes:
    def __init__(self, log: Log = _quiet) -> None:
        self.sent: list[dict[str, Any]] = []
        self.log = log

    def send_email(self, **kw: Any) -> None:
        self.sent.append(kw)
        to = kw["Destination"]["ToAddresses"][0]
        self.log("SES", f"e-mail sent to {to}: {kw['Message']['Subject']['Data']}")
