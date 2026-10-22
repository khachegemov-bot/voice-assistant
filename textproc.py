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
    # английское «I» пишется с заглавной всегда, а русское «я» посреди фразы — со строчной
    if first in ("I", "I'm", "I've", "I'll", "I'd") or (len(first) > 1 and first.isupper()):
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
    """Мелкие правки распознанного куска: «almost8» → «almost 8», «вторник— встреча» → «вторник — встреча»,
    без запинок («э-э-э», «эм», «uh») и оборванных начал слов («отве- отвечать»)."""
    text = re.sub(r"([A-Za-z])(\d)", r"\1 \2", text)
    return drop_disfluencies(re.sub(r"(\w)—", r"\1 —", text))


# запинки: «э», «э-э-э», «эм», «ммм», «м-м», «хм», «а-а-а» (просто «а» — настоящее слово), «uh», «um», «erm»
_FILLER = re.compile(r"^(?:э+(?:-э+)*|эм+|м{2,}|м+(?:-м+)+|хм+|а+(?:-а+)+|u+h+|u+m+|uhm|erm?|hmm+|m{2,})$", re.I)
_EDGE = ",.…!?;:«»\"()"


def drop_disfluencies(text: str) -> str:
    """Убрать запинки и оговорки, сохранив знаки препинания: «помогать мне отве- э-э-э отвечать» →
    «помогать мне отвечать», «Э-э, я думаю» → «Я думаю», «и всё, эм.» → «и всё.»."""
    words = [w for w in text.split(" ") if w]
    kept: list[str] = []
    cap_next = False
    for w in words:
        core = w.strip(_EDGE)
        if core and _FILLER.match(core):
            end = w[len(w.rstrip(".!?…")):]            # запинка закончила фразу — точку отдаём предыдущему
            if end and kept and not kept[-1].endswith((".", "!", "?", "…")):
                kept[-1] = kept[-1].rstrip(",;:") + end
            if (not kept or kept[-1].endswith((".", "!", "?", "…"))) and core[:1].isupper():
                cap_next = True                       # запинка стояла в начале фразы
            continue
        if cap_next and w[:1].islower():
            w = w[0].upper() + w[1:]
        cap_next = False
        kept.append(w)
    out: list[str] = []                               # оборванное слово: «отве-», а следом «отвечать»
    for i, w in enumerate(kept):
        if len(w) > 2 and w.endswith("-") and i + 1 < len(kept) and \
                kept[i + 1].strip(_EDGE).lower().startswith(w[:-1].strip(_EDGE).lower()):
            continue
        out.append(w)
    return " ".join(out)


def parse_replacements(text: str) -> list[tuple[str, str]]:
    """Словарь замен из настроек: по строке «как слышится = как писать» (можно «→» или «->»).
    Звёздочка в конце — любое окончание: «клоуд* = Claude» заменит «клоуд», «клоуда», «клоуду»."""
    rules = []
    for line in text.splitlines():
        parts = re.split(r"\s*(?:=|→|->)\s*", line.strip(), maxsplit=1)
        if len(parts) == 2 and parts[0] and parts[1]:
            rules.append((parts[0], parts[1]))
    return rules


def apply_replacements(text: str, rules: list[tuple[str, str]]) -> str:
    """Заменить слова по словарю: целиком, без учёта регистра; несколько слов — через любые пробелы."""
    for src, dst in rules:
        star = src.endswith("*")
        words = src.rstrip("*").split()
        if not words:
            continue
        rx = r"(?<!\w)" + r"\s+".join(map(re.escape, words)) + (r"\w*" if star else "") + r"(?!\w)"
        text = re.sub(rx, lambda m, dst=dst: dst, text, flags=re.I)
    return text


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


# ── стоп-слово: «…и вот это всё. Отправь.» — диктовка заканчивается сама ──

def _norm_stop(word: str) -> str:
    return re.sub(r"[^\w]", "", word.lower().replace("ё", "е"))


def parse_stop_words(text: str) -> list[str]:
    """«отправь, send» → ["отправь", "send"]."""
    return [w for w in (_norm_stop(x) for x in re.split(r"[,;\n]+", text or "")) if w]


def _is_stop(word: str, stops: list[str]) -> bool:
    w = _norm_stop(word)
    # «отправь», вежливое «отправьте» и «отправ» (модель иногда теряет мягкий знак) — но не «отправлю»
    return bool(w) and any(w in (s, s + "те", s[:-1] if s.endswith("ь") else s) for s in stops)


def ends_with_stop_word(text: str, stops: list[str]) -> bool:
    """Последнее слово текста — стоп-слово («…как дела? Отправь.»)."""
    words = text.split()
    return bool(stops and words and _is_stop(words[-1], stops))


def strip_stop_word(text: str, stops: list[str]) -> str:
    """Убрать стоп-слово в конце: «Купи хлеб, отправь.» → «Купи хлеб.», «Как дела? Отправь.» → «Как дела?»."""
    words = text.rstrip().split(" ")
    if not (stops and words and _is_stop(words[-1], stops)):
        return text
    rest = " ".join(words[:-1]).rstrip()
    if rest.endswith((",", ";", ":", "—", "-")):
        rest = rest.rstrip(",;:—- ") + "."
    return rest
