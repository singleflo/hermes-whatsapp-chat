import pytest


@pytest.fixture(autouse=True)
def _no_service_autostart(monkeypatch):
    # plugin_api would otherwise refresh/restart the real LaunchAgent on import.
    monkeypatch.setenv("HWC_NO_AUTOSTART", "1")
