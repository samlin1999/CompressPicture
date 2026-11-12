"""產生 PyInstaller 用的 Windows 版本資訊檔（exe 右鍵 → 內容 → 詳細資料 看得到）。"""
from version import APP_VERSION

parts = [int(x) for x in APP_VERSION.split(".")] + [0] * 4
v = tuple(parts[:4])

TEMPLATE = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040404B0', [
      StringStruct('FileDescription', '圖片壓縮工具'),
      StringStruct('FileVersion', '{APP_VERSION}'),
      StringStruct('InternalName', 'CompressPicture'),
      StringStruct('OriginalFilename', 'CompressPicture.exe'),
      StringStruct('ProductName', '圖片壓縮工具'),
      StringStruct('ProductVersion', '{APP_VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [0x0404, 1200])])
  ]
)
"""

if __name__ == "__main__":
    with open("version_info.txt", "w", encoding="utf-8") as f:
        f.write(TEMPLATE)
    print(APP_VERSION)
