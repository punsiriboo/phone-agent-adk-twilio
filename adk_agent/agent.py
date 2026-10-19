import os

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.tools import google_search
from google.genai import types


async def end_call() -> dict:
    """Hang up the phone call. Call this only after you have said goodbye."""
    # Returns instantly: tools are synchronous in a live session, so a slow tool
    # freezes the voice. The actual hang-up is done by the run_live loop in
    # live_messaging.py when it sees this function_call.
    return {"result": "ok"}


root_agent = Agent(
    name="google_search_agent",
    # Gemini(...) instead of a bare model string so the voice travels with the
    # agent; agent-level speech_config wins over RunConfig.speech_config anyway.
    model=Gemini(
        model=os.getenv("LIVE_MODEL", "gemini-3.1-flash-live-preview"),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                # https://ai.google.dev/gemini-api/docs/speech-generation#voices
                prebuilt_voice_config=types.PrebuiltVoiceConfig(
                    voice_name=os.getenv("LIVE_VOICE", "Zephyr")
                )
            ),
            # https://ai.google.dev/gemini-api/docs/speech-generation#languages
            # Live models often infer the language from the caller and may ignore this.
            language_code=os.getenv("LIVE_LANGUAGE", "en-US"),
        ),
    ),
    description="Agent to answer questions using Google Search.",
    instruction=(
        "I can answer your questions by searching the internet. Just ask me anything! "
        "When the caller is done, say goodbye and then call end_call."
    ),
    tools=[google_search, end_call],
)
