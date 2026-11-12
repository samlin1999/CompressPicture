"""壓縮引擎（不依賴 Qt，可在子行程中執行）。

每個工作只做一件事：讀入原始檔 → 產生壓縮後的位元組 → 決定要不要採用。
寫檔由 fileops.commit_result 負責。

結果狀態：
  compressed  產生了更小的檔案（data 為新內容）
  kept        壓縮後沒有變小，保留原檔（data 為 None）
  skipped     這個檔案不適合處理（例如動畫 WebP），保留原檔
  error       發生錯誤，沒有動到任何檔案
"""
from __future__ import annotations

import io
import os
import time

import imagequant
import mozjpeg_lossless_optimization as mozjpeg
import numpy as np
import oxipng
from PIL import Image, ImageFile, JpegImagePlugin, PngImagePlugin, UnidentifiedImageError

# 這是本機工具，使用者自己的大圖不該被當成「解壓縮炸彈」擋掉。
Image.MAX_IMAGE_PIXELS = None
ImageFile.LOAD_TRUNCATED_IMAGES = False

SUPPORTED_EXTS = (".png", ".jpg", ".jpeg", ".jfif", ".webp")

ORIENTATION_TAG = 0x0112

DEFAULT_OPTIONS = {
    "mode": "lossless",          # lossless | lossy
    "strip_metadata": True,      # 移除 EXIF/文字等（保留 ICC 與旋轉方向）
    # 無損
    "png_level": 4,              # oxipng 等級 2 / 4 / 6
    "png_zopfli": False,
    # 有損
    "png_quality": 80,           # libimagequant 目標品質 0-100
    "png_max_colors": 256,
    "png_dither": True,
    "jpeg_quality": 80,
    "jpeg_subsampling": "keep",  # keep | 420 | 444
    "webp_quality": 80,
}

LOSSY_PNG_POST_LEVEL = 3  # 調色盤 PNG 的後續無損最佳化等級（快又足夠）


class SkipFile(Exception):
    """此檔不適合處理，保留原檔並說明原因。"""


class VerifyError(Exception):
    """無損驗證失敗。"""


# ---------------------------------------------------------------------------
# 對外入口
# ---------------------------------------------------------------------------

def process_job(job: dict) -> dict:
    """在子行程中執行。job: {id, read_path, options}"""
    t0 = time.perf_counter()
    res = {
        "id": job["id"],
        "status": "error",
        "data": None,
        "orig_size": 0,
        "new_size": 0,
        "format": "",
        "message": "",
        "verified": False,
        "elapsed": 0.0,
    }
    try:
        data, res["read_sig"] = read_stable(job["read_path"])
        res["orig_size"] = len(data)
        res["new_size"] = len(data)
        out = compress_bytes(data, job["options"])
        res.update(out)
        if res["data"] is not None:
            res["new_size"] = len(res["data"])
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as e:  # noqa: BLE001 - 含 Rust panic（PanicException 繼承 BaseException）
        res["status"] = "error"
        res["data"] = None
        res["message"] = friendly_error(e)
    res["elapsed"] = time.perf_counter() - t0
    return res


class FileChanged(Exception):
    """讀檔期間檔案被其他程式修改。"""


def read_stable(path: str):
    """讀入整個檔案，並確認讀取前後檔案沒有被修改（大小、修改時間都一樣）。

    回傳 (data, (size, mtime_ns))。寫檔前會再用這個簽章確認原檔沒被動過。
    """
    with open(path, "rb") as f:
        before = os.fstat(f.fileno())
        data = f.read()
        after = os.fstat(f.fileno())
    sig_before = (before.st_size, before.st_mtime_ns)
    sig_after = (after.st_size, after.st_mtime_ns)
    if sig_before != sig_after or len(data) != after.st_size:
        raise FileChanged("讀取時檔案正在被其他程式寫入")
    return data, sig_after


# 每像素的峰值記憶體估計（實測 4096×4096 雜訊圖，含驗證，再留餘裕）
_BYTES_PER_PIXEL = {"PNG": 64, "JPEG": 24, "MPO": 24, "WEBP": 64}


