"""檔案相關操作：收集檔案、規劃輸出路徑、安全寫入、備份與還原。

所有寫檔都是「先寫暫存檔再 os.replace」，中途失敗或被中止都不會留下半個檔案。

「同一個檔案」一律用檔案身分（磁碟區 + 檔案編號）或解析過的實際路徑判斷，
不能只比字串：同一個檔案可以有短檔名（KAIHSU~1）、長檔名、junction 等多種寫法。
"""
from __future__ import annotations

import os
import shutil
import time
import uuid
from dataclasses import dataclass, field

from compressor import SUPPORTED_EXTS

OUT_OVERWRITE = "overwrite"
OUT_SUFFIX = "suffix"
OUT_FOLDER = "folder"

CHANGED_MESSAGE = ("處理期間原始檔案被其他程式修改了。為了不蓋掉這些修改，已略過這個檔案；"
                   "請重新壓縮。")


class SourceChanged(Exception):
    """原始檔案在讀取之後被修改過。"""


# ---------------------------------------------------------------------------
# 路徑與檔案身分
# ---------------------------------------------------------------------------

def norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


class PathCanon:
    """把路徑轉成可比較的形式：資料夾部分用 realpath 解析（短檔名、junction），並快取。"""

    def __init__(self):
        self._dirs: dict[str, str] = {}

    def __call__(self, path: str) -> str:
        path = os.path.abspath(path)
        d, name = os.path.split(path)
        if "~" in name:  # 8.3 短檔名本身也要展開
            return os.path.normcase(os.path.realpath(path))
        real = self._dirs.get(d)
        if real is None:
            real = self._dirs[d] = os.path.realpath(d)
        return os.path.normcase(os.path.join(real, name))


def file_sig(path: str):
    """(大小, 修改時間 ns)：用來確認檔案在我們讀取之後沒被修改。"""
    st = os.stat(path)
    return (st.st_size, st.st_mtime_ns)


def identity(path: str):
    """檔案身分 (磁碟區, 檔案編號)；檔案不存在或系統不提供時回傳 None。"""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino) if st.st_ino else None


def same_file(a: str, b: str) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return norm(a) == norm(b)


def is_supported(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in SUPPORTED_EXTS


# ---------------------------------------------------------------------------
# 收集檔案
# ---------------------------------------------------------------------------

@dataclass
class CollectResult:
    files: list = field(default_factory=list)   # [(abs_path, rel_path)]
    unsupported: int = 0


def collect(paths) -> CollectResult:
    """展開檔案與資料夾（含子資料夾）。

    rel_path 用於「輸出到資料夾」：拖入資料夾時保留「資料夾名稱/子資料夾/檔名」結構，
    單獨拖入的檔案則只用檔名。
    """
    res = CollectResult()
    for p in paths:
        p = os.path.abspath(p)
        if os.path.isdir(p):
            base = os.path.dirname(p.rstrip("\\/")) or p
            for root, dirs, names in os.walk(p):
                dirs.sort(key=str.lower)
                for name in sorted(names, key=str.lower):
                    full = os.path.join(root, name)
                    if is_supported(full):
                        res.files.append((full, os.path.relpath(full, base)))
                    else:
                        res.unsupported += 1
        elif os.path.isfile(p):
            if is_supported(p):
                res.files.append((p, os.path.basename(p)))
            else:
                res.unsupported += 1
    return res


# ---------------------------------------------------------------------------
# 規劃輸出路徑
# ---------------------------------------------------------------------------

@dataclass
class PlanItem:
    id: int
    path: str
    rel: str


def plan_targets(items, out_mode: str, suffix: str = "", out_dir: str = "", protected=None):
    """回傳 (targets: {id: path}, problems: {id: 說明})，或丟出 ValueError（設定本身有誤）。

    protected: {檔案身分: item id}，清單中「所有」原始檔（不只這次要處理的）。
    輸出檔不可以蓋到其中任何一個。
    """
    if out_mode == OUT_SUFFIX:
        if not suffix or not suffix.strip():
            raise ValueError("檔名後綴不可空白，否則會覆蓋原始檔案。")
        if any(c in suffix for c in '\\/:*?"<>|'):
            raise ValueError('檔名後綴不能包含 \\ / : * ? " < > | 這些字元。')
    if out_mode == OUT_FOLDER:
        if not out_dir or not out_dir.strip():
            raise ValueError("請先選擇輸出資料夾。")
        out_dir = os.path.abspath(out_dir)

    if protected is None:
        protected = {}
        for it in items:
            ident = identity(it.path)
            if ident:
                protected[ident] = it.id

    canon = PathCanon()
    # 也用實際路徑比對（檔案不存在、或檔案系統不提供檔案編號時，身分判斷不出來）
    source_paths = {canon(it.path): it.id for it in items}
    targets, problems, used = {}, {}, set()
    for it in items:
        if out_mode == OUT_OVERWRITE:
            targets[it.id] = it.path
            continue
        if out_mode == OUT_SUFFIX:
            d, name = os.path.split(it.path)
            stem, ext = os.path.splitext(name)
            t = os.path.join(d, stem + suffix + ext)
        else:
            t = os.path.join(out_dir, it.rel)

        # 兩個來源對到同一個輸出檔名 → 自動加上 (2)、(3)…
        base, ext = os.path.splitext(t)
        n = 2
        while canon(t) in used:
            t = f"{base} ({n}){ext}"
            n += 1

        key = canon(t)
        tid = identity(t)
        owner = protected.get(tid) if tid is not None else None
        if owner is None:
            owner = source_paths.get(key)
        if owner == it.id or (owner is None and tid is not None and same_file(t, it.path)):
            problems[it.id] = "輸出位置與原檔是同一個檔案（會覆蓋原檔），已略過。若要覆蓋請改選「覆蓋原始檔案」。"
            continue
        if owner is not None:
            problems[it.id] = f"輸出檔會蓋掉清單中的另一個原始檔：{os.path.basename(t)}，已略過。"
            continue
        used.add(key)
        targets[it.id] = t
    return targets, problems


# ---------------------------------------------------------------------------
# 寫檔
# ---------------------------------------------------------------------------

def _replace(tmp: str, dst: str, attempts: int = 8) -> None:
    """os.replace，遇到 Windows 暫時性鎖檔（防毒掃描、預覽程式）時稍等重試。"""
    delay = 0.05
    for i in range(attempts):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.5)


