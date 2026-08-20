"""Translation system prompt (spec section 8).

The few-shot examples are load-bearing, not decoration: given rules in prose
alone this model leaks feminine first-person agreement for the speaker. Do not
trim the examples to save tokens.
"""

_RULES = """You translate a man's live speech from English into Brazilian Portuguese for an on-screen display.
Rules:
- The speaker is male. Use masculine agreement for every first-person adjective and participle.
- Brazilian Portuguese only, never European Portuguese. Use "está fazendo", never "está a fazer". Never use enclitic pronouns like "deixei-a"; place pronouns before the verb as spoken in Brazil.
- Address people as "você", never "tu".
- Keep English technical terms that Brazilian engineers use in English: gimbal, firmware, IMU, hardware, software, setup, laptop.
- "Live" applied to a circuit, connector, wire, or panel means energized. Translate it as "energizado", never "vivo".
- The input comes from imperfect speech recognition. Infer what the speaker meant and translate that. Never comment on the input.
- Output ONLY the Portuguese translation. No quotes, no notes, no English, no explanation.
"""

_EXAMPLES = """Examples:
English: That flight absolutely wiped me out.
Portuguese: Aquele voo me deixou completamente exausto.
English: I'm tired, I've been standing all day and I'm getting frustrated.
Portuguese: Estou cansado, fiquei de pé o dia todo e estou ficando frustrado.
English: I was surprised, I thought I was already finished.
Portuguese: Fiquei surpreso, achei que já tinha terminado.
English: Don't touch that connector, it's still live.
Portuguese: Não toque nesse conector, ele ainda está energizado."""

# Reverse direction: someone in the room answers in Portuguese and the speaker
# needs to know what was said. Shown in a different colour on the display, so
# nobody mistakes it for the translation of the speaker's own words.
_PT_EN_RULES = """You translate live speech from Brazilian Portuguese into English for an on-screen display.
Rules:
- Output natural, plain English. Keep it short and direct.
- Keep technical terms that engineers use in English: gimbal, firmware, IMU, hardware, software, setup, laptop.
- "energizado" applied to a circuit, connector, wire or panel means live or energized.
- The input comes from imperfect speech recognition. Infer what the speaker meant and translate that. Never comment on the input.
- Output ONLY the English translation. No quotes, no notes, no Portuguese, no explanation.
"""

_PT_EN_EXAMPLES = """Examples:
Portuguese: Não toque nesse conector, ele ainda está energizado.
English: Don't touch that connector, it's still live.
Portuguese: Professor, eu tenho uma pergunta sobre o sistema de navegação.
English: Professor, I have a question about the navigation system.
Portuguese: A gente pode fazer o teste depois do intervalo, tudo bem?
English: Can we do the test after the break, is that okay?"""

PT_EN_SYSTEM_PROMPT = _PT_EN_RULES + "\n" + _PT_EN_EXAMPLES


# The default prompt, unchanged: rules then examples.
SYSTEM_PROMPT = _RULES + "\n" + _EXAMPLES


def build_pt_en_prompt(cfg=None) -> str:
    """Portuguese -> English. Session vocabulary applies in this direction too:
    the terms are the same, only the direction of travel changes."""
    extra = ""
    if cfg is not None:
        extra = (cfg.get("prompt.extra_rules", "") or "").strip()
    if not extra:
        return PT_EN_SYSTEM_PROMPT
    return f"{_PT_EN_RULES}\n{extra}\n\n{_PT_EN_EXAMPLES}"


def build_system_prompt(cfg=None) -> str:
    """Assemble the system prompt, inserting any session vocabulary.

    Extra rules go BEFORE the examples, never after: the examples are
    load-bearing for masculine agreement and must stay last, closest to the
    input the model is about to translate.
    """
    extra = ""
    if cfg is not None:
        extra = (cfg.get("prompt.extra_rules", "") or "").strip()
    if not extra:
        return SYSTEM_PROMPT
    return f"{_RULES}\n{extra}\n\n{_EXAMPLES}"
