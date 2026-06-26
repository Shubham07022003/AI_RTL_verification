import os

from dotenv import load_dotenv
from groq import Groq

load_dotenv()

MODEL_NAME = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
FALLBACK_MODELS = [
    "llama-3.1-8b-instant",
    "llama-3.3-70b-versatile",
]


def invoke_llm(prompt: str, temperature: float = 0.0, max_tokens: int = 2048) -> str:
    """Invoke Groq chat completion and return plain text response."""
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY not found in environment.")

    client = Groq(api_key=api_key)
    model_candidates = [MODEL_NAME] + [m for m in FALLBACK_MODELS if m != MODEL_NAME]
    last_error = None

    for model_name in model_candidates:
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content or ""
        except Exception as exc:
            last_error = exc
            if "model_not_found" not in str(exc):
                break

    raise RuntimeError(f"Groq completion failed: {last_error}")