"""向量化客户端。

三种实现：
- `OllamaEmbeddings`：本地 Ollama 原生 `/api/embed`，默认对接 bge-m3（1024 维）；
- `OpenAICompatEmbeddings`：走 OpenAI 兼容 `/embeddings` 接口，可对接
  OpenAI、通义（text-embedding-v3）、vLLM 等；
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


class EmbeddingDimensionMismatch(RuntimeError):
    """模型返回的向量维度与 `EMBEDDING_DIMENSIONS` 不一致。"""


class EmbeddingClient(Protocol):
    name: str
    dimensions: int

    async def embed_texts(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...


def _check_dimensions(vectors: list[list[float]], expected: int, source: str) -> list[list[float]]:
    """维度不一致时直接报错，不能让错维向量写进 pgvector 列。"""
    for vector in vectors:
        if len(vector) != expected:
            raise EmbeddingDimensionMismatch(
                f"{source} 返回 {len(vector)} 维向量，与 EMBEDDING_DIMENSIONS={expected} 不一致；"
                "请对齐模型与维度（bge-m3=1024，text-embedding-3-small=1536）并重建向量列"
            )
    return vectors


class OllamaEmbeddings:
    """Ollama 原生批量接口，一次 `/api/embed` 可送多条文本。"""

    name = "ollama"

    def __init__(self, *, base_url: str, model: str, dimensions: int) -> None:
        import httpx

        self.model = model
        self.dimensions = dimensions
        self._base = _normalize_ollama_base(base_url)
        self._client = httpx.AsyncClient(base_url=self._base, timeout=120)

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors: list[list[float]] = []
        batch = 32
        for start in range(0, len(texts), batch):
            chunk = texts[start : start + batch]
            response = await self._client.post(
                "/api/embed",
                json={"model": self.model, "input": chunk, "truncate": True},
            )
            response.raise_for_status()
            payload = response.json()
            embeddings = payload.get("embeddings")
            if not isinstance(embeddings, list) or len(embeddings) != len(chunk):
                raise EmbeddingDimensionMismatch(
                    f"Ollama /api/embed 返回异常：期望 {len(chunk)} 条向量，实际 {embeddings!r:.200}"
                )
            vectors.extend([float(value) for value in item] for item in embeddings)
        return _check_dimensions(vectors, self.dimensions, f"ollama:{self.model}")

    async def embed_query(self, text: str) -> list[float]:
        (vector,) = await self.embed_texts([text])
        return vector

    async def aclose(self) -> None:
        await self._client.aclose()


def _normalize_ollama_base(base_url: str) -> str:
    """兼容用户把 OpenAI 兼容地址（.../v1）填进 EMBEDDING_BASE_URL 的写法。"""
    cleaned = (base_url or "").rstrip("/")
    if cleaned.endswith("/v1"):
        return cleaned[: -len("/v1")]
    return cleaned


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
        return _check_dimensions(vectors, self.dimensions, f"openai_compatible:{self.model}")

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


def _looks_like_ollama(base_url: str) -> bool:
    lowered = (base_url or "").lower()
    return ":11434" in lowered or "ollama" in lowered


def describe_embedding_backend(settings: Settings) -> str:
    """不建连接地描述将用到的向量后端，供健康检查与统计接口复用。

    健康检查不能为了取一个名字就新建 httpx/OpenAI 客户端，
    否则每次探测都会多一个需要关闭的连接。
    """
    if settings.rag_use_mock_embedding or settings.embedding_provider == "mock":
        return HashingEmbeddings.name

    provider = settings.embedding_provider
    if provider == "auto":
        base_url = settings.embedding_base_url or settings.llm_base_url
        provider = "ollama" if _looks_like_ollama(base_url) else "openai_compatible"
    return provider


def build_embedding_client(settings: Settings) -> EmbeddingClient:
    """按配置选择向量实现。"""
    if settings.rag_use_mock_embedding or settings.embedding_provider == "mock":
        logger.warning(
            "当前使用哈希 mock 向量（%s 维），检索结果不具备语义相关性，仅用于链路验证",
            settings.embedding_dimensions,
        )
        return HashingEmbeddings(dimensions=settings.embedding_dimensions)

    base_url = settings.embedding_base_url or settings.llm_base_url
    api_key = settings.embedding_api_key or settings.llm_api_key
    provider = settings.embedding_provider
    if provider == "auto":
        provider = "ollama" if _looks_like_ollama(base_url) else "openai_compatible"

    if provider == "ollama":
        return OllamaEmbeddings(
            base_url=base_url,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )

    if not api_key:
        raise EmbeddingNotConfigured(
            "未配置 embedding_api_key/llm_api_key，且已关闭 mock 向量；"
            "请补密钥、把 EMBEDDING_PROVIDER 指向本地 ollama，或把 RAG_USE_MOCK_EMBEDDING 设为 true"
        )

    return OpenAICompatEmbeddings(
        base_url=base_url,
        api_key=api_key,
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
    )
