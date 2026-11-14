"""Конфигурация: пути, модели, пороги. Всё переопределяется переменными окружения."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(os.environ.get("NAVIGATOR_ROOT", Path(__file__).resolve().parent.parent))
DATA_DIR = Path(os.environ.get("NAVIGATOR_DATA", ROOT / "data"))
RAW_DIR = DATA_DIR / "raw"
KB_DIR = DATA_DIR / "kb"
INDEX_DIR = DATA_DIR / "index"
EVAL_DIR = DATA_DIR / "eval"
GLOSSARY_PATH = DATA_DIR / "glossary.yaml"

# --- LLM ---
# provider: "anthropic" | "openai_compat" | "mock"
LLM_PROVIDER = os.environ.get("NAVIGATOR_LLM", "auto")  # auto: anthropic если есть ключ, иначе mock
MODEL_GENERATOR = os.environ.get("NAVIGATOR_MODEL_GENERATOR", "claude-opus-5-5")
MODEL_EXTRACTOR = os.environ.get("NAVIGATOR_MODEL_EXTRACTOR", "claude-opus-5-5")
MODEL_JUDGE = os.environ.get("NAVIGATOR_MODEL_JUDGE", "claude-haiku-5-5")
EFFORT_GENERATOR = os.environ.get("NAVIGATOR_EFFORT_GENERATOR", "high")
EFFORT_EXTRACTOR = os.environ.get("NAVIGATOR_EFFORT_EXTRACTOR", "high")
EFFORT_JUDGE = os.environ.get("NAVIGATOR_EFFORT_JUDGE", "low")
ANTHROPIC_FALLBACKS = os.environ.get("NAVIGATOR_ANTHROPIC_FALLBACKS", "1") == "1"
OPENAI_COMPAT_BASE_URL = os.environ.get("NAVIGATOR_OPENAI_BASE_URL", "http://localhost:8000/v1")
OPENAI_COMPAT_MODEL = os.environ.get("NAVIGATOR_OPENAI_MODEL", "Qwen/Qwen3-32B")
OPENAI_COMPAT_API_KEY = os.environ.get("NAVIGATOR_OPENAI_API_KEY", "EMPTY")

# --- Retrieval ---
EMBEDDING_MODEL = os.environ.get("NAVIGATOR_EMBEDDING_MODEL", "BAAI/bge-m3")
RERANKER_MODEL = os.environ.get("NAVIGATOR_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3")
USE_DENSE = os.environ.get("NAVIGATOR_USE_DENSE", "auto")  # auto | on | off | hash
USE_RERANKER = os.environ.get("NAVIGATOR_USE_RERANKER", "auto")  # auto | on | off
TOP_K_CANDIDATES = int(os.environ.get("NAVIGATOR_TOP_K_CANDIDATES", "40"))
TOP_K_FINAL = int(os.environ.get("NAVIGATOR_TOP_K_FINAL", "8"))
MAX_CONTEXT_CHARS = int(os.environ.get("NAVIGATOR_MAX_CONTEXT_CHARS", "24000"))
# Бюджет реранкера: full — все кандидаты в двух формулировках (GPU, Apple Silicon); lite — экономный режим для CPU
# (меньше кандидатов и короче тексты, чтобы ответ в контейнере занимал секунды, а не минуту); auto — по устройству.
RERANK_BUDGET = os.environ.get("NAVIGATOR_RERANK_BUDGET", "auto")  # auto | full | lite
RERANK_TOP_LITE = int(os.environ.get("NAVIGATOR_RERANK_TOP_LITE", "20"))
DEVICE = os.environ.get("NAVIGATOR_DEVICE", "auto")  # auto | cpu | mps | cuda

# --- Policy thresholds (калибруются на тест-наборе) ---
GATE_MIN_RERANK = float(os.environ.get("NAVIGATOR_GATE_MIN_RERANK", "0.2"))   # логит/скор реранкера после сигмоиды
GATE_MIN_DENSE = float(os.environ.get("NAVIGATOR_GATE_MIN_DENSE", "0.45"))    # косинус bge-m3
GATE_MIN_HASH = float(os.environ.get("NAVIGATOR_GATE_MIN_HASH", "0.18"))      # косинус хеш-эмбеддинга (fallback)
GATE_MIN_BM25_NORM = float(os.environ.get("NAVIGATOR_GATE_MIN_BM25_NORM", "0.35"))
QUOTE_MATCH_THRESHOLD = int(os.environ.get("NAVIGATOR_QUOTE_MATCH", "88"))    # rapidfuzz partial_ratio
CLARIFY_SCORE_GAP = float(os.environ.get("NAVIGATOR_CLARIFY_GAP", "0.08"))

# --- Misc ---
TODAY_OVERRIDE = os.environ.get("NAVIGATOR_TODAY")  # YYYY-MM-DD для воспроизводимых прогонов



def default_kb() -> Path:
    """База по умолчанию: NAVIGATOR_KB, иначе база документов организаторов, иначе демо-база из публичных документов."""
    env = os.environ.get("NAVIGATOR_KB")
    if env:
        return Path(env)
    for cand in (KB_DIR / "org", KB_DIR / "mipt_demo", KB_DIR / "mipt"):
        if (cand / "kb.json").exists():
            return cand
    return KB_DIR / "org"
