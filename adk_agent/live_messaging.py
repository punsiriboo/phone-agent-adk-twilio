"""ADK live session for one call: RunConfig, run_live events -> AgentEvent, call lifecycle."""

import asyncio
import logging
import os
from typing import AsyncGenerator, Awaitable, Callable, Literal

from google.adk.agents.live_request_queue import LiveRequestQueue
from google.adk.agents.run_config import RunConfig
from google.adk.events import Event
from google.adk.runners import InMemoryRunner
from google.genai import types
from google.genai.types import Blob, Content, Part
from pydantic import BaseModel, Field

from .agent import root_agent

log = logging.getLogger(__name__)


def text_to_content(text: str, role: Literal["user", "model"] = "user") -> Content:
    """Helper to create a Content object from text"""
    return Content(role=role, parts=[Part(text=text)])


APP_NAME = "THE VOICE AGENT"

# Sent as the first user turn so the agent speaks first, like answering a phone.
GREETING_PROMPT = "The caller has just picked up. Greet them briefly."

# Gemini Live audio formats: 16-bit mono PCM.
AGENT_INPUT_RATE = 16000
AGENT_OUTPUT_RATE = 24000

# max_llm_calls does not apply to run_live(), so a live call has no built-in
# cost ceiling. This is the hard cap on how long one phone call may last.
MAX_CALL_SECONDS = int(os.getenv("MAX_CALL_SECONDS", "600"))

LiveEvents = AsyncGenerator[Event, None]

# One runner per process; each call gets its own session.
runner = InMemoryRunner(root_agent, app_name=APP_NAME)

automatic_activity_detection = types.AutomaticActivityDetection(
    disabled=False,
    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_HIGH,
    prefix_padding_ms=150,
    silence_duration_ms=400,
)

# Voice and language live on the agent's Gemini model (agent.py).
# streaming_mode is not set: run_live() ignores it in Python.
RUN_CONFIG = RunConfig(
    session_resumption=types.SessionResumptionConfig(),
    context_window_compression=types.ContextWindowCompressionConfig(
        sliding_window=types.SlidingWindow()
    ),
    # On by default; explicit because AgentTranscriptEvent depends on them.
    input_audio_transcription=types.AudioTranscriptionConfig(),
    output_audio_transcription=types.AudioTranscriptionConfig(),
    realtime_input_config=types.RealtimeInputConfig(
        automatic_activity_detection=automatic_activity_detection
    ),
)


async def start_agent_session(
    user_id: str, session_id: str
) -> tuple[LiveEvents, LiveRequestQueue]:
    """Starts an agent session"""

    await runner.session_service.create_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=session_id,
    )

    live_request_queue = LiveRequestQueue()

    live_events = runner.run_live(
        user_id=user_id,
        session_id=session_id,
        live_request_queue=live_request_queue,
        run_config=RUN_CONFIG,
    )
    return live_events, live_request_queue


class AgentInterruptedEvent(BaseModel):
    type: Literal["interrupted"] = "interrupted"
    timestamp: float = Field(description="Unix timestamp of interruption")


class AgentDataEvent(BaseModel):
    payload: bytes = Field(description="Output PCM bytes (16-bit, AGENT_OUTPUT_RATE)")
    type: Literal["data"] = "data"


class AgentTranscriptEvent(BaseModel):
    type: Literal["transcript"] = "transcript"
    role: Literal["user", "agent"]
    text: str = Field(description="Latest fragment; concatenate fragments per turn")
    finished: bool = Field(description="True on the last fragment of the turn")


class AgentHangUpEvent(BaseModel):
    """The agent called end_call.

    Arrives while goodbye audio may still be queued on the caller's side, so the
    client should hang up after that audio has played (e.g. on a Twilio mark).
    """

    type: Literal["hangup"] = "hangup"
    timestamp: float = Field(description="Unix timestamp of the end_call request")


AgentEvent = (
    AgentInterruptedEvent
    | AgentDataEvent
    | AgentTranscriptEvent
    | AgentHangUpEvent
)

