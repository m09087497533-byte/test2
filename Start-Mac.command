#!/bin/bash
set -e
cd -- "$(dirname -- "$0")"
fail() {
  echo "$1"
  read -r -p 'Нажмите Enter, чтобы закрыть окно.'
  exit 1
}
if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  fail 'Нужен FFmpeg. Установите его: brew install ffmpeg. Подробности в README.md.'
fi
if [[ ! -x .venv/bin/python ]]; then
  if command -v python3.12 >/dev/null; then
    SCENEMIX_PYTHON=python3.12
  elif command -v python3 >/dev/null; then
    SCENEMIX_PYTHON=python3
  else
    fail 'Установите Python 3.12 для macOS с python.org и откройте этот файл снова.'
  fi
  "$SCENEMIX_PYTHON" -c 'import sys,tkinter; assert sys.version_info >= (3,11)' ||
    fail 'Нужен Python 3.11+ с Tkinter. Используйте установщик Python 3.12 с python.org.'
  "$SCENEMIX_PYTHON" -m venv .venv || fail 'Не удалось создать окружение Python.'
fi
.venv/bin/python -m pip install -r requirements.lock.txt || fail 'Ошибка установки зависимостей. Проверьте соединение и текст ошибки выше.'
.venv/bin/python main.py || fail 'Ошибка запуска. Скопируйте сообщение выше без API-ключей.'
