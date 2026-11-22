"""Статические проверки опций установщика (installer.iss)."""

from pathlib import Path

import pytest

ISS = Path(__file__).resolve().parents[1] / "installer.iss"


@pytest.fixture(scope="module")
def iss_text() -> str:
    return ISS.read_text(encoding="utf-8")


def _section(text: str, name: str) -> str:
    """Вернуть тело единственного раздела [name]."""
    lines = text.splitlines()
    heading = f"[{name}]"
    found = [i for i, line in enumerate(lines) if line.strip() == heading]
    assert len(found) == 1, f"Ожидался ровно один раздел {heading}, найдено: {len(found)}"
    start = found[0]
    end = next(
        (
            i
            for i in range(start + 1, len(lines))
            if lines[i].strip().startswith("[") and lines[i].strip().endswith("]")
        ),
        len(lines),
    )
    return "\n".join(lines[start + 1:end])


def _entries(section: str) -> list[str]:
    return [
        line.strip()
        for line in section.splitlines()
        if line.strip() and not line.strip().startswith(";")
    ]


def test_autorun_task(iss_text: str) -> None:
    tasks = _entries(_section(iss_text, "Tasks"))
    autorun_lines = [line for line in tasks if 'Name: "autorun"' in line]
    assert len(autorun_lines) == 1
    autorun = autorun_lines[0]
    assert 'Description: "Добавить в автозагрузку"' in autorun
    assert 'GroupDescription: "Дополнительно:"' in autorun
    assert "Check: NeedAutorunTask" in autorun
    assert "unchecked" not in autorun.lower()


def test_registry_autorun_entry(iss_text: str) -> None:
    entries = _entries(_section(iss_text, "Registry"))
    autorun_lines = [line for line in entries if 'ValueName: "YaDiskManager"' in line]
    assert len(autorun_lines) == 1
    entry = autorun_lines[0]
    assert "Root: HKCU" in entry
    assert r'Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"' in entry
    assert "ValueType: string" in entry
    assert "Tasks: autorun" in entry
    assert "uninsdeletevalue" in entry


def test_need_autorun_task_helper(iss_text: str) -> None:
    code = _section(iss_text, "Code")
    assert "function NeedAutorunTask: Boolean;" in code
    assert "RegQueryStringValue(HKEY_CURRENT_USER" in code
    assert r"'Software\Microsoft\Windows\CurrentVersion\Run'" in code
    assert "'YaDiskManager'" in code
    assert "not RegQueryStringValue" in code


def test_finish_page_run_checkbox(iss_text: str) -> None:
    postinstall = [
        line for line in _entries(_section(iss_text, "Run")) if "postinstall" in line
    ]
    assert len(postinstall) == 1, "Должен остаться только один [Run] с postinstall"
    entry = postinstall[0]
    assert 'Description: "Запустить ЯДиск Менеджер"' in entry
    for flag in ("nowait", "postinstall", "skipifsilent"):
        assert flag in entry, f"Отсутствует флаг {flag}"
    tasks = _entries(_section(iss_text, "Tasks"))
    assert not [line for line in tasks if 'Name: "runapp"' in line]


def test_version_and_preserved_user_changes(iss_text: str) -> None:
    assert '#define MyAppVersion "0.13.0"' in iss_text
    setup = _section(iss_text, "Setup")
    assert "CloseApplications=no" in setup
    assert "RestartApplications=no" in setup
    code = _section(iss_text, "Code")
    for signature in (
        "AppImageName",
        "function IsAppRunning(): Boolean;",
        "function CloseRunningApp(): Boolean;",
        "function PrepareToInstall(var NeedsRestart: Boolean): String;",
        "procedure CurStepChanged(CurStep: TSetupStep);",
        "procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);",
    ):
        assert signature in code, f"Потерян пользовательский код: {signature}"
