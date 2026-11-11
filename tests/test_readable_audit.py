"""The template auditor must fail on missing infrastructure, not claim success."""

from types import SimpleNamespace as Obj

from review_cdk_output import audit_template, paths


def sample_configuration():
    fn = Obj(
        function_name="book-slot", handler="clinic.book_slot", runtime="PYTHON_3_14",
        memory_mb=256, timeout_seconds=20, send_to_queues=["booking-events"],
        read_write_tables=["bookings"],
        environment_vars={"APP_MODE": "aws", "CLINIC_HOLIDAYS": ""},
        send_email_from=None,
    )
    table = Obj(table_name="bookings", partition_key="booking_date", sort_key="slot_time",
                indexes=[Obj(index_name="code-index")], ttl_attribute="expires_at")
    api = Obj(routes=[Obj(http_method="POST", path="api/bookings", lambda_name="book-slot")])
    return Obj(app_name="clinic-booking", environment="dev",
               frontend_bucket=Obj(bucket_name="clinic-booking-web-dev-demo"),
               cloudfront=Obj(api_name="clinic-api"),
               lambdas=[fn], tables=[table], api_gateways=[api],
               queues=[Obj(consumer_lambda="notify-worker")],
               event_rules=[Obj(rule_name="daily-reminder-schedule", schedule_expression="cron(30 3 * * ? *)")])


def test_missing_entire_template_fails_loudly():
    result = audit_template(sample_configuration(), {"Resources": {}, "Outputs": {}})
    assert result.failures
    assert any("Table" in failure for failure in result.failures)
    assert any("Lambda" in failure for failure in result.failures)
    assert any("CloudFront" in failure for failure in result.failures)
    assert "RESULT: FAIL" in result.summary()


def test_missing_table_environment_variable_is_detected():
    config = sample_configuration()
    resources = {
        "Function": {"Type": "AWS::Lambda::Function", "Properties": {
            "FunctionName": "clinic-booking-dev-book-slot", "Runtime": "python3.14",
            "Handler": "clinic.book_slot", "MemorySize": 256, "Timeout": 20,
            "Environment": {"Variables": {"APP_MODE": "aws", "CLINIC_HOLIDAYS": ""}},
        }}
    }
    result = audit_template(config, {"Resources": resources, "Outputs": {}})
    assert any("TABLE_NAME_BOOKINGS" in issue for issue in result.failures)


def test_api_path_resolver_handles_nested_routes():
    resources = {
        "Api": {"Type": "AWS::ApiGateway::Resource", "Properties": {
            "PathPart": "api", "ParentId": {"Fn::GetAtt": ["Rest", "RootResourceId"]}}},
        "Bookings": {"Type": "AWS::ApiGateway::Resource", "Properties": {
            "PathPart": "bookings", "ParentId": {"Ref": "Api"}}},
        "Post": {"Type": "AWS::ApiGateway::Method", "Properties": {
            "HttpMethod": "POST", "ResourceId": {"Ref": "Bookings"}}},
    }
    assert ("POST", "/api/bookings") in paths(resources)
