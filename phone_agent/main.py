"""FastAPI app: answers Twilio calls with the ADK live agent.

Call flow:
  1. Twilio POSTs /twilio/voice when a call comes in. The signature is checked
     and TwiML <Connect><Stream> is returned with a per-call token.
  2. Twilio opens the WebSocket at /twilio/stream. The token in the `start`
     message is checked, then the call runs through run_call.
  3. When the socket closes, Twilio moves on to <Hangup/> and the call ends.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket
from fastapi import WebSocketDisconnect
from starlette.websockets import WebSocketState
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import Connect, VoiceResponse

from .live_messaging import run_call
from .twilio_stream import TwilioCall

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger(__name__)

TWILIO_AUTH_TOKEN = os.environ["TWILIO_AUTH_TOKEN"]
# Set when behind a proxy that rewrites the host, e.g. https://agent.example.com
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

VOICE_PATH = "/twilio/voice"
STREAM_PATH = "/twilio/stream"
START_TIMEOUT_SECONDS = 10

validator = RequestValidator(TWILIO_AUTH_TOKEN)
app = FastAPI(title="phone-agent")


# --- Helpers -----------------------------------------------------------------


def _base_url(request: Request) -> str:
    return PUBLIC_BASE_URL or f"{request.url.scheme}://{request.url.netloc}"


def _signed_url(request: Request) -> str:
    """The URL Twilio signed: the public base URL plus path and query."""
    url = _base_url(request) + request.url.path
    if request.url.query:
        url += "?" + request.url.query
    return url


def _stream_url(request: Request) -> str:
    base = _base_url(request)
    return base.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + STREAM_PATH


def _stream_token(call_sid: str) -> str:
    """Binds the WebSocket to a call that came through the signed webhook."""
    return hmac.new(
        TWILIO_AUTH_TOKEN.encode(), call_sid.encode(), hashlib.sha256
    ).hexdigest()


def _connect_twiml(stream_url: str, call_sid: str) -> str:
    """<Connect><Stream> carrying the call's token, then <Hangup/>."""
    response = VoiceResponse()
    connect = Connect()
    stream = connect.stream(url=stream_url)
    stream.parameter(name="token", value=_stream_token(call_sid))
    response.append(connect)
    response.hangup()
    return str(response)


async def _wait_for_start(websocket: WebSocket) -> dict | None:
    """Skips `connected` and returns the `start` payload, or None for anything else."""
    while True:
        message = json.loads(await websocket.receive_text())
        if message.get("event") == "connected":
            continue
        if message.get("event") == "start":
            return message["start"]
        return None


def _is_authorized(start: dict | None) -> bool:
    if not start or not start.get("callSid"):
        return False
    token = start.get("customParameters", {}).get("token", "")
    return hmac.compare_digest(token, _stream_token(start["callSid"]))


# --- Routes ------------------------------------------------------------------


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}


@app.post(VOICE_PATH)
async def twilio_voice(request: Request) -> Response:
    """Incoming-call webhook. Answers with TwiML that streams audio to twilio_connect."""
    # Pass the multi-dict itself: RequestValidator signs every value of a
    # repeated field via getlist(), and dict() would keep only the last one.
    params = await request.form()
    signature = request.headers.get("X-Twilio-Signature", "")
    if not validator.validate(_signed_url(request), params, signature):
        log.warning("rejected webhook with invalid Twilio signature")
        raise HTTPException(status_code=403)

    twiml = _connect_twiml(_stream_url(request), call_sid=params["CallSid"])
    return Response(content=twiml, media_type="application/xml")


@app.websocket(STREAM_PATH)
async def twilio_connect(websocket: WebSocket) -> None:
    """Twilio Media Stream endpoint: runs one phone call through the ADK live agent."""
    await websocket.accept()
    try:
        start = await asyncio.wait_for(_wait_for_start(websocket), START_TIMEOUT_SECONDS)
    except (WebSocketDisconnect, asyncio.TimeoutError, json.JSONDecodeError):
        start = None

    if not _is_authorized(start):
        log.warning("rejected media stream without a valid start/token")
        await websocket.close(code=1008)
        return

    call_sid = start["callSid"]
    log.info("call %s: stream started", call_sid)
    call = TwilioCall(websocket, stream_sid=start["streamSid"], call_sid=call_sid)
    try:
        await run_call(
            user_id=call_sid,
            session_id=call_sid,
            client_to_agent=call.client_to_agent,
            on_agent_event=call.on_agent_event,
        )
    finally:
        if websocket.client_state == WebSocketState.CONNECTED:
            await websocket.close()
        log.info("call %s: ended", call_sid)
