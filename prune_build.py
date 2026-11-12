"""移除打包結果中用不到的 Qt 元件（約 50 MB）。build.bat 會在 PyInstaller 之後呼叫。

刪完之後 build.bat 會跑 --selftest（壓縮引擎 + 背景開主視窗），確認沒有刪到必要的東西。
"""
import os
import shutil
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist", "CompressPicture", "_internal")
QT = os.path.join(ROOT, "PySide6")

REMOVE = [
    # 軟體 OpenGL（Widgets 介面不需要）
    "PySide6/opengl32sw.dll",
    # 虛擬鍵盤外掛會連帶拉進 QML / Quick
    "PySide6/plugins/platforminputcontexts",
    "PySide6/Qt6VirtualKeyboard.dll",
    "PySide6/Qt6Quick.dll", "PySide6/Qt6Qml.dll", "PySide6/Qt6QmlMeta.dll",
    "PySide6/Qt6QmlModels.dll", "PySide6/Qt6QmlWorkerScript.dll",
    # PDF 圖片外掛
    "PySide6/plugins/imageformats/qpdf.dll", "PySide6/Qt6Pdf.dll",
    # 網路（TLS、網路狀態、TUIO 觸控）
    "PySide6/plugins/tls", "PySide6/plugins/networkinformation", "PySide6/plugins/generic",
    "PySide6/Qt6Network.dll", "PySide6/QtNetwork.pyd",
    "libcrypto-3-x64.dll", "libssl-3-x64.dll",
    # Pillow 的 AVIF 外掛（不支援 AVIF；Pillow 找不到時會自動略過）
    "PIL/_avif.cp312-win_amd64.pyd",
]
KEEP_TRANSLATIONS = {"qtbase_zh_TW.qm", "qt_zh_TW.qm"}


def size_of(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    return sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(path) for f in fs)


def main():
    if not os.path.isdir(ROOT):
        print("找不到打包結果：", ROOT)
        return 1
    freed = 0
    for rel in REMOVE:
        p = os.path.join(ROOT, rel)
        if os.path.exists(p):
            freed += size_of(p)
            shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
    tdir = os.path.join(QT, "translations")
    if os.path.isdir(tdir):
        for f in os.listdir(tdir):
            if f not in KEEP_TRANSLATIONS:
                p = os.path.join(tdir, f)
                freed += size_of(p)
                os.remove(p)
    print(f"pruned {freed / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
