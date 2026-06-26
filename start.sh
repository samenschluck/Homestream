#!/bin/bash
# Homestream – macOS / Linux Startskript
set -e
cd "$(dirname "$0")"

if ! command -v python3 &>/dev/null; then
    echo "Python 3 nicht gefunden. Bitte installiere Python 3.11+"
    exit 1
fi

echo "Installiere Abhängigkeiten..."
pip3 install -r requirements.txt --quiet

echo "Starte Homestream..."
python3 streamer.py
