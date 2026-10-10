#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
SCENEMIX_PATCH_DIR="$PWD"

SCENEMIX_LAUNCHER=$(/usr/bin/osascript <<'APPLESCRIPT'
try
    set selectedFile to choose file with prompt "Выберите Start-Mac.command той программы SceneMix, которую вы обычно запускаете. Перед установкой закройте её окно."
    return POSIX path of selectedFile
on error number -128
    return ""
end try
APPLESCRIPT
)

if [[ -z "$SCENEMIX_LAUNCHER" ]]; then
    exit 0
fi
if [[ "$(basename -- "$SCENEMIX_LAUNCHER")" != "Start-Mac.command" ]]; then
    /usr/bin/osascript -e 'display alert "Выберите файл Start-Mac.command вашей программы."'
    exit 1
fi

SCENEMIX_TARGET_DIR="$(dirname -- "$SCENEMIX_LAUNCHER")"
if [[ ! -f "$SCENEMIX_TARGET_DIR/main.py" ]]; then
    /usr/bin/osascript -e 'display alert "Рядом с выбранным Start-Mac.command не найден main.py."'
    exit 1
fi

SCENEMIX_BACKUP="$SCENEMIX_TARGET_DIR/main.before-photo-scroll.$(date +%Y%m%d_%H%M%S).$$.py"
cp -p -- "$SCENEMIX_TARGET_DIR/main.py" "$SCENEMIX_BACKUP"
cp -- "$SCENEMIX_PATCH_DIR/main.py" "$SCENEMIX_TARGET_DIR/main.py"
echo 'Установлена версия с отметкой «Фото+2». Предыдущий main.py сохранён рядом.'
echo 'Запускается выбранный Start-Mac.command.'
open -a Terminal "$SCENEMIX_LAUNCHER"
