#!/usr/bin/env bash
# Install the model launchers for the current user:  bash multimodel/install.sh [target-dir]
# Copies the scripts to target-dir (default ~/llm-launchers), keeps an existing llm.conf (or starts one from
# llm.conf.example), and puts four shortcuts on the Desktop.  Run it again after updating the repository.
set -eu
SRC=$(dirname "$(readlink -f "$0")")
DST=${1:-$HOME/llm-launchers}
DESK=$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")

mkdir -p "$DST"
for f in llm-common.sh model-switch.py llama_ui.py start-qwen38.sh start-deepseek-v4-flash.sh start-glm53.sh stop-all-models.sh \
         strata-start.sh strata-stop.sh llm.conf.example; do
	install -m 755 "$SRC/$f" "$DST/$f"
done
chmod 644 "$DST/llm.conf.example"
if [ ! -f "$DST/llm.conf" ]; then
	install -m 644 "$SRC/llm.conf.example" "$DST/llm.conf"
	echo "Created $DST/llm.conf from the example - edit the paths in it before the first start."
fi

if [ -d "$DESK" ]; then
	icons=$(dirname "$SRC")/icons
	desk() {   # desk <file> <name> <comment> <script> <icon>
		cat > "$DESK/$1.desktop" <<EOF
[Desktop Entry]
Name=$2
Comment=$3
Exec=$DST/$4
Icon=$icons/$5
Terminal=true
Type=Application
Categories=Development;
EOF
		chmod 755 "$DESK/$1.desktop"
		command -v gio >/dev/null && gio set "$DESK/$1.desktop" metadata::trusted true 2>/dev/null || true
	}
	desk Qwen38-Strata-Start "Qwen3.8 Next (Strata)" "Stop the running model and start Qwen3.8 on Strata" start-qwen38.sh strata-start.png
	desk DeepSeek-V4-Flash-Start "DeepSeek V4 Flash" "Stop the running model and start DeepSeek V4 Flash (llama.cpp)" start-deepseek-v4-flash.sh strata-start.png
	desk GLM-53-Flash-Start "GLM 5.3 Flash" "Stop the running model and start GLM-5.3 Flash (llama.cpp)" start-glm53.sh strata-start.png
	desk Stop-All-Models "Stop All Models" "Stop the running model and free the GPUs" stop-all-models.sh strata-stop.png
fi
echo "Installed in $DST."
