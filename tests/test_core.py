import io
import os
import sys

import numpy as np
import pytest
from PIL import Image, ImageCms, PngImagePlugin

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import compressor as C  # noqa: E402
import fileops as F  # noqa: E402

RNG = np.random.default_rng(0)
SRGB = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def photo(w=320, h=240):
    y, x = np.mgrid[0:h, 0:w]
    rgb = np.stack([x / w * 255, y / h * 255, (x + y) % 256], -1) + RNG.normal(0, 6, (h, w, 3))
    return Image.fromarray(np.clip(rgb, 0, 255).astype("uint8"), "RGB")


def ui_rgba(w=200, h=120):
    """像遊戲 UI 的圖：漸層 + 半透明 + 完全透明區（透明處 RGB 是雜訊）。"""
    a = np.zeros((h, w, 4), np.uint8)
    y, x = np.mgrid[0:h, 0:w]
    a[..., 0] = x * 255 // w
    a[..., 1] = 120
    a[..., 2] = y * 255 // h
    a[..., 3] = np.where(x < w // 4, 0, np.where(x < w // 2, 128, 255))
    a[:, : w // 4, :3] = RNG.integers(0, 255, (h, w // 4, 3))
    return Image.fromarray(a, "RGBA")


def png_bytes(im, **kw):
    b = io.BytesIO()
    im.save(b, "PNG", **kw)
    return b.getvalue()


def jpeg_bytes(im, **kw):
    b = io.BytesIO()
    im.save(b, "JPEG", **kw)
    return b.getvalue()


def webp_bytes(im, **kw):
    b = io.BytesIO()
    im.save(b, "WEBP", **kw)
    return b.getvalue()


def opts(**kw):
    return {**C.DEFAULT_OPTIONS, **kw}


def pixels(data):
    with Image.open(io.BytesIO(data)) as im:
        return np.asarray(C.to_rgba8(im) if im.mode != "CMYK" else im)


# ---------------------------------------------------------------- 無損

@pytest.mark.parametrize("make", [
    lambda: png_bytes(ui_rgba()),
    lambda: png_bytes(photo()),
    lambda: png_bytes(photo().convert("L")),
    lambda: png_bytes(ui_rgba().convert("P", palette=Image.Palette.ADAPTIVE, colors=64)),
    lambda: png_bytes(Image.fromarray((np.arange(64 * 64).reshape(64, 64) * 16).astype("uint16"))),
    lambda: png_bytes(photo(), compress_level=0),
    lambda: png_bytes(ui_rgba(), icc_profile=SRGB),
])
def test_png_lossless_is_exact(make):
    data = make()
    r = C.compress_bytes(data, opts(mode="lossless"))
    assert r["status"] in ("compressed", "kept")
    if r["status"] == "compressed":
        assert r["verified"]
        assert len(r["data"]) < len(data)
        C.verify_pixels(data, r["data"])
        # 透明像素底下的 RGB 也必須保留（optimize_alpha 關閉）
        assert np.array_equal(pixels(data), pixels(r["data"]))


def test_png_lossless_keeps_icc_strips_text():
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "secret")
    data = png_bytes(photo(), pnginfo=info, icc_profile=SRGB, compress_level=1)
    r = C.compress_bytes(data, opts(mode="lossless", strip_metadata=True))
    with Image.open(io.BytesIO(r["data"])) as im:
        assert im.info.get("icc_profile")
        assert "Comment" not in getattr(im, "text", {})
    r = C.compress_bytes(data, opts(mode="lossless", strip_metadata=False))
    with Image.open(io.BytesIO(r["data"])) as im:
        assert im.text.get("Comment") == "secret"


def test_apng_lossless_keeps_frames_and_lossy_falls_back():
    frames = [ui_rgba().rotate(i * 30) for i in range(3)]
    b = io.BytesIO()
    frames[0].save(b, "PNG", save_all=True, append_images=frames[1:], duration=80, loop=0, compress_level=1)
    data = b.getvalue()
    for mode in ("lossless", "lossy"):
        r = C.compress_bytes(data, opts(mode=mode))
        assert r["status"] in ("compressed", "kept")
        if r["status"] == "compressed":
            with Image.open(io.BytesIO(r["data"])) as im:
                assert im.n_frames == 3
            C.verify_pixels(data, r["data"])


def test_jpeg_lossless_exact_and_orientation_kept():
    ex = Image.Exif()
    ex[0x0112] = 6
    ex[0x010F] = "Canon"
    data = jpeg_bytes(photo(), quality=92, exif=ex.tobytes(), icc_profile=SRGB)
    r = C.compress_bytes(data, opts(mode="lossless", strip_metadata=True))
    assert r["status"] == "compressed" and r["verified"]
    with Image.open(io.BytesIO(r["data"])) as im:
        e = im.getexif()
        assert e.get(0x0112) == 6          # 旋轉方向保留
        assert 0x010F not in e              # 其餘 EXIF 移除
        assert im.info.get("icc_profile") == SRGB
    C.verify_pixels(data, r["data"])
    r2 = C.compress_bytes(data, opts(mode="lossless", strip_metadata=False))
    with Image.open(io.BytesIO(r2["data"])) as im:
        assert im.getexif().get(0x010F) == "Canon"


@pytest.mark.parametrize("mode", ["L", "CMYK"])
def test_jpeg_lossless_gray_cmyk(mode):
    data = jpeg_bytes(photo().convert(mode), quality=90)
    r = C.compress_bytes(data, opts(mode="lossless"))
    assert r["status"] == "compressed"
    C.verify_pixels(data, r["data"])


def test_webp_lossless():
    data = webp_bytes(ui_rgba(), lossless=True, method=0)
    r = C.compress_bytes(data, opts(mode="lossless"))
    assert r["status"] in ("compressed", "kept")
    if r["status"] == "compressed":
        C.verify_pixels(data, r["data"])
    # 有損 WebP 轉無損一定變大（而且非常慢）→ 直接略過並說明
    lossy_src = webp_bytes(photo(), quality=70)
    r = C.compress_bytes(lossy_src, opts(mode="lossless"))
    assert r["status"] == "skipped" and r["data"] is None and "有損 WebP" in r["message"]


def test_webp_kind():
    assert C.webp_kind(webp_bytes(photo(), quality=70)) == "lossy"
    assert C.webp_kind(webp_bytes(ui_rgba(), quality=70)) == "lossy"       # VP8X + ALPH + VP8
    assert C.webp_kind(webp_bytes(ui_rgba(), lossless=True)) == "lossless"
    assert C.webp_kind(b"not webp") == ""


def test_verify_detects_difference():
    a = png_bytes(ui_rgba())
    im = ui_rgba()
    im.putpixel((100, 50), (0, 0, 0, 255))
    with pytest.raises(C.VerifyError):
        C.verify_pixels(a, png_bytes(im))
    # 透明處顏色不同也要抓到
    im2 = ui_rgba()
    px = im2.getpixel((1, 1))
    im2.putpixel((1, 1), ((px[0] + 1) % 256, px[1], px[2], 0))
    with pytest.raises(C.VerifyError):
        C.verify_pixels(a, png_bytes(im2))


# ---------------------------------------------------------------- 有損

def test_png_lossy_smaller_and_valid():
    data = png_bytes(photo(640, 480))
    r = C.compress_bytes(data, opts(mode="lossy", png_quality=70))
    assert r["status"] == "compressed"
    assert len(r["data"]) < len(data) * 0.6
    with Image.open(io.BytesIO(r["data"])) as im:
        assert im.size == (640, 480)
        assert im.mode in ("P", "PA")


def test_png_lossy_keeps_alpha():
    data = png_bytes(ui_rgba())
    r = C.compress_bytes(data, opts(mode="lossy"))
    assert r["status"] == "compressed"
    alpha_src = pixels(data)[..., 3]
    alpha_out = pixels(r["data"])[..., 3]
    assert np.array_equal(alpha_src == 0, alpha_out == 0)
    assert np.abs(alpha_src.astype(int) - alpha_out).max() <= 2


def test_png_lossy_16bit_gray_not_clipped():
    a = (np.arange(256 * 64).reshape(64, 256) * 4).astype("uint16")
    data = png_bytes(Image.fromarray(a))
    r = C.compress_bytes(data, opts(mode="lossy"))
    if r["status"] == "compressed":
        out = pixels(r["data"])[..., 0].astype(int)
        expect = (a >> 8).astype(int)
        assert np.abs(out - expect).mean() < 3


def test_png_lossy_falls_back_to_lossless_for_tiny_palette():
    im = Image.new("P", (64, 64), 0)
    im.putpalette([255, 0, 0, 0, 255, 0] + [0] * 762)
    im.paste(1, (0, 0, 32, 64))
    data = png_bytes(im, compress_level=0)
    r = C.compress_bytes(data, opts(mode="lossy"))
    assert r["status"] == "compressed"
    C.verify_pixels(data, r["data"])


def test_jpeg_lossy_keeps_orientation_icc_and_size():
    ex = Image.Exif()
    ex[0x0112] = 8
    data = jpeg_bytes(photo(800, 600), quality=95, exif=ex.tobytes(), icc_profile=SRGB, subsampling=0)
    r = C.compress_bytes(data, opts(mode="lossy", jpeg_quality=75))
    assert r["status"] == "compressed"
    assert len(r["data"]) < len(data) * 0.7
    with Image.open(io.BytesIO(r["data"])) as im:
        assert im.size == (800, 600)
        assert im.getexif().get(0x0112) == 8
        assert im.info.get("icc_profile") == SRGB
        from PIL import JpegImagePlugin
        assert JpegImagePlugin.get_sampling(im) == 0  # 依原圖 4:4:4


def test_jpeg_lossy_low_quality_source_uses_lossless():
    data = jpeg_bytes(photo(), quality=40)
    r = C.compress_bytes(data, opts(mode="lossy", jpeg_quality=85))
    assert r["status"] == "compressed"
    assert r["verified"]  # 採用無損結果
    C.verify_pixels(data, r["data"])


def test_jpeg_lossy_cmyk_only_lossless():
    data = jpeg_bytes(photo().convert("CMYK"), quality=95)
    r = C.compress_bytes(data, opts(mode="lossy"))
    assert r["verified"]
    C.verify_pixels(data, r["data"])


def test_webp_lossy():
    data = webp_bytes(photo(800, 600), lossless=True)
    r = C.compress_bytes(data, opts(mode="lossy", webp_quality=75))
    assert r["status"] == "compressed"
    assert len(r["data"]) < len(data)


def test_animated_webp_skipped():
    frames = [photo(64, 64) for _ in range(2)]
    b = io.BytesIO()
    frames[0].save(b, "WEBP", save_all=True, append_images=frames[1:], duration=100)
    r = C.compress_bytes(b.getvalue(), opts(mode="lossy"))
    assert r["status"] == "skipped"


def test_corrupt_file_reports_error(tmp_path):
    p = tmp_path / "bad.png"
    p.write_bytes(b"not an image")
    r = C.process_job({"id": 1, "read_path": str(p), "options": opts()})
    assert r["status"] == "error" and "無法辨識" in r["message"]


def test_extension_mismatch_uses_real_format(tmp_path):
    p = tmp_path / "actually_jpeg.png"
    p.write_bytes(jpeg_bytes(photo(), quality=95))
    r = C.process_job({"id": 1, "read_path": str(p), "options": opts(mode="lossless")})
    assert r["format"] == "JPEG" and r["status"] == "compressed"


# ---------------------------------------------------------------- 檔案操作

def test_collect_and_plan(tmp_path):
    d = tmp_path / "art"
    (d / "sub").mkdir(parents=True)
    (d / "a.png").write_bytes(png_bytes(ui_rgba()))
    (d / "sub" / "b.jpg").write_bytes(jpeg_bytes(photo()))
    (d / "notes.txt").write_text("x")
    single = tmp_path / "c.webp"
    single.write_bytes(webp_bytes(photo()))
    res = F.collect([str(d), str(single)])
    rels = sorted(r for _, r in res.files)
    assert rels == sorted([os.path.join("art", "a.png"), os.path.join("art", "sub", "b.jpg"), "c.webp"])
    assert res.unsupported == 1

    items = [F.PlanItem(i, p, r) for i, (p, r) in enumerate(res.files)]
    out = tmp_path / "out"
    t, prob = F.plan_targets(items, F.OUT_FOLDER, out_dir=str(out))
    assert not prob
    assert t[0] == os.path.join(str(out), items[0].rel)

    t, prob = F.plan_targets(items, F.OUT_SUFFIX, suffix="_min")
    assert all(os.path.splitext(v)[0].endswith("_min") for v in t.values())
    with pytest.raises(ValueError):
        F.plan_targets(items, F.OUT_SUFFIX, suffix="")
    with pytest.raises(ValueError):
        F.plan_targets(items, F.OUT_FOLDER, out_dir="")


def test_plan_conflicts(tmp_path):
    a = str(tmp_path / "x" / "a.png")
    b = str(tmp_path / "y" / "a.png")
    items = [F.PlanItem(1, a, "a.png"), F.PlanItem(2, b, "a.png")]
    t, prob = F.plan_targets(items, F.OUT_FOLDER, out_dir=str(tmp_path / "o"))
    assert t[1].endswith("a.png") and t[2].endswith("a (2).png")
    # 輸出資料夾等於來源資料夾
    t, prob = F.plan_targets([F.PlanItem(1, a, "a.png")], F.OUT_FOLDER, out_dir=str(tmp_path / "x"))
    assert 1 in prob
    # 後綴輸出會蓋到清單中另一個原始檔
    c = str(tmp_path / "x" / "a_min.png")
    t, prob = F.plan_targets([F.PlanItem(1, a, "a.png"), F.PlanItem(2, c, "a_min.png")], F.OUT_SUFFIX, suffix="_min")
    assert 1 in prob and 2 in t


def test_commit_overwrite_backup_and_rerun(tmp_path):
    src = tmp_path / "a.png"
    original = png_bytes(photo(), compress_level=0)
    src.write_bytes(original)
    bdir = str(tmp_path / "bk")
    job = {"id": 1, "target": str(src), "source_path": str(src), "read_path": str(src),
           "overwrite": True, "backup": None, "backup_dir": bdir, "options": opts(mode="lossless")}
    r = F.commit_result(job, C.process_job(job))
    assert r["status"] == "compressed" and r["backup"]
    assert open(r["backup"], "rb").read() == original
    assert src.stat().st_size < len(original)

    # 重新壓縮：從備份讀；若結果「保留原檔」，磁碟上要回到原檔
    job2 = dict(job, read_path=r["backup"], backup=r["backup"], disk_sig=F.file_sig(str(src)))
    res = C.process_job(job2)
    res.update(status="kept", data=None)
    F.commit_result(job2, res)
    assert src.read_bytes() == original

    F.restore_backup(r["backup"], str(src))
    assert src.read_bytes() == original


def test_commit_suffix_kept_copies_original(tmp_path):
    src = tmp_path / "a.webp"
    data = webp_bytes(photo(), quality=60)
    src.write_bytes(data)
    target = tmp_path / "a_min.webp"
    job = {"id": 1, "target": str(target), "source_path": str(src), "read_path": str(src),
           "overwrite": False, "backup": None, "backup_dir": "", "options": opts(mode="lossless")}
    r = F.commit_result(job, C.process_job(job))
    assert r["status"] == "skipped"          # 有損 WebP 在無損模式會略過
    assert target.read_bytes() == data       # 但輸出位置仍放一份原檔，輸出資料夾保持完整
    assert src.read_bytes() == data
    assert not [p for p in os.listdir(tmp_path) if p.endswith(".tmp")]


def test_human_size():
    assert F.human_size(0) == "0 B"
    assert F.human_size(1536) == "1.5 KB"
    assert F.human_size(5 * 1024 * 1024) == "5.0 MB"


# ---------------------------------------------------------------- 競態：原檔在處理期間被修改

def _touch_different(path, data):
    """寫入不同內容，並確保修改時間一定不同。"""
    old = os.stat(path).st_mtime_ns
    with open(path, "wb") as f:
        f.write(data)
    os.utime(path, ns=(old + 10**9, old + 10**9))


@pytest.mark.parametrize("overwrite", [True, False])
def test_source_modified_during_processing_is_not_overwritten(tmp_path, overwrite):
    src = tmp_path / "a.png"
    src.write_bytes(png_bytes(photo(), compress_level=0))
    bdir = tmp_path / "bk"
    target = str(src) if overwrite else str(tmp_path / "a_min.png")
    job = {"id": 1, "target": target, "source_path": str(src), "read_path": str(src),
           "overwrite": overwrite, "backup": None, "backup_dir": str(bdir), "options": opts()}
    res = C.process_job(job)
    assert res["status"] == "compressed" and res["read_sig"]
    edited = png_bytes(ui_rgba())  # 使用者在壓縮期間用別的程式改了這張圖
    _touch_different(str(src), edited)
    r = F.commit_result(job, res)
    assert r["status"] == "error" and r["message"] == F.CHANGED_MESSAGE
    assert src.read_bytes() == edited          # 使用者的修改還在
    assert not r["backup"]
    assert not bdir.exists() or not os.listdir(bdir)  # 沒有留下用不到的備份
    if not overwrite:
        assert not os.path.exists(target)


def test_rerun_from_backup_detects_external_edit(tmp_path):
    src = tmp_path / "a.png"
    original = png_bytes(photo(), compress_level=0)
    src.write_bytes(original)
    job = {"id": 1, "target": str(src), "source_path": str(src), "read_path": str(src),
           "overwrite": True, "backup": None, "backup_dir": str(tmp_path / "bk"), "options": opts()}
    r = F.commit_result(job, C.process_job(job))
    assert r["status"] == "compressed" and r["written_sig"] == F.file_sig(str(src))
    # 重新壓縮規劃時記下的狀態
    job2 = dict(job, read_path=r["backup"], backup=r["backup"], disk_sig=F.file_sig(str(src)))
    res2 = C.process_job(job2)
    edited = png_bytes(ui_rgba())
    _touch_different(str(src), edited)          # 規劃之後、寫入之前被改了
    r2 = F.commit_result(job2, res2)
    assert r2["status"] == "error" and src.read_bytes() == edited


def test_read_stable_returns_signature(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(b"12345")
    data, sig = C.read_stable(str(p))
    assert data == b"12345" and sig == F.file_sig(str(p))


# ---------------------------------------------------------------- 同一個檔案的不同寫法

def _short_path(path):
    import ctypes
    buf = ctypes.create_unicode_buffer(1024)
    n = ctypes.windll.kernel32.GetShortPathNameW(str(path), buf, 1024)
    return buf.value if n else str(path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 8.3 短檔名")
def test_plan_detects_alias_of_source_folder(tmp_path):
    d = tmp_path / "long folder name here"
    d.mkdir()
    src = d / "a.png"
    src.write_bytes(png_bytes(ui_rgba()))
    short = _short_path(d)
    if os.path.normcase(short) == os.path.normcase(str(d)):
        pytest.skip("這個磁碟沒有啟用 8.3 短檔名")
    # 輸出資料夾用短檔名寫法指到同一個資料夾 → 必須認出會蓋到原檔
    items = [F.PlanItem(1, str(src), "a.png")]
    t, prob = F.plan_targets(items, F.OUT_FOLDER, out_dir=short)
    assert 1 in prob and 1 not in t
    # 同一個檔案的兩種寫法，身分與實際路徑都相同
    short_file = os.path.join(short, "a.png")
    assert F.identity(short_file) == F.identity(str(src))
    canon = F.PathCanon()
    assert canon(short_file) == canon(str(src))


def test_plan_protects_sources_not_in_this_run(tmp_path):
    a = tmp_path / "a.png"
    b = tmp_path / "a_min.png"   # 清單中另一個檔案，但這次只重新壓縮 a
    a.write_bytes(png_bytes(ui_rgba()))
    b.write_bytes(png_bytes(photo()))
    protected = {F.identity(str(a)): 1, F.identity(str(b)): 2}
    t, prob = F.plan_targets([F.PlanItem(1, str(a), "a.png")], F.OUT_SUFFIX, suffix="_min",
                             protected=protected)
    assert 1 in prob and "另一個原始檔" in prob[1]


# ---------------------------------------------------------------- 分段驗證

def test_chunked_verify_catches_difference_in_last_band():
    w, h = 64, 40000  # 256 萬像素 > 每段 100 萬像素，會分成好幾段
    a = np.zeros((h, w, 4), np.uint8)
    a[..., 3] = 255
    a[:, :, 0] = (np.arange(h) % 251)[:, None]
    ok = png_bytes(Image.fromarray(a, "RGBA"))
    same_rgb = png_bytes(Image.fromarray(a[..., :3].copy(), "RGB"))  # 模式不同但像素相同
    C.verify_pixels(ok, same_rgb)
    b = a.copy()
    b[h - 1, w - 1, 1] = 7
    with pytest.raises(C.VerifyError):
        C.verify_pixels(ok, png_bytes(Image.fromarray(b, "RGBA")))
    with pytest.raises(C.VerifyError):
        C.verify_pixels(ok, png_bytes(Image.fromarray(b[..., :3].copy(), "RGB")))


def test_verify_16bit_vs_8bit_equivalent():
    v = (np.arange(64 * 64).reshape(64, 64) % 256).astype("uint16")
    sixteen = png_bytes(Image.fromarray(v * 257))      # 16-bit，但每個值都能無損降成 8-bit
    eight = png_bytes(Image.fromarray(v.astype("uint8")))
    C.verify_pixels(sixteen, eight)
    w = (v * 257).copy()
    w[0, 0] += 1
    with pytest.raises(C.VerifyError):
        C.verify_pixels(png_bytes(Image.fromarray(w)), eight)
