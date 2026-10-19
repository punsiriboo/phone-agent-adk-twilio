"""Bridge between the browser test client (frontend/) and the ADK live session.

Lets you talk to the agent without a phone. Protocol over one WebSocket:
  browser -> server  binary: 16-bit mono PCM at AGENT_INPUT_RATE (mic)
  server -> browser  binary: 16-bit mono PCM at AGENT_OUTPUT_RATE (agent voice)
                     JSON:   {"type": "transcript", "role", "text", "finished"}
                             {"type": "interrupted"}  stop playing queued voice
                             {"type": "hangup"}       agent said goodbye
"""

import logging

from fastapi import WebSocket
from google.adk.agents.live_request_queue import LiveRequestQueue

from .live_messaging import (
    GREETING_PROMPT,
    AgentDataEvent,
    AgentEvent,
    AgentHangUpEvent,
    AgentInterruptedEvent,
    AgentTranscriptEvent,
    send_pcm_to_agent,
    text_to_content,
)

log = logging.getLogger(__name__)


class BrowserCall:
    """Passes PCM and events between the browser and ADK; no conversion needed."""

    def __init__(self, websocket: WebSocket, call_id: str):
        self._websocket = websocket
        self.call_id = call_id

    async def client_to_agent(self, live_request_queue: LiveRequestQueue) -> None:
        """Feeds mic audio to the agent. Returns when the browser disconnects."""
        live_request_queue.send_content(text_to_content(GREETING_PROMPT))

        while True:
            message = await self._websocket.receive()
            if message["type"] == "websocket.disconnect":
                log.info("web call %s: browser disconnected", self.call_id)
                return
            if message.get("bytes"):
                send_pcm_to_agent(message["bytes"], live_request_queue)

    async def on_agent_event(self, event: AgentEvent) -> None:
        """Sends agent output to the browser."""
        match event:
            case AgentDataEvent():
                await self._websocket.send_bytes(event.payload)
            case AgentInterruptedEvent():
                await self._websocket.send_json({"type": "interrupted"})
            case AgentTranscriptEvent():
                await self._websocket.send_json(
                    {
                        "type": "transcript",
                        "role": event.role,
                        "text": event.text,
                        "finished": event.finished,
                    }
                )
            case AgentHangUpEvent():
                await self._websocket.send_json({"type": "hangup"})
