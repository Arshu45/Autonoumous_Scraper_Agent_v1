import logging
import os
from langchain_groq import ChatGroq
from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from llm.base import LLMClient

logger = logging.getLogger(__name__)


class GroqClient(LLMClient):
    """LLM client backed by Groq (langchain-groq).
    
    Rate limits (free tier): 100K TPD / 6K TPM on llama-3.3-70b-versatile.
    Falls back gracefully when the factory catches a 429.
    """

    def __init__(self, model: str | None = None):
        self._model   = model or os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
        self._api_key = os.getenv("GROQ_API_KEY")
        self._llm     = ChatGroq(model=self._model, api_key=self._api_key)

    def chat(self, messages: list[dict], temperature: float = 0) -> str:
        logger.debug("Groq call | model=%s | msgs=%d", self._model, len(messages))
        llm = self._llm.bind(temperature=temperature) if temperature != 0 else self._llm
        lc_messages = []
        for m in messages:
            role, content = m["role"], m["content"]
            if role == "system":
                lc_messages.append(SystemMessage(content=content))
            elif role == "user":
                lc_messages.append(HumanMessage(content=content))
            elif role == "assistant":
                lc_messages.append(AIMessage(content=content))
        response = llm.invoke(lc_messages).content
        logger.debug("Groq response | length=%d chars", len(response))
        return response
