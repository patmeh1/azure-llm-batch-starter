import copy
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
        assert len(rows) == 7
        partial = dashboard.load_batch_details(store, "simulated-3")
        assert partial["succeeded"] == 94
        assert partial["failed"] == 6
        assert partial["elapsed_seconds"] == 610
        assert all(row["source"] == "simulated" for row in rows)
        complex_run = dashboard.load_batch_details(store, "simulated-complex-v2")
        assert complex_run["succeeded"] == 13
        assert complex_run["quality"]["passed_records"] == 12
        assert complex_run["semantic_status"] == "needs_review"
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


def test_complex_quality_panel_renders_without_inference(tmp_path: Path, monkeypatch):
    path = tmp_path / "demo.sqlite3"
    seed_demo(path)
    monkeypatch.setenv("LLM_DASHBOARD_STATE", str(path))
    app = AppTest.from_file(str(Path(dashboard.__file__))).run(timeout=30)
    app.selectbox[0].select("simulated-complex-v2").run()
    assert not app.exception
    assert any(item.value == "Complex workload quality" for item in app.subheader)


def test_v4_arm_and_target_views_render_without_inference(tmp_path: Path, monkeypatch):
    path = tmp_path / "demo.sqlite3"
    seed_demo(path)
    store = State(path, readonly=True)
    try:
        detail = dashboard.load_batch_details(store, "simulated-complex-v2")
        history = [
            row
            for row in dashboard.load_run_history(store)
            if row["run_id"] == "simulated-complex-v2"
        ]
    finally:
        store.close()
    history[0]["mode"] = detail["mode"] = "multi-deployment"
    detail["multi_deployment_stages"] = [
        {
            **copy.deepcopy(detail),
            "arm": arm,
            "gate": {"status": "needs_review"},
            "target_metrics": [{"alias": alias, "http_attempts": 13, "errors_429": 0}],
        }
        for arm, alias in (("single", "control"), ("pool", "pool-1"))
    ]
    detail["multi_deployment_progress"] = {"phase": "pool"}
    monkeypatch.setattr(dashboard, "load_batch_details", lambda *_: detail)
    monkeypatch.setattr(dashboard, "load_run_history", lambda *_: history)
    monkeypatch.setenv("LLM_DASHBOARD_STATE", str(path))

    def render():
        from llm_batch_starter.dashboard import render_dashboard

        render_dashboard()

    app = AppTest.from_function(render).run(timeout=30)
    assert not app.exception
    labels = [item.value for item in app.subheader]
    assert "V4 matched-quota comparison" in labels
    assert "V4 durable execution progress" in labels
    assert "Execution artifacts" in labels
    assert "Batch partitions" not in labels