OnAgentEvent = Callable[[AgentEvent], Awaitable[None]]


async def agent_to_client_messaging(
    on_agent_event: OnAgentEvent, live_events: LiveEvents
) -> None:
    """Converts ADK live events to AgentEvents and passes each to on_agent_event."""
    async for event in live_events:
        message: AgentEvent

        # Transcripts arrive separately from event.content.
        for role, transcription in (
            ("user", event.input_transcription),
            ("agent", event.output_transcription),
        ):
            # The final fragment can be empty but still marks the turn's end.
            if transcription and (transcription.text or transcription.finished):
                message = AgentTranscriptEvent(
                    role=role,
                    text=transcription.text or "",
                    finished=bool(transcription.finished),
                )
                await on_agent_event(message)

        if event.turn_complete:
            continue

        if event.interrupted:
            message = AgentInterruptedEvent(timestamp=event.timestamp)
            await on_agent_event(message)
            continue

        if not event.content or not event.content.parts:
            continue

        for part in event.content.parts:
            is_audio = (
                part.inline_data
                and part.inline_data.mime_type
                and part.inline_data.mime_type.startswith("audio/pcm")
            )

            if is_audio:
                audio_data = part.inline_data and part.inline_data.data
                if not audio_data:
                    continue
                message = AgentDataEvent(payload=audio_data)
                await on_agent_event(message)
                continue

            # A tool's return value goes to the model, not the client, so the
            # hang-up is bridged here when the function_call streams past.
            if part.function_call and part.function_call.name == "end_call":
                message = AgentHangUpEvent(timestamp=event.timestamp)
                await on_agent_event(message)
                continue

            if part.text is not None or part.function_call or part.function_response:
                continue

            log.debug("unhandled event content part: %s", part)


def send_pcm_to_agent(pcm_audio: bytes, live_request_queue: LiveRequestQueue) -> None:
    """Sends caller audio (16-bit PCM at AGENT_INPUT_RATE) to the agent."""
    live_request_queue.send_realtime(
        Blob(data=pcm_audio, mime_type=f"audio/pcm;rate={AGENT_INPUT_RATE}")
    )


async def run_call(
    user_id: str,
    session_id: str,
    client_to_agent: Callable[[LiveRequestQueue], Awaitable[None]],
    on_agent_event: OnAgentEvent,
    max_seconds: float = MAX_CALL_SECONDS,
) -> None:
    """
    Runs one phone call end to end.

    Ends when either direction finishes (caller hung up, agent stream ended) or
    after max_seconds, then closes the live session cleanly.

    Args:
        user_id: ADK user id for the call.
        session_id: ADK session id, e.g. the Twilio Call SID.
        client_to_agent: Reads from the client and calls send_pcm_to_agent; returns when the caller hangs up.
        on_agent_event: Async callback invoked per AgentEvent.
        max_seconds: Hard cap on call duration.
    """
    live_events, live_request_queue = await start_agent_session(user_id, session_id)

    upstream = asyncio.create_task(
        client_to_agent(live_request_queue), name="client_to_agent"
    )
    downstream = asyncio.create_task(
        agent_to_client_messaging(on_agent_event, live_events), name="agent_to_client"
    )
    try:
        done, _ = await asyncio.wait(
            {upstream, downstream},
            timeout=max_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not done:
            log.warning("call %s hit the %ss limit", session_id, max_seconds)
    finally:
        # Close the queue first so run_live winds down before anything is cancelled.
        live_request_queue.close()
        for task in (upstream, downstream):
            if not task.done():
                task.cancel()
        results = await asyncio.gather(upstream, downstream, return_exceptions=True)
        for task, result in zip((upstream, downstream), results):
            if isinstance(result, Exception):
                log.error("%s failed", task.get_name(), exc_info=result)
        # InMemorySessionService keeps every session; drop it so a long-running
        # server doesn't grow per call.
        await runner.session_service.delete_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
