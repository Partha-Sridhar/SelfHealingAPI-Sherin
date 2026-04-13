# MCP Self-Healing Demo - Web UI

A beautiful chat interface that showcases the interceptor's self-healing capabilities with a visual toggle to compare healed vs raw error handling.

## 🎯 Features

- **Interactive Chat Interface** - Modern, responsive chat UI
- **Live Toggle** - Switch between interceptor mode (healed) and direct mode (raw errors)
- **Visual Feedback** - See exactly what gets healed, warnings, and error details
- **Example Prompts** - Quick-start buttons for common queries
- **Real-time Status** - Visual indicators for interceptor status
- **Detailed Metadata** - View tool calls, arguments, healing steps, and warnings

## 🚀 Quick Start

### 1. Install Dependencies

```bash
pip install -r web_requirements.txt
```

### 2. Start the Interceptor (in a separate terminal)

```bash
python interceptor.py
```

You should see:
```
[interceptor] INFO  interceptor ready  addr=127.0.0.1:6010  model=llama3
```

### 3. Start the Web UI

```bash
python web_ui.py
```

### 4. Open Your Browser

Navigate to: **http://localhost:5002**

## 🎮 How to Use

### With Interceptor Enabled (🛡️ Green Toggle)

1. Toggle should be **ON** (green)
2. Try: "what's the weather at my location"
3. Notice: Even though you didn't specify a city, the interceptor:
   - Detects the missing argument
   - Uses LLM to infer a default (Delhi)
   - Heals the request automatically
   - Shows healing steps in the metadata

### With Interceptor Disabled (⚡ Red Toggle)

1. Click the toggle to turn it **OFF** (red)
2. Try the same: "what's the weather at my location"
3. Notice: You get a raw schema error:
   ```
   ❌ Schema Error: Missing required field 'city'
   ```

## 🧪 Test Scenarios

### Scenario 1: Missing Arguments
- **Prompt**: "tell me the weather"
- **With Interceptor**: ✅ Heals to default city (Delhi)
- **Without**: ❌ Schema error - missing 'city' field

### Scenario 2: Wrong Field Names
- **Prompt**: "convert GBP to JPY" (LLM might send `from`/`to` instead of `base`/`target`)
- **With Interceptor**: ✅ Maps field names correctly
- **Without**: ❌ Schema error - unknown fields

### Scenario 3: Value Normalization
- **Prompt**: "how much is apple stock" (LLM might send "apple" instead of "AAPL")
- **With Interceptor**: ✅ Normalizes "apple" → "AAPL"
- **Without**: ❌ API error - invalid symbol

### Scenario 4: Schema Drift
Enable drift mode in the server, then:
- **Prompt**: "weather in Paris"
- **With Interceptor**: ✅ Detects V1→V2 drift, re-heals automatically
- **Without**: ❌ Drift error - field names changed

### Scenario 5: Cascading Tools
- **Prompt**: "tell me about France"
- **With Interceptor**: ✅ Gets country info, then automatically fetches weather for capital (Paris)
- **Without**: ✅ Works but no cascade

## 🎨 UI Features

### Color Coding
- **Green** 🟢 - Interceptor enabled, healing active
- **Red** 🔴 - Direct mode, raw errors shown
- **Yellow** 🟡 - Warnings (partial results, stale data)
- **Purple** 🟣 - Cascaded tool calls

### Metadata Panels
Each response shows:
- **Tool Used** - Which API was called
- **Raw Arguments** - What the LLM originally sent
- **Healing Steps** - What the interceptor fixed
- **Warnings** - Data quality issues detected
- **Error Details** - Type, code, and message (if failed)

### Example Prompts
Click any example button to instantly try:
- 🌤️ Weather queries
- 🌍 Country information
- ₿ Bitcoin price
- 💱 Currency exchange
- 📈 Stock prices

## 🔧 Architecture

```
User Browser
    ↓
web_ui.py (Flask server)
    ↓
[Toggle determines path]
    ↓
🛡️ interceptor.py (port 6010) ← if enabled
    ↓
mcp_server.py (subprocess)
    ↓
External APIs
```

## 📊 What You'll See

### Successful Healing Example
```
User: "what's the weather"

🛡️ Interceptor Mode
✓ Arguments validated and normalized

Tool Call: get_weather
Raw Args: {}
Healing:
  • ✓ Arguments validated and normalized
  • Healed: {} → {"city": "Delhi"}

Answer: The current weather in Delhi is 28°C...
```

### Raw Error Example
```
User: "what's the weather"

⚡ Direct Mode

❌ Error Details
Type: schema
Message: [get_weather] Missing required field 'city'. Got: {}

Answer: ❌ Schema Error: The tool received incorrect argument names...
```

## 🐛 Troubleshooting

### "Interceptor is not running"
- Make sure you started `python interceptor.py` in a separate terminal
- Check that port 6010 is not in use
- Look for the message: `interceptor ready  addr=127.0.0.1:6010`

### "Connection refused"
- Ensure Ollama is running: `ollama serve`
- Check that the llama3 model is available: `ollama pull llama3`

### "Module not found"
- Install dependencies: `pip install -r web_requirements.txt`

### Slow responses
- First request to Ollama is always slower (model loading)
- Weather API has a 20-second delay built in (for demo purposes)
- Check your internet connection for external APIs

## 🎓 Learning Points

This demo teaches:
1. **Proactive Error Handling** - Fix issues before they fail
2. **LLM-Powered Recovery** - Use AI to understand intent
3. **Schema Evolution** - Handle API version changes gracefully
4. **Transparent Fallback** - Work with or without healing
5. **User Experience** - Show users what's happening under the hood

## 📝 Notes

- The interceptor uses Ollama's llama3 model for healing decisions
- All healing steps are logged and visible in the UI
- The toggle lets you compare behaviors side-by-side
- Metadata panels show exactly what was fixed and why

Enjoy exploring the self-healing capabilities! 🚀
