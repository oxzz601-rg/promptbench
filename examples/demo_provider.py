#!/usr/bin/env python3
"""A fake model so the starter suite runs offline, with no API key.

    promptbench run examples/starter.json --command "python3 examples/demo_provider.py"

Replace it with your real model: `llm -m gpt-4o`, `ollama run llama3`,
`python my_bot.py`, anything that reads a prompt on stdin and prints a reply.
"""

import json
import sys

prompt = sys.stdin.read().lower()

if "capital of france" in prompt:
    print("Paris")
elif "json" in prompt:
    print(json.dumps({"ok": True, "items": [1, 2, 3]}))
elif "moon" in prompt:
    print("I don't know.")
else:
    print("[demo provider] " + " ".join(prompt.split())[:160])
