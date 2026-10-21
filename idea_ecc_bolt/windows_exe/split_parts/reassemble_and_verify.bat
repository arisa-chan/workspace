@echo off
rem =========================================================
rem  Reassembles EccentricBolt.exe from the split parts and
rem  verifies it with SHA-256 before you run it.
rem  Put ALL part files + this script in ONE folder.
rem =========================================================
setlocal
cd /d "%~dp0"
echo Reassembling EccentricBolt.exe from 5 parts ...
copy /b EccentricBolt.exe.001+EccentricBolt.exe.002+EccentricBolt.exe.003+EccentricBolt.exe.004+EccentricBolt.exe.005 EccentricBolt.exe >nul
if not exist EccentricBolt.exe goto :missing
echo Verifying SHA-256 ...
certutil -hashfile EccentricBolt.exe SHA256 | find /i "0ab066a7a738773ff46d7e34d4b374d7f606787dd444b93ec2e08f32886faba4" >nul
if errorlevel 1 (
    echo ERROR: hash mismatch - a part file is missing or corrupted.
    echo Make sure all 5 part files were downloaded completely.
    pause
    exit /b 1
)
echo.
echo OK - EccentricBolt.exe is intact ^(SHA-256 verified, 116,727,827 bytes^).
echo You can now double-click EccentricBolt.exe
pause
exit /b 0
:missing
echo ERROR: reassembled file not created - are all parts present?
pause
exit /b 1
