import verify_stack as v


def test_runtime_names_are_translated() -> None:
    assert v.runtime_id("PYTHON_3_14") == "python3.14"
    assert v.runtime_id("NODEJS_22_X") == "nodejs22.x"
    assert v.runtime_id("JAVA_21") == "java21"
    assert v.runtime_id("PROVIDED_AL2023") == "provided.al2023"
    assert v.runtime_id("SOMETHING_NEW") is None


def test_api_routes_are_rebuilt_from_the_resource_tree() -> None:
    template = {
        "Resources": {
            "Api": {"Type": "AWS::ApiGateway::Resource", "Properties": {"PathPart": "api", "ParentId": {"Fn::GetAtt": ["X", "RootResourceId"]}}},
            "Slots": {"Type": "AWS::ApiGateway::Resource", "Properties": {"PathPart": "slots", "ParentId": {"Ref": "Api"}}},
            "Get": {"Type": "AWS::ApiGateway::Method", "Properties": {"HttpMethod": "GET", "ResourceId": {"Ref": "Slots"}}},
            "Opt": {"Type": "AWS::ApiGateway::Method", "Properties": {"HttpMethod": "OPTIONS", "ResourceId": {"Ref": "Slots"}}},
        }
    }
    assert v.api_routes(template) == {("GET", "/api/slots")}


def test_a_broken_template_is_reported_as_failed(tmp_path) -> None:
    (tmp_path / "Stack.template.json").write_text('{"Resources": {}, "Outputs": {}}')
    assert v.main(["--out", str(tmp_path)]) == 1
