"""背景執行與排程的競態測試：子行程當掉、中途停止、記憶體預算、嫌疑隔離。"""
import os
import sys
import time

import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import compressor as C  # noqa: E402
import crashy  # noqa: E402
import runner as R  # noqa: E402


def make_png(path, seed=0, size=(160, 120)):
    rng = np.random.default_rng(seed)
    a = rng.integers(0, 40, (size[1], size[0], 3), dtype=np.uint8) + 100
    Image.fromarray(a, "RGB").save(path, compress_level=0)
    return path


def job(tmp_path, i, **extra):
    src = make_png(str(tmp_path / f"in_{i}.png"), seed=i)
    j = {"id": i, "read_path": src, "source_path": src, "target": str(tmp_path / f"out_{i}.png"),
         "overwrite": False, "backup": None, "backup_dir": str(tmp_path / "bk"),
         "options": {**C.DEFAULT_OPTIONS, "mode": "lossless"}}
    j.update(extra)
    return j


def run_batch(batch, jr, timeout=120):
    """模擬介面的主迴圈：fill → drain → on_result，直到結束。"""
    final = {}
    started_alone = []
    s, errs = batch.fill()
    for e in errs:
        final[e["id"]] = e
    deadline = time.monotonic() + timeout
    while not batch.finished:
        assert time.monotonic() < deadline, "批次卡住了（沒有在時限內結束）"
        for res in jr.drain():
            if batch.on_result(res) == "final":
                final[res["id"]] = res
        jr.watchdog()
        s, errs = batch.fill()
        for e in errs:
            final[e["id"]] = e
        if len(s) == 1 and s[0] in batch.suspects:
            started_alone.append((s[0], len(batch.in_flight)))
        time.sleep(0.02)
    batch.close()
    return final, started_alone


# ---------------------------------------------------------------- 真的子行程

def test_worker_crash_does_not_hang_and_only_culprit_fails(tmp_path):
    jr = R.JobRunner(3, func=crashy.job_func)
    jobs = [job(tmp_path, i) for i in range(6)]
    jobs[2]["crash"] = True
    jobs[3]["sleep"] = 0.5  # 讓它和當掉的工作同時在跑，會一起收到 BrokenProcessPool
    try:
        final, alone = run_batch(R.Batch(jr, jobs, budget=10**12), jr)
    finally:
        jr.kill()
    assert set(final) == {j["id"] for j in jobs}
    assert final[2]["status"] == "error" and final[2]["message"] == R.CRASH_MESSAGE
    assert not os.path.exists(jobs[2]["target"])           # 當掉的沒有寫任何東西
    for j in jobs:
        if j["id"] != 2:
            assert final[j["id"]]["status"] == "compressed", final[j["id"]]  # 無辜的不會被誤判
            assert os.path.exists(j["target"])
    assert all(n == 1 for _, n in alone)                      # 嫌疑檔都是單獨跑的


def wait_results(jr, n=1, timeout=60):
    got = []
    deadline = time.monotonic() + timeout
    while len(got) < n and time.monotonic() < deadline:
        got += jr.drain()
        jr.watchdog()
        time.sleep(0.02)
    return got


def test_kill_is_immediate_and_writes_nothing(tmp_path):
    jr = R.JobRunner(2, func=crashy.job_func)
    fast = job(tmp_path, 0)
    slow = [job(tmp_path, i, sleep=60) for i in (1, 2)]
    jr.submit(fast)
    done = wait_results(jr)
    assert done and done[0]["status"] == "compressed"
    for s in slow:
        jr.submit(s)
    time.sleep(1.5)
    t0 = time.monotonic()
    written = jr.kill()
    assert time.monotonic() - t0 < 10
    assert written == []                       # 被中止的工作沒有寫任何東西
    assert jr.drain() == []                    # 被中止的工作也不會以任何形式回報
    assert not any(os.path.exists(s["target"]) for s in slow)
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    # 中止後還能再用（會建立新的子行程池）
    jr.submit(job(tmp_path, 9))
    got = wait_results(jr)
    jr.kill()
    assert got[0]["status"] == "compressed" and not jr.stuck


def test_kill_returns_results_already_written(tmp_path):
    jr = R.JobRunner(2, func=crashy.job_func)
    a = job(tmp_path, 0)
    jr.submit(a)
    deadline = time.monotonic() + 60
    while not os.path.exists(a["target"]) and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)                            # 已寫入，但還沒被 drain()
    written = jr.kill()
    assert [r["id"] for r in written] == [0] and written[0]["status"] == "compressed"


def test_stale_result_never_leaks_into_next_run(tmp_path):
    """強制停止後馬上重新開始同一個檔案：上一輪的結果不能被當成這一輪的。"""
    jr = R.JobRunner(2, func=crashy.job_func)
    j = job(tmp_path, 7, sleep=2)
    jr.submit(j)
    time.sleep(0.8)
    assert jr.kill() == []
    j2 = dict(j, sleep=0)
    jr.submit(j2)
    got = wait_results(jr, n=1)
    time.sleep(1.0)
    got += jr.drain()
    jr.kill()
    assert [r["status"] for r in got] == ["compressed"], got   # 只有新的一筆，沒有舊的 CRASHED


