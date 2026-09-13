"""C4: a global module-level min-interval throttle between arxiv.org requests so
~20 back-to-back exploration ingests don't self-inflict a 429 storm. Env-tunable
via CORTEX_ARXIV_MIN_INTERVAL (default ~1.0s; 0 disables for tests). Cheap +
thread-safe-enough (a lock + last-call monotonic timestamp).
"""
import pytest

from cortex_research import arxiv_client


@pytest.fixture(autouse=True)
def _reset_throttle(monkeypatch):
    # Fresh clock state per test.
    monkeypatch.setattr(arxiv_client, "_last_arxiv_call", 0.0, raising=False)


def test_default_interval_is_about_one_second(monkeypatch):
    monkeypatch.delenv("CORTEX_ARXIV_MIN_INTERVAL", raising=False)
    assert 0.5 <= arxiv_client._arxiv_min_interval() <= 2.0


def test_env_overrides_interval(monkeypatch):
    monkeypatch.setenv("CORTEX_ARXIV_MIN_INTERVAL", "2.5")
    assert arxiv_client._arxiv_min_interval() == 2.5


def test_zero_interval_never_sleeps(monkeypatch):
    monkeypatch.setenv("CORTEX_ARXIV_MIN_INTERVAL", "0")
    slept = []
    monkeypatch.setattr(arxiv_client.time, "sleep", lambda s: slept.append(s))
    # A fake clock that doesn't advance — with interval 0 we still must not sleep.
    monkeypatch.setattr(arxiv_client.time, "monotonic", lambda: 100.0)
    arxiv_client._arxiv_throttle()
    arxiv_client._arxiv_throttle()
    assert slept == []


def test_two_rapid_calls_are_spaced_by_interval(monkeypatch):
    monkeypatch.setenv("CORTEX_ARXIV_MIN_INTERVAL", "1.0")
    clock = {"t": 1000.0}
    slept = []

    def _sleep(s):
        slept.append(s)
        clock["t"] += s  # sleeping advances the clock

    monkeypatch.setattr(arxiv_client.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(arxiv_client.time, "sleep", _sleep)

    # First call: no prior call -> no sleep.
    arxiv_client._arxiv_throttle()
    assert slept == []
    # Immediate second call (clock hasn't advanced): must sleep ~1.0s to space it.
    arxiv_client._arxiv_throttle()
    assert len(slept) == 1
    assert slept[0] == pytest.approx(1.0, abs=0.01)


def test_elapsed_gap_means_no_sleep(monkeypatch):
    monkeypatch.setenv("CORTEX_ARXIV_MIN_INTERVAL", "1.0")
    clock = {"t": 2000.0}
    slept = []
    monkeypatch.setattr(arxiv_client.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(arxiv_client.time, "sleep", lambda s: slept.append(s))
    arxiv_client._arxiv_throttle()
    clock["t"] += 5.0  # plenty of time passed
    arxiv_client._arxiv_throttle()
    assert slept == []  # gap already exceeds the interval


def test_throttle_is_invoked_by_get_with_retry(monkeypatch):
    """_get_with_retry must pass through the throttle gate before the HTTP call."""
    calls = []
    monkeypatch.setattr(arxiv_client, "_arxiv_throttle", lambda: calls.append(1))

    class _Resp:
        status_code = 200
        def raise_for_status(self): return None
    monkeypatch.setattr(arxiv_client.httpx, "get", lambda *a, **k: _Resp())
    arxiv_client._get_with_retry("https://export.arxiv.org/x", timeout=5.0)
    assert calls == [1]
