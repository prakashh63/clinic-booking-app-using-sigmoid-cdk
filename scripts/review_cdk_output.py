#!/usr/bin/env python3
"""Readable, fail-closed review: application inputs vs CDK's generated template.

Usage:
    python scripts/review_cdk_output.py --synth
    python scripts/review_cdk_output.py --stage prod

This reads infrastructure *definitions*, not provisioned AWS resources.
"""

import argparse
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

# Direct execution (python scripts/review_cdk_output.py) starts with scripts/ on
# sys.path. Add the project root BEFORE importing project-local modules.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "bin") not in sys.path:
    sys.path.insert(0, str(ROOT / "bin"))

from tools.verify_stack import runtime_id

PURPOSES = {
    "AWS::S3::Bucket": "Private website storage",
    "AWS::CloudFront::Distribution": "HTTPS website and API routing",
    "AWS::CloudFront::OriginAccessControl": "Private access to S3",
    "AWS::ApiGateway::RestApi": "HTTP API entry point",
    "AWS::ApiGateway::Method": "HTTP endpoint integration",
    "AWS::Lambda::Function": "Backend execution / CDK helpers",
    "AWS::DynamoDB::Table": "Appointment database",
    "AWS::SQS::Queue": "Notification queue and DLQ",
    "AWS::Lambda::EventSourceMapping": "Queue-to-Lambda connection",
    "AWS::Events::Rule": "Scheduled reminders",
    "AWS::IAM::Role": "Service identity",
    "AWS::IAM::Policy": "Resource permissions",
    "AWS::KMS::Key": "Log encryption",
    "AWS::Logs::LogGroup": "Application/API logs",
}


def kind(resources, name):
    return {key: value for key, value in resources.items() if value.get("Type") == name}


def properties(resource):
    return resource.get("Properties", {})


def get_reference_string(value):
    """Represent intrinsic CloudFormation refs without resolving account/region values."""
    return json.dumps(value, sort_keys=True)


def paths(resources):
    nodes = kind(resources, "AWS::ApiGateway::Resource")

    def resolve(resource_id):
        names = []
        while resource_id in nodes:
            value = properties(nodes[resource_id])
            names.append(value.get("PathPart", "?"))
            parent = value.get("ParentId", {})
            resource_id = parent.get("Ref") if isinstance(parent, dict) else None
        return "/" + "/".join(reversed(names))

    found = {}
    for item in kind(resources, "AWS::ApiGateway::Method").values():
        p = properties(item)
        if p.get("HttpMethod") == "OPTIONS":
            continue
        ref = p.get("ResourceId", {})
        found[(p.get("HttpMethod"), resolve(ref.get("Ref") if isinstance(ref, dict) else None))] = p
    return found


class Audit:
    def __init__(self):
        self.lines = []
        self.ok = 0
        self.failures = []
        self.warnings = []

    def line(self, message=""):
        self.lines.append(message)

    def section(self, heading):
        self.line("\n" + heading)
        self.line("─" * 67)

    def check(self, label, correct, reason=""):
        status = "PASS" if correct else "FAIL"
        self.line(f"  [{status}] {label}" + (f" — {reason}" if reason and not correct else ""))
        if correct:
            self.ok += 1
        else:
            self.failures.append(label + (f": {reason}" if reason else ""))

    def warn(self, message):
        self.warnings.append(message)
        self.line(f"  [WARN] {message}")

    def summary(self):
        self.section("RESULT / ITEMS TO REVIEW")
        self.line(f"  PASS {self.ok}   FAIL {len(self.failures)}   WARN {len(self.warnings)}")
        for item in self.failures:
            self.line(f"  MISSING/WRONG: {item}")
        for item in self.warnings:
            self.line(f"  VERIFY: {item}")
        self.line("\nThis checks the synthesized CloudFormation, NOT live AWS deployment.")
        self.line("RESULT: " + ("FAIL (fix before deployment)" if self.failures else "PASS (template matches these inputs)"))
        return "\n".join(self.lines) + "\n"


