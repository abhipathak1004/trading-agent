import json
import re
import tomllib
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]


def _notice_entries(section: str) -> set[tuple[str, str, str]]:
    notices = (PROJECT_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    body = notices.split(f"## {section}\n", maxsplit=1)[1].split("\n## ", maxsplit=1)[0]
    entries: set[tuple[str, str, str]] = set()
    for line in body.splitlines():
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) < 3:
            continue
        package = re.match(r"\[([^\]]+)\]\([^)]+\)", cells[1])
        if package:
            entries.add((package.group(1).casefold(), cells[2], cells[-1]))
    return entries


def test_dependency_notices_match_locked_packages() -> None:
    python_lock = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"))
    expected_python = {
        (package["name"].casefold(), package["version"])
        for package in python_lock["package"]
        if package["name"] != "bharatquant"
    }

    npm_lock = json.loads(
        (PROJECT_ROOT / "web" / "package-lock.json").read_text(encoding="utf-8")
    )
    expected_npm: set[tuple[str, str, str]] = set()
    for path, package in npm_lock["packages"].items():
        if not path.startswith("node_modules/"):
            continue
        name = package.get("name", path[len("node_modules/") :])
        assert package.get("license")
        assert package.get("integrity")
        expected_npm.add((name.casefold(), package["version"], package["license"]))

    python_notices = {
        (name, version) for name, version, _ in _notice_entries("Python packages")
    }
    npm_notices = _notice_entries("Node packages")

    assert python_notices == expected_python
    assert npm_notices == expected_npm
