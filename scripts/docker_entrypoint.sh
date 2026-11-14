#!/bin/sh
# Точка входа контейнера. Ничего не спрашивает и не требует сети, если модели уже есть в образе.
#
#   1. Режим поиска. Значение auto у NAVIGATOR_USE_DENSE и NAVIGATOR_USE_RERANKER уточняется по доступной памяти
#      и по наличию моделей (в образе, в томе models, в сети). Если моделей нет и взять их неоткуда, сервис
#      поднимается в режиме BM25 без моделей и пишет об этом в лог. Явно заданные on / off / hash не меняются.
#   2. База знаний. Готовая база используется как есть. Если её нет, она собирается из папки с документами;
#      сборку ведёт один контейнер (блокировка каталогом), остальные ждут. Брошенная блокировка снимается сама.
#   3. Запуск переданной команды: интерфейс или API.
set -eu
umask 0000  # файлы в примонтированном ./data создаёт root: оставляем их доступными пользователю хоста

# Сообщения точки входа идут в stderr: stdout остаётся чистым для вывода самой команды (например, ask.py --json)
log() { printf '%s\n' "[navigator] $*" >&2; }

# Пустое значение переменной NAVIGATOR_* или ANTHROPIC_* равно её отсутствию: docker-compose.yml передаёт пустую строку
# для всего, что не задано в .env, а приложение тогда берёт значение по умолчанию из navigator/config.py.
for _name in $(env | sed -n -e 's/^\(NAVIGATOR_[A-Z0-9_]*\)=$/\1/p' -e 's/^\(ANTHROPIC_[A-Z0-9_]*\)=$/\1/p'); do
  unset "$_name"
done

DATA_ROOT="${NAVIGATOR_DATA:-/app/data}"
KB="${NAVIGATOR_KB:-$DATA_ROOT/kb/org}"
RAW="${NAVIGATOR_RAW:-$DATA_ROOT/raw/org}"
DENSE="${NAVIGATOR_USE_DENSE:-auto}"
RERANK="${NAVIGATOR_USE_RERANKER:-auto}"
EMB_MODEL="${NAVIGATOR_EMBEDDING_MODEL:-BAAI/bge-m3}"
RR_MODEL="${NAVIGATOR_RERANKER_MODEL:-BAAI/bge-reranker-v2-m3}"
IMG_MODELS="${NAVIGATOR_MODELS_IMAGE:-/opt/models}"      # модели, скачанные при сборке образа
CACHE_MODELS="${NAVIGATOR_MODELS_CACHE:-/models}"        # том models: модели, скачанные при запуске
SERVICE="${NAVIGATOR_SERVICE:-ui}"
MODEL_TIMEOUT="${NAVIGATOR_MODEL_TIMEOUT:-2400}"         # предел ожидания загрузки одной модели, с
LOCK="$KB.building"

CHILD=""
TICKER=""
LOCKED=0
ONLINE=""

on_term() {
  log "получен сигнал остановки"
  if [ -n "$TICKER" ]; then kill "$TICKER" 2>/dev/null || true; fi
  if [ -n "$CHILD" ]; then kill "$CHILD" 2>/dev/null || true; fi
  if [ "$LOCKED" = 1 ]; then rm -rf "$LOCK"; fi
  exit 143
}
trap on_term TERM INT

# Команда выполняется в фоне с ожиданием: так сигнал остановки обрабатывается сразу, а не после её завершения.
run_child() {
  "$@" &
  CHILD=$!
  _rc=0
  wait "$CHILD" || _rc=$?
  CHILD=""
  return "$_rc"
}

stop_ticker() {
  if [ -n "$TICKER" ]; then
    kill "$TICKER" 2>/dev/null || true
    wait "$TICKER" 2>/dev/null || true
    TICKER=""
  fi
}

