# rory-smallest

Rory on Smallest AI. Transport only — the tools, vendor clients and prompt
live in rory-core.

Smallest ships the three legs of a voice agent as separate products rather
than one session, so this process is the pipeline:

```
actor 24 kHz ─► Pulse STT ─► Electron (chat model) + Rory's tools ─► Lightning TTS ─► actor
        └─► Silero VAD ─► barge-in: cancel the TTS connection
```

One `SMALLEST_API_KEY` covers all three legs. Pulse's live endpoint and
Lightning both accept 24 kHz directly — the actor's own rate — so unlike the
Mistral and Hugging Face cascades, no audio is resampled anywhere in this
transport.

| Env | Default |
|---|---|
| `SMALLEST_API_KEY` | required — Pulse, Electron and Lightning, boot fails without it |
| `STRIPE_API_KEY` | required — sandbox fixture; the billing twin's credential |
| `SMALLEST_LLM_MODEL` | `electron` |
| `SMALLEST_TTS_MODEL` | `lightning_v3.1_pro` — the premium pool: broadcast-quality voices, dedicated inference capacity, no cloning |
| `SMALLEST_VOICE` | `kelsey` — must be a voice in the Pro pool |
| `SMALLEST_LANGUAGE` | `en` |

## Pipeline

**STT.** One Pulse WebSocket (`wss://api.smallest.ai/waves/v1/pulse/get_text`)
held open for the whole call. Caller audio goes up unconverted; Pulse finalizes
on its own trailing-silence and word-count thresholds as well as on a
client-sent `{"type": "finalize"}`. Verified live (looping Lightning's own TTS
output back into Pulse): each `is_final` event's `transcript` is the complete
utterance restated so far, not a new fragment since the previous final, so the
turn's text is the *latest* `is_final` transcript, not a concatenation of
several — see `_endpoint` and `_pump_stt_events` in `agent.py`.

**LLM.** A non-streaming Electron chat completion per round, with Rory's tools
registered in the standard OpenAI function-calling shape. Electron can also
return a spoken filler alongside `tool_calls` when prompted for one, but this
transport does not use that: the shared `agent_desc.txt` is loaded verbatim
(see `rory_tools.parity.assert_shares_prompt_and_greeting`), and a transport
does not get to ask the model for different behaviour than the others are
asked for.

**TTS.** Lightning's unified streaming WebSocket (`wss://api.smallest.ai/waves/v1/tts/live`,
`model` as a body field rather than part of the URL — the older per-model path
Smallest's docs flag as deprecated) takes one utterance per connection — there
is no documented way to abandon an utterance mid-stream and reuse the socket —
so this transport opens a fresh connection per reply and closes it once
Lightning reports `"status": "complete"`. Audio chunks are decoded and
forwarded to the actor as they arrive.

## Turn-taking and barge-in

**End of turn** is Silero VAD (`rory_tools.vad`) on the caller's raw audio,
the same model and 0.8 s silence threshold as the Pipecat and Gemini Live
candidates, for the reason the Mistral cascade documents: Pulse's own
transcript timing says nothing about whether the caller has actually stopped,
and the bench mixes background audio (cafe, television) under some callers
that would defeat a frame-energy gate.

**Barge-in** cancels the in-flight Lightning connection. Because Lightning
gives no per-word timing on the plain `chunk` stream, the interrupted reply is
trimmed proportionally — PCM bytes sent to the actor, in ms, against the
reply's word count at `TTS_MS_PER_WORD` — and the kept words are marked
`… [interrupted by the caller]` in the model's history, so the next turn
reasons from what the caller actually heard. `TTS_MS_PER_WORD` is a nominal
estimate, not benchmarked against the deployed voice; Lightning's WebSocket
supports exact `word_timestamps` for English and Hindi voices, which would
replace the estimate with an exact cut and is worth adding once this
transport has a benchmarked voice to measure against.

## Files

- `rory_smallest/web.py` — FastAPI app: `GET /health`, `WS /voice` (raw PCM16, 24 kHz mono).
- `rory_smallest/agent.py` — per call: the Pulse socket, the audio pump, the turn worker, Lightning playback.
- `rory_smallest/tools.py` — `ToolSchema` → OpenAI function-calling shape, plus `tool_surface()` for the parity check.
