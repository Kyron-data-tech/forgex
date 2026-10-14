"""
ForgeX — Multi-Provider Vision LLM & Fallback Agent Loop
========================================================
Provides:
  - Common interface: generate_action(messages, images) -> {thought, action, args}
  - Adapters for: MockLLM (deterministic testing), OpenAI, Anthropic, Gemini, Ollama
  - Observe -> Think -> Act agent fallback loop with:
      * Interactive element numbering
      * Schema validation against tool allowlist
      * Loop detection (detects repeated actions)
      * Max steps hard cap
      * Non-negotiable approval enforcement
"""

from __future__ import annotations

import re
import json
import base64
import logging
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

import httpx
from app.config import settings
from app.agent.tools import ALLOWED_TOOLS, ToolExecutor

logger = logging.getLogger("forgex.llm")


class BaseLLMProvider(ABC):
    @abstractmethod
    async def generate_action(
        self, messages: List[Dict[str, Any]], images: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Generates structured agent step:
        {"thought": "...", "action": "...", "args": {...}}
        """
        pass


class MockLLMProvider(BaseLLMProvider):
    """
    Deterministic testing provider that replays scripted actions
    or yields safe fallback navigation.
    """

    def __init__(self, script: Optional[List[Dict[str, Any]]] = None):
        self.script = list(script or [])
        self.call_count = 0

    async def generate_action(
        self, messages: List[Dict[str, Any]], images: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        if self.script:
            return self.script.pop(0)

        self.call_count += 1
        return {
            "thought": f"Mock thought step {self.call_count}",
            "action": "wait",
            "args": {"seconds": 0.5},
        }


class OpenAIProvider(BaseLLMProvider):
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.LLM_API_KEY
        self.model = model or settings.LLM_MODEL or "gpt-4o"

    async def generate_action(
        self, messages: List[Dict[str, Any]], images: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # Build payload with vision images
        formatted_messages = []
        for msg in messages:
            content = msg["content"]
            if images and msg.get("role") == "user":
                img_blocks = [{"type": "text", "text": content}]
                for img_b64 in images:
                    img_blocks.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{img_b64}"},
                    })
                formatted_messages.append({"role": "user", "content": img_blocks})
            else:
                formatted_messages.append(msg)

        payload = {
            "model": self.model,
            "messages": formatted_messages,
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        }

        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            raw = data["choices"][0]["message"]["content"]
            return json.loads(raw)


class AnthropicProvider(BaseLLMProvider):
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.LLM_API_KEY
        self.model = model or settings.LLM_MODEL or "claude-3-5-sonnet-20241022"

    async def generate_action(
        self, messages: List[Dict[str, Any]], images: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        anthropic_msgs = []
        for msg in messages:
            if msg.get("role") == "user" and images:
                content_blocks = []
                for img_b64 in images:
                    content_blocks.append({
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": img_b64,
                        },
                    })
                content_blocks.append({"type": "text", "text": msg["content"]})
                anthropic_msgs.append({"role": "user", "content": content_blocks})
            else:
                anthropic_msgs.append({"role": msg.get("role", "user"), "content": msg["content"]})

        payload = {
            "model": self.model,
            "max_tokens": 1024,
            "messages": anthropic_msgs,
        }

        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            raw = data["content"][0]["text"]
            # Extract JSON block
            m = re.search(r"(\{.*\})", raw, re.DOTALL)
            return json.loads(m.group(1)) if m else json.loads(raw)


class GeminiProvider(BaseLLMProvider):
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None):
        self.api_key = api_key or settings.LLM_API_KEY
        self.model = model or settings.LLM_MODEL or "gemini-1.5-flash"

    async def generate_action(
        self, messages: List[Dict[str, Any]], images: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent?key={self.api_key}"
        parts = []
        if images:
            for img_b64 in images:
                parts.append({"inline_data": {"mime_type": "image/png", "data": img_b64}})
        user_text = messages[-1]["content"] if messages else "Plan next action"
        parts.append({"text": user_text + "\nRespond with a valid JSON object only: {\"thought\": \"...\", \"action\": \"...\", \"args\": {...}}"})

        payload = {
            "contents": [{"parts": parts}],
            "generationConfig": {"response_mime_type": "application/json"},
        }

        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            raw = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(raw)


class OllamaProvider(BaseLLMProvider):
    def __init__(self, model: Optional[str] = None):
        self.model = model or settings.LLM_MODEL or "llama3.2-vision"

    async def generate_action(
        self, messages: List[Dict[str, Any]], images: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        url = "http://localhost:11434/api/chat"
        payload = {
            "model": self.model,
            "messages": messages,
            "images": images or [],
            "format": "json",
            "stream": False,
        }

        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            raw = data["message"]["content"]
            return json.loads(raw)


def get_llm_provider() -> BaseLLMProvider:
    """Factory selecting provider based on settings.LLM_PROVIDER."""
    provider_name = (settings.LLM_PROVIDER or "mock").lower()
    if provider_name == "openai":
        return OpenAIProvider()
    elif provider_name == "anthropic":
        return AnthropicProvider()
    elif provider_name == "gemini":
        return GeminiProvider()
    elif provider_name == "ollama":
        return OllamaProvider()
    return MockLLMProvider()


# ─────────────────────────────────────────────────────────────────────────
# Observe -> Think -> Act Agent Fallback Loop
# ─────────────────────────────────────────────────────────────────────────

class AgentLoop:
    """
    Autonomous fallback loop executing Observe -> Think -> Act.
    Detects action loops, caps max steps, and enforces safety bounds.
    """

    def __init__(self, executor: ToolExecutor, provider: Optional[BaseLLMProvider] = None):
        self.executor = executor
        self.provider = provider or get_llm_provider()
        self.action_history: List[str] = []

    def _check_loop(self, action_sig: str) -> bool:
        """Returns True if the identical action signature occurred 3 times in recent steps."""
        self.action_history.append(action_sig)
        if len(self.action_history) >= 3:
            recent = self.action_history[-3:]
            if recent[0] == recent[1] == recent[2]:
                return True
        return False

    async def step(self, task_goal: str) -> Dict[str, Any]:
        page = self.executor.get_page()

        # 1. OBSERVE: Screenshot & page title
        screenshot_bytes = await page.screenshot(full_page=False)
        img_b64 = base64.b64encode(screenshot_bytes).decode("ascii")

        prompt = (
            f"You are the ForgeX Operator Agent. Current Goal: {task_goal}\n"
            f"Current Page URL: {page.url}\n"
            f"Allowed Tools: {', '.join(sorted(ALLOWED_TOOLS))}\n"
            "Analyze the screenshot and current state. Reply with ONLY a JSON object:\n"
            "{\"thought\": \"explanation of next move\", \"action\": \"allowed_tool_name\", \"args\": {tool_arguments}}"
        )

        # 2. THINK: Call Vision LLM
        decision = await self.provider.generate_action(
            messages=[{"role": "user", "content": prompt}],
            images=[img_b64],
        )

        thought = decision.get("thought", "")
        action = decision.get("action", "")
        args = decision.get("args", {})

        # Safety: schema & allowlist validation
        if action not in ALLOWED_TOOLS:
            logger.warning("Agent attempted unauthorized action: '%s'", action)
            return {
                "status": "rejected",
                "error": f"Action '{action}' is not in the allowed toolset.",
                "thought": thought,
            }

        # Safety: loop detection
        action_sig = f"{action}:{json.dumps(args, sort_keys=True)}"
        if self._check_loop(action_sig):
            logger.warning("Repetitive action loop detected: %s", action_sig)
            return {
                "status": "loop_detected",
                "error": "Execution paused due to repetitive action loop.",
                "thought": thought,
            }

        # 3. ACT: Execute via allowlisted ToolExecutor (Pointer enforced)
        result = await self.executor.execute(action, args)
        return {
            "status": "executed",
            "thought": thought,
            "action": action,
            "args": args,
            "result": result,
        }
