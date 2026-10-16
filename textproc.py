"""Обработка распознанного текста: голосовые команды, склейка кусков в одну фразу, слова из токенов
модели. Чистые функции без зависимостей — их легко проверять тестами (tests/test_textproc.py)."""
import re


COMMANDS = {   # голосовые команды
    "ru": [(r",?\s*\bновый абзац\b[,.!]?\s*", "\n\n"), (r",?\s*\b(?:новая строка|с новой строки)\b[,.!]?\s*", "\n")],
    "en": [(r",?\s*\bnew paragraph\b[,.!]?\s*", "\n\n"), (r",?\s*\bnew line\b[,.!]?\s*", "\n")],
}


def apply_commands(text: str, lang: str) -> str:
    for rx, repl in COMMANDS[lang]:
        text = re.sub(rx, repl, text, flags=re.I)
    return re.sub(r"\n(\s*)(\w)", lambda m: "\n" + m.group(1) + m.group(2).upper(), text)


def join_to_previous(text: str, prev: str) -> str:
    """Кусок продолжает незаконченную фразу — не начинаем его с заглавной буквы."""
    if not prev or not text or prev.endswith("\n") or prev.rstrip()[-1:] in ".!?…":
        return text
    first = text.split(" ", 1)[0].strip(",.!?")
    if first in ("Я", "I", "I'm", "I've", "I'll", "I'd") or (len(first) > 1 and first.isupper()):
        return text
    return text[0].lower() + text[1:]


def append_piece(text: str, piece: str, lang: str = "", commands: bool = True) -> str:
    """Дописать засчитанный кусок к тексту. Если модель поставила точку на паузе, а следующий кусок
    продолжает фразу со строчной буквы («удали файл. который…») — точка лишняя, меняем её на запятую."""
    piece = join_to_previous(piece, text)
    tail = text.rstrip()
    if tail.endswith(".") and not tail.endswith("..") and piece[:1].islower():
        text = tail[:-1] + ","
    if commands and lang:
        piece = apply_commands(piece, lang)
    if piece.startswith("\n"):                        # «новый абзац» в начале куска — без пробела в конце строки
        return text.rstrip(" ") + piece
    sep = "" if not text or text.endswith("\n") else " "
    return text + sep + piece


def is_filler(text: str) -> bool:
    """«А-а-а», «Ммм», «Угу», «Uh», «Hmm» без настоящих слов — не печатаем."""
    words = re.findall(r"\w+", text.lower())
    fillers = ("угу", "ага", "хм", "эм", "uh", "um", "hmm", "mm", "mhm", "uhm")
    return bool(words) and all(len(set(w)) == 1 or w in fillers for w in words)


def split_sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def tidy(text: str) -> str:
    """Мелкие правки распознанного куска: «almost8» → «almost 8», «вторник— встреча» → «вторник — встреча»."""
    text = re.sub(r"([A-Za-z])(\d)", r"\1 \2", text)
    return re.sub(r"(\w)—", r"\1 —", text)


def tokens_to_words(tokens: list[str], times: list[float]) -> list[tuple[str, float]]:
    """Склеивает кусочки слов от модели в слова. Пробел в начале кусочка — начало нового слова.
    Важно: у моделей есть и отдельный кусочек «просто пробел» — после него тоже начинается новое
    слово (раньше он терялся, и слова слипались: «во времядиктовки», «могувернуть»)."""
    words: list[list] = []
    new_word = True
    for tok, t in zip(tokens, times, strict=False):
        if tok.startswith(" ") or tok.startswith("▁"):
            new_word = True
            tok = tok.lstrip(" ▁")
        if not tok:
            continue                                  # кусочек «только пробел» — следующий начнёт слово
        if new_word or not words:
            words.append([tok, float(t)])
            new_word = False
        else:
            words[-1][0] += tok
    return [(w, t) for w, t in words]
