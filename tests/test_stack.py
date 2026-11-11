import pytest
from app_config import build_config
from aws_cdk.assertions import Annotations, Match, Template
from sigmoid_cdk import SigmoidApp


def synth(stage: str):
    app = SigmoidApp(build_config(stage), context={"aws:cdk:bundling-stacks": []})
    return app, Template.from_stack(app.web_app_stack)


@pytest.mark.parametrize("stage", ["dev", "staging", "prod"])
def test_no_security_findings(stage: str) -> None:
    app, _ = synth(stage)
    errors = Annotations.from_stack(app.web_app_stack).find_error(
        "*",
        Match.string_like_regexp(r"AwsSolutions-.*|\[sigmoid-cdk IAM guardrail\].*"),
    )
    assert errors == []


def test_expected_resources() -> None:
    _, template = synth("dev")
    template.resource_count_is("AWS::Lambda::Function", 7)
    template.resource_count_is("AWS::DynamoDB::Table", 1)
    template.resource_count_is("AWS::SQS::Queue", 2)  # queue + dead-letter queue
    template.resource_count_is("AWS::Events::Rule", 1)
    template.resource_count_is("AWS::S3::Bucket", 1)
    template.resource_count_is("AWS::CloudFront::Distribution", 1)
    template.resource_count_is("AWS::Lambda::EventSourceMapping", 1)


def test_prod_table_is_protected() -> None:
    _, template = synth("prod")
    for table in template.find_resources("AWS::DynamoDB::Table").values():
        assert table["DeletionPolicy"] == "Retain"
        assert table["Properties"]["DeletionProtectionEnabled"] is True


def test_only_two_lambdas_get_the_sender_address() -> None:
    _, template = synth("dev")
    functions = template.find_resources("AWS::Lambda::Function").values()
    with_sender = [
        f for f in functions if "SENDER_EMAIL" in f["Properties"]["Environment"]["Variables"]
    ]
    assert len(with_sender) == 2


def test_every_lambda_uses_the_configured_runtime() -> None:
    _, template = synth("dev")
    for function in template.find_resources("AWS::Lambda::Function").values():
        assert function["Properties"]["Runtime"] == "python3.14"
