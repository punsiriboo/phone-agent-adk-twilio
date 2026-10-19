import os

# Read by adk_agent.main at import time, so set before any test imports it.
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test-token")
os.environ.setdefault("ENABLE_WEB_CLIENT", "true")
