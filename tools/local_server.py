"""Run CareSlot locally, saving bookings to DynamoDB Local by default.

From the application root:
    docker compose up -d
    python tools/local_server.py

Website:   http://127.0.0.1:8000
Inspector: http://127.0.0.1:8000/debug
Database:  http://127.0.0.1:8001 (Docker, NOT the website)

The real src/backend/clinic.py Lambda handlers run here. DynamoDB uses the
local Docker database; SQS and SES remain simulated for development.

For the existing in-memory unit tests only:
    USE_DYNAMODB_LOCAL=0 python -m pytest tests/test_local_server.py

This script never connects to a real AWS DynamoDB endpoint.
"""

import importlib.util
import json
import os
import sys
import time
import types
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from fake_aws import FakeDdb, FakeSes, FakeSqs

ROOT = Path(__file__).resolve().parent.parent
JSON = "application/json"
IST = timezone(timedelta(hours=5, minutes=30))

DASHBOARD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CareSlot | Local developer inspector</title>
<style>
body{font:14px/1.5 ui-monospace,Menlo,Consolas,monospace;max-width:1100px;margin:1.2rem auto;padding:0 1rem;background:#f2f7f7;color:#12303a}
h3{margin:1.2rem 0 .4rem}button{font:inherit;padding:7px 12px;margin:0 6px 6px 0;border:1px solid #0f766e;background:#0f766e;color:#fff;border-radius:7px;cursor:pointer}
button.alt{background:#fff;color:#0f766e}button:disabled{cursor:not-allowed;opacity:.45}
pre{background:#fff;border:1px solid #d8e4e6;border-radius:8px;padding:10px;max-height:340px;overflow:auto;margin:0;white-space:pre-wrap}
.table-scroll{overflow-x:auto}table{border-collapse:collapse;width:100%;background:#fff;border:1px solid #d8e4e6}th,td{text-align:left;padding:6px 10px;border-bottom:1px solid #e6eef0;white-space:nowrap}
.mail{background:#fff;border:1px solid #d8e4e6;border-radius:8px;padding:8px 12px;margin-bottom:8px}.mail b{display:block}
.empty{color:#5d7480;padding:8px}.hint{color:#526c73}
</style></head><body>
<h2>CareSlot — Local developer inspector</h2>
<p>Website: <a href="/" target="_blank" rel="noopener">localhost:8000</a>.
Database: <strong id="dbmode">checking…</strong>. Only available on this computer.</p>
<button onclick="act('/debug/seed')">Add a demo booking on a free weekday</button>
<button onclick="act('/debug/run-reminder')">Run daily reminder simulation</button>
<button id="reset" class="alt" onclick="if(confirm('Clear only the IN-MEMORY demo?'))act('/debug/reset')">Reset in-memory demo</button>
<p id="note" class="hint"></p>
<h3>Timeline</h3><pre id="timeline"></pre>
<h3>DynamoDB bookings (local data)</h3><div id="items" class="table-scroll"></div>
<h3>Email previews (simulated SES — no real email sent)</h3><div id="mails"></div>
<script>
async function act(path){
  try {
    const r=await fetch(path,{method:'POST'});
    if(!r.ok){const d=await r.json();alert(d.error||('HTTP '+r.status));}
  } catch(e){alert('Request failed: '+e.message);}
  await refresh();
}
function cell(tr,text,tag){const c=document.createElement(tag||'td');c.textContent=String(text??'');tr.append(c);}
async function refresh(){
 try {
  const r=await fetch('/debug/state',{cache:'no-store'});
  if(!r.ok)throw new Error('HTTP '+r.status);
  const s=await r.json();
  document.getElementById('dbmode').textContent=s.storage;
  document.getElementById('reset').disabled=s.storage==='DynamoDB Local (Docker)';
  document.getElementById('note').textContent=s.storage==='DynamoDB Local (Docker)'
    ? 'Persistent appointments are read from Docker DynamoDB. Reset is disabled to protect stored data. Booking details are private; keep this inspector on 127.0.0.1.'
    : 'In-memory test mode only. Restarting the server clears these appointments.';
  const tl=document.getElementById('timeline');tl.textContent=s.timeline.join('\\n')||'(nothing yet)';tl.scrollTop=tl.scrollHeight;
  const items=document.getElementById('items');items.replaceChildren();
  if(!s.items.length){const e=document.createElement('div');e.className='empty';e.textContent='(no stored appointments yet)';items.append(e);}
  else{const t=document.createElement('table'),h=document.createElement('tr');
    const keys=['booking_date','slot_time','booking_code','patient_name','patient_email','service','confirm_sent','reminder_sent'];
    keys.forEach(k=>cell(h,k,'th'));t.append(h);
    s.items.forEach(i=>{const row=document.createElement('tr');keys.forEach(k=>cell(row,i[k]));t.append(row);});
    items.append(t);
  }
  const mails=document.getElementById('mails');mails.replaceChildren();
  if(!s.emails.length){const e=document.createElement('div');e.className='empty';e.textContent='(none yet)';mails.append(e);}
  s.emails.forEach(m=>{const d=document.createElement('div');d.className='mail';
    const b=document.createElement('b');b.textContent='To: '+m.to+' | '+m.subject;
    const p=document.createElement('div');p.style.whiteSpace='pre-wrap';p.textContent=m.body;d.append(b,p);mails.append(d);});
 }catch(e){document.getElementById('timeline').textContent='Inspector error: '+e.message;}
}
refresh();setInterval(refresh,2000);
</script></body></html>"""


class LocalDynamoDB:
    """Thin wrapper around the REAL DynamoDB Local low-level boto3 client.

    Uses a fixed localhost endpoint and fake credentials; it cannot silently
    use an AWS profile or production database.
    """

    table_name = "ClinicBookingsLocal"

    def __init__(self, log):
        try:
            import boto3
            from botocore.config import Config
            from botocore.exceptions import (
                ClientError,
                ConnectTimeoutError,
                ConnectionClosedError,
                EndpointConnectionError,
                ReadTimeoutError,
            )
        except ImportError as exc:
            raise RuntimeError(
                "DynamoDB Local needs boto3. Install it with: python -m pip install boto3"
            ) from exc

        endpoint = os.environ.get("DYNAMODB_LOCAL_ENDPOINT", "http://127.0.0.1:8001")
        parsed = urlparse(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "localhost"}
            or not parsed.port
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("DYNAMODB_LOCAL_ENDPOINT must be http://127.0.0.1:<port>")

        self.log = log
        print(f"Connecting to DynamoDB Local at {endpoint} ...", flush=True)
        self.client = boto3.client(
            "dynamodb",
            region_name="ap-south-1",
            endpoint_url=endpoint,
            aws_access_key_id="local",
            aws_secret_access_key="local",
            config=Config(
                connect_timeout=2,
                read_timeout=3,
                retries={"total_max_attempts": 1},
            ),
        )
        try:
            self._ensure_table(ClientError)
        except (EndpointConnectionError, ConnectTimeoutError, ReadTimeoutError, ConnectionClosedError) as exc:
            raise RuntimeError(
                "DynamoDB Local is not responding at " + endpoint + ". "
                "Run 'docker compose ps' and 'docker compose logs --tail=40 dynamodb-local' "
                "to check the container, and confirm the port mapping is 8001:8000. "
                "No records were changed."
            ) from exc
        self.log("DynamoDB Local", f"connected to {self.table_name} at {endpoint}")

    def _ensure_table(self, client_error_type):
        try:
            table = self.client.describe_table(TableName=self.table_name)["Table"]
        except client_error_type as exc:
            if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
                raise
            self.client.create_table(
                TableName=self.table_name,
                BillingMode="PAY_PER_REQUEST",
                AttributeDefinitions=[
                    {"AttributeName": "booking_date", "AttributeType": "S"},
                    {"AttributeName": "slot_time", "AttributeType": "S"},
                    {"AttributeName": "booking_code", "AttributeType": "S"},
                ],
                KeySchema=[
                    {"AttributeName": "booking_date", "KeyType": "HASH"},
                    {"AttributeName": "slot_time", "KeyType": "RANGE"},
                ],
                GlobalSecondaryIndexes=[{
                    "IndexName": "code-index",
                    "KeySchema": [{"AttributeName": "booking_code", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }],
            )
            self.client.get_waiter("table_exists").wait(
                TableName=self.table_name,
                WaiterConfig={"Delay": 1, "MaxAttempts": 30},
            )
            table = self.client.describe_table(TableName=self.table_name)["Table"]

        expected_keys = [
            {"AttributeName": "booking_date", "KeyType": "HASH"},
            {"AttributeName": "slot_time", "KeyType": "RANGE"},
        ]
        if table.get("KeySchema") != expected_keys:
            raise RuntimeError(
                f"Existing {self.table_name} has different key columns. "
                "No table or booking was modified."
            )
        index = next(
            (i for i in table.get("GlobalSecondaryIndexes", []) if i["IndexName"] == "code-index"),
            None,
        )
        if index is None or index.get("KeySchema") != [
            {"AttributeName": "booking_code", "KeyType": "HASH"}
        ]:
            raise RuntimeError(
                f"Existing {self.table_name} is missing the booking-code index. "
                "No table or booking was modified."
            )
        # Wait for the code-index to become queryable on first creation.
        deadline = time.monotonic() + 30
        while index.get("IndexStatus") not in (None, "ACTIVE"):
            if time.monotonic() >= deadline:
                raise RuntimeError("DynamoDB Local code-index did not become ACTIVE")
            time.sleep(1)
            table = self.client.describe_table(TableName=self.table_name)["Table"]
            index = next(
                (i for i in table.get("GlobalSecondaryIndexes", []) if i["IndexName"] == "code-index"),
                {},
            )

    def snapshot(self, max_records=250):
        """Read up to max_records stored bookings without changing the table."""
        collected = []
        next_key = None
        while len(collected) < max_records:
            opts = {"TableName": self.table_name, "Limit": min(100, max_records - len(collected))}
            if next_key:
                opts["ExclusiveStartKey"] = next_key
            page = self.client.scan(**opts)
            collected.extend(page.get("Items", []))
            next_key = page.get("LastEvaluatedKey")
            if not next_key:
                break
        return collected

    def __getattr__(self, name):
        method = getattr(self.client, name)
        if name not in {"put_item", "get_item", "query", "delete_item", "update_item"}:
            return method

        def tracked(**kwargs):
            result = method(**kwargs)
            self.log("DynamoDB Local", f"{name} on {self.table_name}")
            return result

        return tracked


def load_backend(clients: dict) -> types.ModuleType:
    """Load the REAL Lambda handlers with the selected local AWS clients."""
    previous = sys.modules.get("boto3")
    sys.modules["boto3"] = types.SimpleNamespace(client=lambda name: clients[name])
    try:
        spec = importlib.util.spec_from_file_location(
            "clinic", ROOT / "src" / "backend" / "clinic.py"
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("Cannot load src/backend/clinic.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            sys.modules.pop("boto3", None)
        else:
            sys.modules["boto3"] = previous
    return module


class LocalApp:
    """Local CloudFront/API Gateway simulation invoking clinic.py directly."""

    def __init__(self) -> None:
        self.timeline: list[str] = []
        self.persistent_db = os.environ.get("USE_DYNAMODB_LOCAL", "1") == "1"
        self.ddb = LocalDynamoDB(self.log) if self.persistent_db else FakeDdb(self.log)
        self.ses = FakeSes(self.log)
        self.sqs = FakeSqs(self.log, deliver=self._deliver)
        os.environ.update(
            TABLE_NAME_BOOKINGS=self.ddb.table_name if self.persistent_db else "bookings",
            QUEUE_URL_BOOKING_EVENTS="local-queue",
            SENDER_EMAIL="clinic@localhost",
            APP_MODE="local",
        )
        self.clinic = load_backend({"dynamodb": self.ddb, "sqs": self.sqs, "ses": self.ses})

    def log(self, service: str, message: str) -> None:
        self.timeline.append(f"{time.strftime('%H:%M:%S')}  [{service}] {message}")
        del self.timeline[:-300]

    def _deliver(self, body: str) -> None:
        self.clinic.notify_worker({"Records": [{"body": body}]}, None)

    def state(self) -> dict:
        raw = self.ddb.snapshot() if self.persistent_db else [
            item for _, item in sorted(self.ddb.items.items())
        ]
        items = [
            {key: next(iter(value.values())) for key, value in item.items()}
            for item in raw
        ]
        items.sort(key=lambda i: (i.get("booking_date", ""), i.get("slot_time", "")))
        emails = [
            {
                "to": m["Destination"]["ToAddresses"][0],
                "subject": m["Message"]["Subject"]["Data"],
                "body": m["Message"]["Body"]["Text"]["Data"],
            }
            for m in self.ses.sent
        ]
        return {
            "storage": "DynamoDB Local (Docker)" if self.persistent_db else "In-memory test mode",
            "timeline": list(self.timeline),
            "items": items,
            "emails": emails,
        }

    def _route(self, method: str, path: str):
        c = self.clinic
        parts = path.split("/")
        if method == "GET" and path == "/api/slots":
            return "list-slots", c.list_slots, None
        if method == "POST" and path == "/api/bookings":
            return "book-slot", c.book_slot, None
        if len(parts) == 4 and parts[:3] == ["", "api", "bookings"]:
            handlers = {
                "GET": ("get-booking", c.get_booking),
                "DELETE": ("cancel-booking", c.cancel_booking),
            }
            if method in handlers:
                name, handler = handlers[method]
                return name, handler, {"code": parts[3]}
        if method == "GET" and path == "/api/staff/today":
            return "staff-today", c.staff_today, None
        return None

    def _debug(self, method: str, path: str):
        if method == "GET" and path == "/debug":
            return 200, DASHBOARD, "text/html"
        if method == "GET" and path == "/debug/state":
            return 200, json.dumps(self.state()), JSON
        if method == "POST" and path == "/debug/reset":
            if self.persistent_db:
                return 403, json.dumps({"error": "Reset disabled for persistent DynamoDB Local data"}), JSON
            self.ddb.items.clear()
            self.sqs.messages.clear()
            self.ses.sent.clear()
            self.timeline.clear()
            self.log("Local", "in-memory demonstration data cleared")
            return 200, json.dumps({"reset": True}), JSON
        if method == "POST" and path == "/debug/run-reminder":
            self.log("EventBridge", "daily reminder triggered manually")
            result = self.clinic.daily_reminder({}, None)
            self.log("Lambda", f"daily-reminder finished: {result}")
            return 200, json.dumps(result), JSON
        if method == "POST" and path == "/debug/seed":
            day_obj = (datetime.now(IST) + timedelta(days=1)).date()
            for _ in range(7):
                if day_obj.weekday() < 5 and day_obj.isoformat() not in self.clinic.HOLIDAYS:
                    break
                day_obj += timedelta(days=1)
            day = day_obj.isoformat()
            slots = json.loads(
                self.clinic.list_slots({"queryStringParameters": {"date": day}}, None)["body"]
            )["slots"]
            free = next((s["time"] for s in slots if s["free"]), None)
            if free is None:
                return 409, json.dumps({"error": "next open day is fully booked"}), JSON
            body = {
                "date": day, "time": free, "service": "checkup",
                "name": "Demo Patient", "email": "demo.patient@example.com",
            }
            self.log("Local", f"adding demo booking at {day} {free}")
            result = self.clinic.book_slot({"body": json.dumps(body)}, None)
            return result["statusCode"], result["body"], JSON
        return None

    def handle(self, method: str, raw_path: str, body: str | None):
        url = urlparse(raw_path)
        path = url.path.rstrip("/") or "/"
        debug = self._debug(method, path) if path.startswith("/debug") else None
        if debug is not None:
            return debug
        if path.startswith("/api/"):
            route = self._route(method, path)
            if route is None:
                return 404, json.dumps({"error": "no such route"}), JSON
            name, handler, path_params = route
            query = {k: v[0] for k, v in parse_qs(url.query).items()} or None
            self.log("CloudFront", f"{method} {path} -> API Gateway (local simulation)")
            self.log("API Gateway", f"route matched -> Lambda {name}")
            event = {
                "httpMethod": method, "pathParameters": path_params,
                "queryStringParameters": query, "body": body,
            }
            result = handler(event, None)
            self.log("Lambda", f"{name} returned status {result['statusCode']}")
            return result["statusCode"], result["body"], JSON
        static_files = {
            "/": ("index.html", "text/html"),
            "/index.html": ("index.html", "text/html"),
            "/assets/styles.css": ("assets/styles.css", "text/css"),
            "/assets/app.js": ("assets/app.js", "application/javascript"),
            "/favicon.svg": ("favicon.svg", "image/svg+xml"),
        }
        resource = static_files.get(path)
        if resource is None or method != "GET":
            return 404, "Not found", "text/plain"
        filename, media_type = resource
        self.log("Website", f"{method} {path} -> local frontend {filename}")
        page = (ROOT / "src" / "frontend" / filename).read_text(encoding="utf-8")
        return 200, page, media_type


def make_server(port: int, host: str = "127.0.0.1") -> HTTPServer:
    if host not in ("127.0.0.1", "localhost"):
        raise ValueError("Local developer inspector must stay on localhost")
    app = LocalApp()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def _go(self, method: str) -> None:
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > 8192:
                    self.send_error(413, "Request is too large")
                    return
                body = self.rfile.read(length).decode("utf-8") if length else None
                status, content, content_type = app.handle(method, self.path, body)
            except Exception as exc:
                app.log("Server error", f"{type(exc).__name__}: {exc}")
                status, content, content_type = (
                    503,
                    json.dumps({"error": "Local service unavailable. Check Docker and the server terminal."}),
                    JSON,
                )
            payload = content.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            self._go("GET")

        def do_POST(self) -> None:
            self._go("POST")

        def do_DELETE(self) -> None:
            self._go("DELETE")

    return HTTPServer((host, port), Handler)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    try:
        server = make_server(port)
    except Exception as exc:
        raise SystemExit(f"Startup failed: {exc}") from exc
    print(f"App:        http://127.0.0.1:{port}")
    print(f"Debug:      http://127.0.0.1:{port}/debug")
    print("Storage:    DynamoDB Local (Docker) by default")
    server.serve_forever()
