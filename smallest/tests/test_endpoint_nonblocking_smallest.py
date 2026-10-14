"""The audio pump must never block on a finalize ack.

Smallest's own reference client runs audio-sending and response-reading as two
independent tasks and never pauses the sender to wait on a reply — confirmed
against docs.smallest.ai before this fix. The original version of
``_pump_actor_to_stt`` awaited ``_endpoint()`` inline, which itself waits up to
``FINALIZE_TIMEOUT_S`` for Pulse to acknowledge a finalize request — stalling
audio ingestion, Pulse forwarding, and barge-in detection for that whole
window, on every single turn. This test simulates a finalize that never acks
and asserts the pump keeps forwarding frames anyway.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.websockets import WebSocketDisconnect

from rory_smallest import agent

# 20ms of PCM16 mono at the actor's rate, matching the real audio pump's frames.
FRAME_BYTES = int(agent.ACTOR_RATE_HZ * 0.02) * 2
FRAME_SILENCE_S = FRAME_BYTES / 2 / agent.ACTOR_RATE_HZ
# Frames of silence needed to cross END_OF_TURN_S, plus a few more to prove
# the pump keeps going past the trigger.
FRAMES_TO_ENDPOINT = int(agent.END_OF_TURN_S / FRAME_SILENCE_S) + 1
EXTRA_FRAMES_AFTER = 5


class _FakeVad:
    """Replays a fixed speech/silence pattern instead of scoring real audio."""

    def __init__(self, pattern):
        self._pattern = list(pattern)

    def push(self, _frame: bytes) -> bool:
        return self._pattern.pop(0) if self._pattern else False


class _FakeActorWS:
    def __init__(self, frames):
        self._frames = list(frames)

    async def receive_bytes(self) -> bytes:
        # A real socket read suspends here; without a genuine yield point the
        # whole pump loop runs synchronously and the _endpointing task this
        # test exists to check never gets a turn to run before cleanup cancels it.
        await asyncio.sleep(0)
        if not self._frames:
            raise WebSocketDisconnect()
        return self._frames.pop(0)


class _FakeSTTSocket:
    """Records every frame/control message sent; never acknowledges finalize."""

    def __init__(self):
        self.sent: list = []

    async def send(self, payload) -> None:
        self.sent.append(payload)


def test_pump_keeps_forwarding_frames_while_finalize_is_unacknowledged(monkeypatch):
    monkeypatch.setattr(agent, "FINALIZE_TIMEOUT_S", 5.0)  # would dwarf the test's own budget if still blocking

    n_frames = FRAMES_TO_ENDPOINT + EXTRA_FRAMES_AFTER
    frames = [bytes(FRAME_BYTES) for _ in range(n_frames)]

    call = agent._Call(_FakeActorWS(frames))
    call._vad = _FakeVad([True] + [False] * n_frames)  # one voiced frame opens the turn
    call._stt = _FakeSTTSocket()
    # _finalize_ack is never set — the real Pulse response is never simulated.

    async def scenario():
        # One event loop for both the pump run and cleaning up the endpoint
        # task it schedules — a Task can't be awaited from a different loop.
        # _pump_actor_to_stt catches WebSocketDisconnect itself (logs and
        # returns) rather than propagating it, so running out of frames ends
        # the pump cleanly.
        try:
            await asyncio.wait_for(call._pump_actor_to_stt(), timeout=1.0)
        finally:
            if call._endpointing is not None:
                call._endpointing.cancel()
                await asyncio.gather(call._endpointing, return_exceptions=True)

    try:
        asyncio.run(scenario())
    except TimeoutError:
        pytest.fail(
            "the audio pump stalled waiting on the finalize ack instead of "
            "continuing to forward frames — _endpoint() must be fire-and-forget"
        )

    assert len(call._stt.sent) == n_frames + 1  # every frame, plus the finalize control message
    assert json.dumps({"type": "finalize"}) in call._stt.sent
    assert call._endpointing is not None


def test_a_second_pause_does_not_schedule_an_overlapping_endpoint(monkeypatch):
    """Resume-then-pause-again before the first finalize acks must not double-fire.

    Two concurrent _endpoint() calls would both await the same _finalize_ack
    and could both wake on a single ack, each reading/clearing
    _latest_transcript and potentially both dispatching a turn — a duplicate
    or corrupted turn. Only one finalize should ever be in flight at a time.
    """
    monkeypatch.setattr(agent, "FINALIZE_TIMEOUT_S", 5.0)

    silence = [False] * FRAMES_TO_ENDPOINT
    # voiced, pause past the threshold, resume, pause past the threshold again —
    # all before the first finalize could plausibly have acked.
    pattern = [True] + silence + [True] + silence + [False] * EXTRA_FRAMES_AFTER
    frames = [bytes(FRAME_BYTES) for _ in pattern]

    call = agent._Call(_FakeActorWS(frames))
    call._vad = _FakeVad(pattern)
    call._stt = _FakeSTTSocket()
    # _finalize_ack is never set, simulating the first finalize still pending
    # when the second pause would otherwise have triggered another one.

    async def scenario():
        try:
            await asyncio.wait_for(call._pump_actor_to_stt(), timeout=1.0)
        finally:
            if call._endpointing is not None:
                call._endpointing.cancel()
                await asyncio.gather(call._endpointing, return_exceptions=True)

    try:
        asyncio.run(scenario())
    except TimeoutError:
        pytest.fail("the pump stalled — see test_pump_keeps_forwarding_frames_...")

    finalize_count = call._stt.sent.count(json.dumps({"type": "finalize"}))
    assert finalize_count == 1, (
        f"expected exactly one finalize in flight, got {finalize_count} — "
        "the second pause scheduled an overlapping _endpoint()"
    )
