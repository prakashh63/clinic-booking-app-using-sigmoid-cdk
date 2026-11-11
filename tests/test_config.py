import pytest
from app_config import BUCKET_SUFFIX, DEFAULT_RUNTIME, LAMBDA_SOURCE, build_config


@pytest.mark.parametrize("stage", ["dev", "staging", "prod"])
def test_config_is_valid_for_every_stage(stage: str) -> None:
    config = build_config(stage)
    assert config.environment == stage
    assert len(config.lambdas) == 7
    assert config.frontend_bucket.bucket_name.endswith(f"-{stage}-{BUCKET_SUFFIX}")
    assert config.cloudfront.api_name == "clinic-api"
    assert all(fn.runtime == DEFAULT_RUNTIME for fn in config.lambdas)


def test_unknown_stage_is_rejected() -> None:
    with pytest.raises(ValueError):
        build_config("test")


def test_every_lambda_uses_the_booking_table() -> None:
    for function in build_config("dev").lambdas:
        assert function.read_write_tables == ["bookings"]
        assert function.entry_point_dir == LAMBDA_SOURCE


def test_only_the_two_email_sending_lambdas_get_ses() -> None:
    senders = {f.function_name for f in build_config("dev").lambdas if f.send_email_from}
    assert senders == {"notify-worker", "daily-reminder"}


def test_per_function_runtime_override(monkeypatch) -> None:
    from app_config import LAMBDA_RUNTIMES

    monkeypatch.setitem(LAMBDA_RUNTIMES, "list-slots", "PYTHON_3_13")
    functions = {f.function_name: f for f in build_config("dev").lambdas}
    assert functions["list-slots"].runtime == "PYTHON_3_13"
    assert functions["book-slot"].runtime == DEFAULT_RUNTIME


def test_extra_s3_bucket_configuration(monkeypatch) -> None:
    import app_config

    monkeypatch.setattr(app_config, "ADDITIONAL_BUCKETS", ("reports", "private-assets"))
    config = build_config("dev")
    assert [bucket.bucket_name for bucket in config.extra_buckets] == [
        f"clinic-booking-reports-dev-{BUCKET_SUFFIX}",
        f"clinic-booking-private-assets-dev-{BUCKET_SUFFIX}",
    ]
