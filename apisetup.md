# 🔌 Custom API Setup Guide for AI Coding Agents

This guide explains how to connect your **AI Key-Rotating Proxy** to popular AI coding agents and development environments.

Since the proxy provides OpenAI and Anthropic compatible endpoints, you can route all traffic through your proxy to get **automatic key rotation, token/request budget caps, and multi-site failover**.

---

## 🌐 Quick Reference: Endpoints & Authentication

* **Proxy Host**: `http://YOUR-VPS-IP:8000` (or `http://localhost:8000`)
* **API Key**: Your `PROXY_API_KEY` configured in `.env` (if left blank, send any arbitrary string like `sk-proxy-dummy`).
* **Path Prefix for Sites (V1 to V10)**:
  * Default site (V1): `http://YOUR-VPS-IP:8000/v1`
  * Secondary site (V2): `http://YOUR-VPS-IP:8000/v2`
  * Up to Site 10 (V10): `http://YOUR-VPS-IP:8000/v10`
* **Test Connectivity (Health Check)**:
  ```bash
  curl http://YOUR-VPS-IP:8000/health
  ```

---

## Table of Contents
1. [Claude Code](#1-claude-code)
2. [Cursor](#2-cursor-ide--agent)
3. [Cline (VS Code Extension)](#3-cline-vs-code-extension)
4. [Codex / GitHub Copilot CLI](#4-codex--openai-codex-tools)
5. [Grok Build](#5-grok-build)
6. [OpenCode](#6-opencode)
7. [Hermes (Nous Hermes Agent)](#7-hermes-nous-hermes)
8. [OpenClaw / OpenHands](#8-openclaw--openhands)
9. [Pi (Inflection / Pi Coding Agent)](#9-pi-coding-agent)

---

## 1. Claude Code

[Claude Code](https://github.com/anthropics/claude-code) is Anthropic's official agentic terminal coding tool. It speaks the Anthropic API format, which our proxy supports natively.

### Environment Variable Setup
Set the base URL to your proxy and supply your `PROXY_API_KEY`:

```bash
# Set in your current shell session or add to ~/.bashrc / ~/.zshrc
export ANTHROPIC_BASE_URL="http://YOUR-VPS-IP:8000/v1"
export ANTHROPIC_API_KEY="your-proxy-api-key"

# If your Anthropic keys are assigned to slot V3:
export ANTHROPIC_BASE_URL="http://YOUR-VPS-IP:8000/v3"
```

### Launching Claude Code
```bash
claude
```

### Persistent Configuration
In `~/.claude/config.json`:
```json
{
  "apiBaseUrl": "http://YOUR-VPS-IP:8000/v1",
  "apiKey": "your-proxy-api-key"
}
```

---

## 2. Cursor (IDE & Agent)

[Cursor](https://cursor.com) is an AI-first IDE that allows custom OpenAI-compatible endpoints with full code indexing, inline generation, and agentic multi-file edits.

### Configuration Walkthrough
1. Open Cursor and press `Ctrl+,` (or `Cmd+,` on macOS) to open **Settings**.
2. Navigate to **Features** → **Models** (or **Cursor Settings** → **Models**).
3. Under the **OpenAI API Key** section:
   * **API Key**: Enter your `PROXY_API_KEY` (e.g. `sk-proxy-...`).
   * Click **Override OpenAI Base URL**.
   * Enter:
     ```text
     http://YOUR-VPS-IP:8000/v1
     ```
     *(Use `/v2` through `/v10` if targeting a specific upstream slot)*.
4. Under **Model Names**, click **Add Model** and enter the exact model ID accepted by your upstream provider (e.g. `openai/gpt-4o`, `anthropic/claude-3-7-sonnet`, `deepseek/deepseek-chat`).
5. Click **Verify** to test connectivity.

---

## 3. Cline (VS Code Extension)

[Cline](https://github.com/cline/cline) (formerly Claude Dev) is an autonomous coding assistant for VS Code with terminal and filesystem execution capabilities.

### Configuration Walkthrough
1. Open the **Cline** panel in VS Code.
2. Click the ⚙️ **Settings** gear icon in the top right of the Cline sidebar.
3. In **API Provider**, select:
   * **OpenAI Compatible**
4. Fill in the fields:
   * **Base URL**: `http://YOUR-VPS-IP:8000/v1` (or `/v2`..`/v10`)
   * **API Key**: `your-proxy-api-key`
   * **Model ID**: e.g., `openai/gpt-4o`, `anthropic/claude-3-5-sonnet`, `deepseek/deepseek-coder`
5. Click **Done** or **Save Settings**.

### Persistent Settings via `settings.json`
You can also configure Cline directly in VS Code's `settings.json`:
```json
{
  "cline.apiProvider": "openai-compatible",
  "cline.openAiBaseUrl": "http://YOUR-VPS-IP:8000/v1",
  "cline.openAiApiKey": "your-proxy-api-key",
  "cline.openAiModelId": "anthropic/claude-3-7-sonnet"
}
```

---

## 4. Codex / OpenAI Codex Tools

For CLI tools and scripts utilizing OpenAI Codex or standard OpenAI models:

### Environment Variable Setup
```bash
# Standard OpenAI environment variables honored by most CLI tools
export OPENAI_BASE_URL="http://YOUR-VPS-IP:8000/v1"
export OPENAI_API_KEY="your-proxy-api-key"
```

### Python Codex Tool Script Example
```python
from openai import OpenAI

client = OpenAI(
    base_url="http://YOUR-VPS-IP:8000/v1",
    api_key="your-proxy-api-key"
)

response = client.chat.completions.create(
    model="openai/gpt-4o",
    messages=[
        {"role": "system", "content": "You are a code synthesis assistant."},
        {"role": "user", "content": "Write a binary search function in Go"}
    ]
)
print(response.choices[0].message.content)
```

---

## 5. Grok Build

For developers and agents building on xAI's Grok API models (`grok-2`, `grok-beta`):

### Environment Variable Setup
Since Grok uses an OpenAI-compatible REST API:

```bash
export XAI_BASE_URL="http://YOUR-VPS-IP:8000/v1"
export XAI_API_KEY="your-proxy-api-key"

# Or if your Grok keys are mapped to slot V4:
export OPENAI_BASE_URL="http://YOUR-VPS-IP:8000/v4"
export OPENAI_API_KEY="your-proxy-api-key"
```

### Direct Agent Invocation
```bash
curl http://YOUR-VPS-IP:8000/v4/chat/completions \
  -H "Authorization: Bearer your-proxy-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "x-ai/grok-2-1212",
    "messages": [{"role": "user", "content": "Build an async queue in Rust"}]
  }'
```

---

## 6. OpenCode

[OpenCode](https://opencode.ai) is an open-source terminal AI coding assistant. It uses the Vercel AI SDK (`@ai-sdk/openai`, `@ai-sdk/anthropic`) under the hood and resolves configuration from environment variables or `~/.config/opencode/opencode.json`.

---

### Method 1: Environment Variables (Recommended & Quickest)

The simplest and most reliable way to route OpenCode through your proxy is setting standard OpenAI environment variables in your shell (or adding them to `~/.bashrc` / `~/.zshrc`):

```bash
# Point to your proxy slot (V1 to V10)
export OPENAI_BASE_URL="http://YOUR-VPS-IP:8000/v1"
export OPENAI_API_KEY="your-proxy-api-key"

# Optional: Default model
export OPENCODE_MODEL="openai/gpt-4o"

# Run OpenCode
opencode
```

> [!NOTE]
> Do **not** use `OPENCODE_BASE_URL` or `OPENCODE_API_KEY`. OpenCode does not recognize those variables and will bypass your proxy. Use `OPENAI_BASE_URL` and `OPENAI_API_KEY`.

If routing Anthropic Claude models through your proxy slot:
```bash
export ANTHROPIC_BASE_URL="http://YOUR-VPS-IP:8000/v1"
export ANTHROPIC_API_KEY="your-proxy-api-key"
opencode
```

---

### Method 2: Global Configuration File (`~/.config/opencode/opencode.json`)

OpenCode does **not** read `~/.opencode/config.json`. Its configuration file is located at:
* **Linux / macOS**: `~/.config/opencode/opencode.json` (or `opencode.jsonc`)
* **Project Directory**: `./opencode.json` in your project root
* **Windows**: `%USERPROFILE%\.config\opencode\opencode.json`

Create or edit `~/.config/opencode/opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "openai/gpt-4o",
  "provider": {
    "openai": {
      "settings": {
        "baseURL": "http://YOUR-VPS-IP:8000/v1"
      }
    }
  }
}
```

*(Note: Depending on your OpenCode version, the key may also be defined under `"options": { "baseURL": "..." }`)*.

---

### Method 3: Custom OpenAI-Compatible Provider (`opencode.json`)

To configure the proxy as a dedicated custom provider in OpenCode:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "ai-proxy/gpt-4o",
  "provider": {
    "ai-proxy": {
      "name": "AI Rotating Proxy",
      "package": "@opencode/ai/providers/openai-compatible",
      "settings": {
        "baseURL": "http://YOUR-VPS-IP:8000/v1"
      },
      "models": {
        "gpt-4o": {
          "name": "GPT-4o (Rotated)"
        }
      }
    }
  }
}
```

---

### Method 4: CLI Authentication (`opencode auth login`)

To save credentials without placing keys in plain text config files:
1. Run:
   ```bash
   opencode auth login openai
   ```
2. Enter your `PROXY_API_KEY`.
3. In your environment (e.g. `~/.bashrc`), set:
   ```bash
   export OPENAI_BASE_URL="http://YOUR-VPS-IP:8000/v1"
   ```

---

### 💡 Using Open Interpreter (`interpreter`)?
If your setup uses **Open Interpreter** (CLI: `interpreter`), connect using:
```bash
interpreter --api_base "http://YOUR-VPS-IP:8000/v1" --api_key "your-proxy-api-key" --model "openai/gpt-4o"
```
Or in `~/.config/open-interpreter/config.yaml`:
```yaml
api_base: "http://YOUR-VPS-IP:8000/v1"
api_key: "your-proxy-api-key"
model: "openai/gpt-4o"
```

---

## 7. Hermes (Nous Hermes)

Nous Hermes agents and function-calling runners (`hermes-3-llama-3.1-405b`, `hermes-2-pro`):

### Environment Variable Setup
```bash
export HERMES_API_BASE="http://YOUR-VPS-IP:8000/v1"
export OPENAI_BASE_URL="http://YOUR-VPS-IP:8000/v1"
export OPENAI_API_KEY="your-proxy-api-key"
export HERMES_MODEL="nousresearch/hermes-3-llama-3.1-405b"
```

### Agent Configuration (`hermes.yaml`)
```yaml
llm:
  provider: openai
  base_url: "http://YOUR-VPS-IP:8000/v1"
  api_key: "your-proxy-api-key"
  model: "nousresearch/hermes-3-llama-3.1-405b"
  stream: true
  temperature: 0.1
```

---

## 8. OpenClaw / OpenHands

[OpenHands](https://github.com/All-Hands-AI/OpenHands) (formerly OpenDevin / OpenClaw) is an autonomous software development platform.

### Configuration via `config.toml`
In the root directory of your project or in `~/.openhands/config.toml`:
```toml
[llm]
model = "openai/gpt-4o"
base_url = "http://YOUR-VPS-IP:8000/v1"
api_key = "your-proxy-api-key"
custom_llm_provider = "openai"
temperature = 0.0
max_tokens = 4096
```

### Launching via Docker
```bash
docker run -it \
  -e LLM_BASE_URL="http://YOUR-VPS-IP:8000/v1" \
  -e LLM_API_KEY="your-proxy-api-key" \
  -e LLM_MODEL="openai/gpt-4o" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -p 3000:3000 \
  ghcr.io/all-hands-ai/openhands:latest
```

---

## 9. Pi (Coding Agent)

For Inflection Pi or Pi coding assistant tools:

### Configuration File (`~/.pi/config.json`)
```json
{
  "api_type": "openai",
  "base_url": "http://YOUR-VPS-IP:8000/v1",
  "api_key": "your-proxy-api-key",
  "model": "openai/gpt-4o",
  "stream": true
}
```

### Terminal Environment Setup
```bash
export PI_BASE_URL="http://YOUR-VPS-IP:8000/v1"
export PI_API_KEY="your-proxy-api-key"
export OPENAI_BASE_URL="http://YOUR-VPS-IP:8000/v1"
```

---

## 🔍 Verifying Your Configuration

To test if your proxy is answering before starting an agent, run this command from your terminal:

```bash
curl -i http://YOUR-VPS-IP:8000/v1/chat/completions \
  -H "Authorization: Bearer your-proxy-api-key" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "openai/gpt-4o-mini",
    "messages": [{"role": "user", "content": "Ping"}]
  }'
```

* If you receive HTTP `200 OK` with choices and tokens, your proxy is fully operational and rotating keys properly.
* If you receive HTTP `503`, check your proxy Telegram bot via `/health` or `/stats` to verify that usable keys are assigned to that site slot.
* To discover the exact models supported by a site slot, send `/models V1` (or tap **🧠 Models**) in your Telegram bot.
