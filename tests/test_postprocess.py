"""Known-defect handling for the translation model (spec section 8)."""
import logging

import pytest

from livetranslate.postprocess import (capitalize_and_punctuate, fix_enclisis,
                                       postprocess, strip_scaffolding)


def test_capitalizes_first_letter_and_adds_terminal_period():
    # The model mirrors the lowercase, unpunctuated ASR input.
    assert postprocess("o firmware está desatualizado") == \
        "O firmware está desatualizado."


def test_model_supplied_acronym_casing_is_preserved():
    # The model emits "IMU" itself; post-processing must not flatten it.
    assert postprocess("o IMU está com defeito") == "O IMU está com defeito."


@pytest.mark.parametrize("text,expected", [
    ("já está pronto!", "Já está pronto!"),
    ("você está pronto?", "Você está pronto?"),
    ("certo...", "Certo..."),
])
def test_existing_terminal_punctuation_is_preserved(text, expected):
    assert postprocess(text) == expected


@pytest.mark.parametrize("raw", [
    "Portuguese: o firmware está desatualizado",
    "Português: o firmware está desatualizado",
    '"o firmware está desatualizado"',
])
def test_leaked_scaffolding_is_stripped(raw):
    assert postprocess(raw) == "O firmware está desatualizado."


def test_diacritics_are_untouched():
    s = "A manutenção não está concluída, é preciso atenção."
    assert postprocess(s) == s


def test_enclisis_is_moved_before_the_verb():
    # The prompt forbids enclitics; the model emits them anyway.
    out, changed = fix_enclisis("Encontro-me às 4:20")
    assert out == "Me encontro às 4:20" and changed


def test_infinitive_contraction_is_left_alone():
    # "fazê-lo" must not be rewritten; moving it produces broken Portuguese.
    out, changed = fix_enclisis("vou fazê-lo agora")
    assert out == "vou fazê-lo agora" and not changed


def test_hyphenated_nouns_are_not_mangled():
    assert postprocess("guarda-roupa novo") == "Guarda-roupa novo."


def test_feminine_agreement_leak_is_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="livetranslate.postprocess"):
        postprocess("estou cansada e fiquei surpresa", "i'm tired and surprised")
    assert any("FEMININE_AGREEMENT_LEAK" in r.message for r in caplog.records)


def test_correct_masculine_agreement_is_not_logged(caplog):
    with caplog.at_level(logging.WARNING, logger="livetranslate.postprocess"):
        postprocess("estou cansado e fiquei surpreso", "i'm tired and surprised")
    assert not any("FEMININE" in r.message for r in caplog.records)


def test_third_person_feminine_is_not_a_false_positive(caplog):
    """"a porta está fechada" is correct Portuguese, not a speaker-gender leak."""
    with caplog.at_level(logging.WARNING, logger="livetranslate.postprocess"):
        postprocess("a porta está fechada", "the door is closed")
    assert not any("FEMININE" in r.message for r in caplog.records)


@pytest.mark.parametrize("text", ["", "   ", None])
def test_empty_input_is_safe(text):
    assert postprocess(text or "") == ""