# ------------------------------------------------------------------ 1. режим поиска
# Доступная память в МБ: меньшее из лимита контейнера и памяти машины (для Docker Desktop это память его ВМ).
mem_mb() {
  if [ -n "${NAVIGATOR_MEM_MB:-}" ]; then echo "$NAVIGATOR_MEM_MB"; return 0; fi
  _total=""
  _limit=""
  if [ -r /proc/meminfo ]; then _total=$(awk '/^MemTotal:/ {print int($2 / 1024)}' /proc/meminfo); fi
  for _f in /sys/fs/cgroup/memory.max /sys/fs/cgroup/memory/memory.limit_in_bytes; do
    if [ -r "$_f" ]; then
      _v=$(cat "$_f")
      case "$_v" in
        ''|*[!0-9]*) ;;
        *) _limit=$((_v / 1048576)) ;;
      esac
      break
    fi
  done
  if [ -n "$_limit" ] && [ -n "$_total" ] && [ "$_limit" -lt "$_total" ]; then echo "$_limit"; else echo "$_total"; fi
}

# Модель считается готовой, если её имя записано в .navigator-ready каталога кэша (пишется после успешной загрузки).
ready() {
  if [ -f "$1/.navigator-ready" ] && grep -qxF "$2" "$1/.navigator-ready"; then return 0; fi
  return 1
}

hub_online() {
  if [ -z "$ONLINE" ]; then
    # тем же клиентом, которым потом скачиваются модели: учитывает HF_ENDPOINT, прокси и сертификаты
    if python -c "import sys; from huggingface_hub import HfApi; HfApi().model_info(sys.argv[1], timeout=10)" "$1" >/dev/null 2>&1; then
      ONLINE=yes
    else
      ONLINE=no
    fi
  fi
  if [ "$ONLINE" = yes ]; then return 0; fi
  return 1
}

# Загрузка модели теми же загрузчиками, что работают в приложении. $1: dense | rerank, $2: имя модели.
fetch_model() {
  if ! hub_online "$2"; then
    log "модели $2 нет ни в образе, ни в томе models, а Hugging Face недоступен"
    return 1
  fi
  log "скачиваю модель $2 (около 2,3 ГБ, один раз; сохраняется в томе models)"
  ( while :; do sleep 20; log "  загружено $(du -sm "$CACHE_MODELS" 2>/dev/null | cut -f1) МБ"; done ) &
  TICKER=$!
  if [ "$1" = dense ]; then
    _code="from navigator.index.dense import STEmbedder; e = STEmbedder(); e.encode(['проверка'])"
  else
    _code="from navigator.index.rerank import Reranker; r = Reranker(); r.score('вопрос', ['текст пункта'])"
  fi
  _ok=0
  if command -v timeout >/dev/null 2>&1; then
    run_child timeout "$MODEL_TIMEOUT" python -c "$_code" || _ok=$?
  else
    run_child python -c "$_code" || _ok=$?
  fi
  stop_ticker
  if [ "$_ok" = 0 ]; then
    printf '%s\n' "$2" >> "$CACHE_MODELS/.navigator-ready"
    log "модель $2 готова"
    return 0
  fi
  log "модель $2 скачать не удалось (код $_ok)"
  return 1
}

