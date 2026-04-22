import json
import uuid
from unittest.mock import MagicMock

import pytest

from app.services.agent_lab.progress_emitter import ProgressEmitter


@pytest.fixture
def run_id():
    return uuid.UUID("11111111-2222-3333-4444-555555555555")


@pytest.fixture
def redis_mock():
    r = MagicMock()
    r.get.return_value = None  # cancel key absent by default
    return r


@pytest.fixture
def db_mock():
    return MagicMock()


def test_emit_writes_xadd_with_correct_payload(run_id, redis_mock, db_mock):
    emitter = ProgressEmitter(run_id, redis_mock, db_mock)
    emitter.emit("case_started", {"case_id": "c1", "index": 1})

    redis_mock.xadd.assert_called_once()
    args, kwargs = redis_mock.xadd.call_args
    stream_key = args[0]
    payload = args[1]
    assert stream_key == f"agent_lab_run:{run_id}"
    assert payload["event"] == "case_started"
    assert json.loads(payload["data"]) == {"case_id": "c1", "index": 1}
    assert kwargs["maxlen"] == 1000
    assert kwargs["approximate"] is True


def test_emit_sets_expire_once_on_first_emit(run_id, redis_mock, db_mock):
    emitter = ProgressEmitter(run_id, redis_mock, db_mock)
    emitter.emit("case_started", {"case_id": "c1"})
    emitter.emit("case_started", {"case_id": "c2"})
    emitter.emit("case_complete", {"case_id": "c1", "cases_completed": 1, "running_cost_usd": 0.5,
                                   "result": {}})

    assert redis_mock.expire.call_count == 1
    redis_mock.expire.assert_called_with(f"agent_lab_run:{run_id}", 1800)


def test_emit_case_complete_updates_db(run_id, redis_mock, db_mock):
    emitter = ProgressEmitter(run_id, redis_mock, db_mock)
    emitter.emit("case_complete", {
        "case_id": "c1",
        "cases_completed": 5,
        "running_cost_usd": 1.23,
        "result": {},
    })

    # Verify the UPDATE was issued against AgentLabRun for this run_id
    db_mock.query.assert_called_once()
    query_call = db_mock.query.return_value
    query_call.filter_by.assert_called_with(id=run_id)
    query_call.filter_by.return_value.update.assert_called_with({
        "cases_completed": 5,
        "cost_usd_actual": 1.23,
    })
    db_mock.commit.assert_called_once()


def test_emit_non_case_complete_does_not_touch_db(run_id, redis_mock, db_mock):
    emitter = ProgressEmitter(run_id, redis_mock, db_mock)
    emitter.emit("case_started", {"case_id": "c1", "index": 1})
    emitter.emit("run_started", {"total_cases": 18})
    emitter.emit("preparing", {"phase": "mining"})

    db_mock.query.assert_not_called()
    db_mock.commit.assert_not_called()


def test_cancelled_returns_bool(run_id, redis_mock, db_mock):
    emitter = ProgressEmitter(run_id, redis_mock, db_mock)

    redis_mock.get.return_value = None
    assert emitter.cancelled() is False

    redis_mock.get.return_value = b"1"
    assert emitter.cancelled() is True

    redis_mock.get.assert_called_with(f"agent_lab_run:{run_id}:cancel")
