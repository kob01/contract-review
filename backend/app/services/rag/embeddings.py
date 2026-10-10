"""向量化客户端。

两种实现：
- `OpenAICompatEmbeddings`：走 OpenAI 兼容 `/embeddings` 接口，可对接
  OpenAI、通义（text-embedding-v3）、BGE 服务、vLLM 等；
- `HashingEmbeddings`：确定性哈希向量，离线开发与单元测试专用。

哈希向量不承载语义，用它跑出来的相似度检索结果**不可用于业务判断**，
只用于验证链路连通；日志与接口响应都必须标明当前用的是哪一种。
"""

from __future__ import annotations

import hashlib
import math
from typing import Protocol

from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger(__name__)


class EmbeddingNotConfigured(RuntimeError):
    """需要真实语义向量但当前配置缺失。"""


class EmbeddingClient(Protocol):
    name: str
    dimensions: int

    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


class OpenAICompatEmbeddings:
    name = "openai_compatible"

    def __init__(self, *, base_url: str, api_key: str, model: str, dimensions: int) -> None:
        from openai import AsyncOpenAI

        self.model = model
        self.dimensions = dimensions
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=60)

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # 单次请求条数受限，分批发送
        vectors: list[list[float]] = []
        batch = 64
        for start in range(0, len(texts), batch):
            chunk = texts[start : start + batch]
            response = await self._client.embeddings.create(
                model=self.model, input=chunk, dimensions=self.dimensions
            )
            vectors.extend(item.embedding for item in response.data)
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        (vector,) = await self.embed_texts([text])
        return vector


class HashingEmbeddings:
    """词哈希 + 计数的确定性向量，做过 L2 归一化，可用余弦距离比较。"""

    name = "mock_hashing"

    def __init__(self, dimensions: int = 1536) -> None:
        self.dimensions = dimensions
        self.model = f"hashing-{dimensions}d"

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self._encode(text) for text in texts]

    async def embed_query(self, text: str) -> list[float]:
        return self._encode(text)

    def _encode(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in self._tokenize(text):
            digest = hashlib.md5(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self.dimensions
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[index] += sign * (1.0 + math.log1p(int.from_bytes(digest[5:8], "big") / 1e6))
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """中文按二元组、ASCII 词按整词切分，够用于近重复检测与链路验证。"""
        compact = "".join(text.split())
        tokens: list[str] = []
        ascii_buffer: list[str] = []

        for char in compact:
            if char.isascii() and char.isalnum():
                ascii_buffer.append(char.lower())
                continue
            if ascii_buffer:
                tokens.append("".join(ascii_buffer))
                ascii_buffer.clear()
            tokens.append(char)
            # 相邻汉字组成二元组，捕捉"利率""年化"这类搭配
        if ascii_buffer:
            tokens.append("".join(ascii_buffer))

        bigrams = [compact[index : index + 2] for index in range(len(compact) - 1)]
        return tokens + bigrams


def build_embedding_client(settings: Settings) -> EmbeddingClient:
    """按配置选择向量实现。"""
    api_key = settings.embedding_api_key or settings.llm_api_key
    base_url = settings.embedding_base_url or settings.llm_base_url

    if api_key and not settings.rag_use_mock_embedding:
        return OpenAICompatEmbeddings(
            base_url=base_url,
            api_key=api_key,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )

    if not settings.rag_use_mock_embedding:
        raise EmbeddingNotConfigured(
            "未配置 embedding_api_key/llm_api_key，且已关闭 mock 向量；"
            "请补密钥或把 RAG_USE_MOCK_EMBEDDING 设为 true"
        )

    logger.warning(
        "当前使用哈希 mock 向量（%s 维），检索结果不具备语义相关性，仅用于链路验证",
        settings.embedding_dimensions,
    )
    return HashingEmbeddings(dimensions=settings.embedding_dimensions)
