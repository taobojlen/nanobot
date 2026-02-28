import pytest

from nanobot.cron.service import CronService
from nanobot.cron.types import CronSchedule


def test_add_job_rejects_unknown_timezone(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    with pytest.raises(ValueError, match="unknown timezone 'America/Vancovuer'"):
        service.add_job(
            name="tz typo",
            schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="America/Vancovuer"),
            message="hello",
        )

    assert service.list_jobs(include_disabled=True) == []


def test_add_job_accepts_valid_timezone(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    job = service.add_job(
        name="tz ok",
        schedule=CronSchedule(kind="cron", expr="0 9 * * *", tz="America/Vancouver"),
        message="hello",
    )

    assert job.schedule.tz == "America/Vancouver"
    assert job.state.next_run_at_ms is not None


def test_update_job_recalculates_next_run(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    job = service.add_job(
        name="daily",
        schedule=CronSchedule(kind="cron", expr="0 9 * * *"),
        message="good morning",
    )
    original_next_run = job.state.next_run_at_ms

    updated = service.update_job(
        job.id,
        schedule=CronSchedule(kind="cron", expr="0 7 * * *"),
    )

    assert updated is not None
    assert updated.schedule.expr == "0 7 * * *"
    assert updated.state.next_run_at_ms is not None
    assert updated.state.next_run_at_ms != original_next_run


def test_update_job_unknown_id_returns_none(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    result = service.update_job("nonexistent", message="hi")

    assert result is None


def test_update_job_message_only(tmp_path) -> None:
    service = CronService(tmp_path / "cron" / "jobs.json")

    job = service.add_job(
        name="interval",
        schedule=CronSchedule(kind="every", every_ms=3600 * 1000),
        message="original message",
    )
    original_expr = job.schedule.every_ms
    original_next_run = job.state.next_run_at_ms

    updated = service.update_job(job.id, message="new message")

    assert updated is not None
    assert updated.payload.message == "new message"
    assert updated.schedule.every_ms == original_expr
    # next run should not change when only message is updated
    assert updated.state.next_run_at_ms == original_next_run
