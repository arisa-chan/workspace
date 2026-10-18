"""Split the built EccentricBolt.exe into transfer-robust parts.

Writes ``EccentricBolt.exe.001 ... .00N`` plus a freshly generated
``reassemble_and_verify.bat`` (with the SHA-256 of the current binary baked in)
and ``EccentricBolt.exe.sha256``, so the delivered parts always match the
executable that was just built.

Usage:  python windows_exe/make_split_parts.py [--parts 5]
"""
from __future__ import annotations

import argparse
import hashlib
import os

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = """@echo off
rem =========================================================
rem  Reassembles EccentricBolt.exe from the split parts and
rem  verifies it with SHA-256 before you run it.
rem  Put ALL part files + this script in ONE folder.
rem =========================================================
setlocal
cd /d "%~dp0"
echo Reassembling EccentricBolt.exe from {n} parts ...
copy /b {copy_list} EccentricBolt.exe >nul
if not exist EccentricBolt.exe goto :missing
echo Verifying SHA-256 ...
certutil -hashfile EccentricBolt.exe SHA256 | find /i "{sha}" >nul
if errorlevel 1 (
    echo ERROR: hash mismatch - a part file is missing or corrupted.
    echo Make sure all {n} part files were downloaded completely.
    pause
    exit /b 1
)
echo.
echo OK - EccentricBolt.exe is intact ^(SHA-256 verified, {size:,} bytes^).
echo You can now double-click EccentricBolt.exe
pause
exit /b 0
:missing
echo ERROR: reassembled file not created - are all parts present?
pause
exit /b 1
"""


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def split(exe: str, parts: int = 5) -> None:
    size = os.path.getsize(exe)
    digest = sha256(exe)
    chunk = -(-size // parts)                     # ceil
    out_dir = os.path.join(HERE, "split_parts")
    os.makedirs(out_dir, exist_ok=True)
    names = []
    with open(exe, "rb") as fh:
        for i in range(1, parts + 1):
            data = fh.read(chunk)
            if not data:
                break
            name = f"EccentricBolt.exe.{i:03d}"
            with open(os.path.join(out_dir, name), "wb") as out:
                out.write(data)
            names.append(name)
    with open(os.path.join(HERE, "EccentricBolt.exe.sha256"), "w") as fh:
        fh.write(f"{digest}  EccentricBolt.exe\n")
    with open(os.path.join(out_dir, "reassemble_and_verify.bat"), "w",
              newline="\r\n") as fh:
        fh.write(TEMPLATE.format(n=len(names), copy_list="+".join(names),
                                 sha=digest, size=size))
    print(f"{os.path.basename(exe)}: {size:,} bytes, sha256 {digest}")
    print(f"split into {len(names)} parts of up to {chunk:,} bytes each")
    print(f"written to {out_dir}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--exe", default=os.path.join(HERE, "EccentricBolt.exe"))
    ap.add_argument("--parts", type=int, default=5)
    args = ap.parse_args(argv)
    if not os.path.isfile(args.exe):
        raise SystemExit(f"not found: {args.exe} (build the exe first)")
    split(args.exe, args.parts)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
