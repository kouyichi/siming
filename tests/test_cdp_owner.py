"""CDP 9222 监听者识别：不能凭"只有 IPv4 在听"就判 aTrust。"""

from __future__ import annotations

import io
import json

from siming import watchdog


class _FakeOpener:
    def __init__(self, payload: dict):
        self.payload = payload

    def open(self, url, timeout=None):
        return io.BytesIO(json.dumps(self.payload).encode())


def _patch(monkeypatch, payload):
    monkeypatch.setattr(watchdog.urllib.request, "build_opener", lambda *a, **k: _FakeOpener(payload))


def test_main_chromium_on_ipv4_only(monkeypatch):
    _patch(monkeypatch, {"Browser": "Chrome/155.0.8059.84", "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/155.0.0.0 Safari/537.36"})
    assert watchdog._identify_cdp_owner("127.0.0.1", 9222) == "chromium"


def test_atrust_detected(monkeypatch):
    _patch(monkeypatch, {"Browser": "Chrome/83.0.4103.122", "User-Agent": "Mozilla/5.0 aTrustTray/2.3 Chrome/83.0 Electron/9.4.4"})
    assert watchdog._identify_cdp_owner("127.0.0.1", 9222) == "atrust"


def test_unreachable_is_unknown(monkeypatch):
    class _Boom:
        def open(self, url, timeout=None):
            raise OSError("refused")

    monkeypatch.setattr(watchdog.urllib.request, "build_opener", lambda *a, **k: _Boom())
    assert watchdog._identify_cdp_owner("::1", 9222) == "unknown"
