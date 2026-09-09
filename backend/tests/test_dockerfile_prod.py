import re
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile.prod"
DEV_DOCKERFILE = Path(__file__).resolve().parents[1] / "Dockerfile"

# The native libraries WeasyPrint dlopen()s at import time (report_pdf.py). Without
# every one of them render_report_pdf() raises ReportPdfUnavailableError on the
# deployed image, and POST /reports/{id}/deliver answers 502 on every call.
WEASYPRINT_APT_PACKAGES = frozenset(
    {
        "libpango-1.0-0",
        "libpangoft2-1.0-0",
        "libcairo2",
        "libgdk-pixbuf-2.0-0",
        "libffi-dev",
        "shared-mime-info",
        "fonts-dejavu-core",
    }
)


def _dockerfile_instructions(path: Path = DOCKERFILE) -> list[str]:
    instructions: list[str] = []
    current: list[str] = []

    for raw_line in path.read_text().splitlines():
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

    data_dir_setup = " ".join(instruction for instruction in root_instructions if instruction.startswith("RUN "))

    assert "mkdir -p /data" in data_dir_setup
    assert "chown -R appuser:appuser /data" in data_dir_setup


def _apt_packages_installed(instructions: list[str]) -> set[str]:
    """Every package name handed to `apt-get install` across the RUN layers."""
    packages: set[str] = set()
    for instruction in instructions:
        if not instruction.startswith("RUN "):
            continue
        for match in re.finditer(r"apt-get install\s+(?:-[-\w]+\s+)*([^&|;]+)", instruction):
            packages.update(tok for tok in match.group(1).split() if not tok.startswith("-"))
    return packages


def test_prod_dockerfile_installs_weasyprint_native_libs():
    """deploy.yml builds Dockerfile.prod, not the dev Dockerfile. Slice 1 Task 4 added
    the WeasyPrint system libraries to the dev image only (its brief's file list), so
    the image that actually ships would have raised ReportPdfUnavailableError on the
    first Drive delivery. The apt layer must run before `COPY backend/ .` (root, before
    USER appuser) exactly like the other system deps."""
    instructions = _dockerfile_instructions()
    appuser_index = instructions.index("USER appuser")
    installed = _apt_packages_installed(instructions[:appuser_index])
    missing = WEASYPRINT_APT_PACKAGES - installed
    assert not missing, f"Dockerfile.prod lacks WeasyPrint native libs: {sorted(missing)}"


def test_prod_dockerfile_gives_fontconfig_a_writable_cache_for_appuser():
    """Observed on the built image: `USER appuser` has HOME=/nonexistent, so fontconfig
    (which WeasyPrint/pango drive) logs "Fontconfig error: No writable cache
    directories" a dozen times per render and re-scans every font on each process
    start. XDG_CACHE_HOME must point somewhere appuser can write (/tmp is)."""
    instructions = _dockerfile_instructions()
    env_lines = " ".join(i for i in instructions if i.startswith("ENV "))
    assert "XDG_CACHE_HOME=/tmp/" in env_lines


def test_prod_and_dev_dockerfiles_install_the_same_weasyprint_libs():
    """Drift guard: the next edit to one image's WeasyPrint layer must land in both,
    or the dev image's green PDF tests stop being evidence for what production runs."""
    prod = _apt_packages_installed(_dockerfile_instructions(DOCKERFILE)) & WEASYPRINT_APT_PACKAGES
    dev = _apt_packages_installed(_dockerfile_instructions(DEV_DOCKERFILE)) & WEASYPRINT_APT_PACKAGES
    assert dev == WEASYPRINT_APT_PACKAGES, f"dev Dockerfile drifted: {sorted(WEASYPRINT_APT_PACKAGES - dev)}"
    assert prod == dev, f"Dockerfile.prod drifted from Dockerfile: {sorted(dev - prod)}"
