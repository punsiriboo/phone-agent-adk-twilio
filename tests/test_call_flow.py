"""End-to-end call flow through the webhook and twilio_connect, with run_live stubbed."""

import asyncio
import base64
import json
import os
from urllib.parse import urlencode

os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")

import pytest
from fastapi.testclient import TestClient
from google.adk.events import Event
from google.genai import types
from starlette.datastructures import ImmutableMultiDict
from starlette.websockets import WebSocketDisconnect
from twilio.request_validator import RequestValidator

from adk_agent import live_messaging, main

CALL_SID = "CA123"
WEBHOOK_URL = "http://testserver/twilio/voice"


@pytest.fixture
def client():
    return TestClient(main.app)


def _start_message(token: str) -> dict:
    return {
        "event": "start",
        "start": {
            "streamSid": "MZ1",
            "callSid": CALL_SID,
            "customParameters": {"token": token},
        },
    }


def test_webhook_rejects_unsigned_request(client):
    assert client.post("/twilio/voice", data={"CallSid": CALL_SID}).status_code == 403


def test_webhook_returns_stream_twiml(client):
    params = {"CallSid": CALL_SID}
    signature = RequestValidator("test-token").compute_signature(WEBHOOK_URL, params)
    response = client.post(
        "/twilio/voice", data=params, headers={"X-Twilio-Signature": signature}
    )
    assert response.status_code == 200
    assert '<Stream url="ws://testserver/twilio/stream">' in response.text
    assert main._stream_token(CALL_SID) in response.text
    assert "<Hangup />" in response.text


def test_webhook_signs_repeated_fields(client):
    fields = [("CallSid", CALL_SID), ("Tag", "a"), ("Tag", "b")]
    signature = RequestValidator("test-token").compute_signature(
        WEBHOOK_URL, ImmutableMultiDict(fields)
    )
    response = client.post(
        "/twilio/voice",
        content=urlencode(fields),
        headers={
            "X-Twilio-Signature": signature,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    assert response.status_code == 200


def test_stream_rejects_bad_token(client):
    with client.websocket_connect("/twilio/stream") as ws:
        ws.send_text(json.dumps({"event": "connected"}))
        ws.send_text(json.dumps(_start_message("forged")))
        with pytest.raises(WebSocketDisconnect) as exc:
            ws.receive_text()
    assert exc.value.code == 1008


def test_call_round_trip(client, monkeypatch):
    received = []

    async def fake_run_live(*, live_request_queue, **kwargs):
        received.append(await live_request_queue.get())  # greeting
        received.append(await live_request_queue.get())  # caller audio
        pcm_24k = b"\x00\x10" * 2400  # 100 ms
        yield Event(
            author="agent",
            content=types.Content(
                role="model",
                parts=[types.Part(inline_data=types.Blob(data=pcm_24k, mime_type="audio/pcm;rate=24000"))],
            ),
        )
        yield Event(author="agent", interrupted=True)
        yield Event(
            author="agent",
            content=types.Content(
                role="model",
                parts=[types.Part(function_call=types.FunctionCall(name="end_call", args={}))],
            ),
        )
        await asyncio.Event().wait()

    monkeypatch.setattr(live_messaging.runner, "run_live", fake_run_live)

    with client.websocket_connect("/twilio/stream") as ws:
        ws.send_text(json.dumps({"event": "connected"}))
        ws.send_text(json.dumps(_start_message(main._stream_token(CALL_SID))))
        # The streaming resampler holds back its filter delay, so send a few frames.
        ulaw_20ms = base64.b64encode(b"\xff" * 160).decode()
        for _ in range(10):
            ws.send_text(json.dumps({"event": "media", "media": {"payload": ulaw_20ms}}))

        media = json.loads(ws.receive_text())
        assert media["event"] == "media" and media["streamSid"] == "MZ1"
        assert json.loads(ws.receive_text())["event"] == "clear"
        mark = json.loads(ws.receive_text())
        assert mark["event"] == "mark" and mark["mark"]["name"] == "end_call"

        # Twilio echoes the mark once the goodbye has played; the server hangs up.
        ws.send_text(json.dumps({"event": "mark", "mark": {"name": "end_call"}}))
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()

    greeting, caller_audio = received
    assert greeting.content.parts[0].text == "The caller has just picked up. Greet them briefly."
    assert caller_audio.blob.mime_type == "audio/pcm;rate=16000"

    session = asyncio.run(
        live_messaging.runner.session_service.get_session(
            app_name=live_messaging.APP_NAME, user_id=CALL_SID, session_id=CALL_SID
        )
    )
    assert session is None
