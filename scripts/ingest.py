"""python scripts/ingest.py --src data/raw/mipt --out data/kb/mipt"""
import sys
from pathlib import Path as _P

sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
import argparse
from pathlib import Path

from navigator.ingest.pipeline import ingest

ap = argparse.ArgumentParser()
ap.add_argument("--src", required=True, nargs="+", help="одна или несколько папок/файлов")
ap.add_argument("--out", required=True)
ap.add_argument("--llm", action="store_true", help="LLM-экстракция метаданных, процедур и связей; расшифровка сканов моделью")
args = ap.parse_args()
enrich = transcribe = None
if args.llm:
    from navigator.ingest.extract_llm import enrich_document, transcribe_pdf
    from navigator.llm.base import get_provider
    from navigator.llm.privacy import is_external, kb_is_confidential

    provider = get_provider()
    if kb_is_confidential(Path(args.out)) and is_external(provider):
        print("!! база помечена как закрытая (policy.json): внешняя модель для экстракции не используется; подключите локальную модель")
        provider = None
    if provider is None:
        pass
    elif provider.kind == "mock":
        print("!! --llm задан, но ключ модели не найден (ANTHROPIC_API_KEY): экстракция пропущена")
    else:
        enrich = lambda doc, raw: enrich_document(doc, raw, provider)  # noqa: E731
        transcribe = lambda path, raw: transcribe_pdf(path, raw, provider)  # noqa: E731
try:
    kb = ingest([Path(x) for x in args.src], Path(args.out), progress=print, enrich=enrich, transcribe=transcribe)
except (FileNotFoundError, ValueError) as e:  # нет источника или в нём нет читаемых документов: существующая база не тронута
    print(f"!! {e}", file=sys.stderr)
    sys.exit(2)
print(f"\nдокументов: {len(kb.documents)}; находок аудита: {len(kb.audit.findings)}; {kb.audit.counts}")
