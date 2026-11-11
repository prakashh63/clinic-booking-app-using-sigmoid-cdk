#!/usr/bin/env python3
"""Checks that the stack was configured correctly AND built correctly, with readable output.

1. Config input   : builds the config for the stage and checks it (names, handlers, files).
2. Output template: reads the CloudFormation template that `cdk synth` wrote to cdk.out and
                    checks that every resource exists and has its security settings.

Usage (run from the project root):
    python tools/verify_stack.py                  # stage dev, reads cdk.out
    python tools/verify_stack.py --stage prod
    python tools/verify_stack.py --synth          # runs `cdk synth` first
    python tools/verify_stack.py --out some/cdk.out

Exit code 0 means every check passed, 1 means at least one failed.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
USE_COLOR = sys.stdout.isatty() and "NO_COLOR" not in os.environ
COLORS = {"PASS": "32", "FAIL": "31", "WARN": "33", "INFO": "36", "BOLD": "1"}
RETENTION_DAYS = {"dev": 7, "staging": 30, "prod": 90}
REQUIRED_TAGS = ("Environment", "Owner", "CostCenter", "ManagedBy", "Application")
TAGGABLE = (
    "AWS::S3::Bucket",
    "AWS::Lambda::Function",
    "AWS::IAM::Role",
    "AWS::KMS::Key",
    "AWS::DynamoDB::Table",
    "AWS::SQS::Queue",
    "AWS::Logs::LogGroup",
    "AWS::CloudFront::Distribution",
    "AWS::ApiGateway::RestApi",
    "AWS::ApiGateway::Stage",
)
WILDCARD_RESOURCE_OK = {"xray:PutTraceSegments", "xray:PutTelemetryRecords"}


def paint(text: str, key: str) -> str:
    return f"\033[{COLORS[key]}m{text}\033[0m" if USE_COLOR else text


# ---------------------------------------------------------------- report printing
class Report:
    def __init__(self) -> None:
        self.counts = {"PASS": 0, "FAIL": 0, "WARN": 0}
        self.sections = 0

    def section(self, title: str) -> None:
        self.sections += 1
        print(f"\n{paint(f'{self.sections}. {title}', 'BOLD')}")

    def _line(self, tag: str, text: str, detail: str = "") -> None:
        if tag in self.counts:
            self.counts[tag] += 1
        suffix = f"   <- {detail}" if detail else ""
        print(f"  [{paint(tag, tag)}] {text}{suffix}")

    def check(self, ok: bool, text: str, detail: str = "") -> bool:
        self._line("PASS" if ok else "FAIL", text, "" if ok else detail)
        return ok

    def warn(self, text: str, detail: str = "") -> None:
        self._line("WARN", text, detail)

    def info(self, text: str) -> None:
        print(f"  [{paint('INFO', 'INFO')}] {text}")

    def table(self, headers: list[str], rows: list[list[str]], oks: list[bool]) -> None:
        """Prints an aligned table. The last column shows OK / FAIL and is counted."""
        grid = [headers + ["Result"]] + [r + ["OK" if ok else "FAIL"] for r, ok in zip(rows, oks)]
        widths = [max(len(str(row[i])) for row in grid) for i in range(len(headers) + 1)]
        for index, row in enumerate(grid):
            cells = [str(c).ljust(widths[i]) for i, c in enumerate(row[:-1])]
            result = row[-1]
            if index == 0:
                print("      " + "  ".join(cells + [result]))
                print("      " + "  ".join("-" * w for w in widths))
                continue
            ok = oks[index - 1]
            self.counts["PASS" if ok else "FAIL"] += 1
            print("      " + "  ".join(cells + [paint(result, "PASS" if ok else "FAIL")]))


# ---------------------------------------------------------------- template helpers
def of_type(template: dict[str, Any], kind: str) -> dict[str, dict[str, Any]]:
    return {k: v for k, v in template.get("Resources", {}).items() if v.get("Type") == kind}


def props(resource: dict[str, Any]) -> dict[str, Any]:
    return resource.get("Properties", {})


def as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def tags_of(resource: dict[str, Any]) -> dict[str, Any]:
    return {t.get("Key"): t.get("Value") for t in as_list(props(resource).get("Tags"))}


def statements(policy: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in as_list(policy.get("Statement")) if isinstance(s, dict)]


def upper_snake(text: str) -> str:
    return "_".join(w.upper() for w in re.split(r"[^A-Za-z0-9]+", text) if w)


def pascal(text: str) -> str:
    return "".join(w.capitalize() for w in re.split(r"[^A-Za-z0-9]+", text) if w)


def runtime_id(name: str) -> str | None:
    """PYTHON_3_14 -> python3.14, NODEJS_22_X -> nodejs22.x ... None when unknown."""
    for pattern, fmt in (
        (r"(PYTHON|RUBY)_(\d+)_(\d+)", lambda m: f"{m[1].lower()}{m[2]}.{m[3]}"),
        (r"NODEJS_(\d+)_X", lambda m: f"nodejs{m[1]}.x"),
        (r"JAVA_(\d+)", lambda m: f"java{m[1]}"),
        (r"DOTNET_(\d+)", lambda m: f"dotnet{m[1]}"),
    ):
        match = re.fullmatch(pattern, name)
        if match:
            return fmt(match)
    return {"PROVIDED_AL2023": "provided.al2023", "PROVIDED_AL2": "provided.al2"}.get(name)


def api_routes(template: dict[str, Any]) -> set[tuple[str, str]]:
    """(METHOD, /path) pairs rebuilt from the API Gateway resource tree."""
    resources = of_type(template, "AWS::ApiGateway::Resource")

    def path_of(logical_id: str | None) -> str:
        parts: list[str] = []
        while logical_id in resources:
            p = props(resources[logical_id])
            parts.append(p["PathPart"])
            parent = p.get("ParentId")
            logical_id = parent.get("Ref") if isinstance(parent, dict) else None
        return "/" + "/".join(reversed(parts))

    routes = set()
    for method in of_type(template, "AWS::ApiGateway::Method").values():
        p = props(method)
        if p["HttpMethod"] == "OPTIONS":
            continue
        resource_id = p.get("ResourceId")
        logical_id = resource_id.get("Ref") if isinstance(resource_id, dict) else None
        routes.add((p["HttpMethod"], path_of(logical_id)))
    return routes


# ---------------------------------------------------------------- 1. config input
def load_config(stage: str) -> tuple[Any, str]:
    sys.path.insert(0, str(ROOT / "bin"))
    try:
        from app_config import build_config

        return build_config(stage), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"{type(exc).__name__}: {exc}"


def check_config(r: Report, config: Any, error: str) -> None:
    r.section("Config input (bin/app_config.py)")
    if config is None:
        r.check(False, "Config loads and passes validation", "see the message below")
        for line in error.splitlines()[:12]:
            print(f"        {line}")
        r.warn("Config not loaded, so counts and values cannot be compared with the template")
        return
    r.check(True, "Config loads and passes validation (pydantic)")
    routes = sum(len(a.routes) for a in config.api_gateways)
    r.info(
        f"app '{config.app_name}', stage '{config.environment}': "
        f"{len(config.lambdas)} lambdas, {len(config.tables)} tables, {len(config.queues)} queues, "
        f"{len(config.event_rules)} schedules, {len(config.api_gateways)} API(s) with {routes} routes, "
        f"{1 + len(config.extra_buckets)} bucket(s)"
    )
    for fn in config.lambdas:
        module, _, func = fn.handler.rpartition(".")
        if not fn.runtime.startswith("PYTHON"):
            continue  # code of other languages is checked by its own build
        source = ROOT / fn.entry_point_dir / f"{module}.py"
        found = source.is_file() and f"def {func}(" in source.read_text(encoding="utf-8")
        r.check(found, f"Handler of {fn.function_name}: {fn.handler}", f"{source} has no function '{func}'")
    senders = {f.send_email_from for f in config.lambdas if f.send_email_from}
    for sender in senders:
        if sender.endswith("@example.com"):
            r.warn(f"Sender e-mail is the placeholder '{sender}'", "set the SENDER_EMAIL variable before a real deploy")


# ---------------------------------------------------------------- 2. output template
def find_template(out_dir: Path, config: Any, stage: str) -> Path | None:
    candidates = sorted(out_dir.glob("*.template.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if config is not None:
        wanted = f"{pascal(config.app_name)}{pascal(stage)}Stack.template.json"
        for path in candidates:
            if path.name == wanted:
                return path
    return candidates[0] if candidates else None


def check_counts(r: Report, template: dict[str, Any], config: Any) -> None:
    r.section("Resources created (expected from config vs found in cdk.out)")
    queues_with_consumer = sum(1 for q in config.queues if q.consumer_lambda)
    expected = [
        ("AWS::Lambda::Function", len(config.lambdas)),
        ("AWS::DynamoDB::Table", len(config.tables)),
        ("AWS::SQS::Queue", 2 * len(config.queues)),
        ("AWS::S3::Bucket", 1 + len(config.extra_buckets)),
        ("AWS::CloudFront::Distribution", 1),
        ("AWS::ApiGateway::RestApi", len(config.api_gateways)),
        ("AWS::Events::Rule", len(config.event_rules)),
        ("AWS::Lambda::EventSourceMapping", queues_with_consumer),
        ("AWS::KMS::Key", 1),
        ("AWS::Logs::LogGroup", len(config.lambdas) + len(config.api_gateways)),
    ]
    rows, oks = [], []
    for kind, want in expected:
        found = len(of_type(template, kind))
        rows.append([kind, str(want), str(found)])
        oks.append(found == want)
    r.table(["Resource type", "Expected", "Found"], rows, oks)


def check_buckets(r: Report, template: dict[str, Any]) -> None:
    r.section("S3 buckets")
    policies = " ".join(json.dumps(p) for p in of_type(template, "AWS::S3::BucketPolicy").values())
    for logical_id, bucket in of_type(template, "AWS::S3::Bucket").items():
        p = props(bucket)
        block = p.get("PublicAccessBlockConfiguration", {})
        name = p.get("BucketName", logical_id)
        r.check(
            all(block.get(k) is True for k in ("BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets")),
            f"{name}: all public access blocked",
            "PublicAccessBlockConfiguration is incomplete",
        )
        r.check("BucketEncryption" in p, f"{name}: encrypted at rest", "no BucketEncryption")
    r.check("aws:SecureTransport" in policies, "Bucket policies refuse plain HTTP", "no aws:SecureTransport rule")


def check_tables(r: Report, template: dict[str, Any], config: Any, stage: str) -> None:
    tables = of_type(template, "AWS::DynamoDB::Table")
    if not tables:
        return
    r.section("DynamoDB tables")
    expected = {f"{config.app_name}-{stage}-{t.table_name}": t for t in config.tables} if config else {}
    for logical_id, table in tables.items():
        p = props(table)
        name = p.get("TableName", logical_id)
        r.check(p.get("BillingMode") == "PAY_PER_REQUEST", f"{name}: on-demand billing", f"BillingMode is {p.get('BillingMode')}")
        r.check(p.get("SSESpecification", {}).get("SSEEnabled") is True, f"{name}: encrypted at rest", "SSE is off")
        pitr = p.get("PointInTimeRecoverySpecification", {}).get("PointInTimeRecoveryEnabled")
        r.check(pitr is True, f"{name}: point-in-time recovery on", "PITR is off")
        want = expected.get(name)
        if want is not None:
            keys = {k["KeyType"]: k["AttributeName"] for k in p.get("KeySchema", [])}
            r.check(
                keys.get("HASH") == want.partition_key and keys.get("RANGE") == want.sort_key,
                f"{name}: keys {want.partition_key} / {want.sort_key or '-'}",
                f"template has {keys}",
            )
            indexes = {i["IndexName"] for i in p.get("GlobalSecondaryIndexes", [])}
            r.check(indexes == {i.index_name for i in want.indexes}, f"{name}: indexes {sorted(indexes) or '-'}", f"template has {sorted(indexes)}")
            ttl = p.get("TimeToLiveSpecification", {}).get("AttributeName")
            r.check(ttl == want.ttl_attribute, f"{name}: expiry attribute {ttl or '-'}", f"config says {want.ttl_attribute}")
        elif config is not None:
            r.check(False, f"{name}: is in the config", "table not described in app_config.py")
        if stage == "prod":
            kept = table.get("DeletionPolicy") == "Retain" and p.get("DeletionProtectionEnabled") is True
            r.check(kept, f"{name}: retained and deletion-protected in prod", "prod data could be deleted")


def check_lambdas(r: Report, template: dict[str, Any], config: Any, stage: str) -> None:
    r.section("Lambda functions")
    functions = {props(f).get("FunctionName"): props(f) for f in of_type(template, "AWS::Lambda::Function").values()}
    for fn in config.lambdas:
        name = f"{config.app_name}-{stage}-{fn.function_name}"
        p = functions.get(name)
        if p is None:
            r.check(False, f"{fn.function_name}", f"function '{name}' is not in the template")
            continue
        issues = []
        want_runtime = runtime_id(fn.runtime)
        if want_runtime is not None and p.get("Runtime") != want_runtime:
            issues.append(f"runtime {p.get('Runtime')} != {want_runtime}")
        if p.get("Handler") != fn.handler:
            issues.append(f"handler {p.get('Handler')} != {fn.handler}")
        if p.get("MemorySize") != fn.memory_mb:
            issues.append(f"memory {p.get('MemorySize')} != {fn.memory_mb}")
        if p.get("Timeout") != fn.timeout_seconds:
            issues.append(f"timeout {p.get('Timeout')} != {fn.timeout_seconds}")
        env = p.get("Environment", {}).get("Variables", {})
        wanted_env = ["STAGE"]
        wanted_env += [f"QUEUE_URL_{upper_snake(q)}" for q in fn.send_to_queues]
        wanted_env += [f"BUCKET_NAME_{upper_snake(b)}" for b in fn.read_write_buckets]
        wanted_env += [f"TABLE_NAME_{upper_snake(t)}" for t in fn.read_write_tables]
        if fn.send_email_from:
            wanted_env.append("SENDER_EMAIL")
            if env.get("SENDER_EMAIL") != fn.send_email_from:
                issues.append("SENDER_EMAIL differs from the config")
        missing = [k for k in wanted_env if k not in env]
        if missing:
            issues.append(f"missing environment variables {missing}")
        if "LoggingConfig" not in p:
            issues.append("no encrypted log group attached")
        handler_text = str(p.get("Handler")).ljust(24)
        summary = f"{fn.function_name:<16} {p.get('Runtime')}  {handler_text}{p.get('MemorySize')} MB  {p.get('Timeout')} s"
        r.check(not issues, summary, "; ".join(issues))


def check_logs(r: Report, template: dict[str, Any], stage: str) -> None:
    r.section("CloudWatch log groups")
    days = RETENTION_DAYS[stage]
    for logical_id, group in of_type(template, "AWS::Logs::LogGroup").items():
        p = props(group)
        name = p.get("LogGroupName", logical_id)
        ok = "KmsKeyId" in p and p.get("RetentionInDays") == days
        r.check(ok, f"{name}: KMS-encrypted, kept {days} days", f"KmsKeyId={'KmsKeyId' in p}, retention={p.get('RetentionInDays')}")


def check_queues(r: Report, template: dict[str, Any], config: Any) -> None:
    queues = of_type(template, "AWS::SQS::Queue")
    if not queues:
        return
    r.section("SQS queues")
    for logical_id, queue in queues.items():
        p = props(queue)
        name = p.get("QueueName", logical_id)
        r.check(p.get("SqsManagedSseEnabled") is True or "KmsMasterKeyId" in p, f"{name}: encrypted", "no encryption")
    with_dlq = sum(1 for q in queues.values() if "RedrivePolicy" in props(q))
    want = len(config.queues) if config else with_dlq
    r.check(with_dlq == want and with_dlq > 0, f"{with_dlq} queue(s) send failed messages to a dead-letter queue", f"expected {want}")
    secure = sum(1 for p in of_type(template, "AWS::SQS::QueuePolicy").values() if "aws:SecureTransport" in json.dumps(p))
    r.check(secure == len(queues), "Every queue refuses plain HTTP", f"{secure} of {len(queues)} have the rule")


def check_api_and_rules(r: Report, template: dict[str, Any], config: Any) -> None:
    r.section("API Gateway routes and throttling")
    expected = {(route.http_method, "/" + route.path) for api in config.api_gateways for route in api.routes}
    found = api_routes(template)
    rows, oks = [], []
    for method, path in sorted(expected | found):
        state = "in config and template" if (method, path) in expected & found else (
            "missing from template" if (method, path) in expected else "not in config"
        )
        rows.append([method, path, state])
        oks.append((method, path) in expected & found)
    r.table(["Method", "Path", "Status"], rows, oks)
    stages = sorted(
        (props(s).get("StageName"), props(s)["MethodSettings"][0].get("ThrottlingRateLimit"), props(s)["MethodSettings"][0].get("ThrottlingBurstLimit"))
        for s in of_type(template, "AWS::ApiGateway::Stage").values()
        if props(s).get("MethodSettings")
    )
    want = sorted((a.stage_name, a.rate_limit, a.burst_limit) for a in config.api_gateways)
    r.check(stages == want, f"Throttling (stage, rate, burst): {stages}", f"config says {want}")
    r.check(len(of_type(template, "AWS::ApiGateway::RequestValidator")) >= len(config.api_gateways), "Request validation is on")
    schedules = sorted((e.rule_name, e.schedule_expression) for e in config.event_rules if e.schedule_expression)
    rules = sorted(
        (str(props(x).get("Name", "")).split(f"{config.environment}-", 1)[-1], props(x).get("ScheduleExpression"))
        for x in of_type(template, "AWS::Events::Rule").values()
        if props(x).get("ScheduleExpression")
    )
    r.check(rules == schedules, f"Schedules: {rules or '-'}", f"config says {schedules}")


def check_cloudfront(r: Report, template: dict[str, Any], config: Any) -> None:
    r.section("CloudFront")
    for dist in of_type(template, "AWS::CloudFront::Distribution").values():
        cfg = props(dist)["DistributionConfig"]
        behaviors = [cfg["DefaultCacheBehavior"], *cfg.get("CacheBehaviors", [])]
        r.check(all(b.get("ViewerProtocolPolicy") == "redirect-to-https" for b in behaviors), "Every behaviour redirects HTTP to HTTPS", "a behaviour allows plain HTTP")
        if config is not None and config.cloudfront_api is not None:
            patterns = [b.get("PathPattern") for b in cfg.get("CacheBehaviors", [])]
            r.check("api/*" in patterns, "api/* is routed to API Gateway", f"patterns are {patterns}")
    r.check(len(of_type(template, "AWS::CloudFront::OriginAccessControl")) >= 1, "S3 is reached only through Origin Access Control", "no OAC found")


def check_iam(r: Report, template: dict[str, Any], config: Any) -> None:
    r.section("IAM")
    problems = []
    for logical_id, policy in of_type(template, "AWS::IAM::Policy").items():
        for st in statements(props(policy).get("PolicyDocument", {})):
            if st.get("Effect") != "Allow":
                continue
            actions = [a for a in as_list(st.get("Action")) if isinstance(a, str)]
            bad = [a for a in actions if a == "*" or a.endswith(":*")]
            if bad:
                problems.append(f"{logical_id}: action {bad}")
            if "*" in as_list(st.get("Resource")):
                loose = [a for a in actions if a not in WILDCARD_RESOURCE_OK]
                if loose:
                    problems.append(f"{logical_id}: {loose} on Resource '*'")
    r.check(not problems, "No bare '*' action or resource in any policy", "; ".join(problems))
    open_roles = [k for k, v in of_type(template, "AWS::IAM::Role").items() if '"Principal": {"AWS": "*"}' in json.dumps(props(v).get("AssumeRolePolicyDocument", {}))]
    r.check(not open_roles, "No role can be assumed by any principal", f"open roles: {open_roles}")
    if config is None:
        return
    role_ids = {props(f).get("Role", {}).get("Fn::GetAtt", [None])[0] for f in of_type(template, "AWS::Lambda::Function").values()}
    roles = of_type(template, "AWS::IAM::Role")
    managed = [i for i in role_ids if i in roles and props(roles[i]).get("ManagedPolicyArns")]
    r.check(not managed, f"Each of the {len(role_ids)} Lambda roles has no AWS-managed policy", f"managed policies on {managed}")
    senders = [f.send_email_from for f in config.lambdas if f.send_email_from]
    if senders:
        mail = [st for p in of_type(template, "AWS::IAM::Policy").values() for st in statements(props(p).get("PolicyDocument", {})) if "ses:SendEmail" in as_list(st.get("Action"))]
        scoped = all("*" not in as_list(st.get("Resource")) and any(s in json.dumps(st) for s in senders) for st in mail)
        r.check(len(mail) == len(senders) and scoped, f"{len(senders)} Lambda(s) may send e-mail, only as the verified sender", f"found {len(mail)} statement(s)")


def check_tags(r: Report, template: dict[str, Any], config: Any, stage: str) -> None:
    r.section("Standard tags")
    wanted = {"Environment": stage}
    if config is not None:
        wanted |= {"Owner": config.owner_team, "CostCenter": config.cost_center, "Application": config.app_name}
    wanted["ManagedBy"] = "sigmoid-cdk"
    bad, total = [], 0
    for kind in TAGGABLE:
        for logical_id, resource in of_type(template, kind).items():
            total += 1
            tags = tags_of(resource)
            wrong = [k for k in REQUIRED_TAGS if k not in tags] + [k for k, v in wanted.items() if k in tags and tags[k] != v]
            if wrong:
                bad.append(f"{kind.split('::')[-1]} {logical_id}: {sorted(set(wrong))}")
    r.check(not bad, f"All {total} taggable resources carry the standard tags", "; ".join(bad[:4]))
    r.check("SiteUrl" in template.get("Outputs", {}), "Stack output SiteUrl exists", "no SiteUrl output")


# ---------------------------------------------------------------- main
def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Verify the config and the synthesized stack.")
    parser.add_argument("--stage", default=os.environ.get("STAGE", "dev"), choices=["dev", "staging", "prod"])
    parser.add_argument("--out", default="cdk.out", help="folder written by cdk synth")
    parser.add_argument("--synth", action="store_true", help="run `cdk synth` first")
    args = parser.parse_args(argv)
    os.environ["STAGE"] = args.stage
    os.chdir(ROOT)

    if args.synth:
        print(f"Running: cdk synth --quiet  (STAGE={args.stage})")
        result = subprocess.run(["cdk", "synth", "--quiet"], capture_output=True, text=True)
        if result.returncode != 0:
            print(result.stdout + result.stderr)
            print(paint("cdk synth failed, so there is no template to verify.", "FAIL"))
            return 1

    r = Report()
    title = f" Stack verification: stage '{args.stage}' "
    print("=" * 70 + f"\n{paint(title, 'BOLD')}\n" + "=" * 70)

    config, error = load_config(args.stage)
    check_config(r, config, error)

    out_dir = Path(args.out)
    path = find_template(out_dir, config, args.stage) if out_dir.is_dir() else None
    r.section("Output template")
    if path is None:
        r.check(False, f"Template found in {out_dir}/", "run `cdk synth` (or use --synth) first")
        return finish(r)
    template = json.loads(path.read_text(encoding="utf-8"))
    r.check(True, f"Template read: {path.name} ({len(template.get('Resources', {}))} resources)")

    if config is not None:
        check_counts(r, template, config)
        check_lambdas(r, template, config, args.stage)
    check_buckets(r, template)
    check_tables(r, template, config, args.stage)
    check_logs(r, template, args.stage)
    check_queues(r, template, config)
    if config is not None:
        check_api_and_rules(r, template, config)
    check_cloudfront(r, template, config)
    check_iam(r, template, config)
    check_tags(r, template, config, args.stage)
    return finish(r)


def finish(r: Report) -> int:
    c = r.counts
    print("\n" + "=" * 70)
    print(f" {paint('PASS', 'PASS')} {c['PASS']}    {paint('FAIL', 'FAIL')} {c['FAIL']}    {paint('WARN', 'WARN')} {c['WARN']}")
    if c["FAIL"]:
        print(paint(f" RESULT: {c['FAIL']} CHECK(S) FAILED. Read the lines marked [FAIL] above.", "FAIL"))
    else:
        print(paint(" RESULT: ALL CHECKS PASSED. The stack matches the config and the security rules.", "PASS"))
    print("=" * 70)
    return 1 if c["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
