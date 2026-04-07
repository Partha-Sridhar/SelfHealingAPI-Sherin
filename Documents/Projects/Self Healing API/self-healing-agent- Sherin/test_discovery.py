"""Quick test of dynamic discovery with 50 diverse questions."""
import json, sys, time, os
os.chdir(os.path.dirname(os.path.abspath(__file__)))
# Load env
from pathlib import Path
env = Path(".env")
if env.exists():
    for line in env.read_text().splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

import dynamic_discovery

QUESTIONS = [
    "Where is the ISS right now?",
    "Give me a random inspirational quote",
    "How many people are in space right now?",
    "Tell me a random cat fact",
    "What is the current price of gold?",
    "What is my public IP address?",
    "Generate a random color hex code",
    "What are the top headlines today?",
    "Tell me a random dog fact",
    "What is the population of the world?",
    "Give me a random trivia question",
    "What is the current Bitcoin block height?",
    "Show me a random fox image",
    "What is the exchange rate of EUR to INR?",
    "Tell me about the planet Mars",
    "What is the latest xkcd comic?",
    "Generate a random UUID",
    "What is the speed of light?",
    "Show me a random meme",
    "What are the holidays in India this year?",
    "Tell me a random Chuck Norris joke",
    "What is the current time in UTC?",
    "Give me a random word",
    "What is the boiling point of water?",
    "Show me the GitHub profile of torvalds",
    "What is the current phase of the moon?",
    "Tell me a random number fact",
    "What is the capital of Mongolia?",
    "Give me a random advice",
    "What is the current earthquake activity?",
    "Show me a random avatar image",
    "What is the meaning of the word serendipity?",
    "Tell me about the Eiffel Tower",
    "What is the current UV index in London?",
    "Give me a random programming joke",
    "What is the distance from Earth to the Moon?",
    "Show me trending repositories on GitHub",
    "What is the current air quality in Delhi?",
    "Tell me a random history fact",
    "What is the ISBN of Harry Potter?",
    "Give me a random cocktail recipe",
    "What is the elevation of Mount Everest?",
    "Show me a random user profile",
    "What is the current Ethereum gas price?",
    "Tell me about the Titanic",
    "What is the sunrise time in Tokyo?",
    "Give me a random Bible verse",
    "What is the atomic number of gold?",
    "Show me a random kanye west quote",
    "What is the current world population clock?",
]

results = []
for i, q in enumerate(QUESTIONS):
    print(f"\n[{i+1}/50] {q}")
    t0 = time.time()
    try:
        r = dynamic_discovery.discover_and_call(q)
        elapsed = (time.time() - t0)
        if r and "error" not in r.get("result", {}):
            print(f"  ✅ {r['tool_name']} ({elapsed:.1f}s)")
            results.append({"q": q, "status": "ok", "tool": r["tool_name"], "time_s": round(elapsed,1)})
        elif r:
            print(f"  ⚠️  {r['tool_name']} returned error ({elapsed:.1f}s)")
            results.append({"q": q, "status": "api_error", "tool": r["tool_name"], "time_s": round(elapsed,1)})
        else:
            print(f"  ❌ No API found ({elapsed:.1f}s)")
            results.append({"q": q, "status": "no_api", "tool": None, "time_s": round(elapsed,1)})
    except Exception as e:
        elapsed = (time.time() - t0)
        print(f"  💥 Exception: {e} ({elapsed:.1f}s)")
        results.append({"q": q, "status": "exception", "tool": None, "time_s": round(elapsed,1), "error": str(e)})

# Summary
ok = sum(1 for r in results if r["status"] == "ok")
api_err = sum(1 for r in results if r["status"] == "api_error")
no_api = sum(1 for r in results if r["status"] == "no_api")
exc = sum(1 for r in results if r["status"] == "exception")
print(f"\n{'='*50}")
print(f"RESULTS: {ok}/50 OK | {api_err} API errors | {no_api} no API found | {exc} exceptions")
print(f"{'='*50}")

with open("discovery_test_results.json", "w") as f:
    json.dump(results, f, indent=2)
print("Saved to discovery_test_results.json")
