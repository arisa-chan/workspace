@echo off
rem ============================================================
rem  One-click builder: creates EccentricBolt.exe (single file)
rem  Run this ON WINDOWS by double-clicking it.
rem  Requires: Python 3.12 from python.org (with "Add to PATH").
rem  Output:   windows_exe\EccentricBolt.exe  (runs without Python)
rem ============================================================
setlocal
cd /d "%~dp0.."
echo [1/4] creating virtual environment ...
py -3.12 -m venv .venv_build || python -m venv .venv_build
call .venv_build\Scripts\activate.bat
echo [2/4] installing dependencies ...
python -m pip install --upgrade pip
pip install -r requirements.txt || goto :err
pip install pyinstaller || goto :err
echo [3/5] building single-file executable ...
pyinstaller --noconfirm --clean windows_exe\EccentricBolt.spec ^
    --distpath windows_exe --workpath .pybuild || goto :err
echo [4/5] self test of the built executable ...
windows_exe\EccentricBolt.exe --selftest || goto :err
echo [5/5] hashing and splitting for transfer ...
python windows_exe\make_split_parts.py || goto :err
echo.
echo DONE:  windows_exe\EccentricBolt.exe
echo        windows_exe\EccentricBolt.exe.sha256
echo        windows_exe\split_parts\  (5 parts + reassemble_and_verify.bat)
pause
exit /b 0
:err
echo BUILD FAILED
pause
exit /b 1
