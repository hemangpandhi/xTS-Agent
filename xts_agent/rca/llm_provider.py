"""LLM providers for AI triage.

On-prem (llama.cpp) is the default. External providers send failure logs,
stack traces and retrieved OEM source code to a third party, so they are
refused unless ``ai_rca.allow_external_providers`` is explicitly true.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

EXTERNAL_PROVIDERS = frozenset({"gemini"})


class ExternalProviderNotAllowed(ValueError):
    """Raised when an external LLM is configured without explicit opt-in."""


class LLMProvider(ABC):
    model_id: str = ""

    @abstractmethod
    def generate(self, prompt: str, json_mode: bool = False) -> str:
        """Return the model's text; with json_mode the model is asked for a JSON object."""


class GeminiProvider(LLMProvider):
    def __init__(self, api_key: str, model: str = "gemini-1.5-pro", timeout_secs: int = 120):
        self.api_key = api_key
        self.model = model
        self.timeout_secs = timeout_secs
        self.model_id = f"gemini:{model}"
        # Key goes in a header, never the URL, so it cannot leak into logs
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

    def generate(self, prompt: str, json_mode: bool = False) -> str:
        if not self.api_key:
            return "Error: Gemini API key not configured."

        generation = {"temperature": 0.2}
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


class LlamaCppProvider(LLMProvider):
    def __init__(self, model_path: str, n_ctx: int = 16384, n_gpu_layers: int = -1):
        self.model_path = model_path
        self.n_ctx = n_ctx
        self.n_gpu_layers = n_gpu_layers
        self.llm = None
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


def get_llm_provider(config) -> LLMProvider:
    provider = str(config.provider).lower()
    if provider in EXTERNAL_PROVIDERS and not getattr(config, "allow_external_providers", False):
        raise ExternalProviderNotAllowed(
            f"ai_rca.provider={provider!r} sends failure logs and OEM source code to an "
            "external service; set ai_rca.allow_external_providers: true to opt in, "
            "or use provider: llama_cpp"
        )
    timeout = int(getattr(config, "request_timeout_secs", 120))
    if provider == "gemini":
        logger.warning("AI RCA is using external provider gemini (explicitly allowed)")
        return GeminiProvider(api_key=config.gemini_api_key, model=config.gemini_model, timeout_secs=timeout)
    if provider == "llama_cpp":
        return LlamaCppProvider(
            model_path=config.llama_model_path,
            n_ctx=config.llama_n_ctx,
            n_gpu_layers=config.llama_n_gpu_layers,
        )
    raise ValueError(f"Unknown LLM provider: {config.provider}")
