"""Склейка распознанных кусков в текст, голосовые команды, слова из токенов модели."""
import pytest

from textproc import (append_piece, apply_commands, apply_replacements, is_filler, join_to_previous,
                      parse_replacements, split_sentences, tidy, tokens_to_words)


# ── голосовые команды ──

def test_new_line_ru():
    assert apply_commands("Купи хлеб, новая строка молоко", "ru") == "Купи хлеб\nМолоко"


def test_new_paragraph_en():
    assert apply_commands("Hello. New paragraph. how are you", "en") == "Hello.\n\nHow are you"


def test_commands_ignore_other_language():
    assert apply_commands("Hello new line world", "ru") == "Hello new line world"


# ── склейка кусков ──

def test_continuation_starts_lowercase():
    assert join_to_previous("Который был вчера", "Удали файл,") == "который был вчера"


@pytest.mark.parametrize("piece", ["I think so", "NASA launched"])
def test_continuation_keeps_words_that_are_always_capitalized(piece):
    assert join_to_previous(piece, "Well,") == piece


def test_new_sentence_keeps_capital():
    assert join_to_previous("Как дела?", "Привет.") == "Как дела?"


def test_false_period_becomes_comma():
    """Модель поставила точку на паузе, а фраза продолжилась — точка лишняя."""
    assert append_piece("Удали файл.", "который только что создал.", "ru") == "Удали файл, который только что создал."


def test_real_sentence_end_is_kept():
    assert append_piece("Привет.", "Как дела?", "ru") == "Привет. Как дела?"


def test_ellipsis_is_kept():
    assert append_piece("Ну...", "и что дальше", "ru") == "Ну... и что дальше"


def test_continuation_after_comma_is_lowercased():
    assert append_piece("Купи хлеб,", "Молоко и сыр.", "ru") == "Купи хлеб, молоко и сыр."


def test_new_paragraph_has_no_dangling_space():
    assert append_piece("Первая фраза.", "Новый абзац. Вторая фраза.", "ru") == "Первая фраза.\n\nВторая фраза."


def test_piece_after_new_line_has_no_leading_space():
    assert append_piece("Список:\n", "Хлеб.", "ru") == "Список:\nХлеб."


def test_first_piece():
    assert append_piece("", "Привет.", "ru") == "Привет."


def test_lectures_do_not_apply_commands():
    assert append_piece("Hello.", "Next, new line please.", "en", commands=False) == "Hello. Next, new line please."


# ── прочее ──

@pytest.mark.parametrize("text", ["Ммм", "Угу.", "Hmm", "А-а-а", "uh, um"])
def test_fillers(text):
    assert is_filler(text)


@pytest.mark.parametrize("text", ["Ммм, привет", "Да", "", "OK"])
def test_not_fillers(text):
    assert not is_filler(text)


def test_split_sentences():
    assert split_sentences("Один. Два? Три! Четыре") == ["Один.", "Два?", "Три!", "Четыре"]


def test_tidy():
    assert tidy("almost8 months, вторник— встреча") == "almost 8 months, вторник — встреча"


def test_tokens_to_words():
    tokens = ["▁при", "вет", "▁как", "▁де", "ла"]
    assert tokens_to_words(tokens, [0.0, 0.1, 0.4, 0.7, 0.8]) == [("привет", 0.0), ("как", 0.4), ("дела", 0.7)]


def test_lone_space_token_starts_new_word():
    """Отдельный токен «только пробел» — раньше терялся, и слова слипались: «времядиктовки»."""
    tokens = ["▁во", "▁", "время", "▁", "дик", "товки"]
    words = [w for w, _ in tokens_to_words(tokens, [0, 0.1, 0.2, 0.5, 0.6, 0.7])]
    assert words == ["во", "время", "диктовки"]


def test_russian_ya_is_lowercase_mid_sentence():
    """Русское «я» посреди фразы — со строчной (раньше оставалось «…например, Я включаю»)."""
    assert join_to_previous("Я включаю приложение", "Вот, например,") == "я включаю приложение"


# ── запинки и оговорки ──

@pytest.mark.parametrize("raw,clean", [
    ("помогать мне отве- э-э-э отвечать на вопросы.", "помогать мне отвечать на вопросы."),
    ("Э-э, я думаю, что да.", "Я думаю, что да."),
    ("И всё, эм.", "И всё."),
    ("Ну, ммм, хорошо", "Ну, хорошо"),
    ("А я не знаю, а-а-а, может быть", "А я не знаю, может быть"),
    ("Uh, I think um we should go.", "I think we should go."),
    ("Это ко- компания", "Это компания"),
])
def test_disfluencies_removed(raw, clean):
    assert tidy(raw) == clean


@pytest.mark.parametrize("text", ["А вот и я.", "Мама мыла раму.", "Северо-запад", "Hmm is a word here? No."])
def test_real_words_kept(text):
    expected = text if not text.startswith("Hmm") else "Is a word here? No."
    assert tidy(text) == expected


# ── словарь замен ──

RULES = parse_replacements("клоуд* = Claude\nчерез cloud → через Claude\n лоуда -> Claude\nстрока без замены")


def test_parse_replacements():
    assert RULES == [("клоуд*", "Claude"), ("через cloud", "через Claude"), ("лоуда", "Claude")]


def test_apply_replacements():
    text = "Как чат клоуда, через Cloud, а Клоуду я напишу. Cloudflare не трогаем."
    assert apply_replacements(text, RULES) == "Как чат Claude, через Claude, а Claude я напишу. Cloudflare не трогаем."


def test_replacement_is_whole_word_only():
    assert apply_replacements("переклоудка", [("клоуд", "Claude")]) == "переклоудка"
