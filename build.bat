@echo off
rem Build a portable package that runs on any Windows 10/11 x64 PC without Python:
rem   dist\CompressPicture\                      (the program folder)
rem   dist\CompressPicture-<version>-win64.zip   (the same folder zipped, ready to hand out)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv || goto :fail
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller==6.22.3 || goto :fail
".venv\Scripts\python.exe" make_icon.py || goto :fail
".venv\Scripts\python.exe" make_version.py || goto :fail
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --windowed ^
    --name CompressPicture --icon icon.ico --add-data "icon.ico;." ^
    --version-file version_info.txt ^
    --hidden-import _cffi_backend ^
    --exclude-module tkinter --exclude-module pytest --exclude-module unittest ^
    main.py || goto :fail
".venv\Scripts\python.exe" prune_build.py || goto :fail

echo Self-test...
"dist\CompressPicture\CompressPicture.exe" --selftest "dist\selftest.json"
if errorlevel 1 (
    echo Self-test FAILED, see dist\selftest.json
    goto :fail
)
".venv\Scripts\python.exe" package.py || goto :fail
rmdir /s /q build 2>nul
del /q CompressPicture.spec version_info.txt 2>nul
echo.
echo Done. Hand out the zip in the dist folder.
exit /b 0

:fail
echo Build failed.
exit /b 1
