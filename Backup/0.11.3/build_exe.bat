@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem ══════════════════════════════════════════════════════════
rem  Сборка YaDiskManager.exe (PyInstaller onefile)
rem  - иконка: Assets\icon.ico
rem  - IPC_ENABLED = False в собранной копии (правило релиза)
rem  - исходники в корне проекта НЕ меняются (остаются для dev)
rem ══════════════════════════════════════════════════════════

if not exist ".venv\Scripts\pyinstaller.exe" (
    echo [ОШИБКА] PyInstaller не найден в .venv.
    echo Установите: uv pip install --python .venv\Scripts\python.exe pyinstaller
    pause
    exit /b 1
)

set "BUILD_DIR=%TEMP%\yadisk-build"

echo [1/4] Подготовка исходников в %BUILD_DIR% ...
if exist "%BUILD_DIR%" rmdir /s /q "%BUILD_DIR%"
mkdir "%BUILD_DIR%" >nul
copy /y *.py "%BUILD_DIR%" >nul
xcopy /y /s /e /q "Assets" "%BUILD_DIR%\Assets\" >nul

echo [2/4] IPC_ENABLED=False для релизной сборки ...
powershell -NoProfile -Command "(Get-Content -Raw '%BUILD_DIR%\ipc.py') -replace 'IPC_ENABLED = True', 'IPC_ENABLED = False' | Set-Content -NoNewline '%BUILD_DIR%\ipc.py'"

echo [3/4] PyInstaller: onefile, windowed, иконка Assets\icon.ico ...
cd /d "%BUILD_DIR%"
"%~dp0.venv\Scripts\pyinstaller.exe" --noconfirm --onefile --windowed --name YaDiskManager --icon "Assets\icon.ico" --add-data "Assets;Assets" main.py
if errorlevel 1 (
    echo [ОШИБКА] Сборка не удалась. Лог: %BUILD_DIR%\build\YaDiskManager\warn-YaDiskManager.txt
    pause
    exit /b 1
)

echo [4/4] Копирование в папку проекта ...
copy /y "%BUILD_DIR%\dist\YaDiskManager.exe" "%~dp0YaDiskManager.exe" >nul

echo.
echo Готово: %~dp0YaDiskManager.exe
pause
