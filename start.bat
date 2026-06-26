@echo off
:: Homestream – Windows Startskript
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo Python nicht gefunden! Bitte installiere Python 3.11+ von python.org
    pause
    exit /b 1
)

echo Installiere Abhaengigkeiten...
pip install -r requirements.txt --quiet

echo Starte Homestream...
python streamer.py
pause
