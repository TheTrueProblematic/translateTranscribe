#!/bin/bash
# Synthesizes the fixtures for spec section 12 tests 4, 5 and 6.
# macOS `say` + `afconvert` only; nothing leaves the machine.
set -e
cd "$(dirname "$0")/.."
OUT=tests/audio
mkdir -p "$OUT"

mk() { # name voice rate text
  local name="$1" voice="$2" rate="$3" text="$4"
  say -v "$voice" -r "$rate" -o "$OUT/$name.aiff" "$text"
  afconvert -f WAVE -d LEI16@16000 -c 1 "$OUT/$name.aiff" "$OUT/$name.wav"
  rm -f "$OUT/$name.aiff"
}

# English: the speaker's own voice. Technical + conversational, per spec.
mk en_technical Daniel 165 "The IMU is reporting a fault on the left side. \
Check the firmware version on the USB port. Do not touch that connector, it is still live. \
The fuselage has a crack near the gimbal mount. I am ready to start the calibration now."

mk en_casual Daniel 170 "I am tired, I have been standing all day and I am getting frustrated. \
That flight absolutely wiped me out. I was surprised, I thought I was already finished. \
Let us take a short break and then we will look at the wiring."

# Brazilian Portuguese: other people in the room. Must never reach the display.
mk pt_speaker1 Luciana 175 "Não toque nesse conector, ele ainda está energizado. \
Eu vou verificar a versão do firmware agora mesmo. \
O problema está no lado esquerdo da fuselagem, perto do suporte."

mk pt_speaker2 Felipe 180 "Professor, eu tenho uma pergunta sobre o sistema de navegação. \
Quando você vai mostrar a calibração do gimbal? \
A gente pode fazer o teste depois do intervalo, tudo bem?"

for f in "$OUT"/*.wav; do
  printf "%-34s %s\n" "$(basename "$f")" "$(afinfo "$f" | awk -F': ' '/estimated duration/{print $2}')"
done
