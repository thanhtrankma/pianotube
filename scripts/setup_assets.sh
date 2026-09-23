#!/bin/bash
# Tải soundfont Salamander Grand Piano (~310 MB) và 5 bản MIDI mẫu từ Mutopia Project.
set -euo pipefail
cd "$(dirname "$0")/.."
PT=".venv/bin/python -m pianotube"

SF_URL="https://freepats.zenvoid.org/Piano/SalamanderGrandPiano/SalamanderGrandPiano-SF2-V3+20200602.tar.xz"
if [ -z "$(find assets/soundfonts -name "*.sf2" -print -quit)" ]; then
  echo "Tải soundfont…"
  curl -L --fail --progress-bar -o assets/soundfonts/salamander.tar.xz "$SF_URL"
  tar -xJf assets/soundfonts/salamander.tar.xz -C assets/soundfonts
  rm assets/soundfonts/salamander.tar.xz
fi

M=https://www.mutopiaproject.org/ftp
add() {  # url title composer license
  local f="midi/_dl_$(basename "$1")"
  curl -sL --fail -o "$f" "$1"
  $PT library add "$f" --title "$2" --composer "$3" --license "$4" --source "$1"
  rm "$f"
}
add "$M/SatieE/gymnopedie_1/gymnopedie_1.mid" "Gymnopédie No. 1" "Erik Satie" "Public Domain"
add "$M/SatieE/Gnossienne/no_1/no_1.mid" "Gnossienne No. 1" "Erik Satie" "CC BY-SA 4.0"
add "$M/ChopinFF/O9/chopin_nocturne_op9_n2/chopin_nocturne_op9_n2.mid" "Nocturne Op. 9 No. 2" "Frédéric Chopin" "CC BY-SA 3.0"
add "$M/DebussyC/L66/debussy_Arabesque_1/debussy_Arabesque_1.mid" "Arabesque No. 1" "Claude Debussy" "Public Domain"
add "$M/DebussyC/L75/debussy_Ste_Bergamesq_Clair/debussy_Ste_Bergamesq_Clair.mid" "Clair de Lune" "Claude Debussy" "Public Domain"
echo "Xong."
