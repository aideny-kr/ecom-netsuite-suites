import pytest

from app.services.drive_rag.url_parser import parse_file_id, parse_folder_id


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://drive.google.com/drive/folders/1aB2c_-3D4Ef", "1aB2c_-3D4Ef"),
        ("https://drive.google.com/drive/u/0/folders/1aB2c_-3D4Ef?usp=sharing", "1aB2c_-3D4Ef"),
        ("https://drive.google.com/drive/folders/1aB2c_-3D4Ef/", "1aB2c_-3D4Ef"),
        ("1aB2c_-3D4Ef", "1aB2c_-3D4Ef"),
    ],
)
def test_parse_folder_id(url: str, expected: str):
    assert parse_folder_id(url) == expected


@pytest.mark.parametrize("bad", ["", "   ", "not a url", "https://example.com/foo"])
def test_parse_folder_id_invalid_raises(bad: str):
    with pytest.raises(ValueError):
        parse_folder_id(bad)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://docs.google.com/document/d/1aB2c_-3D4Ef/edit", "1aB2c_-3D4Ef"),
        ("https://docs.google.com/spreadsheets/d/1aB2c_-3D4Ef/edit?gid=0", "1aB2c_-3D4Ef"),
        ("https://drive.google.com/file/d/1aB2c_-3D4Ef/view", "1aB2c_-3D4Ef"),
        ("https://drive.google.com/file/d/1aB2c_-3D4Ef/view?usp=sharing", "1aB2c_-3D4Ef"),
        ("1aB2c_-3D4Ef", "1aB2c_-3D4Ef"),
    ],
)
def test_parse_file_id(url: str, expected: str):
    assert parse_file_id(url) == expected


def test_parse_file_id_invalid_raises():
    with pytest.raises(ValueError):
        parse_file_id("https://example.com/not-a-drive-url")
