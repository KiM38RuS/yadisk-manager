@echo off
rem ══════════════════════════════════════════════════════════
rem  YaDisk Manager — запуск через локальное виртуальное окружение
rem  (зависимости НЕ устанавливаются в систему)
rem ══════════════════════════════════════════════════════════
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo [ОШИБКА] Виртуальное окружение .venv не найдено.
    echo.
    echo Создание окружения (один раз):
    echo   uv venv .venv --python "C:\Program Files\Python312\python.exe"
    echo   uv pip install --python .venv\Scripts\python.exe -r requirements.txt
    echo.
    pause
    exit /b 1
)

rem Запуск без консольного окна; логи пишутся в %APPDATA%\yadisk-client\yadisk.log
start "" ".venv\Scripts\pythonw.exe" main.py
