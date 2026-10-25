@echo off
REM Builds "Reclaimer Launcher.exe" into the dist folder.
REM Needs Python 3.9+ from python.org (with the "py" launcher) on Windows.
cd /d "%~dp0"
py -m pip install --upgrade pyinstaller -r requirements.txt || goto :fail
py -m PyInstaller --noconfirm --clean --onefile --noconsole ^
  --name "Reclaimer Launcher" ^
  --icon assets\icon.ico ^
  --add-data "assets;assets" ^
  --hidden-import PIL._tkinter_finder ^
  launcher.py || goto :fail
echo.
echo Done. Your exe is in: %~dp0dist\Reclaimer Launcher.exe
pause
exit /b 0
:fail
echo.
echo Build failed.
pause
exit /b 1
