"""把 dist\\CompressPicture 打包成可以直接交給別人的 zip。build.bat 在自我檢查通過後呼叫。

zip 內容：CompressPicture\\ { CompressPicture.exe, _internal\\, README.txt, selftest.bat }
檔名全部限定 ASCII：Windows 10 內建的「解壓縮全部」可能把 zip 裡的中文檔名解成亂碼。
"""
import os
import shutil
import sys
import zipfile

from version import APP_VERSION

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
APP_DIR = os.path.join(DIST, "CompressPicture")

SELFTEST_BAT = "\r\n".join([
    "@echo off",
    "rem Runs a self-check of every compression engine (no window). Takes about 10 seconds.",
    'cd /d "%~dp0"',
    "echo Checking, please wait...",
    # 用完整路徑：有些電腦（或公司政策）設定了 NoDefaultCurrentDirectoryInExePath，
    # 只寫 CompressPicture.exe 會找不到（實測 errorlevel 9009）
    '"%~dp0CompressPicture.exe" --selftest "%~dp0selftest.json"',
    "if errorlevel 1 (",
    "    echo.",
    "    echo FAILED - please send selftest.json in this folder to the maintainer.",
    ") else (",
    "    echo.",
    "    echo OK - all checks passed.",
    ")",
    "pause",
    "",
])


def main() -> int:
    if not os.path.isfile(os.path.join(APP_DIR, "CompressPicture.exe")):
        print("找不到 dist\\CompressPicture\\CompressPicture.exe，請先執行 build.bat")
        return 1
    shutil.copyfile(os.path.join(ROOT, "README_dist.txt"), os.path.join(APP_DIR, "README.txt"))
    with open(os.path.join(APP_DIR, "selftest.bat"), "w", encoding="ascii", newline="") as f:
        f.write(SELFTEST_BAT)
    for leftover in ("selftest.json", "selftest.png"):  # 打包前自我檢查留下的檔案不要放進去
        p = os.path.join(APP_DIR, leftover)
        if os.path.exists(p):
            os.remove(p)

    zip_path = os.path.join(DIST, f"CompressPicture-{APP_VERSION}-win64.zip")
    non_ascii = []
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for root, dirs, files in os.walk(APP_DIR):
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(root, name)
                arc = os.path.relpath(full, DIST).replace(os.sep, "/")
                if not arc.isascii():
                    non_ascii.append(arc)
                z.write(full, arc)
                count += 1
    if non_ascii:
        os.remove(zip_path)
        print("zip 裡有非 ASCII 檔名（Windows 10 內建解壓縮可能變亂碼）：", *non_ascii, sep="\n  ")
        return 1
    size = os.path.getsize(zip_path) / 2**20
    print(f"{zip_path}  ({count} files, {size:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
