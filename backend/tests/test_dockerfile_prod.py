from pathlib import Path


DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile.prod"


def _dockerfile_instructions() -> list[str]:
    instructions: list[str] = []
    current: list[str] = []

    for raw_line in DOCKERFILE.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        continued = line.endswith("\\")
        current.append(line.removesuffix("\\").strip())
        if not continued:
            instructions.append(" ".join(current))
            current = []

    if current:
        instructions.append(" ".join(current))

    return instructions


def test_prod_dockerfile_prepares_celerybeat_data_dir_before_appuser():
    instructions = _dockerfile_instructions()
    appuser_index = instructions.index("USER appuser")
    root_instructions = instructions[:appuser_index]

    data_dir_setup = " ".join(
        instruction for instruction in root_instructions if instruction.startswith("RUN ")
    )

    assert "mkdir -p /data" in data_dir_setup
    assert "chown -R appuser:appuser /data" in data_dir_setup
