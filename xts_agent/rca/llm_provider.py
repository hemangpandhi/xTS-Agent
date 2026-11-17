"""LLM providers for AI triage.

On-prem (llama.cpp) is the default. External providers send failure logs,
stack traces and retrieved OEM source code to a third party, so they are
refused unless ``ai_rca.allow_external_providers`` is explicitly true.
"""

from __future__ import annotations

import ipaddress
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

EXTERNAL_PROVIDERS = frozenset({"gemini"})


def is_local_endpoint(url: str) -> bool:
    """True when ``url`` points at this host or a private network (RFC 1918 / ULA).

    Used to decide whether an OpenAI-compatible endpoint (Ollama, vLLM,
    llama.cpp server, LM Studio) counts as on-prem or as an external service.
    """
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    if host in ("localhost", "host.docker.internal") or host.endswith(".local"):
        return True
    try:
        return ipaddress.ip_address(host).is_private or ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ExternalProviderNotAllowed(ValueError):
    """Raised when an external LLM is configured without explicit opt-in."""


class LLMProvider(ABC):
    model_id: str = ""

    @abstractmethod
    def generate(self, prompt: str, json_mode: bool = False) -> str:
        """Return the model's text; with json_mode the model is asked for a JSON object."""

    def unload(self) -> None:
        """Release GPU VRAM / model resources when AI triage finishes."""
        return None


class GeminiProvider(LLMProvider):
    def __init__(self, api_key: str, model: str = "gemini-2.5-flash", timeout_secs: int = 120):
        self.api_key = api_key
        self.model = model
        self.timeout_secs = timeout_secs
        self.model_id = f"gemini:{model}"
        # Key goes in a header, never the URL, so it cannot leak into logs
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def generate(self, prompt: str, json_mode: bool = False) -> str:
        if not self.api_key:
            return "Error: Gemini API key not configured."

        generation: Dict[str, Any] = {"temperature": 0.2}
        if json_mode:
            generation["responseMimeType"] = "application/json"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": generation,
        }
        headers = {"Content-Type": "application/json", "x-goog-api-key": self.api_key}
        try:
            response = requests.post(
                self.url, json=payload, headers=headers, timeout=self.timeout_secs
            )
            response.raise_for_status()
            data = response.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            logger.error("Gemini API returned HTTP %s for model %s", status, self.model)
            return f"Failed to generate RCA using Gemini: HTTP {status}"
        except Exception as e:
            # Exception type only: request exceptions can echo request details
            logger.error("Gemini API call failed: %s", type(e).__name__)
            return f"Failed to generate RCA using Gemini: {type(e).__name__}"


