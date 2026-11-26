import json
from pathlib import Path

import pytest

from bq.cli import main
from bq.infra.database import check_database


def test_runtime_cli_bench_backup_and_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("BQ_HOME", str(tmp_path))
    assert main(["init"]) == 0
    capsys.readouterr()

    assert main(["bench"]) == 0
    bench = json.loads(capsys.readouterr().out)
    assert bench["status"] == "ok"

    backup = tmp_path / "backup.db"
    assert main(["db", "backup", "--output", str(backup)]) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["bytes"] > 0
    assert backup.is_file()

    assert main(["db", "restore", "--backup", str(backup), "--confirm"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["pre_restore"] is not None
    check_database(tmp_path / "bq.db")