if [ "${NAVIGATOR_SKIP_SETUP:-0}" != 1 ]; then
  # --- память
  MEM=$(mem_mb)
  if [ -n "$MEM" ]; then
    # интерфейс — основной сервис; API (необязательный профиль) получает модели только при заметном запасе памяти,
    # чтобы два процесса с моделями не исчерпали память вместе
    if [ "$SERVICE" = api ]; then _full=15000; _rr=10000; else _full=9500; _rr=4500; fi
    _full="${NAVIGATOR_MEM_FULL_MB:-$_full}"
    _rr="${NAVIGATOR_MEM_RERANK_MB:-$_rr}"
    if [ "$MEM" -lt "$_rr" ]; then
      if [ "$DENSE" = auto ] || [ "$RERANK" = auto ]; then
        log "доступно $MEM МБ памяти: моделям поиска этого мало, включаю режим BM25 без моделей"
        log "  (чтобы включить реранкер, дайте Docker от $_rr МБ; полный режим: от $_full МБ)"
      fi
      if [ "$DENSE" = auto ]; then DENSE=off; fi
      if [ "$RERANK" = auto ]; then RERANK=off; fi
    elif [ "$MEM" -lt "$_full" ]; then
      if [ "$DENSE" = auto ]; then
        log "доступно $MEM МБ памяти: работаю с реранкером без плотного поиска (полный режим: от $_full МБ)"
        DENSE=off
      fi
    fi
  fi

  # --- модели
  WANT_DENSE=0
  WANT_RERANK=0
  case "$DENSE" in auto|on) WANT_DENSE=1 ;; esac
  case "$RERANK" in auto|on) WANT_RERANK=1 ;; esac
  if [ "$WANT_DENSE" = 1 ] || [ "$WANT_RERANK" = 1 ]; then
    IN_IMAGE=1
    if [ "$WANT_DENSE" = 1 ] && ! ready "$IMG_MODELS" "$EMB_MODEL"; then IN_IMAGE=0; fi
    if [ "$WANT_RERANK" = 1 ] && ! ready "$IMG_MODELS" "$RR_MODEL"; then IN_IMAGE=0; fi
    if [ "$IN_IMAGE" = 1 ]; then
      export HF_HOME="$IMG_MODELS" HF_HUB_OFFLINE=1
      log "модели поиска взяты из образа, доступ в интернет не нужен"
    else
      export HF_HOME="$CACHE_MODELS"
      mkdir -p "$CACHE_MODELS" 2>/dev/null || true
      if [ "$WANT_DENSE" = 1 ] && ! ready "$CACHE_MODELS" "$EMB_MODEL"; then
        if ! fetch_model dense "$EMB_MODEL"; then
          if [ "$DENSE" = auto ]; then
            DENSE=off
            WANT_DENSE=0
            log "плотный поиск отключён: работаю без модели эмбеддингов"
          else
            log "ВНИМАНИЕ: NAVIGATOR_USE_DENSE=on, но модель недоступна: сервис не запустится, пока она не скачается"
          fi
        fi
      fi
      if [ "$WANT_RERANK" = 1 ] && ! ready "$CACHE_MODELS" "$RR_MODEL"; then
        if ! fetch_model rerank "$RR_MODEL"; then
          if [ "$RERANK" = auto ]; then
            RERANK=off
            WANT_RERANK=0
            log "реранкер отключён: работаю без модели переранжирования"
          else
            log "ВНИМАНИЕ: NAVIGATOR_USE_RERANKER=on, но модель недоступна: сервис не запустится, пока она не скачается"
          fi
        fi
      fi
      ALL_CACHED=1
      if [ "$WANT_DENSE" = 1 ] && ! ready "$CACHE_MODELS" "$EMB_MODEL"; then ALL_CACHED=0; fi
      if [ "$WANT_RERANK" = 1 ] && ! ready "$CACHE_MODELS" "$RR_MODEL"; then ALL_CACHED=0; fi
      if [ "$ALL_CACHED" = 1 ] && { [ "$WANT_DENSE" = 1 ] || [ "$WANT_RERANK" = 1 ]; }; then
        export HF_HUB_OFFLINE=1
        log "модели поиска взяты из тома models, доступ в интернет не нужен"
      fi
    fi
  fi
  export NAVIGATOR_USE_DENSE="$DENSE" NAVIGATOR_USE_RERANKER="$RERANK"
  if [ "$WANT_DENSE" = 1 ] && [ "$WANT_RERANK" = 1 ]; then
    log "режим поиска: BM25 + плотный поиск ($EMB_MODEL) + реранкер ($RR_MODEL)"
  elif [ "$WANT_RERANK" = 1 ]; then
    log "режим поиска: BM25 + реранкер ($RR_MODEL)"
  elif [ "$WANT_DENSE" = 1 ]; then
    log "режим поиска: BM25 + плотный поиск ($EMB_MODEL), без реранкера"
  else
    log "режим поиска: BM25 без моделей (NAVIGATOR_USE_DENSE=$DENSE, NAVIGATOR_USE_RERANKER=$RERANK)"
  fi

  # ---------------------------------------------------------------- 2. база знаний
  # В папке с документами есть хотя бы один файл (скрытые не считаются): пустую или отсутствующую папку не разбираем.
  has_docs() {
    if [ -d "$1" ] && [ -n "$(find "$1" -type f ! -name '.*' 2>/dev/null | head -n 1)" ]; then return 0; fi
    return 1
  }

  # База пригодна, если kb.json читается и в нём есть хотя бы один документ.
  usable_kb() {
    if [ ! -f "$1/kb.json" ]; then return 1; fi
    _n=$(python -c "import json, sys; print(len(json.load(open(sys.argv[1], encoding='utf-8')).get('documents', [])))" "$1/kb.json" 2>/dev/null || echo 0)
    if [ "${_n:-0}" -gt 0 ]; then return 0; fi
    return 1
  }

  # Сколько секунд назад сборщик последний раз подтверждал, что жив.
  lock_age() {
    python -c "import os, sys, time; p = sys.argv[1]; hb = os.path.join(p, 'heartbeat'); print(int(time.time() - os.path.getmtime(hb if os.path.exists(hb) else p)))" "$1" 2>/dev/null || echo 0
  }

  build_or_wait() {
    mkdir -p "$(dirname "$KB")" 2>/dev/null || true
    _nolock=0
    while ! usable_kb "$KB" || [ -d "$LOCK" ]; do
      if mkdir "$LOCK" 2>/dev/null; then
        LOCKED=1
        if usable_kb "$KB" || ! has_docs "$RAW"; then  # базу уже собрал другой контейнер либо собирать не из чего
          rm -rf "$LOCK"
          LOCKED=0
          break
        fi
        log "база $KB не найдена: собираю из $RAW (разбор документов, индекс, поиск противоречий)"
        ( while [ -d "$LOCK" ]; do touch "$LOCK/heartbeat" 2>/dev/null || exit 0; sleep 10; done ) &
        TICKER=$!
        _build=0
        run_child python scripts/rebuild.py --kb "$KB" --src "$RAW" || _build=$?
        stop_ticker
        rm -rf "$LOCK"
        LOCKED=0
        if [ "$_build" != 0 ]; then
          log "ОШИБКА: сборка базы из $RAW завершилась с кодом $_build (подробности выше)"
          return 1
        fi
        if ! usable_kb "$KB"; then
          log "ОШИБКА: после сборки из $RAW в базе нет документов (в папке нет файлов поддерживаемых форматов?)"
          return 1
        fi
        log "база собрана"
        break
      fi
      if [ ! -d "$LOCK" ]; then  # блокировку не удалось создать, и её никто не держит: каталог недоступен для записи
        _nolock=$((_nolock + 1))
        if [ "$_nolock" -ge 3 ]; then
          log "ОШИБКА: не удаётся создать $LOCK: каталог базы недоступен для записи"
          return 1
        fi
        sleep 1
        continue
      fi
      _age=$(lock_age "$LOCK")
      if [ "$_age" -gt 120 ]; then
        log "блокировка сборки не обновлялась $_age с: считаю её брошенной и снимаю"
        rm -rf "$LOCK"
        continue
      fi
      log "базу собирает другой контейнер, жду"
      sleep 5
    done
    return 0
  }

  # Пересборка запускается, только если базы нет (или она пуста) и в папке документов есть файлы.
  if [ -d "$LOCK" ] || { ! usable_kb "$KB" && has_docs "$RAW"; }; then
    build_or_wait || log "базу $KB собрать не удалось: ищу другую готовую базу"
  fi

  if ! usable_kb "$KB"; then
    FOUND=""
    for _cand in "$DATA_ROOT/kb/org" "$DATA_ROOT/kb/mipt_demo" "$DATA_ROOT/kb/mipt" "$DATA_ROOT"/kb/*; do
      if [ "$_cand" != "$KB" ] && usable_kb "$_cand"; then FOUND="$_cand"; break; fi
    done
    if [ -n "$FOUND" ]; then
      log "базы $KB нет, а собрать её не из чего (папка $RAW пуста или недоступна): использую готовую базу $FOUND"
      KB="$FOUND"
    elif has_docs "$RAW"; then
      log "ОШИБКА: базу $KB не удалось собрать из $RAW (причина выше), других готовых баз нет."
      exit 1
    else
      log "ОШИБКА: нет ни базы знаний ($KB), ни документов для её сборки ($RAW)."
      log "  Положите документы в data/raw/org на хосте (каталог ./data примонтирован в контейнер) и перезапустите его."
      exit 1
    fi
  fi
  export NAVIGATOR_KB="$KB"
  log "база знаний: $KB"
fi

# ------------------------------------------------------------------ 3. запуск
if [ "$#" -eq 0 ]; then
  set -- streamlit run navigator/ui/app.py --server.port 8501 --server.address 0.0.0.0
fi
trap - TERM INT
log "запуск: $*"
exec "$@"