def test_kill_while_worker_is_sending_result_never_hangs():
    """實測會卡住的情況：子行程把大結果傳到一半時被結束。kill() 必須在時限內返回，並救回管理執行緒。"""
    jr = R.JobRunner(1, func=crashy.big_result)
    jr.submit({"id": 0})
    t = time.monotonic()
    first = wait_results(jr, timeout=120)
    roundtrip = time.monotonic() - t
    assert first
    for i in range(8):
        jr.submit({"id": i + 1})
        time.sleep(roundtrip * (0.2 + 0.08 * i))
        t0 = time.monotonic()
        jr.kill()
        assert time.monotonic() - t0 < 20, "kill() 卡住了"
    assert not jr.stuck
    jr.submit({"id": 99})
    assert wait_results(jr, timeout=120)       # 之後還能正常使用
    jr.kill()
    print(f"rescued {jr.rescued} stuck manager thread(s)")


def test_watchdog_rescues_worker_that_died_while_sending():
    """子行程在傳送結果途中自己死掉（不是我們砍的）：整批不能永遠卡住。"""
    jr = R.JobRunner(1, func=crashy.die_while_sending)
    for i, delay in enumerate((0.05, 0.12, 0.2, 0.3)):
        jr.submit({"id": i, "die_after": delay})
        got = wait_results(jr, timeout=30)
        assert got, f"die_after={delay}: 30 秒內沒有任何結果（卡住了）"
        assert got[0]["id"] == i
    jr.kill()
    assert not jr.stuck
    print(f"watchdog rescued {jr.rescued} time(s)")


# ---------------------------------------------------------------- 排程邏輯（假的 runner）

class FakeRunner:
    def __init__(self, workers):
        self.workers = workers
        self.submitted = []

    def submit(self, job):
        self.submitted.append(job["id"])


def fake_jobs(n):
    return [{"id": i, "read_path": f"f{i}"} for i in range(n)]


def test_memory_budget_limits_concurrency_but_always_runs_one():
    est = {0: 60, 1: 60, 2: 30, 3: 500}
    fr = FakeRunner(workers=8)
    b = R.Batch(fr, fake_jobs(4), budget=100, estimate=lambda p: est[int(p[1:])])
    started, _ = b.fill()
    assert started == [0]                      # 60 + 60 > 100，第二個要等
    b.on_result({"id": 0, "status": "compressed"})
    assert b.fill()[0] == [1, 2]               # 60 + 30 = 90 ≤ 100；再加 3（500）就超過
    assert fr.submitted == [0, 1, 2]
    b.on_result({"id": 1, "status": "compressed"})
    b.on_result({"id": 2, "status": "compressed"})
    assert b.fill()[0] == [3]                  # 超過預算的大圖，在沒有其他工作時單獨跑
    b.on_result({"id": 3, "status": "compressed"})
    assert b.finished and b.done == 4


def test_worker_limit():
    fr = FakeRunner(workers=2)
    b = R.Batch(fr, fake_jobs(5), budget=10**12, estimate=lambda p: 1)
    assert b.fill()[0] == [0, 1]
    b.on_result({"id": 0, "status": "kept"})
    assert b.fill()[0] == [2]


def test_crash_isolation_logic():
    fr = FakeRunner(workers=3)
    b = R.Batch(fr, fake_jobs(4), budget=10**12, estimate=lambda p: 1)
    assert b.fill()[0] == [0, 1, 2]
    for i in (0, 1, 2):
        assert b.on_result({"id": i, "status": R.CRASHED}) == "retry"
    assert b.suspects == {0, 1, 2}
    assert b.fill()[0] == [2]                  # 嫌疑檔一次只跑一個
    assert b.fill()[0] == []                   # 嫌疑檔在跑時不送其他工作
    assert b.on_result({"id": 2, "status": "compressed"}) == "final"
    assert b.fill()[0] == [1]
    assert b.on_result({"id": 1, "status": R.CRASHED}) == "final"   # 單獨跑還當 → 就是它
    assert b.fill()[0] == [0]
    b.on_result({"id": 0, "status": "compressed"})
    assert b.fill()[0] == [3]                  # 嫌疑都清掉後恢復平行
    b.on_result({"id": 3, "status": "compressed"})
    assert b.finished and b.done == 4


def test_unknown_result_is_ignored():
    fr = FakeRunner(workers=2)
    b = R.Batch(fr, fake_jobs(2), budget=10**12, estimate=lambda p: 1)
    b.fill()
    assert b.on_result({"id": 99, "status": "compressed"}) == "ignore"
    assert b.done == 0


def test_submit_failure_does_not_hang():
    class Broken(FakeRunner):
        def submit(self, job):
            raise RuntimeError("boom")
    b = R.Batch(Broken(2), fake_jobs(3), budget=10**12, estimate=lambda p: 1)
    started, errors = b.fill()
    assert started == [] and len(errors) == 3 and b.finished and b.done == 3


def test_estimate_memory(tmp_path):
    p = tmp_path / "a.png"
    Image.new("RGBA", (1000, 500)).save(p)
    assert C.estimate_memory(str(p)) >= 1000 * 500 * 40
    for kw in ({"quality": 80}, {"lossless": True}):
        w = tmp_path / "a.webp"
        Image.new("RGB", (321, 123), "red").save(w, **kw)
        assert C.webp_dimensions(open(w, "rb").read(64)) == (321, 123)
    w = tmp_path / "b.webp"
    Image.new("RGBA", (77, 55), (1, 2, 3, 100)).save(w, quality=80)   # VP8X
    assert C.webp_dimensions(open(w, "rb").read(64)) == (77, 55)
    assert C.estimate_memory(str(tmp_path / "missing.png")) > 0
