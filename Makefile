PY=.venv/bin/python
KB=data/kb/mipt

# Настройки из .env (если файл есть) действуют и при запуске без Docker
ifneq (,$(wildcard .env))
include .env
export
endif

.PHONY: venv fetch ingest ingest-llm index contradictions eval ui api test lint demo examples \
        rebuild-org crawl ui-org ui-demo up up-api up-light stop logs

# ---------------------------------------------------------------- окружение и проверки
venv:
	python3 -m venv .venv && .venv/bin/pip install -q -U pip && .venv/bin/pip install -q -r requirements.txt -e . && .venv/bin/pip install -q torch sentence-transformers ruff

test:
	NAVIGATOR_OCR=off $(PY) -m pytest -q tests

lint:
	$(PY) -m ruff check navigator scripts tests

# ---------------------------------------------------------------- база документов организаторов (data/kb/org)
# Положите файлы в data/raw/org (ссылки: make crawl URL=https://...), затем make rebuild-org и make ui-org
rebuild-org:
	$(PY) scripts/rebuild.py --kb data/kb/org --src data/raw/org

crawl:
	$(PY) scripts/crawl_site.py --url $(URL) --depth 1 --out data/raw/org_web

ui-org:
	NAVIGATOR_KB=data/kb/org .venv/bin/streamlit run navigator/ui/app.py

ui:
	.venv/bin/streamlit run navigator/ui/app.py

api:
	.venv/bin/uvicorn navigator.api.app:app --reload

examples:
	$(PY) scripts/make_examples.py --kb data/kb/org --out examples
	$(PY) scripts/make_examples.py --kb data/kb/mipt_demo --out examples/mipt_demo

# ---------------------------------------------------------------- Docker
# интерфейс: http://localhost:8501
up:
	docker compose up -d --build

# интерфейс и API: http://localhost:8000/docs
up-api:
	docker compose --profile api up -d --build

# без моделей и без скачиваний (только BM25): для слабой машины или закрытой сети
up-light:
	PRELOAD_MODELS=0 NAVIGATOR_USE_DENSE=off NAVIGATOR_USE_RERANKER=off docker compose up -d --build

logs:
	docker compose --profile api logs -f

stop:
	docker compose --profile api down

# ---------------------------------------------------------------- публичные документы mipt.ru (data/kb/mipt)
fetch:
	$(PY) scripts/fetch_mipt_docs.py --out data/raw/mipt --limit 110

ingest:
	$(PY) scripts/ingest.py --src data/raw/mipt --out $(KB)

ingest-llm:
	$(PY) scripts/ingest.py --src data/raw/mipt --out $(KB) --llm

index:
	$(PY) scripts/build_index.py --kb $(KB) --dense on

contradictions:
	$(PY) scripts/find_contradictions.py --kb $(KB)

eval:
	$(PY) scripts/run_eval.py --kb $(KB) --label $(or $(LABEL),run)

demo: ingest index contradictions ui

ui-demo:
	NAVIGATOR_KB=data/kb/mipt_demo .venv/bin/streamlit run navigator/ui/app.py
