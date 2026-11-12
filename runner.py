"""背景執行：子行程池、寫檔、結果佇列，以及一次批次的排程。不依賴 Qt。

執行緒模型
  主執行緒：submit()、drain()、kill()、watchdog()、Batch 的所有方法。
  executor 的管理執行緒：_done() —— 壓縮成功就交給寫檔執行緒，失敗則直接把結果放進佇列。
  寫檔執行緒（數條）：_commit() —— fileops.commit_result 寫檔後把結果放進佇列，再呼叫 notify。
  子行程：只做壓縮運算（compressor.process_job），不碰任何檔案的寫入。

  停止的兩種方式（介面）：
    一般停止：不再送出新工作，等進行中的做完（不砍子行程，不可能卡住）。
    強制停止：kill() —— 世代加一（之後舊工作的回呼一律丟棄）→ 結束子行程 → 等寫檔執行緒做完。
             kill() 返回後不會再有任何寫檔，回傳中止前已寫入的結果。
  同一個工作同時只會有一份在執行，不同工作的輸出／備份路徑一定不同，平行寫檔不會互相干擾。

為什麼不用 multiprocessing.Pool：子行程被系統砍掉（記憶體不足）或原生函式庫當掉時，
Pool 永遠不會回呼那個工作，整批會卡死。ProcessPoolExecutor 會回報 BrokenProcessPool。
"""
from __future__ import annotations

import ctypes
import multiprocessing
import queue
import sys
import threading
import time
from collections import deque
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from functools import partial

import compressor
import fileops

CRASHED = "crashed"
CRASH_MESSAGE = ("處理這張圖時子行程意外終止（多半是圖片太大、記憶體不足，或圖片內容異常）。"
                 "檔案沒有被修改。")
WORKER_BASE_MEMORY = 120 * 2**20  # 每個子行程載入函式庫後的基本用量（實測約 80–100 MB）


def available_memory() -> int:
    """目前可用的實體記憶體（位元組）。"""
    if sys.platform == "win32":
        class MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMORYSTATUSEX()
        st.dwLength = ctypes.sizeof(st)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return int(st.ullAvailPhys)
    return 4 * 2**30


def memory_budget(workers: int) -> int:
    """同時處理的圖片，估計記憶體總和的上限。"""
    return max(512 * 2**20, int(available_memory() * 0.75) - workers * WORKER_BASE_MEMORY)


FINAL = "final"      # 子行程做完並經過寫檔（commit）的結果：磁碟可能已被改變，一定要套用
ABORTED = "aborted"  # 子行程當掉／被取消：什麼都沒寫