def audit_template(config, template):
    report = Audit()
    resource = template.get("Resources", {})
    report.line("CLINIC BOOKING | CONFIG vs CDK TEMPLATE")
    report.line(f"App: {config.app_name}  Stage: {config.environment}  CloudFormation resources: {len(resource)}")

    report.section("1. APPLICATION CONFIGURATION")
    report.line(f"  S3 website bucket: {config.frontend_bucket.bucket_name}")
    report.line(f"  S3 additional buckets: {[b.bucket_name for b in getattr(config, 'extra_buckets', [])]}")
    report.line("  Clinic: Mon–Fri 09:00–17:00 IST (13:00–14:00 break), weekend closed")
    report.line(f"  Lambdas: {len(config.lambdas)} | APIs: {len(config.api_gateways)} | Tables: {len(config.tables)}")
    for fn in config.lambdas:
        report.line(f"    {fn.function_name:<19} {fn.handler:<22} {fn.runtime} / {fn.memory_mb} MB")

    report.section("2. S3 AND CLOUDFRONT")
    buckets = list(kind(resource, "AWS::S3::Bucket").values())
    report.check("Website bucket exists", any(properties(b).get("BucketName") == config.frontend_bucket.bucket_name for b in buckets))
    expected_bucket_names = {
        b.bucket_name for b in [config.frontend_bucket, *getattr(config, "extra_buckets", [])]
    }
    actual_bucket_names = {properties(b).get("BucketName") for b in buckets}
    report.check(
        "All configured S3 buckets exist",
        expected_bucket_names == actual_bucket_names,
        f"expected {sorted(expected_bucket_names)}, found {sorted(str(x) for x in actual_bucket_names)}",
    )
    for b in buckets:
        p = properties(b)
        report.check("S3 public access blocked", all(p.get("PublicAccessBlockConfiguration", {}).get(k) is True for k in
            ("BlockPublicAcls", "BlockPublicPolicy", "IgnorePublicAcls", "RestrictPublicBuckets")))
    cf = list(kind(resource, "AWS::CloudFront::Distribution").values())
    report.check("One CloudFront distribution", len(cf) == 1)
    if cf:
        c = properties(cf[0]).get("DistributionConfig", {})
        patterns = [b.get("PathPattern") for b in c.get("CacheBehaviors", [])]
        report.check("Default root is index.html", c.get("DefaultRootObject") == "index.html")
        report.check("/api/* forwarded to API Gateway", "api/*" in patterns)
        behaviors = [c.get("DefaultCacheBehavior", {}), *c.get("CacheBehaviors", [])]
        report.check("Every CloudFront behavior redirects HTTPS", all(b.get("ViewerProtocolPolicy") == "redirect-to-https" for b in behaviors))
    report.check("CloudFront Origin Access Control exists", bool(kind(resource, "AWS::CloudFront::OriginAccessControl")))

    report.section("3. DYNAMODB")
    tables = list(kind(resource, "AWS::DynamoDB::Table").values())
    report.check("Expected DynamoDB table count", len(tables) == len(config.tables), f"expected {len(config.tables)}, found {len(tables)}")
    for table in config.tables:
        matched = [properties(x) for x in tables if str(properties(x).get("TableName", "")).endswith("-" + table.table_name)]
        report.check(f"Table '{table.table_name}' exists", len(matched) == 1)
        if not matched:
            continue
        p = matched[0]
        schema = {(v.get("AttributeName"), v.get("KeyType")) for v in p.get("KeySchema", [])}
        wanted = {(table.partition_key, "HASH")}
        if table.sort_key:
            wanted.add((table.sort_key, "RANGE"))
        report.check("DynamoDB partition/sort keys", wanted == schema)
        report.check("DynamoDB Global Secondary Index", all(any(g.get("IndexName") == i.index_name for g in p.get("GlobalSecondaryIndexes", [])) for i in table.indexes))
        report.check("DynamoDB TTL", p.get("TimeToLiveSpecification", {}).get("AttributeName") == table.ttl_attribute and p.get("TimeToLiveSpecification", {}).get("Enabled") is True)
        report.check("DynamoDB point-in-time recovery", p.get("PointInTimeRecoverySpecification", {}).get("PointInTimeRecoveryEnabled") is True)
        report.check("DynamoDB on-demand billing", p.get("BillingMode") == "PAY_PER_REQUEST")

    report.section("4. LAMBDA FUNCTIONS AND ENVIRONMENT")
    functions = {properties(fn).get("FunctionName"): properties(fn) for fn in kind(resource, "AWS::Lambda::Function").values()}
    for fn in config.lambdas:
        name = f"{config.app_name}-{config.environment}-{fn.function_name}"
        f = functions.get(name)
        report.check(f"Lambda {fn.function_name} exists", f is not None)
        if f is None:
            continue
        runtime = runtime_id(fn.runtime)
        report.check(f"{fn.function_name}: {runtime} runtime", f.get("Runtime") == runtime)
        report.check(f"{fn.function_name}: handler / memory / timeout", f.get("Handler") == fn.handler and f.get("MemorySize") == fn.memory_mb and f.get("Timeout") == fn.timeout_seconds)
        variables = f.get("Environment", {}).get("Variables", {})
        for table in fn.read_write_tables:
            key = "TABLE_NAME_" + table.upper().replace("-", "_")
            report.check(f"{fn.function_name}: {key} injected", key in variables,
                         "missing from framework-generated Lambda variables")
        for q in fn.send_to_queues:
            key = "QUEUE_URL_" + q.upper().replace("-", "_")
            report.check(f"{fn.function_name}: {key} injected", key in variables)
        report.check(f"{fn.function_name}: application mode set", variables.get("APP_MODE") == "aws")
        report.check(f"{fn.function_name}: holiday calendar matches", variables.get("CLINIC_HOLIDAYS") == fn.environment_vars.get("CLINIC_HOLIDAYS", ""))
        if fn.send_email_from:
            report.check(f"{fn.function_name}: SES sender matches", variables.get("SENDER_EMAIL") == fn.send_email_from)
    report.warn("CDK helper Lambdas may appear in addition to the application Lambdas")

    report.section("5. API GATEWAY ENDPOINTS")
    actual_routes = paths(resource)
    named_logicals = {properties(f).get("FunctionName"): key
                      for key, f in kind(resource, "AWS::Lambda::Function").items()}
    for api in config.api_gateways:
        for route in api.routes:
            key = (route.http_method, "/" + route.path)
            exists = key in actual_routes
            report.check(f"{key[0]} {key[1]} present", exists)
            if exists:
                function_name = f"{config.app_name}-{config.environment}-{route.lambda_name}"
                logical_id = named_logicals.get(function_name)
                uri = get_reference_string(actual_routes[key].get("Integration", {}).get("Uri", {}))
                report.check(f"{key[0]} {key[1]} → {route.lambda_name}", bool(logical_id) and logical_id in uri,
                             "API integration does not reference the configured Lambda")
    report.check("API Gateway exists", len(kind(resource, "AWS::ApiGateway::RestApi")) == len(config.api_gateways))
    report.warn("API integration references are checked, but actual HTTP invocation requires deployed smoke tests")

    report.section("6. SQS, EVENTBRIDGE AND EMAIL")
    queue_list = list(kind(resource, "AWS::SQS::Queue").values())
    report.check("SQS queue plus dead-letter queue", len(queue_list) == 2 * len(config.queues))
    report.check("SQS → Lambda event mapping", len(kind(resource, "AWS::Lambda::EventSourceMapping")) == sum(bool(q.consumer_lambda) for q in config.queues))
    rules = [properties(x) for x in kind(resource, "AWS::Events::Rule").values()]
    for rule in config.event_rules:
        report.check(f"EventBridge: {rule.rule_name} schedule", any(r.get("ScheduleExpression") == rule.schedule_expression and r.get("Targets") for r in rules))
    email_fns = [f for f in config.lambdas if f.send_email_from]
    if any(f.send_email_from.endswith("@example.com") for f in email_fns):
        report.warn("SES uses placeholder e-mail; verify SENDER_EMAIL before deploying")
    policies = json.dumps(kind(resource, "AWS::IAM::Policy"))
    report.check("DynamoDB IAM policy actions present", "dynamodb:PutItem" in policies and "dynamodb:Query" in policies)
    report.check("SES send permission present", "ses:SendEmail" in policies)

    report.section("7. GENERATED CLOUDFORMATION INVENTORY")
    counts = Counter(res.get("Type") for res in resource.values())
    for kind_name, count in sorted(counts.items()):
        report.line(f"  {kind_name:<47} {count:>3}  {PURPOSES.get(kind_name, 'Supporting resource')}")
    outputs = template.get("Outputs", {})
    report.check("CloudFormation SiteUrl output", "SiteUrl" in outputs)
    report.check("CloudFormation FrontendBucketName output", "FrontendBucketName" in outputs)
    report.warn("Synthesis verifies the template, not that AWS resources have been deployed")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", default=os.environ.get("STAGE", "dev"), choices=["dev", "staging", "prod"])
    parser.add_argument("--synth", action="store_true", help="run CDK synth before reviewing")
    parser.add_argument("--report", type=Path, help="write a NEW report to this path (will not overwrite)")
    args = parser.parse_args(argv)
    os.chdir(ROOT)
    os.environ["STAGE"] = args.stage
    if args.synth:
        result = subprocess.run(["cdk", "synth", "--quiet"], check=False)
        if result.returncode:
            print("[FAIL] CDK synthesis failed. No template review performed.")
            return 1
    try:
        from app_config import build_config
        config = build_config(args.stage)
        file = ROOT / "cdk.out" / f"{''.join(x.capitalize() for x in config.app_name.split('-'))}{args.stage.capitalize()}Stack.template.json"
        if not file.is_file():
            print(f"[FAIL] Expected {file.name} not found. Run 'cdk synth' for the matching stage.")
            return 1
        template = json.loads(file.read_text(encoding="utf-8"))
        report = audit_template(config, template)
        text = report.summary()
        print(text)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            # Reject overwriting earlier reports.
            with args.report.open("x", encoding="utf-8") as output:
                output.write(text)
            print(f"Saved new report: {args.report}")
        return int(bool(report.failures))
    except (OSError, ValueError, TypeError, ImportError) as exc:
        print(f"[FAIL] Cannot run template review: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())