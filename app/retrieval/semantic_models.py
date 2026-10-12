from __future__ import annotations

"""Lazy local Qwen3 semantic embedding and reranking adapters."""

import importlib.util
import math
from pathlib import Path
from typing import Any

from app.agents.model_router import QWEN3_EMBEDDING_MODEL, QWEN3_RERANKER_MODEL


QWEN3_EMBEDDING_TASK = (
    "Retrieve the most relevant Korean public-institution regulation clauses "
    "that answer the user's question with exact structural and temporal context."
)
QWEN3_EMBEDDING_WINDOW_CHARS = 2048
QWEN3_EMBEDDING_INPUT_STRATEGY = "char-windows-2048-weighted-mean-v1"


def semantic_runtime_available() -> bool:
    return all(
        importlib.util.find_spec(module) is not None
        for module in ("sentence_transformers", "torch", "transformers")
    )


class Qwen3EmbeddingAdapter:
    def __init__(
        self,
        *,
        model_name: str = QWEN3_EMBEDDING_MODEL,
        device: str = "cpu",
        truncate_dim: int | None = None,
        local_files_only: bool = False,
        model: Any | None = None,
    ) -> None:
        if model_name != QWEN3_EMBEDDING_MODEL:
            raise ValueError(f"Unsupported semantic embedding model: {model_name}")
        if truncate_dim is not None and not 64 <= int(truncate_dim) <= 4096:
            raise ValueError("truncate_dim must be between 64 and 4096")
        self.model_name = model_name
        self.device = str(device or "cpu")
        self.truncate_dim = int(truncate_dim) if truncate_dim is not None else None
        self.local_files_only = bool(local_files_only)
        self._model = model

    def encode_documents(self, texts: list[str], *, batch_size: int = 8) -> list[list[float]]:
        normalized = _validated_texts(texts)
        windows: list[str] = []
        spans: list[tuple[int, int]] = []
        for text in normalized:
            start = len(windows)
            windows.extend(text[offset : offset + QWEN3_EMBEDDING_WINDOW_CHARS]
                           for offset in range(0, len(text), QWEN3_EMBEDDING_WINDOW_CHARS))
            spans.append((start, len(windows)))
        vectors = self._encode(windows, batch_size=batch_size)
        if len(vectors) != len(windows):
            raise ValueError("embedding model returned an unexpected number of window vectors")
        result: list[list[float]] = []
        for start, end in spans:
            parts = [_normalized_vector(vector) for vector in vectors[start:end]]
            if len(parts) == 1:
                result.append(parts[0])
                continue
            dimension = len(parts[0])
            if any(len(part) != dimension for part in parts):
                raise ValueError("embedding window dimensions must match")
            # Keep every character of long appendices represented. Weight by
            # length so a short final window cannot dominate the document.
            pooled = [sum(part[axis] * len(windows[start + index])
                          for index, part in enumerate(parts)) for axis in range(dimension)]
            result.append(_normalized_vector(pooled))
        return result

    def encode_queries(self, queries: list[str], *, batch_size: int = 8) -> list[list[float]]:
        normalized = _validated_texts(queries)
        instructed = [f"Instruct: {QWEN3_EMBEDDING_TASK}\nQuery: {query}" for query in normalized]
        vectors = self._encode(instructed, batch_size=batch_size)
        return [_normalized_vector(vector) for vector in vectors]

    def _encode(self, texts: list[str], *, batch_size: int) -> list[Any]:
        model = self._load_model()
        kwargs: dict[str, Any] = {
            "batch_size": max(1, min(int(batch_size), 64)),
            "normalize_embeddings": True,
            "convert_to_numpy": True,
            "show_progress_bar": False,
        }
        if str(getattr(model, "device", self.device)).startswith("cuda"):
            # SentenceTransformer pads each batch to its longest input. A long
            # appendix must not multiply that allocation across eight clauses.
            longest = max((len(text) for text in texts), default=1)
            kwargs["batch_size"] = min(kwargs["batch_size"], max(1, 8192 // max(1, longest)))
        while True:
            try:
                return list(model.encode(texts, **kwargs))
            except RuntimeError as exc:
                if (
                    not str(getattr(model, "device", self.device)).startswith("cuda")
                    or "out of memory" not in str(exc).lower()
                    or kwargs["batch_size"] <= 1
                ):
                    raise
                kwargs["batch_size"] = max(1, kwargs["batch_size"] // 2)
            # Leave the exception scope first so its traceback releases tensors.
            # Retry the same complete input, without shortening or dropping text.
            import torch

            torch.cuda.empty_cache()

    def _load_model(self) -> Any:
        if self._model is not None:
            return self._model
        if not semantic_runtime_available():
            raise RuntimeError("sentence-transformers, transformers, and torch are required")
        from sentence_transformers import SentenceTransformer

        device = self.device
        model_kwargs: dict[str, Any] = {}
        if device == "auto":
            import torch

            device = "cuda" if torch.cuda.is_available() else "cpu"
        if device.startswith("cuda"):
            # Load directly at half precision instead of first allocating FP32
            # weights in host memory. Keep CPU callers at their original dtype.
            model_kwargs["torch_dtype"] = "float16"
        self._model = SentenceTransformer(
            self.model_name,
            device=device,
            truncate_dim=self.truncate_dim,
            trust_remote_code=True,
            local_files_only=self.local_files_only,
            **({"model_kwargs": model_kwargs} if model_kwargs else {}),
        )
        return self._model


class Qwen3RerankerAdapter:
    """Official yes/no token scoring contract for Qwen3-Reranker-0.6B."""

    SYSTEM_PROMPT = (
        'Judge whether the Document meets the requirements based on the Query and the Instruct provided. '
        'Note that the answer can only be "yes" or "no".'
    )

    def __init__(
        self,
        *,
        model_name: str = QWEN3_RERANKER_MODEL,
        device: str = "cpu",
        max_length: int = 4096,
        local_files_only: bool = False,
        model_path: str | Path | None = None,
        tokenizer: Any | None = None,
        model: Any | None = None,
    ) -> None:
        if model_name != QWEN3_RERANKER_MODEL:
            raise ValueError(f"Unsupported reranker model: {model_name}")
        if not 512 <= int(max_length) <= 8192:
            raise ValueError("max_length must be between 512 and 8192")
        self.model_name = model_name
        self.device = str(device or "cpu")
        self.max_length = int(max_length)
        self.local_files_only = bool(local_files_only)
        self.model_source = str(Path(model_path).resolve()) if model_path is not None else self.model_name
        self._tokenizer = tokenizer
        self._model = model

    def score(self, query: str, documents: list[str], *, batch_size: int = 8) -> list[float]:
        normalized_query = _validated_texts([query])[0]
        normalized_documents = _validated_texts(documents)
        tokenizer, model = self._load_runtime()
        prefix = f"<|im_start|>system\n{self.SYSTEM_PROMPT}<|im_end|>\n<|im_start|>user\n"
        suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
        prefix_tokens = tokenizer.encode(prefix, add_special_tokens=False)
        suffix_tokens = tokenizer.encode(suffix, add_special_tokens=False)
        true_id = tokenizer.convert_tokens_to_ids("yes")
        false_id = tokenizer.convert_tokens_to_ids("no")
        if not isinstance(true_id, int) or not isinstance(false_id, int):
            raise RuntimeError("Qwen3 reranker tokenizer lacks yes/no token ids")
        scores: list[float] = []
        for offset in range(0, len(normalized_documents), max(1, min(int(batch_size), 32))):
            batch = normalized_documents[offset : offset + max(1, min(int(batch_size), 32))]
            pairs = [
                f"<Instruct>: {QWEN3_EMBEDDING_TASK}\n<Query>: {normalized_query}\n<Document>: {document}"
                for document in batch
            ]
            encoded = tokenizer(
                pairs,
                padding=False,
                truncation=True,
                max_length=self.max_length - len(prefix_tokens) - len(suffix_tokens),
                add_special_tokens=False,
            )
            input_ids = [prefix_tokens + values + suffix_tokens for values in encoded["input_ids"]]
            attention = [[1] * len(values) for values in input_ids]
            padded = tokenizer.pad(
                {"input_ids": input_ids, "attention_mask": attention},
                padding=True,
                return_tensors="pt",
            )
            padded = {key: value.to(self.device) for key, value in padded.items()}
            import torch

            with torch.no_grad():
                logits = model(**padded).logits[:, -1, :]
                pair_logits = torch.stack((logits[:, false_id], logits[:, true_id]), dim=1)
                probabilities = torch.nn.functional.softmax(pair_logits, dim=1)[:, 1]
            scores.extend(float(value) for value in probabilities.detach().cpu().tolist())
        return [round(max(0.0, min(score, 1.0)), 8) for score in scores]

    def rerank(
        self,
        query: str,
        candidates: list[tuple[float, dict[str, Any]]],
        *,
        top_k: int,
    ) -> list[tuple[float, dict[str, Any]]]:
        bounded = candidates[:50]
        scores = self.score(query, [str(record.get("text") or "") for _, record in bounded])
        reranked = [
            (
                score,
                {
                    **record,
                    "retrieval_score": float(original_score),
                    "reranker_score": score,
                    "reranker_model": self.model_name,
                },
            )
            for (original_score, record), score in zip(bounded, scores, strict=True)
        ]
        return sorted(reranked, key=lambda item: item[0], reverse=True)[: max(1, int(top_k))]

    def _load_runtime(self) -> tuple[Any, Any]:
        if self._tokenizer is not None and self._model is not None:
            return self._tokenizer, self._model
        if not semantic_runtime_available():
            raise RuntimeError("transformers and torch are required for Qwen3 reranking")
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self._tokenizer = AutoTokenizer.from_pretrained(
            self.model_source,
            padding_side="left",
            trust_remote_code=True,
            local_files_only=self.local_files_only,
        )
        self._model = AutoModelForCausalLM.from_pretrained(
            self.model_source,
            trust_remote_code=True,
            local_files_only=self.local_files_only,
        ).to(self.device).eval()
        return self._tokenizer, self._model


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or len(left) != len(right):
        raise ValueError("cosine vectors must be non-empty and have matching dimensions")
    return round(sum(float(a) * float(b) for a, b in zip(left, right, strict=True)), 8)


def _validated_texts(values: list[str]) -> list[str]:
    if not isinstance(values, list) or not values:
        raise ValueError("model input must be a non-empty list")
    normalized = [" ".join(str(value or "").split()) for value in values]
    if any(not value for value in normalized):
        raise ValueError("model input text must not be empty")
    return normalized


def _normalized_vector(vector: Any) -> list[float]:
    values = [float(value) for value in vector]
    if not values:
        raise ValueError("embedding model returned an empty vector")
    norm = math.sqrt(sum(value * value for value in values))
    if not math.isfinite(norm) or norm <= 0.0:
        raise ValueError("embedding model returned a zero or non-finite vector")
    return [round(value / norm, 8) for value in values]
