"""測試用的工作函式（必須是可匯入的模組，子行程用 spawn 啟動時才找得到）。"""
import os
import time

import compressor


def job_func(job):
    if job.get("crash"):
        os._exit(3)  # 模擬原生函式庫當掉、或被系統因記憶體不足砍掉
    if job.get("sleep"):
        time.sleep(job["sleep"])
    return compressor.process_job(job)


def crash_by_name(job):
    """檔名含 CRASHME 的工作會讓子行程當掉（GUI 流程測試用）。"""
    if "CRASHME" in os.path.basename(job["read_path"]):
        os._exit(3)
    return compressor.process_job(job)


BIG = 300 * 2**20


def big_result(job):
    """回傳很大的結果（傳回父行程要一段時間），用來測試「傳送途中被中止」。"""
    return {"id": job["id"], "status": "error", "message": "big", "data": b"x" * BIG}


def die_while_sending(job):
    """子行程在傳送結果的途中自己死掉（模擬傳送時記憶體不足被系統砍掉）。"""
    import threading
    threading.Timer(job["die_after"], os._exit, (3,)).start()
    return {"id": job["id"], "status": "error", "message": "big", "data": b"x" * BIG}
