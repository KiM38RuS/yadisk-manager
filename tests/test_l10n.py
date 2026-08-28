"""Verify l10n end-to-end: translated output, formatter, config langs."""
import logging

import l10n


def test_translate_ru():
    assert l10n.translate("Uploaded: %s") == "Загружено: %s"
    assert l10n.translate("Account changed, token renewed") == "Аккаунт изменён, токен обновлён"


def test_en_passthrough():
    l10n.set_language("en")
    try:
        assert l10n.translate("Uploaded: %s") == "Uploaded: %s"
    finally:
        l10n.set_language("ru")


def test_formatter_substitutes_after_translation():
    """/foo/bar.txt и 1024 должны подставиться, шаблон — переведён."""
    l10n.set_language("ru")
    fmt = l10n.make_formatter()
    rec = logging.LogRecord("t", logging.INFO, "", 0,
                            "Downloaded %s (%d bytes)", ("/foo/bar.txt", 1024), None)
    out = fmt.format(rec)
    assert "/foo/bar.txt" in out and "1024" in out, out
    assert "Скачано" in out, out


def test_named_placeholders():
    """%(matched)d / %(uploaded)d — словарные аргументы лога."""
    fmt = l10n.make_formatter()
    rec = logging.LogRecord("t", logging.INFO, "", 0,
                            "FullSync done: %(matched)d matched, %(uploaded)d uploaded, ",
                            {"matched": 3, "uploaded": 5}, None)
    out = fmt.format(rec)
    assert "%(matched)d" not in out and "3" in out and "5" in out, out


def test_verify_no_errors():
    """Все переводы сохраняют плейсхолдеры."""
    assert l10n.verify() == []


def test_unknown_message_passthrough():
    """Неизвестный шаблон не должен падать и не формат-иться заново."""
    fmt = l10n.make_formatter()
    rec = logging.LogRecord("t", logging.INFO, "", 0, "some untranslated msg", (), None)
    out = fmt.format(rec)
    assert "some untranslated msg" in out