class JobRunner:
    def __init__(self, workers: int, notify=None, func=compressor.process_job, writers: int = 4):
        self.workers = workers
        self._notify = notify or (lambda: None)
        self._func = func  # 測試時可以換成會當掉的函式
        self._ex: ProcessPoolExecutor | None = None
        # 寫檔用幾條執行緒平行做（實測每檔約 3.5 ms，單執行緒會成為大批小檔的瓶頸）。
        # 不同工作的輸出檔、備份檔一定不同（規劃時已保證），所以平行寫入是安全的。
        self._writer_count = writers
        self._writer: ThreadPoolExecutor | None = None
        self._results: queue.SimpleQueue = queue.SimpleQueue()  # (世代, 種類, 結果)
        # 世代：kill() 時加一。舊世代的回呼不會再寫檔，舊世代的 ABORTED 結果也不會被 drain() 交出去，
        # 避免「上一輪被中止的工作」的結果混進下一輪（同一個檔案可能正在下一輪處理）。
        self._lock = threading.Lock()
        self._gen = 0
        self._dead_since: float | None = None
        self.stuck = False  # 有內部執行緒救不回來（極少見）：程式結束時要強制離開，否則會卡住
        self.rescued = 0    # 被救回來的卡住次數（診斷／測試用）

    def _writers(self) -> ThreadPoolExecutor:
        if self._writer is None:
            self._writer = ThreadPoolExecutor(self._writer_count)
        return self._writer

    def _executor(self) -> ProcessPoolExecutor:
        if self._ex is None:
            self._ex = ProcessPoolExecutor(self.workers, mp_context=multiprocessing.get_context("spawn"))
        return self._ex

    def submit(self, job: dict) -> None:
        self._writers()  # 先建好寫檔執行緒池（之後由管理執行緒使用）
        try:
            fut = self._executor().submit(self._func, job)
        except (BrokenProcessPool, RuntimeError):
            # 上一個池因為子行程當掉而失效 → 換一個新的再送一次
            ex, self._ex = self._ex, None
            self._abandon(ex)
            fut = self._executor().submit(self._func, job)
        fut.add_done_callback(partial(self._done, self._gen, job))

    def _done(self, gen: int, job: dict, fut) -> None:
        """在 executor 管理執行緒執行（被取消時則在呼叫 cancel 的執行緒）。"""
        with self._lock:
            if gen != self._gen:
                return  # 已被 kill() 放棄的工作：不寫檔、不回報
            try:
                if fut.cancelled():
                    res = {"id": job["id"], "status": CRASHED, "message": ""}
                else:
                    exc = fut.exception()
                    if exc is None:
                        # 在鎖內送出：kill() 拿到鎖之後，就不會再有這一世代的新寫檔
                        self._writer.submit(self._commit, gen, job, fut.result())
                        return
                    if isinstance(exc, BrokenProcessPool):
                        res = {"id": job["id"], "status": CRASHED, "message": ""}
                    else:
                        res = {"id": job["id"], "status": "error", "message": compressor.friendly_error(exc)}
            except BaseException as e:  # noqa: BLE001 - 結果一定要送回去，否則這個工作會永遠「進行中」
                res = {"id": job["id"], "status": "error", "message": f"內部錯誤：{e}"}
        self._put(gen, ABORTED if res["status"] == CRASHED else FINAL, res)

    def _commit(self, gen: int, job: dict, res: dict) -> None:
        """在寫檔執行緒執行。"""
        try:
            res = fileops.commit_result(job, res)
        except BaseException as e:  # noqa: BLE001
            res = {"id": job["id"], "status": "error", "message": f"寫入失敗：{e}"}
        self._put(gen, FINAL, res)

    def _put(self, gen: int, kind: str, res: dict) -> None:
        self._results.put((gen, kind, res))
        self._notify()

    def drain(self) -> list[dict]:
        """取出目前世代的所有結果（主執行緒）。"""
        out = []
        while True:
            try:
                gen, _kind, res = self._results.get_nowait()
            except queue.Empty:
                return out
            if gen == self._gen:
                out.append(res)

    # ------------------------------------------------------------ 中止與救援

    def _abandon(self, ex: ProcessPoolExecutor | None) -> None:
        """丟棄一個 executor，保證不會卡住。

        直接結束子行程時，若它正在把結果傳回來，管理執行緒會卡在讀取管線、永遠等不到剩下的資料
        （實測重現）。此時關掉父行程手上的寫入端（子行程都已結束，就沒有任何寫入端了），
        讀取會收到「管線已關閉」而結束，管理執行緒就能正常收尾。
        """
        if ex is None:
            return
        procs = list((getattr(ex, "_processes", None) or {}).values())
        mgr = getattr(ex, "_executor_manager_thread", None)
        result_queue = getattr(ex, "_result_queue", None)
        for p in procs:
            try:
                p.terminate()
            except Exception:  # noqa: BLE001
                pass
        for p in procs:
            p.join(5)
        ex.shutdown(wait=False, cancel_futures=True)
        if mgr is not None:
            mgr.join(2)
            if mgr.is_alive() and result_queue is not None:
                try:
                    result_queue._writer.close()
                except Exception:  # noqa: BLE001
                    pass
                mgr.join(5)
                if not mgr.is_alive():
                    self.rescued += 1
            if mgr.is_alive():
                self.stuck = True

    def kill(self) -> list[dict]:
        """強制中止所有工作。返回時：子行程都已結束、不會再有任何寫檔。

        回傳中止前已經寫入磁碟、但還沒被 drain() 取走的結果（呼叫端一定要套用）。
        """
        with self._lock:
            old = self._gen
            self._gen += 1
            ex, self._ex = self._ex, None
            writer, self._writer = self._writer, None
        self._abandon(ex)
        if writer is not None:
            writer.shutdown(wait=True)  # 寫到一半的檔案不能被中斷，等它們做完
        self._dead_since = None
        written = []
        while True:
            try:
                gen, kind, res = self._results.get_nowait()
            except queue.Empty:
                break
            if gen == old and kind == FINAL:
                written.append(res)
        return written

    def watchdog(self) -> bool:
        """定期呼叫（約每秒）。子行程死在回傳結果途中時，管理執行緒不會發現，整批會永遠卡住；
        偵測到「有子行程已經結束好幾秒、池卻沒有被標記為失效」就主動救援。
        救援後進行中的工作會收到 CRASHED，由 Batch 單獨重試。回傳是否做了救援。"""
        ex = self._ex
        if ex is None:
            return False
        procs = list((getattr(ex, "_processes", None) or {}).values())
        if not any(p.exitcode is not None for p in procs) or getattr(ex, "_broken", False):
            self._dead_since = None
            return False
        now = time.monotonic()
        if self._dead_since is None:
            self._dead_since = now
            return False
        if now - self._dead_since < 3.0:
            return False
        self._dead_since = None
        self._ex = None
        self._abandon(ex)  # 世代不變：進行中的工作會以 CRASHED 回報
        return True


