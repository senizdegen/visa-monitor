import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def _names(reqs: list[str]) -> set[str]:
    return {re.split(r"[\s<>=!\[;~]", r, maxsplit=1)[0].lower() for r in reqs}


def test_description_is_not_placeholder():
    desc = PYPROJECT["project"]["description"]
    assert desc and desc != "Add your description here"


def test_httpx_is_dev_only_and_unused_at_runtime():
    assert "httpx" not in _names(PYPROJECT["project"]["dependencies"])
    assert "httpx" in _names(PYPROJECT["dependency-groups"]["dev"])
    imports = re.compile(r"^\s*(import|from)\s+httpx\b", re.MULTILINE)
    offenders = [p for p in (ROOT / "src").rglob("*.py") if imports.search(p.read_text(encoding="utf-8"))]
    assert offenders == []
