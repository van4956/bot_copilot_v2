"""Воспроизводимо подготавливает локальный словник без зависимостей бота."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "prototypes" / "wordli" / "data"
BLOCKED_TAGS = {
    "Name",
    "Surn",
    "Patr",
    "Geox",
    "Orgn",
    "Abbr",
    "Trad",
    "Init",
    "Arch",
    "Erro",
    "Dist",
}
PROPER_TAGS = {"Name", "Surn", "Patr", "Geox", "Orgn", "Trad", "Abbr"}
SOURCE_PACKAGES = [
    {
        "name": "pymorphy3-dicts-ru",
        "version": "2.4.417150.4580142",
        "filename": (
            "pymorphy3_dicts_ru-2.4.417150.4580142-py2.py3-none-any.whl"
        ),
        "sha256": (
            "718bac64c73c10c16073a199402657283d9b64c04188b694f6d3e9b0d85440f4"
        ),
        "url": "https://pypi.org/project/pymorphy3-dicts-ru/2.4.417150.4580142/",
        "role": "OpenCorpora: словарные формы и грамматические признаки",
        "attribution": (
            "OpenCorpora contributors; Danylo Halaiko (сборка пакета)"
        ),
        "code_license": "MIT",
        "data_license": "CC BY-SA 3.0",
        "data_license_url": "https://creativecommons.org/licenses/by-sa/3.0/",
    },
    {
        "name": "pymorphy3",
        "version": "2.0.6",
        "filename": "pymorphy3-2.0.6-py3-none-any.whl",
        "sha256": (
            "0254317c02ce3ea17e080b7fc9d675e44662b3a5296bae68605b7a41d25b36c3"
        ),
        "url": "https://pypi.org/project/pymorphy3/2.0.6/",
        "role": "Инструмент отбора, не требуется при запуске игры",
        "attribution": "Danylo Halaiko и участники pymorphy3/pymorphy2",
        "code_license": "MIT",
    },
    {
        "name": "wordfreq",
        "version": "3.1.1",
        "filename": "wordfreq-3.1.1-py3-none-any.whl",
        "sha256": (
            "4b1c6ecffc6198be3396d5cf871c4423ca71c907c231348d352dd54d62b97473"
        ),
        "url": "https://pypi.org/project/wordfreq/3.1.1/",
        "role": "Отбор по частоте; сами таблицы частот не экспортируются",
        "attribution": (
            "Robyn Speer; авторы корпусов и SUBTLEX перечислены "
            "в wordfreq-NOTICE.md"
        ),
        "code_license": "Apache-2.0",
        "data_license": "CC BY-SA 4.0",
        "data_license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
        "notice_url": (
            "https://github.com/rspeer/wordfreq/blob/"
            "9e033608b2538615fa4424569c18009a5c303376/NOTICE.md"
        ),
        "notice_sha256": (
            "d9b104dafe63886af55e7cea7484824e32e12d4e1f57f319923dd643b36d6b15"
        ),
    },
]


def normalize_word(word: str) -> str:
    """Приводит слово к игровому алфавиту без отдельной буквы Ё."""
    return unicodedata.normalize("NFC", word).upper().replace("Ё", "Е")


def _write_json(path: Path, value: Any) -> None:
    """Записывает стабильный UTF-8 JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def _list_digest(words: list[str]) -> str:
    """Возвращает контрольную сумму нормализованного списка."""
    return hashlib.sha256(
        ("\n".join(words) + "\n").encode("utf-8")
    ).hexdigest()


def _verify_sources(wheels_dir: Path) -> None:
    """Проверяет версии инструментов и хеши опубликованных PyPI-пакетов."""
    for source in SOURCE_PACKAGES:
        actual_version = importlib.metadata.version(source["name"])
        if actual_version != source["version"]:
            raise ValueError(
                f"Неверная версия {source['name']}: {actual_version}"
            )
        wheel = wheels_dir / source["filename"]
        if hashlib.sha256(wheel.read_bytes()).hexdigest() != source["sha256"]:
            raise ValueError(f"Не совпал SHA-256 источника: {wheel.name}")


