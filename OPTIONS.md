# ⏬ Agentic — Options

Environment variables may also be set in a `.env` file (cwd, then beside `agent.py`); the file wins over variables already set in the shell.

### `inference_url`
Inference API URL
- Environment variable: `AGENT_INFERENCE_URL`
- Expected format `http:{{SERVER_HOST}}:{{SERVER_PORT?}}/v1`
- OpenRouter URL: `https://openrouter.ai/api/v1`
- Ollama URL: `http://127.0.0.1:11434/v1 `

### `inference_key`
Inference API key.
- Environment variable: `AGENT_INFERENCE_KEY`
- Expected format `{{SECRET_KEY}}` or empty/null

### `inference_llm`
Inference model ID (general)
- Environment variable: `AGENT_INFERENCE_LLM`
- Expected format `{{VENDOR_ID}}/{{MODEL_ID}}`
- OpenRouter ID: `deepseek/deepseek-v4-flash-0731`
- Ollama ID: `huihui_ai/Qwen3.8-abliterated:27b`

### `inference_vlm`
Inference model ID (vision)
- Environment variable: `AGENT_INFERENCE_VLM`
- Expected format `{{VENDOR_ID}}/{{MODEL_ID}}`
- OpenRouter ID: `deepseek/deepseek-v4-flash-0731`
- Ollama ID: `huihui_ai/Qwen3.8-abliterated:27b`
