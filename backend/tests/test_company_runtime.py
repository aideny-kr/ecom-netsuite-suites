"""Separate-process checks for the environment-selected worker configuration."""

import json
import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize("single_company", ["true", "false"])
def test_customer_schedules_survive_but_vendor_jobs_are_disabled(single_company):
    script = (
        "import json; from app.workers.celery_app import celery_app; "
        "print(json.dumps([v['task'] for v in celery_app.conf.beat_schedule.values()]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "SINGLE_COMPANY": single_company},
        capture_output=True,
        text=True,
        check=True,
    )
    tasks = json.loads(result.stdout)
    assert "tasks.scheduled_jobs_sweep_all" in tasks
    assert "tasks.proactive_token_refresh" in tasks
    vendor_tasks = {"tasks.billing_sync", "tasks.auto_learning", "tasks.knowledge_crawler"}
    assert bool(vendor_tasks.intersection(tasks)) is (single_company == "false")


def test_noninteractive_bootstrap_does_not_accept_or_echo_password():
    secret = "Never-Echo-This-Password1!"
    result = subprocess.run(
        [sys.executable, "-m", "app.cli.company", "bootstrap"],
        input=secret,
        capture_output=True,
        text=True,
        env={**os.environ, "SINGLE_COMPANY": "true"},
    )
    assert result.returncode == 1
    assert "interactive terminal" in result.stderr
    assert secret not in result.stdout + result.stderr
