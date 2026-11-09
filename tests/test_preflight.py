import importlib.util
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize("online_only", [True, False])
def test_online_only_does_not_assert_batch_support(monkeypatch, capsys, online_only):
    path = Path(__file__).parents[1] / "scripts" / "preflight.py"
    spec = importlib.util.spec_from_file_location("preflight_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    subscription = "00000000-0000-0000-0000-000000000001"

    def az(*arguments):
        if arguments[:2] == ("account", "show"):
            return {"id": subscription}
        if arguments[:2] == ("cognitiveservices", "model"):
            return [
                {
                    "kind": "AIServices",
                    "model": {
                        "name": "gpt-5-mini",
                        "version": "2025-08-07",
                        "capabilities": {"area": "US"},
                        "skus": [{"name": "DataZoneStandard", "usageName": "online-quota"}],
                    },
                }
            ]
        if arguments[:2] == ("cognitiveservices", "usage"):
            return [{"name": {"value": "online-quota"}, "limit": 300, "currentValue": 0}]
        raise AssertionError(
            "Online-only mode must not check or claim unused orchestration services"
        )

    monkeypatch.setattr(module, "az", az)
    arguments = [
        "preflight.py",
        "--subscription",
        subscription,
        "--model",
        "gpt-5-mini",
        "--version",
        "2025-08-07",
    ]
    if online_only:
        arguments.append("--online-only")
    monkeypatch.setattr(sys, "argv", arguments)
    if online_only:
        module.main()
        assert '"batch_support_asserted": false' in capsys.readouterr().out
    else:
        with pytest.raises(SystemExit, match="does not list DataZoneBatch"):
            module.main()
