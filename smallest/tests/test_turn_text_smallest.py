"""A turn's text is Pulse's latest ``is_final`` transcript, not a concatenation.

Verified live (2026-10-14) by looping Lightning's own TTS output back into
Pulse: each ``is_final`` event restates the *complete* utterance so far, not a
new fragment since the previous final. Appending finals together — the
original design, written from the docs before this was checked against the
real API — would duplicate the caller's words whenever Pulse's own
trailing-silence or word-count thresholds finalize part of a long utterance
before this transport's own VAD-driven endpoint does.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from rory_smallest import agent


class _FakeSTT:
    """Pulse's WebSocket: a fixed list of events, plus a hook on ``send``."""

    def __init__(self, messages=(), on_send=None):
        self._messages = list(messages)
        self.sent: list[str] = []
        self._on_send = on_send

    async def send(self, payload: str) -> None:
        self.sent.append(payload)
        if self._on_send is not None:
            self._on_send(payload)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)


def _event(is_final: bool, transcript: str) -> str:
    return json.dumps({"type": "transcription", "is_final": is_final, "transcript": transcript})


def test_is_final_events_overwrite_rather_than_append():
    call = agent._Call(SimpleNamespace())
    call._stt = _FakeSTT([
        _event(False, "This is"),
        _event(True, "This is a test"),
        _event(True, "This is a test of everything"),
        _event(True, ""),  # e.g. close_stream's empty ack — must not clobber
    ])

    with pytest.raises(RuntimeError, match="closed the stream"):
        asyncio.run(call._pump_stt_events())

    assert call._latest_transcript == "This is a test of everything"
    assert call._finalize_ack.is_set()


def test_endpoint_dispatches_the_latest_transcript_and_requests_finalize():
    call = agent._Call(SimpleNamespace())
    call._latest_transcript = "already captured text"
    call._stt = _FakeSTT(on_send=lambda _payload: call._finalize_ack.set())

    asyncio.run(call._endpoint())

    assert json.loads(call._stt.sent[0]) == {"type": "finalize"}
    assert call._turns.get_nowait() == "already captured text"
    assert call._latest_transcript == ""


def test_endpoint_falls_back_to_whatever_arrived_if_finalize_never_acks(monkeypatch):
    monkeypatch.setattr(agent, "FINALIZE_TIMEOUT_S", 0.05)
    call = agent._Call(SimpleNamespace())
    call._latest_transcript = "partial before the timeout"
    call._stt = _FakeSTT()  # never acknowledges

    asyncio.run(call._endpoint())

    assert call._turns.get_nowait() == "partial before the timeout"


def test_endpoint_dispatches_nothing_on_an_empty_turn():
    call = agent._Call(SimpleNamespace())
    call._stt = _FakeSTT(on_send=lambda _payload: call._finalize_ack.set())

    asyncio.run(call._endpoint())

    assert call._turns.empty()
