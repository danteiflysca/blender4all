from pathlib import Path
from xml.etree import ElementTree

ROOT = Path(__file__).resolve().parents[1]


def test_readme_contains_reproducible_launch_and_smoke_commands() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    required = (
        'python -m pip install -e ".[dev]"',
        "FARMHAND_TOKEN",
        "FARMHAND_DATA_DIR",
        "uvicorn coordinator.main:app --host 0.0.0.0 --port 8420",
        ".venv/bin/python -m worker.agent --config worker/config.toml",
        "X-Farm-Token: $TOKEN",
        "/jobs/$JOB_ID/frames.zip",
    )
    for text in required:
        assert text in readme


def test_readme_documents_operational_boundaries() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8").lower()

    required = (
        "lan-only security boundary",
        "major.minor",
        "headless",
        "linked `.blend` libraries",
        "failure drills",
        "explicitly out of scope for v1",
    )
    for text in required:
        assert text in readme


def test_systemd_template_is_editable_and_uses_worker_module() -> None:
    unit = (ROOT / "packaging" / "farmhand-worker.service").read_text(encoding="utf-8")

    assert "User=EDIT_ME_WORKER_USER" in unit
    assert "WorkingDirectory=/EDIT_ME/" in unit
    assert "-m worker.agent --config" in unit
    assert "After=network-online.target" in unit
    assert "/Users/" not in unit
    assert "C:\\Users\\" not in unit


def test_windows_task_template_is_valid_xml_with_safe_placeholders() -> None:
    path = ROOT / "packaging" / "FarmhandWorkerTask.xml"
    task = path.read_text(encoding="utf-8")
    root = ElementTree.parse(path).getroot()

    namespace = {"task": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
    assert root.tag.endswith("Task")
    assert root.find("task:Triggers/task:BootTrigger", namespace) is not None
    assert root.find("task:Actions/task:Exec", namespace) is not None
    assert "EDIT_ME" in task
    assert "-m worker.agent --config" in task
    assert "aviatoronline" not in task.lower()