class OpenAICompatibleProvider(LLMProvider):
    """Any server speaking the OpenAI chat-completions API.

    On-prem: Ollama (``http://127.0.0.1:11434/v1``), vLLM, llama.cpp server,
    LM Studio. Hosted: OpenAI, Gemini's OpenAI endpoint, OpenRouter, etc. The
    same provider serves a customer demo on a hosted model and the OEM lab on
    a local one: only ``base_url``, ``model`` and the key change.
    """

    def __init__(self, base_url: str, model: str, api_key: str = "", timeout_secs: int = 120,
                 extra_body: Optional[Dict[str, Any]] = None):
        self.base_url = base_url.rstrip("/")
        # Server-specific request fields, e.g. vLLM
        # {"chat_template_kwargs": {"enable_thinking": false}} for Qwen3 reasoning models
        self.extra_body = dict(extra_body or {})
        self.model = model
        self.api_key = api_key
        self.timeout_secs = timeout_secs
        self.model_id = f"openai:{model}"
        self.url = f"{self.base_url}/chat/completions"

    def generate(self, prompt: str, json_mode: bool = False) -> str:
        if not self.model:
            return "Error: ai_rca.openai_model not configured."
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [
                {
                    "role": "system",
                    "content": "You are an expert Android OS systems engineer tasked with triaging CTS failures.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 1024,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        payload.update(self.extra_body)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            # Key goes in a header, never the URL, so it cannot leak into logs
            headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = requests.post(self.url, json=payload, headers=headers, timeout=self.timeout_secs)
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except requests.HTTPError as e:
            status = e.response.status_code if e.response is not None else "?"
            logger.error("OpenAI-compatible endpoint returned HTTP %s for model %s", status, self.model)
            return f"Failed to generate RCA using {self.model}: HTTP {status}"
        except Exception as e:
            # Exception type only: request exceptions can echo request details
            logger.error("OpenAI-compatible call failed: %s", type(e).__name__)
            return f"Failed to generate RCA using {self.model}: {type(e).__name__}"

    def unload(self) -> None:
        """Unload model from GPU VRAM on local Ollama / llama-server endpoints when idle."""
        if not self.model or not is_local_endpoint(self.base_url):
            return
        root_url = self.base_url[:-3] if self.base_url.endswith("/v1") else self.base_url
        try:
            resp = requests.post(
                f"{root_url}/api/generate",
                json={"model": self.model, "keep_alive": 0},
                timeout=5,
            )
            if resp.ok:
                logger.info("Unloaded model %s from GPU VRAM (%s)", self.model, root_url)
        except Exception as e:
            logger.debug("Model unload request skipped: %s", type(e).__name__)


class LlamaCppProvider(LLMProvider):
    def __init__(self, model_path: str, n_ctx: int = 16384, n_gpu_layers: int = -1):
        self.model_path = model_path
        self.n_ctx = n_ctx
        self.n_gpu_layers = n_gpu_layers
        self.llm: Any = None  # llama_cpp.Llama, loaded on first use
        self.model_id = f"llama_cpp:{Path(model_path).name}" if model_path else "llama_cpp"

    def _lazy_load(self):
        if self.llm is None:
            try:
                from llama_cpp import Llama

                logger.info(
                    "Loading llama.cpp model from %s with %s GPU layers...",
                    self.model_path,
                    self.n_gpu_layers,
                )
                self.llm = Llama(
                    model_path=self.model_path,
                    n_ctx=self.n_ctx,
                    n_gpu_layers=self.n_gpu_layers,
                    verbose=False,
                )
            except Exception as e:
                logger.error("Failed to load llama.cpp model: %s", e)
                raise

    def generate(self, prompt: str, json_mode: bool = False) -> str:
        if not self.model_path:
            return "Error: llama_model_path not configured."

        try:
            self._lazy_load()
            extra = {"response_format": {"type": "json_object"}} if json_mode else {}
            response = self.llm.create_chat_completion(
                **extra,
                messages=[
                    {
                        "role": "system",
                        "content": "You are an expert Android OS systems engineer tasked with triaging CTS failures.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=1024,
            )
            return response["choices"][0]["message"]["content"]
        except Exception as e:
            logger.error("llama.cpp generation error: %s", e)
            return f"Failed to generate RCA using llama.cpp: {str(e)}"

    def unload(self) -> None:
        """Free in-process llama.cpp GGUF weights and GPU VRAM when idle."""
        if self.llm is not None:
            try:
                close_fn = getattr(self.llm, "close", None)
                if callable(close_fn):
                    close_fn()
            except Exception as e:
                logger.debug("llama.cpp close warning: %s", type(e).__name__)
            self.llm = None
            import gc

            gc.collect()
            logger.info("Unloaded llama.cpp model from GPU VRAM")


class FallbackProvider(LLMProvider):
    """Try on-prem endpoints in order; a dead or failing server falls through.

    OpenAICompatibleProvider reports failures as a "Failed to generate ..."
    string rather than raising, so both forms count as a failure.
    """

    def __init__(self, providers: List[LLMProvider]):
        self.providers = list(providers)
        self.model_id = getattr(self.providers[0], "model_id", "") if self.providers else ""
        self.model = getattr(self.providers[0], "model", "") if self.providers else ""

    def generate(self, prompt: str, json_mode: bool = False) -> str:
        last = "Failed to generate: no LLM provider configured"
        for prov in self.providers:
            try:
                out = prov.generate(prompt, json_mode=json_mode)
            except Exception as exc:
                last = f"Failed to generate: {type(exc).__name__}"
                continue
            if out and not out.startswith(("Failed to generate", "Error:")):
                # Report the model that actually answered
                self.model_id = getattr(prov, "model_id", self.model_id)
                self.model = getattr(prov, "model", self.model)
                return out
            last = out or last
            logger.warning("LLM %s unavailable; trying next", getattr(prov, "model_id", type(prov).__name__))
        return last

    def unload(self) -> None:
        for prov in self.providers:
            prov.unload()


def get_llm_provider(config) -> LLMProvider:
    provider = str(config.provider).lower()
    base_url = str(getattr(config, "openai_base_url", "") or "")
    external = provider in EXTERNAL_PROVIDERS or (
        provider == "openai_compatible" and not is_local_endpoint(base_url)
    )
    if external and not getattr(config, "allow_external_providers", False):
        where = f" ({base_url})" if provider == "openai_compatible" else ""
        raise ExternalProviderNotAllowed(
            f"ai_rca.provider={provider!r}{where} sends failure logs and OEM source code to an "
            "external service; set ai_rca.allow_external_providers: true to opt in, "
            "or use provider: llama_cpp / an openai_compatible endpoint on this host"
        )
    timeout = int(getattr(config, "request_timeout_secs", 120))
    if provider == "gemini":
        logger.warning("AI RCA is using external provider gemini (explicitly allowed)")
        return GeminiProvider(api_key=config.gemini_api_key, model=config.gemini_model, timeout_secs=timeout)
    if provider == "openai_compatible":
        if external:
            logger.warning("AI RCA is using external endpoint %s (explicitly allowed)", base_url)
        return OpenAICompatibleProvider(
            base_url=base_url,
            model=config.openai_model,
            api_key=getattr(config, "openai_api_key", ""),
            timeout_secs=timeout,
            extra_body=getattr(config, "openai_extra_body", None),
        )
    if provider == "llama_cpp":
        return LlamaCppProvider(
            model_path=config.llama_model_path,
            n_ctx=config.llama_n_ctx,
            n_gpu_layers=config.llama_n_gpu_layers,
        )
    raise ValueError(f"Unknown LLM provider: {config.provider}")
