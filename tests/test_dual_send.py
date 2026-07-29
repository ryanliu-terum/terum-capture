"""Best-effort dual-send mirror tests (_post_to_mirrors + its _post_events wiring).

Load-bearing properties: the mirror runs ONLY after the primary send succeeded and the
sidecar offset is persisted; a mirror failure of any kind can never change the primary
result, the offset, or raise; malformed targets are skipped.
"""
import json

from terum_capture import upload
from terum_capture.upload import _post_events

PRIMARY = {"api_key": "trm_prod", "api_url": "https://api.terum.ai/api"}
MIRROR = {"api_key": "trm_stg", "api_url": "https://api-staging.terum.ai/api"}
EVENTS = [{"conversation_id": "c1", "prompt": "p", "response": "r"}]


class Resp:
    def __init__(self, code=200):
        self.status_code = code


def _run(monkeypatch, tmp_path, config, post):
    monkeypatch.setattr(upload.httpx, "post", post)
    sidecar = tmp_path / "sidecar.json"
    result = _post_events(config, EVENTS, sidecar, 7, "owner/repo", None)
    return result, sidecar


def test_mirror_receives_same_events_after_primary_success(monkeypatch, tmp_path):
    calls = []

    def post(url, json=None, headers=None, timeout=None):
        calls.append((url, headers["Authorization"], json))
        return Resp()

    config = {**PRIMARY, "extra_targets": [MIRROR]}
    result, sidecar = _run(monkeypatch, tmp_path, config, post)

    assert result.status == "uploaded"
    assert [c[0] for c in calls] == [
        "https://api.terum.ai/api/ingest/llm-history",
        "https://api-staging.terum.ai/api/ingest/llm-history",
    ]
    assert calls[1][1] == "Bearer trm_stg"       # mirror auths with ITS key
    assert calls[1][2] == calls[0][2]            # identical payload
    assert sidecar.exists()                      # offset persisted before mirror ran


def test_primary_failure_skips_mirror_entirely(monkeypatch, tmp_path):
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        return Resp(500)

    config = {**PRIMARY, "extra_targets": [MIRROR]}
    result, sidecar = _run(monkeypatch, tmp_path, config, post)

    assert result.status == "failed"
    assert calls == ["https://api.terum.ai/api/ingest/llm-history"]  # mirror never tried
    assert not sidecar.exists()


def test_mirror_exception_swallowed_result_unchanged(monkeypatch, tmp_path, capsys):
    def post(url, **kwargs):
        if "staging" in url:
            raise RuntimeError("mirror down")
        return Resp()

    config = {**PRIMARY, "extra_targets": [MIRROR]}
    result, sidecar = _run(monkeypatch, tmp_path, config, post)

    assert result.status == "uploaded"           # primary outcome untouched
    assert sidecar.exists()
    assert "mirror" in capsys.readouterr().err   # warned, not raised


def test_mirror_non_200_warns_and_result_unchanged(monkeypatch, tmp_path, capsys):
    def post(url, **kwargs):
        return Resp(503) if "staging" in url else Resp()

    config = {**PRIMARY, "extra_targets": [MIRROR]}
    result, _ = _run(monkeypatch, tmp_path, config, post)

    assert result.status == "uploaded"
    assert "503" in capsys.readouterr().err


def test_malformed_and_absent_targets_skipped(monkeypatch, tmp_path):
    calls = []

    def post(url, **kwargs):
        calls.append(url)
        return Resp()

    config = {**PRIMARY, "extra_targets": [{"api_url": "https://x"}, {"api_key": "trm_y"}]}
    result, _ = _run(monkeypatch, tmp_path, config, post)
    assert result.status == "uploaded"
    assert len(calls) == 1                       # primary only — both targets malformed

    calls.clear()
    result, _ = _run(monkeypatch, tmp_path, PRIMARY, post)  # no extra_targets key at all
    assert result.status == "uploaded"
    assert len(calls) == 1
