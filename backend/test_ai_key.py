"""Standalone check that OPENAI_API_KEY (in the repo-root .env) is valid --
run this once after setting the key, before wiring the Telegram bot to it.

    cd backend && uv run python test_ai_key.py
"""

import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()  # walks up from this file to the repo-root .env

api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    print("ERROR: OPENAI_API_KEY is not set (checked the environment and .env).")
    raise SystemExit(1)

client = OpenAI(api_key=api_key)

try:
    response = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        messages=[{"role": "user", "content": "Hello! Confirm you are JalNetra AI assistant in 1 sentence."}],
    )
    print("SUCCESS! OpenAI response:", response.choices[0].message.content)
except Exception as e:
    print("ERROR with OpenAI key:", e)
