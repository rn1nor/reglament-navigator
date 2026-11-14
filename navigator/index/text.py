"""Токенизация для BM25 и сопоставления: нижний регистр, стемминг Snowball (ru/en), стоп-слова."""
from __future__ import annotations

import re
from functools import lru_cache

import snowballstemmer

_ru = snowballstemmer.stemmer("russian")
_en = snowballstemmer.stemmer("english")
_WORD = re.compile(r"[а-яёa-z0-9]+(?:[-–][а-яёa-z0-9]+)*", re.I)
STOP = set("""и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот от меня еще нет о из ему
теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам ведь там потом себя ничего ей может они тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже себе под будет ж тогда кто этот того потому этого какой совсем ним здесь этом один почти мой тем чтобы нее сейчас были куда зачем всех никогда можно при наконец два об другой хоть после над больше тот через эти нас про всего них какая много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть том нельзя такой им более всегда конечно всю между
настоящий настоящего настоящим данный данного данным также либо иной иного иных
the a an of to in for on and or is are be""".split())


@lru_cache(maxsize=200_000)
def stem(w: str) -> str:
    if re.search(r"[а-яё]", w):
        return _ru.stemWord(w)
    return _en.stemWord(w)


def tokenize(text: str, keep_numbers: bool = True) -> list[str]:
    out = []
    for w in _WORD.findall(text.lower().replace("ё", "е")):
        if w in STOP or len(w) < 2:
            continue
        if w.isdigit() and not keep_numbers:
            continue
        out.append(stem(w))
    return out


def normalize_for_match(text: str) -> str:
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[«»\"'“”„]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
