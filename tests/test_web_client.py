"""Browser test client: static files and the /web/ws call, with run_live stubbed."""

import pytest
from fastapi.testclient import TestClient
from google.adk.events import Event
from google.genai import types
from starlette.websockets import WebSocketDisconnect

from adk_agent import live_messaging, main


def test_serves_frontend():
    response = TestClient(main.app).get("/web/")
    assert response.status_code == 200
    assert '<script src="main.js">' in response.text


def test_web_call_round_trip(monkeypatch):
    received = []

    async def fake_run_live(*, live_request_queue, **kwargs):
        received.append(await live_request_queue.get())  # greeting
        received.append(await live_request_queue.get())  # mic audio
        yield Event(
            author="agent",
            content=types.Content(
                role="model",
                parts=[types.Part(inline_data=types.Blob(data=b"\x00\x10" * 240, mime_type="audio/pcm;rate=24000"))],
            ),
        )
        yield Event(
            author="agent",
            output_transcription=types.Transcription(text="Hello!", finished=True),
        )
        yield Event(author="agent", interrupted=True)
        yield Event(
            author="agent",
            content=types.Content(
                role="model",
                parts=[types.Part(function_call=types.FunctionCall(name="end_call", args={}))],
            ),
        )
        # The live stream ends here, so run_call returns and the server closes.

    monkeypatch.setattr(live_messaging.runner, "run_live", fake_run_live)

    with TestClient(main.app).websocket_connect("/web/ws") as ws:
        ws.send_bytes(b"\x00\x00" * 320)
        assert ws.receive_bytes() == b"\x00\x10" * 240
        assert ws.receive_json() == {
            "type": "transcript", "role": "agent", "text": "Hello!", "finished": True,
        }
        assert ws.receive_json() == {"type": "interrupted"}
        assert ws.receive_json() == {"type": "hangup"}
        with pytest.raises(WebSocketDisconnect):
            ws.receive_bytes()

    greeting, mic_audio = received
    assert greeting.content.parts[0].text == live_messaging.GREETING_PROMPT
    assert mic_audio.blob.data == b"\x00\x00" * 320
