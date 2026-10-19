# phone-agent-adk-twilio

Answers phone calls with a Google ADK live agent (Gemini Live), using Twilio Media Streams.

```
caller ─ Twilio ─ POST /twilio/voice ──> TwiML <Connect><Stream>
                └ WS   /twilio/stream ─> twilio_connect ─> runner.run_live()
                     8 kHz μ-law  <──>  16 kHz PCM in / 24 kHz PCM out
```

| File | Role |
|---|---|
| `phone_agent/agent.py` | `root_agent`: Live model, voice, tools (`google_search`, `end_call`) |
| `phone_agent/live_messaging.py` | `RunConfig`, `run_live` session, ADK events → `AgentEvent`, `run_call` lifecycle |
| `phone_agent/twilio_stream.py` | Twilio protocol: μ-law codec and resampling, barge-in (`clear`), hang-up after goodbye (`mark`) |
| `phone_agent/main.py` | FastAPI: signed webhook + `twilio_connect` WebSocket |

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # fill in GOOGLE_API_KEY, TWILIO_AUTH_TOKEN
ngrok http 8080               # put the https URL in PUBLIC_BASE_URL
uvicorn phone_agent.main:app --port 8080
```

In the Twilio console, set the number's **A call comes in** webhook to `POST https://<PUBLIC_BASE_URL>/twilio/voice`.

Try the agent without a phone: `adk web .` and pick `phone_agent`.

Tests (Gemini is stubbed, no keys needed): `pip install pytest httpx && python -m pytest`.

## Deploy to Cloud Run

One Cloud Run service serves both the webhook and the WebSocket. Cloud Functions can't hold a WebSocket for the length of a call.

```bash
gcloud run deploy phone-agent --source . \
  --timeout=3600 \
  --set-env-vars=PUBLIC_BASE_URL=https://<service-url> \
  --set-secrets=GOOGLE_API_KEY=google-api-key:latest,TWILIO_AUTH_TOKEN=twilio-auth-token:latest
```

- `--timeout` is the longest a call can last on Cloud Run (default 300 s). Keep `MAX_CALL_SECONDS` below it.
- Sessions are in memory and per call, so any instance can take any call; no session affinity needed.

## Security

- Every webhook is checked against `X-Twilio-Signature`.
- The WebSocket only accepts a stream whose `start` message carries an HMAC token for its `CallSid`, issued by the signed webhook.
- Transcripts are logged only at `DEBUG`, because they contain caller data.
