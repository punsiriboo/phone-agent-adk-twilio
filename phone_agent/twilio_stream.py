"""Bridge between one Twilio Media Stream and the ADK live session.

Twilio protocol: https://www.twilio.com/docs/voice/media-streams/websocket-messages
Twilio carries 8 kHz mono μ-law. `audioop` was removed in Python 3.13, so the
μ-law codec is implemented here with numpy.
"""

import base64
import json
import logging

import numpy as np
import soxr
from fastapi import WebSocket, WebSocketDisconnect
from google.adk.agents.live_request_queue import LiveRequestQueue

from .live_messaging import (
    AGENT_INPUT_RATE,
    AGENT_OUTPUT_RATE,
    AgentDataEvent,
    AgentEvent,
    AgentHangUpEvent,
    AgentInterruptedEvent,
    AgentTranscriptEvent,
    send_pcm_to_agent,
    text_to_content,
)

log = logging.getLogger(__name__)

TWILIO_RATE = 8000

# Sent as a mark after the goodbye audio; Twilio echoes it back once played.
HANGUP_MARK = "end_call"

GREETING_PROMPT = "The caller has just picked up. Greet them briefly."


# --- G.711 μ-law <-> 16-bit PCM, vectorised with lookup tables ---------------

_BIAS = 0x84
_BIAS_14 = 0x21
_CLIP_14 = 8159
_SEGMENT_END = np.array([0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF])


def _build_decode_table() -> np.ndarray:
    u = ~np.arange(256, dtype=np.uint8)
    sign = u & 0x80
    exponent = (u >> 4).astype(np.int32) & 0x07
    mantissa = u.astype(np.int32) & 0x0F
    sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
    return np.where(sign, -sample, sample).astype(np.int16)


def _build_encode_table() -> np.ndarray:
    # 14-bit CCITT G.711 encoder, bit-exact with CPython's audioop.lin2ulaw.
    pcm = np.arange(-32768, 32768, dtype=np.int32) >> 2
    mask = np.where(pcm < 0, 0x7F, 0xFF)
    magnitude = np.minimum(np.abs(pcm), _CLIP_14) + _BIAS_14
    segment = np.searchsorted(_SEGMENT_END, magnitude)
    ulaw = (np.minimum(segment, 7) << 4) | ((magnitude >> (segment + 1)) & 0x0F)
    ulaw = np.where(segment >= 8, 0x7F, ulaw)
    return (ulaw ^ mask).astype(np.uint8)


_DECODE = _build_decode_table()
_ENCODE = _build_encode_table()  # indexed by pcm + 32768


def ulaw_to_pcm16(data: bytes) -> np.ndarray:
    """Decode μ-law bytes to int16 samples."""
    return _DECODE[np.frombuffer(data, dtype=np.uint8)]


def pcm16_to_ulaw(samples: np.ndarray) -> bytes:
    """Encode int16 samples to μ-law bytes."""
    return _ENCODE[samples.astype(np.int32) + 32768].tobytes()


# --- Call bridge -------------------------------------------------------------


def _downsampler() -> soxr.ResampleStream:
    return soxr.ResampleStream(AGENT_OUTPUT_RATE, TWILIO_RATE, 1, dtype="int16")


class TwilioCall:
    """Converts audio and events between Twilio (8 kHz μ-law) and ADK (16/24 kHz PCM)."""

    def __init__(self, websocket: WebSocket, stream_sid: str, call_sid: str):
        self._websocket = websocket
        self.stream_sid = stream_sid
        self.call_sid = call_sid
        # Streaming resamplers keep filter state across chunks, so 20 ms frames
        # join without clicks.
        self._upsampler = soxr.ResampleStream(
            TWILIO_RATE, AGENT_INPUT_RATE, 1, dtype="int16"
        )
        self._downsampler = _downsampler()
        self._transcripts: dict[str, list[str]] = {"user": [], "agent": []}

    async def client_to_agent(self, live_request_queue: LiveRequestQueue) -> None:
        """Feeds caller audio to the agent. Returns when the call should end."""
        live_request_queue.send_content(text_to_content(GREETING_PROMPT))

        while True:
            try:
                message = json.loads(await self._websocket.receive_text())
            except WebSocketDisconnect:
                log.info("call %s: stream disconnected", self.call_sid)
                return

            event = message.get("event")
            if event == "media":
                ulaw = base64.b64decode(message["media"]["payload"])
                pcm = self._upsampler.resample_chunk(ulaw_to_pcm16(ulaw))
                if pcm.size:
                    send_pcm_to_agent(pcm.tobytes(), live_request_queue)
            elif event == "mark" and message["mark"]["name"] == HANGUP_MARK:
                log.info("call %s: goodbye played, hanging up", self.call_sid)
                return
            elif event == "stop":
                log.info("call %s: caller hung up", self.call_sid)
                return

    async def on_agent_event(self, event: AgentEvent) -> None:
        """Sends agent output to Twilio."""
        match event:
            case AgentDataEvent():
                pcm = self._downsampler.resample_chunk(
                    np.frombuffer(event.payload, dtype=np.int16)
                )
                if pcm.size:
                    payload = base64.b64encode(pcm16_to_ulaw(pcm)).decode()
                    await self._send({"event": "media", "media": {"payload": payload}})
            case AgentInterruptedEvent():
                # Barge-in: drop audio Twilio has buffered but not yet played.
                self._downsampler = _downsampler()
                await self._send({"event": "clear"})
            case AgentTranscriptEvent():
                self._transcripts[event.role].append(event.text)
                if event.finished:
                    text = "".join(self._transcripts[event.role]).strip()
                    self._transcripts[event.role].clear()
                    # Transcripts are caller PII: keep them out of default logs.
                    log.debug("call %s %s: %s", self.call_sid, event.role, text)
            case AgentHangUpEvent():
                await self._send({"event": "mark", "mark": {"name": HANGUP_MARK}})

    async def _send(self, message: dict) -> None:
        await self._websocket.send_text(
            json.dumps({**message, "streamSid": self.stream_sid})
        )