class Batch:
    """一次「開始壓縮」的排程。只在主執行緒使用。

    - 同時最多 runner.workers 個工作；
    - 同時處理的圖片估計記憶體總和不超過 budget（但至少會跑一個，再大的圖也能單獨處理）；
    - 子行程當掉時，當下所有進行中的工作都會收到 CRASHED，分不出是哪一張造成的 →
      全部標為「嫌疑」放回佇列最前面，之後嫌疑檔一次只跑一個。
      嫌疑檔單獨跑還是當掉，才判定是它的問題；其他檔案不會被誤判為失敗。
    """

    def __init__(self, runner: JobRunner, jobs: list[dict], budget: int,
                 estimate=compressor.estimate_memory, prefetch_threads: int = 8):
        self.runner = runner
        self.jobs = {j["id"]: j for j in jobs}
        self.queue: deque[int] = deque(j["id"] for j in jobs)
        self.in_flight: dict[int, int] = {}  # id → 估計記憶體
        self.suspects: set[int] = set()
        self.budget = budget
        self._estimate = estimate
        self._est: dict[int, int] = {}
        self.total = len(jobs)
        self.done = 0
        self.stopping = False
        # 估算要讀檔頭。第一次開啟檔案可能很慢（實測約 13 ms，多半是防毒即時掃描），
        # 在主執行緒逐一做會變成整批的瓶頸 → 用幾條背景執行緒依佇列順序先平行讀好。
        self._prefetch = ThreadPoolExecutor(prefetch_threads) if prefetch_threads else None
        self._est_futures = {}
        if self._prefetch:
            for j in jobs:
                self._est_futures[j["id"]] = self._prefetch.submit(estimate, j["read_path"])

    @property
    def finished(self) -> bool:
        return not self.queue and not self.in_flight

    def stop_dispatch(self) -> list[int]:
        """不再送出新工作（一般停止）。回傳被撤回、尚未開始的工作 id。"""
        ids = list(self.queue)
        self.queue.clear()
        self.stopping = True
        return ids

    def close(self) -> None:
        """批次結束或中止時呼叫：取消還沒做的檔頭預讀。"""
        if self._prefetch:
            self._prefetch.shutdown(wait=False, cancel_futures=True)
            self._prefetch = None

    def _estimate_of(self, iid: int) -> int:
        if iid not in self._est:
            fut = self._est_futures.pop(iid, None)
            try:
                self._est[iid] = fut.result() if fut is not None else self._estimate(self.jobs[iid]["read_path"])
            except Exception:  # noqa: BLE001 - 包含預讀被取消；估不出來就用保守值
                self._est[iid] = 256 * 2**20
        return self._est[iid]

    def fill(self):
        """送出可以開始的工作。回傳 (開始的 id, 無法送出的錯誤結果)。"""
        started, errors = [], []
        while self.queue:
            if any(i in self.suspects for i in self.in_flight):
                break  # 嫌疑檔正在單獨執行
            iid = self.queue[0]
            if iid in self.suspects:
                if self.in_flight:
                    break  # 等其他工作做完，讓它單獨跑
            else:
                if len(self.in_flight) >= self.runner.workers:
                    break
                if self.in_flight and sum(self.in_flight.values()) + self._estimate_of(iid) > self.budget:
                    break  # 記憶體預算已滿，等有工作完成
            self.queue.popleft()
            try:
                self.runner.submit(self.jobs[iid])
            except Exception as e:  # noqa: BLE001
                self.done += 1
                errors.append({"id": iid, "status": "error", "message": f"無法啟動壓縮：{e}"})
                continue
            self.in_flight[iid] = self._estimate_of(iid)
            started.append(iid)
        return started, errors

    def on_result(self, res: dict) -> str:
        """回傳 final（結果確定）、retry（當機，已放回佇列）、dropped（停止中當機，不重試）
        或 ignore（不屬於進行中的工作）。"""
        iid = res["id"]
        if iid not in self.in_flight:
            return "ignore"
        del self.in_flight[iid]
        if res.get("status") == CRASHED:
            if iid in self.suspects:
                res["status"] = "error"
                res["message"] = CRASH_MESSAGE
                self.done += 1
                return "final"
            if self.stopping:
                return "dropped"  # 正在停止：不重試，維持「等待中」
            self.suspects.add(iid)
            self.queue.appendleft(iid)
            return "retry"
        self.done += 1
        return "final"