def webp_dimensions(head: bytes):
    """只看檔頭取得 WebP 寬高（Pillow 開 WebP 會整張解碼，不能拿來估算）。"""
    if len(head) < 30 or head[:4] != b"RIFF" or head[8:12] != b"WEBP":
        return None
    kind = head[12:16]
    if kind == b"VP8X":
        return (int.from_bytes(head[24:27], "little") + 1, int.from_bytes(head[27:30], "little") + 1)
    if kind == b"VP8L" and head[20] == 0x2F:
        bits = int.from_bytes(head[21:25], "little")
        return ((bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if kind == b"VP8 " and head[23:26] == b"\x9d\x01\x2a":
        return (int.from_bytes(head[26:28], "little") & 0x3FFF, int.from_bytes(head[28:30], "little") & 0x3FFF)
    return None


def estimate_memory(path: str) -> int:
    """估計處理這個檔案時，子行程最多會用多少記憶體（位元組）。只讀檔頭，很快。"""
    fallback = 256 * 2**20
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            head = f.read(64)
        dims = webp_dimensions(head)
        if dims:
            fmt = "WEBP"
        else:
            with Image.open(path) as im:  # PNG / JPEG 只讀檔頭
                dims, fmt = im.size, im.format or ""
    except Exception:  # noqa: BLE001 - 讀不到就給保守的預設值，錯誤留給真正處理時回報
        return fallback
    w, h = dims
    return int(w * h * _BYTES_PER_PIXEL.get(fmt, 64) + size * 3)


def compress_bytes(data: bytes, options: dict) -> dict:
    """回傳 {status, data, format, message, verified}。"""
    opts = {**DEFAULT_OPTIONS, **(options or {})}
    with Image.open(io.BytesIO(data)) as probe:
        fmt = probe.format or ""
        n_frames = getattr(probe, "n_frames", 1)
        mode = probe.mode

    if fmt == "MPO" and n_frames <= 1:
        fmt = "JPEG"
    out = {"format": fmt, "verified": False}

    try:
        if fmt == "PNG":
            out.update(_png(data, opts, animated=n_frames > 1))
        elif fmt == "JPEG":
            out.update(_jpeg(data, opts, cmyk=(mode == "CMYK")))
        elif fmt == "MPO":
            raise SkipFile("多影像 JPEG（MPO，例如相機的 3D／深度圖），壓縮會遺失附加影像，已略過")
        elif fmt == "WEBP":
            if n_frames > 1:
                raise SkipFile("動畫 WebP 目前不支援，已略過")
            out.update(_webp(data, opts))
        else:
            raise SkipFile(f"不支援的格式：{fmt or '未知'}")
    except SkipFile as e:
        out.update(status="skipped", data=None, message=str(e))
        return out

    new = out.get("data")
    if new is None or len(new) >= len(data):
        out["status"] = "kept"
        out["data"] = None
        out["verified"] = False
        out["message"] = "壓縮後沒有變小，保留原檔"
    else:
        out["status"] = "compressed"
    return out


# ---------------------------------------------------------------------------
# PNG
# ---------------------------------------------------------------------------

def _png_lossless(data: bytes, opts: dict) -> bytes:
    kwargs = dict(
        level=int(opts["png_level"]),
        strip=oxipng.StripChunks.safe() if opts["strip_metadata"] else oxipng.StripChunks.none(),
        interlace=oxipng.Interlacing.Off,
        optimize_alpha=False,   # 完全透明像素的顏色也保持不變，才是真正無損
        scale_16=False,
    )
    if opts.get("png_zopfli"):
        kwargs["deflate"] = oxipng.Deflaters.zopfli(15)
    return oxipng.optimize_from_memory(data, **kwargs)


def _png(data: bytes, opts: dict, animated: bool) -> dict:
    if opts["mode"] == "lossless" or animated:
        new = _png_lossless(data, opts)
        verify_pixels(data, new)
        msg = "已驗證：像素與原圖完全相同"
        if animated and opts["mode"] == "lossy":
            msg = "動畫 PNG 只做無損最佳化（已驗證每一格像素相同）"
        return {"data": new, "verified": True, "message": msg}

    # --- 有損：libimagequant 量化成調色盤，再用 oxipng 最佳化 ---
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        icc = im.info.get("icc_profile")
        dpi = im.info.get("dpi")
        exif = im.info.get("exif")
        texts = dict(getattr(im, "text", {}) or {})
        rgba = to_rgba8(im)

    # 直接傳 raw bytes（imagequant 的 Pillow 包裝會在 Python 裡逐像素轉換，大圖非常慢）
    size = rgba.size
    raw = rgba.tobytes()
    del rgba  # 盡早釋放，降低大圖的峰值記憶體
    indices, palette = imagequant.quantize_raw_rgba_bytes(
        raw,
        size[0],
        size[1],
        dithering_level=1.0 if opts["png_dither"] else 0.0,
        max_colors=max(2, min(256, int(opts["png_max_colors"]))),
        min_quality=0,
        max_quality=max(0, min(100, int(opts["png_quality"]))),
    )
    del raw
    quant = Image.frombytes("P", size, indices)
    del indices
    quant.putpalette(palette, rawmode="RGBA")
    n_colors = len(palette) // 4

    save_kw = {}
    if icc:
        save_kw["icc_profile"] = icc  # 色彩描述檔影響顯示，一律保留
    if not opts["strip_metadata"]:
        if dpi:
            save_kw["dpi"] = dpi
        if exif:
            save_kw["exif"] = exif
        if texts:
            info = PngImagePlugin.PngInfo()
            for k, v in texts.items():
                try:
                    info.add_text(k, v)
                except Exception:  # noqa: BLE001
                    pass
            save_kw["pnginfo"] = info
    buf = io.BytesIO()
    # 中間檔用最快的壓縮等級：oxipng 會從像素重新過濾、重新壓縮，結果與這裡的等級無關
    quant.save(buf, "PNG", compress_level=1, **save_kw)
    del quant

    lossy = oxipng.optimize_from_memory(
        buf.getvalue(),
        level=LOSSY_PNG_POST_LEVEL,
        strip=oxipng.StripChunks.safe() if opts["strip_metadata"] else oxipng.StripChunks.none(),
        interlace=oxipng.Interlacing.Off,
        optimize_alpha=True,
    )
    if len(lossy) < len(data):
        return {"data": lossy, "message": f"減少為 {n_colors} 色"}

    # 量化沒有變小（例如原圖本來就是小調色盤）→ 改試無損最佳化
    lossless = _png_lossless(data, opts)
    verify_pixels(data, lossless)
    return {
        "data": lossless,
        "verified": True,
        "message": "有損壓縮沒有變小，改用無損最佳化（畫質不變，已驗證）",
    }


# ---------------------------------------------------------------------------
# JPEG
# ---------------------------------------------------------------------------

def _jpeg_lossless(data: bytes, strip: bool) -> bytes:
    if not strip:
        return mozjpeg.optimize(data, mozjpeg.COPY_MARKERS.ALL)
    out = mozjpeg.optimize(data, mozjpeg.COPY_MARKERS.ICC)
    orientation = _orientation_of(data)
    if orientation not in (None, 1):
        out = insert_jpeg_exif(out, minimal_exif(orientation))
    return out


def _jpeg(data: bytes, opts: dict, cmyk: bool) -> dict:
    strip = opts["strip_metadata"]
    lossless = _jpeg_lossless(data, strip)

    if opts["mode"] == "lossless" or cmyk:
        verify_pixels(data, lossless)
        msg = "已驗證：像素與原圖完全相同"
        if opts["mode"] == "lossy":
            msg = "CMYK JPEG 只做無損最佳化（避免色彩偏差，已驗證）"
        return {"data": lossless, "verified": True, "message": msg}

    # --- 有損：重新以指定品質編碼 ---
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        icc = im.info.get("icc_profile")
        sub_src = JpegImagePlugin.get_sampling(im)
        exif_bytes = _exif_for_output(im, strip)
        xmp = im.info.get("xmp")
        work = im if im.mode in ("RGB", "L") else im.convert("RGB")
        if strip:
            work.info.pop("comment", None)  # Pillow 會自動沿用 im.info 的註解

        sub_opt = opts.get("jpeg_subsampling", "keep")
        if sub_opt == "444":
            subsampling = 0
        elif sub_opt == "420":
            subsampling = 2
        else:
            subsampling = sub_src if sub_src in (0, 1, 2) else 2

        # 霍夫曼最佳化與漸進式交給後面的 mozjpeg 做（Pillow 再做一次只是浪費時間）
        save_kw = dict(quality=int(opts["jpeg_quality"]))
        if work.mode == "RGB":
            save_kw["subsampling"] = subsampling
        if icc:
            save_kw["icc_profile"] = icc
        if exif_bytes:
            save_kw["exif"] = exif_bytes
        if xmp and not strip:
            save_kw["xmp"] = xmp
        buf = io.BytesIO()
        work.save(buf, "JPEG", **save_kw)

    lossy = mozjpeg.optimize(buf.getvalue(), mozjpeg.COPY_MARKERS.ALL)
    lossy_result = {"data": lossy, "message": f"品質 {int(opts['jpeg_quality'])}"}

    # 若無損最佳化反而更小（原圖品質本來就比設定低），採用無損結果：更小而且畫質不變。
    # 只有要採用時才驗證（省下一次完整解碼比對）；萬一驗證不過就用有損結果。
    if len(lossless) <= len(lossy):
        try:
            verify_pixels(data, lossless)
        except VerifyError:
            return lossy_result
        return {"data": lossless, "verified": True,
                "message": "無損最佳化的結果比重新編碼更小，採用無損結果（畫質不變，已驗證）"}
    return lossy_result


# ---------------------------------------------------------------------------
# WebP
# ---------------------------------------------------------------------------

def webp_kind(data: bytes) -> str:
    """回傳 'lossless'、'lossy' 或 ''（看 RIFF 區塊：VP8L = 無損，VP8 = 有損）。"""
    if data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return ""
    pos = 12
    while pos + 8 <= len(data):
        fourcc = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        if fourcc == b"VP8L":
            return "lossless"
        if fourcc == b"VP8 ":
            return "lossy"
        pos += 8 + size + (size & 1)
    return ""


# 無損 WebP 的編碼強度，跟著「最佳化程度」（oxipng 等級）走
_WEBP_EFFORT = {2: (4, 75), 4: (5, 90), 6: (6, 100)}


def _webp(data: bytes, opts: dict) -> dict:
    strip = opts["strip_metadata"]
    if opts["mode"] == "lossless" and webp_kind(data) == "lossy":
        raise SkipFile("這是有損 WebP，無法再做無損壓縮（重新編碼只會變大）；要再縮小請改用有損模式")
    with Image.open(io.BytesIO(data)) as im:
        im.load()
        icc = im.info.get("icc_profile")
        exif_bytes = _exif_for_output(im, strip)
        work = im if im.mode in ("RGB", "RGBA") else im.convert("RGBA")
        save_kw = {}
        if icc:
            save_kw["icc_profile"] = icc
        if exif_bytes:
            save_kw["exif"] = exif_bytes
        buf = io.BytesIO()
        if opts["mode"] == "lossless":
            method, effort = _WEBP_EFFORT.get(int(opts["png_level"]), (5, 90))
            work.save(buf, "WEBP", lossless=True, quality=effort, method=method, exact=True, **save_kw)
        else:
            work.save(buf, "WEBP", quality=int(opts["webp_quality"]), method=6, **save_kw)
    new = buf.getvalue()

    if opts["mode"] == "lossless":
        if len(new) < len(data):
            verify_pixels(data, new)
            return {"data": new, "verified": True, "message": "已驗證：像素與原圖完全相同"}
        return {"data": None}
    return {"data": new, "message": f"品質 {int(opts['webp_quality'])}"}


# ---------------------------------------------------------------------------
# 中繼資料小工具
# ---------------------------------------------------------------------------

def _orientation_of(data: bytes):
    try:
        with Image.open(io.BytesIO(data)) as im:
            return im.getexif().get(ORIENTATION_TAG)
    except Exception:  # noqa: BLE001
        return None


def minimal_exif(orientation: int) -> bytes:
    ex = Image.Exif()
    ex[ORIENTATION_TAG] = int(orientation)
    return ex.tobytes()


def _exif_for_output(im: Image.Image, strip: bool):
    """不移除時沿用原 EXIF；移除時只留下旋轉方向（否則照片會轉向）。"""
    if not strip:
        return im.info.get("exif")
    orientation = im.getexif().get(ORIENTATION_TAG)
    if orientation not in (None, 1):
        return minimal_exif(orientation)
    return None


def insert_jpeg_exif(jpeg: bytes, exif_payload: bytes) -> bytes:
    """在 SOI（及 JFIF APP0）之後插入 APP1 Exif 區段。"""
    if not exif_payload.startswith(b"Exif\x00\x00"):
        exif_payload = b"Exif\x00\x00" + exif_payload
    if len(exif_payload) + 2 > 0xFFFF:
        return jpeg
    seg = b"\xff\xe1" + (len(exif_payload) + 2).to_bytes(2, "big") + exif_payload
    pos = 2  # SOI 之後
    if jpeg[2:4] == b"\xff\xe0":
        pos = 4 + int.from_bytes(jpeg[4:6], "big")
    return jpeg[:pos] + seg + jpeg[pos:]


# ---------------------------------------------------------------------------
# 像素轉換與驗證
# ---------------------------------------------------------------------------

_SIXTEEN_BIT_GRAY = ("I;16", "I;16L", "I;16B", "I;16N", "I")


def to_rgba8(im: Image.Image) -> Image.Image:
    """轉成 8-bit RGBA。Pillow 直接轉 16-bit 灰階會截斷，這裡改為正確縮放。"""
    if im.mode in _SIXTEEN_BIT_GRAY:
        a = np.asarray(im)  # I;16 → uint16（值域 0..65535）
        trns = im.info.get("transparency")
        g = (a >> 8).astype(np.uint8)
        alpha = np.full(g.shape, 255, np.uint8)
        if isinstance(trns, int):
            alpha[a == trns] = 0
        return Image.fromarray(np.dstack([g, g, g, alpha]), "RGBA")
    if im.mode == "RGBA":
        return im
    return im.convert("RGBA")


# 驗證時每次比對的像素數。整張比對要把兩張圖各複製好幾份（實測每像素 56 bytes），
# 分段後額外記憶體固定在幾十 MB 以內。
_VERIFY_BAND_PIXELS = 1 << 20


def _canonical_band(im: Image.Image, wide: bool) -> np.ndarray:
    """轉成可比較的 RGBA 陣列。wide=True 時用 16-bit 尺度（8-bit 值 ×257），
    只有其中一邊是 16-bit 灰階時才需要。"""
    if im.mode in _SIXTEEN_BIT_GRAY:
        a = np.asarray(im).astype(np.uint32)
        alpha = np.full(a.shape, 65535, np.uint32)
        trns = im.info.get("transparency")
        if isinstance(trns, int):
            alpha[a == trns] = 0
        return np.dstack([a, a, a, alpha])
    rgba = np.asarray(im.convert("RGBA"))
    return rgba.astype(np.uint32) * 257 if wide else rgba


def _frame_equal(a: Image.Image, b: Image.Image) -> bool:
    if a.size != b.size:
        return False
    simple = (
        a.mode == b.mode
        and a.mode not in ("P", "PA")
        and "transparency" not in a.info
        and "transparency" not in b.info
    )
    if not simple and "CMYK" in (a.mode, b.mode):
        return False  # CMYK 只會和 CMYK 相比（上面的 simple 路徑）
    wide = a.mode in _SIXTEEN_BIT_GRAY or b.mode in _SIXTEEN_BIT_GRAY
    w, h = a.size
    rows = max(1, _VERIFY_BAND_PIXELS // max(1, w))
    for y in range(0, h, rows):
        box = (0, y, w, min(h, y + rows))
        ba, bb = a.crop(box), b.crop(box)  # crop 會保留調色盤與透明色資訊
        if simple:
            if ba.tobytes() != bb.tobytes():
                return False
        elif not np.array_equal(_canonical_band(ba, wide), _canonical_band(bb, wide)):
            return False
    return True


def verify_pixels(original: bytes, new: bytes) -> None:
    """逐格、逐像素比對。不一致就丟出 VerifyError（呼叫端會保留原檔）。"""
    with Image.open(io.BytesIO(original)) as a, Image.open(io.BytesIO(new)) as b:
        na, nb = getattr(a, "n_frames", 1), getattr(b, "n_frames", 1)
        if na != nb:
            raise VerifyError(f"影格數不同（{na} → {nb}）")
        for i in range(na):
            if na > 1:
                a.seek(i)
                b.seek(i)
            a.load()
            b.load()
            if not _frame_equal(a, b):
                where = f"第 {i + 1} 格" if na > 1 else "影像"
                raise VerifyError(f"{where}像素不一致")


# ---------------------------------------------------------------------------
# 錯誤訊息
# ---------------------------------------------------------------------------

def friendly_error(e: BaseException) -> str:
    if isinstance(e, VerifyError):
        return f"無損驗證失敗（{e}），已保留原檔"
    if isinstance(e, FileChanged):
        return "讀取時檔案正在被其他程式寫入，已略過（請稍後重新壓縮）"
    if isinstance(e, FileNotFoundError):
        return "找不到檔案（可能已被移動或刪除）"
    if isinstance(e, PermissionError):
        return "沒有權限讀取這個檔案"
    if isinstance(e, UnidentifiedImageError):
        return "無法辨識的圖片（檔案可能已損毀，或副檔名與內容不符）"
    if isinstance(e, MemoryError):
        return "記憶體不足，圖片太大"
    if isinstance(e, oxipng.PngError):
        return f"PNG 處理失敗：{e}"
    if isinstance(e, OSError) and "truncated" in str(e).lower():
        return "圖片檔不完整（可能已損毀）"
    return f"{type(e).__name__}：{e}"
