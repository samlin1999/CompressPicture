"""圖片壓縮工具 — 程式進入點。

保持這個檔案精簡：子行程（spawn）會重新匯入它，這裡不能在模組層級載入 Qt。

  CompressPicture.exe [檔案或資料夾…]     開啟並加入這些檔案
  CompressPicture.exe --selftest 報告.json  自我檢查（不開視窗）：確認各壓縮引擎與子行程都正常
"""
import multiprocessing
import sys


def selftest(report_path: str) -> int:
    import json
    import os
    import shutil
    import tempfile

    d = tempfile.mkdtemp(prefix="CompressPicture_selftest_")
    try:
        from PIL import Image

        import compressor

        grad = Image.merge("RGB", [Image.linear_gradient("L").resize((400, 300)),
                                   Image.radial_gradient("L").resize((400, 300)),
                                   Image.effect_noise((400, 300), 40)])
        samples = {
            "a.png": lambda p: grad.save(p, compress_level=1),
            "b.jpg": lambda p: grad.save(p, quality=95),
            "c.webp": lambda p: grad.convert("RGBA").save(p, lossless=True, method=0),
        }
        jobs = []
        for i, (name, make) in enumerate(samples.items()):
            p = os.path.join(d, name)
            make(p)
            for mode in ("lossless", "lossy"):
                jobs.append({"id": len(jobs), "read_path": p, "source_path": p, "overwrite": False,
                             "target": os.path.join(d, f"out_{mode}_{name}"), "backup": None,
                             "backup_dir": "", "options": {**compressor.DEFAULT_OPTIONS, "mode": mode}})
        # 走和介面相同的路徑：JobRunner（子行程 + 寫檔）+ Batch（排程）
        import time

        import runner as rn
        jr = rn.JobRunner(2)
        batch = rn.Batch(jr, jobs, rn.memory_budget(2))
        results = {}
        deadline = time.monotonic() + 300
        batch.fill()
        while not batch.finished and time.monotonic() < deadline:
            for res in jr.drain():
                if batch.on_result(res) == "final":
                    results[res["id"]] = res
            batch.fill()
            time.sleep(0.02)
        batch.close()
        jr.kill()
        report = []
        for job in jobs:
            res = results.get(job["id"], {"status": "timeout", "orig_size": 0, "new_size": 0, "message": ""})
            ok = res["status"] in ("compressed", "kept") and os.path.exists(job["target"])
            report.append({"file": os.path.basename(job["target"]), "status": res["status"],
                           "orig": res["orig_size"], "new": res["new_size"], "message": res["message"],
                           "ok": ok})
        all_ok = all(r["ok"] for r in report)

        # --- 介面：在背景建立主視窗、加入檔案、截圖，確認 Qt 元件與中文翻譯都在
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
        os.environ["CPIC_SETTINGS_INI"] = os.path.join(d, "settings.ini")
        from PySide6.QtWidgets import QApplication, QMessageBox

        import gui
        app = QApplication(["selftest"])
        tr_ok = gui.install_translations(app)
        w = gui.MainWindow()
        w.show()
        app.processEvents()
        w.add_paths([os.path.join(d, n) for n in samples])
        app.processEvents()
        w.grab().save(os.path.splitext(report_path)[0] + ".png")
        box = QMessageBox(QMessageBox.Question, "t", "t", QMessageBox.Yes | QMessageBox.No)
        gui_info = {"items": len(w.model.items), "translation": tr_ok,
                    "yes_button": box.button(QMessageBox.Yes).text(), "style": app.style().name()}
        w.close()
        # 先刪視窗再刪 QApplication，避免結束時當掉
        import shiboken6
        shiboken6.delete(box)
        shiboken6.delete(w)
        app.shutdown()
        gui_ok = gui_info["items"] == len(samples) and tr_ok and gui_info["yes_button"] != "&Yes"
        report.append({"gui": gui_info, "ok": gui_ok})
        all_ok = all_ok and gui_ok
    except Exception as e:  # noqa: BLE001
        import traceback
        report, all_ok = [{"error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()}], False
    finally:
        shutil.rmtree(d, ignore_errors=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({"ok": all_ok, "results": report}, f, ensure_ascii=False, indent=2)
    return 0 if all_ok else 1


def main() -> int:
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        return selftest(sys.argv[2])
    from gui import run
    return run()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