def atomic_write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        _replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def atomic_copy(src: str, dst: str) -> None:
    if os.path.exists(dst) and same_file(src, dst):
        return
    with open(src, "rb") as f:
        atomic_write(dst, f.read())


def make_backup(path: str, backup_dir: str, item_id: int) -> str:
    os.makedirs(backup_dir, exist_ok=True)
    dst = os.path.join(backup_dir, f"{item_id:06d}_{os.path.basename(path)}")
    shutil.copy2(path, dst)  # copy2 保留修改時間，之後可用 file_sig 確認備份的是哪個版本
    return dst


def commit_result(job: dict, res: dict) -> dict:
    """依壓縮結果寫入輸出檔。在背景執行緒呼叫；回傳更新後的 res（不含 data）。

    job: {id, source_path, read_path, target, overwrite, backup, backup_dir, disk_sig}
      read_path 是原檔本身，或（重新壓縮時）之前的備份。
      disk_sig 是規劃時原檔的簽章，從備份讀取時用來確認原檔之後沒被別人改過。

    保證：
      - 原檔在處理期間被其他程式修改 → 什麼都不寫，回報錯誤（不會蓋掉別人的修改）。
      - 覆蓋前一定先備份，且確認備份內容就是被壓縮的那個版本。
      - 最終輸出檔 = 壓縮結果，或 = 原檔（沒變小／略過時）。
    """
    data = res.pop("data", None)
    res["target"] = job["target"]
    res["backup"] = job.get("backup")
    res["written_sig"] = None
    if res["status"] == "error":
        return res

    src = job["source_path"]
    from_source = job["read_path"] == src
    expect = res.get("read_sig") if from_source else job.get("disk_sig")
    expect = tuple(expect) if expect else None
    new_backup = None
    try:
        if expect is not None and file_sig(src) != expect:
            raise SourceChanged
        if data is not None:
            if job["overwrite"] and not res["backup"]:
                new_backup = make_backup(src, job["backup_dir"], job["id"])
                if expect is not None and file_sig(new_backup) != expect:
                    raise SourceChanged  # 備份的瞬間檔案被改了
                res["backup"] = new_backup
            atomic_write(job["target"], data)
        elif job["overwrite"]:
            # 保留原檔：如果之前寫過壓縮結果，就把原檔放回去
            if res["backup"]:
                atomic_copy(res["backup"], job["target"])
        else:
            # 另存模式：把原檔複製到輸出位置，讓輸出資料夾保持完整
            atomic_copy(job["read_path"], job["target"])
        if job["overwrite"]:
            res["written_sig"] = file_sig(job["target"])
    except SourceChanged:
        res.update(status="error", message=CHANGED_MESSAGE)
    except PermissionError:
        res.update(status="error", message="沒有權限寫入輸出檔（檔案可能是唯讀或正被其他程式開啟）")
    except OSError as e:
        res.update(status="error", message=f"寫入失敗：{e}")
    if res["status"] == "error" and new_backup:
        # 原檔沒有被覆蓋，這次建立的備份用不到
        res["backup"] = job.get("backup")
        try:
            os.remove(new_backup)
        except OSError:
            pass
    return res


def restore_backup(backup: str, path: str) -> None:
    atomic_copy(backup, path)


def human_size(n) -> str:
    if n is None:
        return ""
    n = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.1f} {unit}" if abs(n) < 100 else f"{n:.0f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
