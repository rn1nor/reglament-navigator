# Навигатор по регламентам: интерфейс (Streamlit, порт 8501) и API (FastAPI, порт 8000) в одном образе.
#
#   docker compose up -d --build                      обычная сборка: модели поиска скачиваются в образ (около 4,6 ГБ)
#   PRELOAD_MODELS=0 docker compose up -d --build     без моделей в образе: они скачаются при первом запуске в том models
#
# После сборки с моделями контейнер работает без доступа в интернет. Если модели скачать не удалось, сборка не падает:
# при запуске точка входа докачает их или включит режим без моделей (BM25) и напишет об этом в лог.
FROM python:3.13-slim-bookworm

# 1 — скачать модели поиска в образ; 0 — пропустить
ARG PRELOAD_MODELS=1
# зеркало Hugging Face, если основной адрес недоступен (например, https://hf-mirror.com)
ARG HF_ENDPOINT=https://huggingface.co
ARG NAVIGATOR_EMBEDDING_MODEL=BAAI/bge-m3
ARG NAVIGATOR_RERANKER_MODEL=BAAI/bge-reranker-v2-m3

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore \
    HF_ENDPOINT=${HF_ENDPOINT} \
    HF_HUB_DISABLE_TELEMETRY=1 \
    HF_HUB_DISABLE_PROGRESS_BARS=1 \
    DISABLE_SAFETENSORS_CONVERSION=1 \
    TOKENIZERS_PARALLELISM=false \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_SERVER_FILE_WATCHER_TYPE=none \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    NAVIGATOR_EMBEDDING_MODEL=${NAVIGATOR_EMBEDDING_MODEL} \
    NAVIGATOR_RERANKER_MODEL=${NAVIGATOR_RERANKER_MODEL} \
    NAVIGATOR_MODELS_IMAGE=/opt/models \
    NAVIGATOR_MODELS_CACHE=/models \
    HF_HOME=/models \
    NAVIGATOR_KB=/app/data/kb/org \
    NAVIGATOR_RAW=/app/data/raw/org \
    NAVIGATOR_OCR=auto

WORKDIR /app

# Системные программы: tesseract с русским языком для OCR сканов; antiword и catdoc для чтения старых файлов .doc
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng antiword catdoc \
 && rm -rf /var/lib/apt/lists/*

# Зависимости отдельным слоем: слой пересобирается только при изменении списков.
# Версии зафиксированы в requirements-lock.txt; torch ставится CPU-сборкой (без CUDA, образ меньше на несколько гигабайт).
COPY requirements.txt requirements-lock.txt ./
RUN pip install -r requirements.txt torch sentence-transformers \
      -c requirements-lock.txt --extra-index-url https://download.pytorch.org/whl/cpu

# Код приложения (импортируется через PYTHONPATH=/app, отдельная установка пакета не нужна)
COPY navigator ./navigator

# Модели поиска: скачиваются теми же загрузчиками, что работают в приложении, и сразу проверяются коротким прогоном.
# Список готовых моделей записывается в $NAVIGATOR_MODELS_IMAGE/.navigator-ready: по нему точка входа понимает,
# что можно работать без сети. Сбой загрузки сборку не прерывает.
RUN mkdir -p "$NAVIGATOR_MODELS_IMAGE" "$NAVIGATOR_MODELS_CACHE"; \
    if [ "$PRELOAD_MODELS" = "1" ]; then \
      echo "[navigator] скачиваю модели поиска в образ (около 4,6 ГБ); пропустить: --build-arg PRELOAD_MODELS=0"; \
      export HF_HOME="$NAVIGATOR_MODELS_IMAGE"; unset HF_HUB_DISABLE_PROGRESS_BARS; \
      if python -c "from navigator.index.dense import STEmbedder; e = STEmbedder(); print('[navigator] модель эмбеддингов готова:', e.name, e.encode(['проверка']).shape)" \
         && python -c "from navigator.index.rerank import Reranker; r = Reranker(); print('[navigator] реранкер готов:', r.name, r.score('вопрос', ['текст пункта']))"; then \
        printf '%s\n%s\n' "$NAVIGATOR_EMBEDDING_MODEL" "$NAVIGATOR_RERANKER_MODEL" > "$NAVIGATOR_MODELS_IMAGE/.navigator-ready"; \
        rm -rf "$NAVIGATOR_MODELS_IMAGE/xet" "$NAVIGATOR_MODELS_IMAGE/hub/.locks"; \
        find "$NAVIGATOR_MODELS_IMAGE" -name '*.incomplete' -delete; \
        echo "[navigator] модели в образе: $(du -sh "$NAVIGATOR_MODELS_IMAGE" | cut -f1)"; \
      else \
        echo "[navigator] ВНИМАНИЕ: модели скачать не удалось, образ собран без них; они будут скачаны при первом запуске"; \
        rm -rf "$NAVIGATOR_MODELS_IMAGE"; mkdir -p "$NAVIGATOR_MODELS_IMAGE"; \
      fi; \
    else \
      echo "[navigator] PRELOAD_MODELS=$PRELOAD_MODELS: модели в образ не скачиваются"; \
    fi

# Скрипты, оформление интерфейса и данные: готовая база организаторов, её документы, распознанный текст сканов,
# глоссарий и тест-наборы (состав задаёт .dockerignore). В compose поверх монтируется ./data с хоста.
COPY scripts ./scripts
COPY .streamlit ./.streamlit
COPY data ./data
# файл мог получить окончания строк Windows при клонировании: приводим к виду, который понимает sh
RUN sed -i 's/\r$//' scripts/docker_entrypoint.sh

EXPOSE 8501 8000

# По умолчанию контейнер запускает интерфейс; для API проверка переопределена в docker-compose.yml
HEALTHCHECK --interval=20s --timeout=10s --start-period=900s --retries=5 \
  CMD python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8501/_stcore/health', timeout=8)" || exit 1

ENTRYPOINT ["sh", "scripts/docker_entrypoint.sh"]
CMD ["streamlit", "run", "navigator/ui/app.py", "--server.port", "8501", "--server.address", "0.0.0.0"]