def build_dictionary(
    tools_dir: Path,
    wheels_dir: Path,
    output: Path,
    audit_output: Path | None = None,
) -> dict[str, Any]:
    """Отбирает существительные и более простой набор ответов."""
    sys.path.insert(0, str(tools_dir.resolve()))
    _verify_sources(wheels_dir)
    pymorphy = importlib.import_module("pymorphy3")
    wordfreq = importlib.import_module("wordfreq")
    rules_path = DATA_DIR / "selection_rules.json"
    rules = json.loads(rules_path.read_text(encoding="utf-8"))
    rule_words = (
        rules["common_candidates"]
        + rules["proper_homonyms_allowed"]
        + rules["answer_excluded"]
        + [word for group in rules["excluded"].values() for word in group]
    )
    if any(not re.fullmatch("[А-Я]{5}", word) for word in rule_words):
        raise ValueError("Ручные правила должны содержать пятибуквенные слова")
    excluded = {word for group in rules["excluded"].values() for word in group}
    common_candidates = set(rules["common_candidates"])
    allowed_homonyms = set(rules["proper_homonyms_allowed"])
    answer_excluded = set(rules["answer_excluded"])
    morph = pymorphy.MorphAnalyzer()
    source_words = wordfreq.top_n_list("ru", rules["frequency_candidates"])
    source_words = sorted(
        set(source_words) | {word.lower() for word in common_candidates}
    )
    candidates: dict[str, dict[str, Any]] = {}
    for source_word in source_words:
        normalized = normalize_word(source_word)
        if not re.fullmatch("[А-Я]{5}", normalized) or normalized in excluded:
            continue
        if not morph.word_is_known(source_word):
            continue
        parses = morph.parse(source_word)
        valid = [
            parsed
            for parsed in parses
            if parsed.is_known
            and {"NOUN", "sing", "nomn"}.issubset(parsed.tag.grammemes)
            and not BLOCKED_TAGS.intersection(parsed.tag.grammemes)
            and normalize_word(parsed.normal_form) == normalized
        ]
        if not valid:
            continue
        noun_score = max(parsed.score for parsed in valid)
        proper_score = max(
            (
                parsed.score
                for parsed in parses
                if PROPER_TAGS.intersection(parsed.tag.grammemes)
            ),
            default=0,
        )
        if proper_score > noun_score and normalized not in allowed_homonyms:
            continue
        frequency = max(
            wordfreq.zipf_frequency(source_word, "ru"),
            wordfreq.zipf_frequency(normalized.lower(), "ru"),
        )
        if (
            frequency < rules["minimum_zipf"]
            and normalized not in common_candidates
        ):
            continue
        entry = {
            "word": normalized,
            "zipf": frequency,
            "source_form": source_word,
            "tags": sorted({str(parsed.tag) for parsed in valid}),
        }
        previous = candidates.get(normalized)
        if previous is None or previous["zipf"] < frequency:
            candidates[normalized] = entry

    preferred = common_candidates.intersection(candidates)
    ranked = sorted(
        candidates, key=lambda word: (-candidates[word]["zipf"], word)
    )
    remaining = [word for word in ranked if word not in preferred]
    target = rules["target_words"]
    capacity = (
        len(remaining) if target is None else max(0, target - len(preferred))
    )
    words = sorted(preferred | set(remaining[:capacity]))
    answers = sorted(
        word
        for word in words
        if word not in answer_excluded
        and (
            candidates[word]["zipf"] >= rules["answer_minimum_zipf"]
            or word in preferred
        )
    )
    expected = {"КОШКА", "КОМАР", "ТАЧКА", "СЛОВО", "КНИГА", "РАДИО"}
    if not expected.issubset(words) or not answers:
        raise ValueError("Отбор потерял контрольные слова или набор ответов")
    words_hash = _list_digest(words)
    answers_hash = _list_digest(answers)
    canonical_rules = json.dumps(
        rules, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    rules_hash = hashlib.sha256(canonical_rules).hexdigest()
    result = {
        "schema_version": 1,
        "dictionary_id": (
            f"ru5-opencorpora-wordfreq-v{rules['version']}-{answers_hash[:12]}"
        ),
        "language": "ru",
        "word_length": 5,
        "normalization": "Ё=Е",
        "license": "CC BY-SA 4.0",
        "license_url": "https://creativecommons.org/licenses/by-sa/4.0/",
        "words_sha256": words_hash,
        "answers_sha256": answers_hash,
        "words": words,
        "answers": answers,
        "sources": SOURCE_PACKAGES,
        "selection": {
            "rules_file": "selection_rules.json",
            "rules_sha256": rules_hash,
            "minimum_zipf": rules["minimum_zipf"],
            "answer_minimum_zipf": rules["answer_minimum_zipf"],
            "morphology": (
                "known NOUN + sing + nomn; normal_form matches input"
            ),
            "proper_name_filter": (
                "tag exclusion + dominant proper parse exclusion"
            ),
            "curation": (
                "Автоматический отбор с явными ручными исключениями "
                "и добавлением известных бытовых кандидатов; "
                "полная экспертная вычитка не выполнена"
            ),
            "candidate_count": len(candidates),
            "preferred_common_count": len(preferred),
            "word_count": len(words),
            "answer_count": len(answers),
        },
    }
    _write_json(output, result)
    if audit_output is not None:
        _write_json(audit_output, [candidates[word] for word in words])
    return result


def main() -> None:
    """Собирает словник из изолированных офлайн-инструментов."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tools-dir",
        type=Path,
        default=ROOT / ".test-data" / "wordli-dictionary-tools",
    )
    parser.add_argument(
        "--wheels-dir",
        type=Path,
        default=ROOT / ".test-data" / "wordli-dictionary-downloads",
    )
    parser.add_argument("--output", type=Path, default=DATA_DIR / "ru5.json")
    parser.add_argument("--audit-output", type=Path)
    arguments = parser.parse_args()
    result = build_dictionary(
        arguments.tools_dir,
        arguments.wheels_dir,
        arguments.output,
        arguments.audit_output,
    )
    print(
        json.dumps(
            {
                "dictionary_id": result["dictionary_id"],
                "words": len(result["words"]),
                "answers": len(result["answers"]),
                "words_sha256": result["words_sha256"],
                "answers_sha256": result["answers_sha256"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
