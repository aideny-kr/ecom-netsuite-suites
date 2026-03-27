"""Unit tests for TaskFileService._validate_upload()."""
import pytest
from app.services.task_file_service import TaskFileService, MAX_FILE_SIZE


@pytest.fixture
def svc():
    return TaskFileService()


def test_validate_xlsx_extension(svc):
    """Valid .xlsx file passes without error."""
    svc._validate_upload("prices.xlsx", b"data")


def test_validate_csv_extension(svc):
    """Valid .csv file passes without error."""
    svc._validate_upload("data.csv", b"data")


def test_reject_exe_extension(svc):
    """Disallowed extension raises ValueError."""
    with pytest.raises(ValueError, match="not allowed"):
        svc._validate_upload("malware.exe", b"data")


def test_reject_oversized_file(svc):
    """File exceeding 10MB raises ValueError."""
    with pytest.raises(ValueError, match="exceeds"):
        svc._validate_upload("big.xlsx", b"x" * (11 * 1024 * 1024))


def test_accept_max_size(svc):
    """File exactly at 10MB limit passes without error."""
    svc._validate_upload("max.xlsx", b"x" * MAX_FILE_SIZE)
