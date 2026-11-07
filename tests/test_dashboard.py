from pathlib import Path

from streamlit.testing.v1 import AppTest

from llm_batch_starter import dashboard
from llm_batch_starter.demo import seed_demo
from llm_batch_starter.state import State


def test_demo_history_and_details_agree(tmp_path: Path):
    path = tmp_path / "demo.sqlite3"
    seed_demo(path)
    store = State(path, readonly=True)
    try:
        rows = dashboard.load_run_history(store)
        assert len(rows) == 6
        partial = dashboard.load_batch_details(store, "simulated-3")
        assert partial["succeeded"] == 94
        assert partial["failed"] == 6
        assert partial["elapsed_seconds"] == 610
        assert all(row["source"] == "simulated" for row in rows)
    finally:
        store.close()


def test_dashboard_renders_without_azure(tmp_path: Path, monkeypatch):
    path = tmp_path / "demo.sqlite3"
    seed_demo(path)
    monkeypatch.setenv("LLM_DASHBOARD_STATE", str(path))
    app = AppTest.from_file(str(Path(dashboard.__file__))).run(timeout=30)
    assert not app.exception
    assert app.title[0].value == "LLM Batch Operations"
    assert any("SIMULATED" in item.value for item in app.warning)
    assert len(app.dataframe) >= 2


def test_empty_dashboard_is_explicit(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LLM_DASHBOARD_STATE", str(tmp_path / "missing.sqlite3"))
    app = AppTest.from_file(str(Path(dashboard.__file__))).run(timeout=30)
    assert not app.exception
    assert any("No local history" in item.value for item in app.info)
