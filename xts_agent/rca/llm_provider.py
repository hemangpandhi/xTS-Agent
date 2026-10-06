from abc import ABC, abstractmethod
import logging
import json
import requests

logger = logging.getLogger(__name__)

class LLMProvider(ABC):
    @abstractmethod
    def generate(self, prompt: str) -> str:
        pass

class GeminiProvider(LLMProvider):
    def __init__(self, api_key: str, model: str = "gemini-1.5-pro"):
        self.api_key = api_key
        self.model = model
        self.url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"

    def generate(self, prompt: str) -> str:
        if not self.api_key:
            return "Error: Gemini API key not configured."
            
        payload = {
            "contents": [{
                "parts": [{"text": prompt}]
            }],
            "generationConfig": {
                "temperature": 0.2
            }
        }
        
        try:
            response = requests.post(self.url, json=payload, headers={"Content-Type": "application/json"})
            response.raise_for_status()
            data = response.json()
            return data["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            logger.error(f"Gemini API error: {e}")
            return f"Failed to generate RCA using Gemini: {str(e)}"

class LlamaCppProvider(LLMProvider):
    def __init__(self, model_path: str, n_ctx: int = 16384, n_gpu_layers: int = -1):
        self.model_path = model_path
        self.n_ctx = n_ctx
        self.n_gpu_layers = n_gpu_layers
        self.llm = None
        
    def _lazy_load(self):
        if self.llm is None:
            try:
                from llama_cpp import Llama
                logger.info(f"Loading llama.cpp model from {self.model_path} with {self.n_gpu_layers} GPU layers...")
                self.llm = Llama(
                    model_path=self.model_path,
                    n_ctx=self.n_ctx,
                    n_gpu_layers=self.n_gpu_layers,
                    verbose=False
                )
            except Exception as e:
                logger.error(f"Failed to load llama.cpp model: {e}")
                raise

    def generate(self, prompt: str) -> str:
        if not self.model_path:
            return "Error: llama_model_path not configured."
            
        try:
            self._lazy_load()
            response = self.llm.create_chat_completion(
                messages=[
                    {"role": "system", "content": "You are an expert Android OS systems engineer tasked with triaging CTS failures."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.1,
                max_tokens=1024
            )
            return response['choices'][0]['message']['content']
        except Exception as e:
            logger.error(f"llama.cpp generation error: {e}")
            return f"Failed to generate RCA using llama.cpp: {str(e)}"

def get_llm_provider(config) -> LLMProvider:
    if config.provider == "gemini":
        return GeminiProvider(api_key=config.gemini_api_key, model=config.gemini_model)
    elif config.provider == "llama_cpp":
        return LlamaCppProvider(
            model_path=config.llama_model_path,
            n_ctx=config.llama_n_ctx,
            n_gpu_layers=config.llama_n_gpu_layers
        )
    else:
        raise ValueError(f"Unknown LLM provider: {config.provider}")
