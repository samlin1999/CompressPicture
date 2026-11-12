"""圖片壓縮工具 — 主視窗。"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass

from PySide6.QtCore import (QAbstractTableModel, QLibraryInfo, QModelIndex, QObject, QSettings,
                            QSortFilterProxyModel, Qt, QTimer, QTranslator, Signal)
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QKeySequence, QPalette
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QButtonGroup, QCheckBox,
                               QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
                               QProgressBar, QPushButton, QRadioButton, QScrollArea,
                               QSizePolicy, QSlider, QSpinBox, QStackedWidget, QTableView,
                               QVBoxLayout, QWidget)

import compressor
import fileops
from fileops import OUT_FOLDER, OUT_OVERWRITE, OUT_SUFFIX, human_size
from runner import CRASHED, Batch, JobRunner, memory_budget
from version import APP_VERSION

APP_NAME = "圖片壓縮工具"
ORG = "CompressPicture"


def resource_path(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# =============================================================================
# 清單資料
# =============================================================================

@dataclass
class Item:
    id: int
    path: str
    rel: str
    size: int
    fmt: str
    status: str = "pending"
    new_size: int | None = None
    message: str = ""
    verified: bool = False
    target: str | None = None
    backup: str | None = None
    elapsed: float = 0.0
    keys: tuple = ()                  # 去重用：(實際路徑,) + (磁碟區, 檔案編號)
    written_sig: tuple | None = None  # 覆蓋模式下我們最後寫入原檔時的 (大小, 修改時間)


STATUS_TEXT = {
    "pending": "等待中",
    "queued": "排隊中",
    "running": "壓縮中…",
    "compressed": "✓ 已壓縮",
    "kept": "＝ 保留原檔",
    "skipped": "－ 略過",
    "error": "✕ 失敗",
    "restored": "↺ 已還原",
}
STATUS_ORDER = {k: i for i, k in enumerate(
    ["error", "running", "queued", "pending", "skipped", "kept", "restored", "compressed"])}
DONE_STATES = ("compressed", "kept", "skipped")

GREEN, RED, AMBER, BLUE = QColor("#16a34a"), QColor("#dc2626"), QColor("#d97706"), QColor("#2563eb")

COLUMNS = ["狀態", "檔名", "格式", "原始大小", "壓縮後", "節省", "所在資料夾"]
C_STATUS, C_NAME, C_FMT, C_ORIG, C_NEW, C_SAVE, C_DIR = range(len(COLUMNS))
SORT_ROLE = Qt.UserRole + 1
ID_ROLE = Qt.UserRole + 2

EXT_FMT = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".jfif": "JPEG", ".webp": "WebP"}


def saving_ratio(it: Item):
    if it.status in DONE_STATES and it.new_size is not None and it.size > 0:
        return (it.size - it.new_size) / it.size
    return None


class FileModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items: list[Item] = []
        self._row: dict[int, int] = {}
        self._keys: set = set()
        self._next_id = 1
        self._muted = QApplication.palette().color(QPalette.PlaceholderText)

    # ---- Qt 介面
    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def columnCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal:
            if role == Qt.DisplayRole:
                return COLUMNS[section]
            if role == Qt.TextAlignmentRole and section in (C_ORIG, C_NEW, C_SAVE):
                return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        it = self.items[index.row()]
        col = index.column()
        if role == Qt.DisplayRole:
            if col == C_STATUS:
                return STATUS_TEXT.get(it.status, it.status)
            if col == C_NAME:
                return os.path.basename(it.path)
            if col == C_FMT:
                return it.fmt
            if col == C_ORIG:
                return human_size(it.size)
            if col == C_NEW:
                return human_size(it.new_size) if it.status in DONE_STATES else ""
            if col == C_SAVE:
                r = saving_ratio(it)
                if r is None:
                    return ""
                return f"−{r * 100:.1f}%" if r > 0 else "0%"
            if col == C_DIR:
                return os.path.dirname(it.path)
        elif role == Qt.ForegroundRole:
            if col == C_STATUS:
                return {
                    "compressed": GREEN, "error": RED, "skipped": AMBER, "running": BLUE,
                }.get(it.status, self._muted if it.status in ("pending", "queued", "kept", "restored") else None)
            if col == C_SAVE:
                r = saving_ratio(it)
                return GREEN if r and r > 0 else self._muted
            if col == C_DIR:
                return self._muted
        elif role == Qt.FontRole and col == C_STATUS and it.status == "running":
            f = QFont()
            f.setBold(True)
            return f
        elif role == Qt.TextAlignmentRole:
            if col in (C_ORIG, C_NEW, C_SAVE):
                return int(Qt.AlignRight | Qt.AlignVCenter)
        elif role == Qt.ToolTipRole:
            return self._tooltip(it)
        elif role == SORT_ROLE:
            if col == C_STATUS:
                return STATUS_ORDER.get(it.status, 99)
            if col == C_NAME:
                return os.path.basename(it.path).lower()
            if col == C_FMT:
                return it.fmt
            if col == C_ORIG:
                return it.size
            if col == C_NEW:
                return it.new_size if it.new_size is not None else -1
            if col == C_SAVE:
                r = saving_ratio(it)
                return r if r is not None else -1.0
            if col == C_DIR:
                return os.path.dirname(it.path).lower()
        elif role == ID_ROLE:
            return it.id
        return None

    def _tooltip(self, it: Item) -> str:
        lines = [it.path]
        if it.message:
            lines.append(it.message)
        if it.status in DONE_STATES and it.new_size is not None:
            lines.append(f"{human_size(it.size)} → {human_size(it.new_size)}")
        if it.target and fileops.norm(it.target) != fileops.norm(it.path):
            lines.append(f"輸出：{it.target}")
        if it.backup:
            lines.append("原檔已備份，可按右鍵「還原原始檔案」")
        if it.elapsed:
            lines.append(f"耗時 {it.elapsed:.2f} 秒")
        return "\n".join(lines)

    # ---- 操作
    def add_files(self, files):
        """files: [(abs_path, rel)]，回傳 (新增數, 重複數)

        去重同時看「實際路徑」與「檔案身分」：同一個檔案用短檔名（KAIHSU~1）或 junction
        加入第二次也認得出來，避免兩個子行程同時覆寫同一個檔案。
        """
        new = []
        dup = 0
        canon = fileops.PathCanon()
        for path, rel in files:
            try:
                st = os.stat(path)
                keys = (("path", canon(path)),)
            except OSError:
                continue
            if st.st_ino:
                keys += (("ino", st.st_dev, st.st_ino),)
            if any(k in self._keys for k in keys):
                dup += 1
                continue
            self._keys.update(keys)
            ext = os.path.splitext(path)[1].lower()
            new.append(Item(self._next_id, path, rel, st.st_size, EXT_FMT.get(ext, ext.upper().strip(".")),
                            keys=keys))
            self._next_id += 1
        if new:
            start = len(self.items)
            self.beginInsertRows(QModelIndex(), start, start + len(new) - 1)
            for it in new:
                self._row[it.id] = len(self.items)
                self.items.append(it)
            self.endInsertRows()
        return len(new), dup

    def remove_ids(self, ids):
        ids = set(ids)
        if not ids:
            return
        self.beginResetModel()
        self.items = [it for it in self.items if it.id not in ids]
        self._reindex()
        self.endResetModel()

    def clear(self):
        self.beginResetModel()
        self.items = []
        self._reindex()
        self.endResetModel()

    def _reindex(self):
        self._row = {it.id: i for i, it in enumerate(self.items)}
        self._keys = {k for it in self.items for k in it.keys}

    def get(self, item_id) -> Item | None:
        r = self._row.get(item_id)
        return self.items[r] if r is not None else None

    def touch(self, item_id):
        r = self._row.get(item_id)
        if r is not None:
            self.dataChanged.emit(self.index(r, 0), self.index(r, len(COLUMNS) - 1))

    def touch_all(self):
        if self.items:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self.items) - 1, len(COLUMNS) - 1))


# =============================================================================
# 背景壓縮（子行程池）
# =============================================================================

class RunnerSignal(QObject):
    """背景執行緒寫完檔、把結果放進佇列後發出；主執行緒收到後再一次取出所有結果。"""
    resultsReady = Signal()


# =============================================================================
# 小元件
# =============================================================================

def hint_label(text: str) -> QLabel:
    lb = QLabel(text)
    lb.setWordWrap(True)
    pal = lb.palette()
    pal.setColor(QPalette.WindowText, pal.color(QPalette.PlaceholderText))
    lb.setPalette(pal)
    return lb


def section_title(text: str) -> QLabel:
    lb = QLabel(text)
    f = lb.font()
    f.setBold(True)
    f.setPointSizeF(f.pointSizeF() + 1)
    lb.setFont(f)
    lb.setContentsMargins(0, 8, 0, 2)
    return lb


def narrow_combo() -> QComboBox:
    """不讓長選項撐寬面板；下拉清單本身仍會完整顯示文字。"""
    cb = QComboBox()
    cb.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    cb.setMinimumContentsLength(8)
    cb.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    cb.view().setMinimumWidth(300)
    return cb


def separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Plain)
    pal = line.palette()
    pal.setColor(QPalette.WindowText, pal.color(QPalette.Mid))
    line.setPalette(pal)
    return line


class SliderSpin(QWidget):
    valueChanged = Signal(int)

    def __init__(self, lo, hi, value, left_hint="", right_hint=""):
        super().__init__()
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(lo, hi)
        self.spin = QSpinBox()
        self.spin.setRange(lo, hi)
        self.spin.setButtonSymbols(QSpinBox.NoButtons)  # 用滑桿調整；框內可直接輸入數字
        self.spin.setAlignment(Qt.AlignCenter)
        self.spin.setMinimumWidth(self.spin.fontMetrics().horizontalAdvance("1000") + 20)
        self.slider.valueChanged.connect(self.spin.setValue)
        self.spin.valueChanged.connect(self.slider.setValue)
        self.spin.valueChanged.connect(self.valueChanged)
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.slider, 1)
        top.addWidget(self.spin)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(top)
        if left_hint or right_hint:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 70, 0)
            row.addWidget(hint_label(left_hint))
            row.addStretch(1)
            row.addWidget(hint_label(right_hint))
            lay.addLayout(row)
        self.setValue(value)

    def value(self):
        return self.spin.value()

    def setValue(self, v):
        self.spin.setValue(int(v))


SEGMENT_QSS = """
QPushButton#seg { padding: 7px 10px; border: 1px solid palette(mid); background: palette(button);
                  font-weight: 600; }
QPushButton#seg:hover:!checked { background: palette(midlight); }
QPushButton#seg:checked { background: palette(highlight); color: palette(highlighted-text);
                          border-color: palette(highlight); }
QPushButton#seg[pos="left"]  { border-top-left-radius: 6px;  border-bottom-left-radius: 6px; }
QPushButton#seg[pos="right"] { border-top-right-radius: 6px; border-bottom-right-radius: 6px;
                               border-left: none; }
"""

PRIMARY_QSS = """
QPushButton#primary { background: palette(highlight); color: palette(highlighted-text);
                      font-weight: 600; padding: 8px 26px; border-radius: 6px; border: none; }
QPushButton#primary:hover { background: #1d4ed8; }
QPushButton#primary:disabled { background: palette(mid); color: palette(window); }
"""


class Segmented(QWidget):
    changed = Signal(int)

    def __init__(self, labels):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.group = QButtonGroup(self)
        for i, text in enumerate(labels):
            b = QPushButton(text)
            b.setObjectName("seg")
            b.setProperty("pos", "left" if i == 0 else "right")
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet(SEGMENT_QSS)
            b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            self.group.addButton(b, i)
            lay.addWidget(b)
        self.group.idToggled.connect(lambda i, on: on and self.changed.emit(i))

    def index(self):
        return self.group.checkedId()

    def setIndex(self, i):
        self.group.button(i).setChecked(True)


# =============================================================================
# 設定面板
# =============================================================================

PNG_LEVELS = [("快速", 2, False), ("標準（建議）", 4, False), ("最高", 6, False),
              ("最高 + Zopfli（非常慢，約再小 3–8%）", 6, True)]
SUBSAMPLING = [("依原圖（建議）", "keep"), ("4:2:0（檔案較小）", "420"),
               ("4:4:4（色彩較精準，適合文字與 UI）", "444")]

MODE_DESC = [
    "畫質完全不變，只重新最佳化編碼。每個檔案都會逐像素比對，確認與原圖一致才採用。"
    "通常可減少 5–30%。",
    "以些微畫質換取更小的檔案（PNG 減少顏色、JPEG／WebP 降低品質），通常可減少 50–80%。"
    "完成後雙擊檔案可比較前後差異。",
]


class SettingsPanel(QWidget):
    def __init__(self, settings: QSettings):
        super().__init__()
        self.settings = settings
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 6, 14, 14)
        lay.setSpacing(6)

        # ---- 壓縮模式
        lay.addWidget(section_title("壓縮模式"))
        self.mode = Segmented(["無損壓縮", "有損壓縮"])
        lay.addWidget(self.mode)
        self.mode_desc = hint_label("")
        self.mode_desc.setMinimumHeight(54)
        self.mode_desc.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        lay.addWidget(self.mode_desc)

        # 兩個頁面直接放進版面、切換時顯示/隱藏（隱藏的不佔空間）
        self.pages = [self._lossless_page(), self._lossy_page()]
        for p in self.pages:
            lay.addWidget(p)
        self.mode.changed.connect(self._on_mode)

        # ---- 中繼資料
        lay.addWidget(separator())
        lay.addWidget(section_title("中繼資料"))
        self.strip = QCheckBox("移除中繼資料（EXIF、文字說明、縮圖等）")
        lay.addWidget(self.strip)
        lay.addWidget(hint_label("色彩描述檔（ICC）與照片的旋轉方向一律保留，不會影響顯示。"))

        # ---- 輸出
        lay.addWidget(separator())
        lay.addWidget(section_title("輸出位置"))
        self.out_group = QButtonGroup(self)
        self.rb_suffix = QRadioButton("另存新檔：在檔名後面加上")
        self.suffix = QLineEdit()
        self.suffix.setFixedWidth(90)
        self.suffix.setPlaceholderText("_min")
        row = QHBoxLayout()
        row.addWidget(self.rb_suffix)
        row.addWidget(self.suffix)
        row.addStretch(1)
        lay.addLayout(row)
        self.suffix_example = hint_label("")
        self.suffix_example.setContentsMargins(24, 0, 0, 0)
        lay.addWidget(self.suffix_example)

        self.rb_folder = QRadioButton("輸出到資料夾（保留子資料夾結構）")
        lay.addWidget(self.rb_folder)
        self.out_dir = QLineEdit()
        self.out_dir.setPlaceholderText("選擇輸出資料夾…")
        self.btn_browse = QPushButton("瀏覽…")
        self.btn_open_dir = QPushButton("開啟")
        self.btn_open_dir.setToolTip("在檔案總管中開啟輸出資料夾")
        row = QHBoxLayout()
        row.setContentsMargins(24, 0, 0, 0)
        row.addWidget(self.out_dir, 1)
        row.addWidget(self.btn_browse)
        row.addWidget(self.btn_open_dir)
        lay.addLayout(row)

        self.rb_overwrite = QRadioButton("覆蓋原始檔案")
        lay.addWidget(self.rb_overwrite)
        ow = hint_label("原檔會先暫存備份；在關閉程式前，都可以在清單上按右鍵「還原原始檔案」。")
        ow.setContentsMargins(24, 0, 0, 0)
        lay.addWidget(ow)

        for i, rb in enumerate((self.rb_suffix, self.rb_folder, self.rb_overwrite)):
            self.out_group.addButton(rb, i)
        self.out_group.idToggled.connect(lambda *_: self._sync_output_enabled())
        self.suffix.textChanged.connect(self._update_example)
        self.btn_browse.clicked.connect(self._browse)
        self.btn_open_dir.clicked.connect(self._open_out_dir)

        lay.addStretch(1)
        self.btn_reset = QPushButton("恢復預設值")
        self.btn_reset.setFlat(True)
        self.btn_reset.setCursor(Qt.PointingHandCursor)
        self.btn_reset.clicked.connect(self.reset_defaults)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.btn_reset)
        lay.addLayout(row)

        self.load()

    # ---- 頁面
    def _lossless_page(self):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(4)
        lay.addWidget(QLabel("最佳化程度（PNG、無損 WebP）"))
        self.png_level = narrow_combo()
        for text, _, _ in PNG_LEVELS:
            self.png_level.addItem(text)
        lay.addWidget(self.png_level)
        lay.addWidget(hint_label("程度越高檔案越小，但越花時間。"))
        lay.addSpacing(6)
        lay.addWidget(hint_label(
            "JPEG：重新編排編碼並轉為漸進式。\n"
            "WebP：無損 WebP 會重新編碼；有損 WebP 無法無損再壓縮，會略過。\n"
            "若結果沒有比原檔小，會保留原檔。"))
        lay.addStretch(1)
        return w

    def _lossy_page(self):
        w = QWidget()
        g = QGridLayout(w)
        g.setContentsMargins(0, 2, 0, 0)
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(5)
        g.setColumnStretch(1, 1)
        row = 0

        def header(text):
            nonlocal row
            if row:
                g.setRowMinimumHeight(row, 6)
                row += 1
            g.addWidget(self._fmt_header(text), row, 0, 1, 2)
            row += 1

        def field(label, widget, tip=""):
            nonlocal row
            lb = QLabel(label)
            if tip:
                lb.setToolTip(tip)
                widget.setToolTip(tip)
            g.addWidget(lb, row, 0)
            g.addWidget(widget, row, 1)
            row += 1

        q_tip = "數值越低，檔案越小、畫質越差。"
        header("PNG")
        self.png_quality = SliderSpin(0, 100, 80)
        field("品質", self.png_quality, q_tip + "建議 65–85。")
        colors = QWidget()
        h = QHBoxLayout(colors)
        h.setContentsMargins(0, 0, 0, 0)
        self.png_colors = QSpinBox()
        self.png_colors.setRange(2, 256)
        self.png_dither = QCheckBox("抖色")
        self.png_dither.setToolTip("讓漸層更平滑、減少色帶。\n關閉可讓檔案更小，適合像素風或大面積純色的圖。")
        h.addWidget(self.png_colors)
        h.addSpacing(12)
        h.addWidget(self.png_dither)
        h.addStretch(1)
        field("最多顏色", colors, "調色盤最多使用幾種顏色（2–256）。")

        header("JPEG")
        self.jpeg_quality = SliderSpin(1, 100, 80)
        field("品質", self.jpeg_quality, q_tip + "建議 75–85。")
        self.jpeg_sub = narrow_combo()
        for text, _ in SUBSAMPLING:
            self.jpeg_sub.addItem(text)
        field("色度抽樣", self.jpeg_sub,
              "依原圖：維持原本的設定（建議）。\n4:2:0：色彩解析度減半，檔案較小，照片看不出差別。\n"
              "4:4:4：保留完整色彩解析度，適合文字、線條、UI 截圖。")

        header("WebP")
        self.webp_quality = SliderSpin(1, 100, 80)
        field("品質", self.webp_quality, q_tip + "建議 75–85。")

        g.setRowMinimumHeight(row, 4)
        row += 1
        g.addWidget(hint_label("若壓縮後沒有變小（例如原圖品質本來就低），會改用無損最佳化或保留原檔，"
                               "不會越壓越糊。"), row, 0, 1, 2)
        return w

    def _fmt_header(self, text):
        lb = QLabel(text)
        f = lb.font()
        f.setBold(True)
        lb.setFont(f)
        pal = lb.palette()
        pal.setColor(QPalette.WindowText, pal.color(QPalette.Highlight))
        lb.setPalette(pal)
        return lb

    # ---- 互動
    def _on_mode(self, i):
        for k, p in enumerate(self.pages):
            p.setVisible(k == i)
        self.mode_desc.setText(MODE_DESC[i])

    def _sync_output_enabled(self):
        self.suffix.setEnabled(self.rb_suffix.isChecked())
        folder = self.rb_folder.isChecked()
        self.out_dir.setEnabled(folder)
        self.btn_browse.setEnabled(folder)
        self.btn_open_dir.setEnabled(folder)
        self._update_example()

    def _update_example(self):
        s = self.suffix.text()
        if not s.strip():
            self.suffix_example.setText("⚠ 後綴不可空白")
        else:
            self.suffix_example.setText(f"例：photo.png → photo{s}.png（存在原檔旁邊）")

    def _browse(self):
        d = QFileDialog.getExistingDirectory(self, "選擇輸出資料夾", self.out_dir.text() or os.path.expanduser("~"))
        if d:
            self.out_dir.setText(os.path.normpath(d))
            self.rb_folder.setChecked(True)

    def _open_out_dir(self):
        d = self.out_dir.text().strip()
        if not d:
            return
        if not os.path.isdir(d):
            QMessageBox.information(self, APP_NAME, "這個資料夾還不存在，開始壓縮後會自動建立。")
            return
        os.startfile(d)  # noqa: S606 - Windows 專用工具

    # ---- 讀寫設定
    def options(self) -> dict:
        _, level, zopfli = PNG_LEVELS[self.png_level.currentIndex()]
        return {
            "mode": "lossless" if self.mode.index() == 0 else "lossy",
            "strip_metadata": self.strip.isChecked(),
            "png_level": level,
            "png_zopfli": zopfli,
            "png_quality": self.png_quality.value(),
            "png_max_colors": self.png_colors.value(),
            "png_dither": self.png_dither.isChecked(),
            "jpeg_quality": self.jpeg_quality.value(),
            "jpeg_subsampling": SUBSAMPLING[self.jpeg_sub.currentIndex()][1],
            "webp_quality": self.webp_quality.value(),
        }

    def output(self):
        mode = {0: OUT_SUFFIX, 1: OUT_FOLDER, 2: OUT_OVERWRITE}[self.out_group.checkedId()]
        return mode, self.suffix.text(), self.out_dir.text().strip()

    def _apply(self, o: dict, out_mode: str, suffix: str, out_dir: str):
        self.mode.setIndex(0 if o["mode"] == "lossless" else 1)
        self._on_mode(self.mode.index())
        self.strip.setChecked(bool(o["strip_metadata"]))
        idx = next((i for i, (_, lv, z) in enumerate(PNG_LEVELS)
                    if lv == int(o["png_level"]) and z == bool(o["png_zopfli"])), 1)
        self.png_level.setCurrentIndex(idx)
        self.png_quality.setValue(o["png_quality"])
        self.png_colors.setValue(int(o["png_max_colors"]))
        self.png_dither.setChecked(bool(o["png_dither"]))
        self.jpeg_quality.setValue(o["jpeg_quality"])
        self.jpeg_sub.setCurrentIndex(next((i for i, (_, k) in enumerate(SUBSAMPLING)
                                            if k == o["jpeg_subsampling"]), 0))
        self.webp_quality.setValue(o["webp_quality"])
        {OUT_SUFFIX: self.rb_suffix, OUT_FOLDER: self.rb_folder,
         OUT_OVERWRITE: self.rb_overwrite}.get(out_mode, self.rb_suffix).setChecked(True)
        self.suffix.setText(suffix)
        self.out_dir.setText(out_dir)
        self._sync_output_enabled()

    def load(self):
        s = self.settings
        o = dict(compressor.DEFAULT_OPTIONS)
        types = {k: type(v) for k, v in o.items()}
        for k in o:
            v = s.value(f"options/{k}")
            if v is None:
                continue
            t = types[k]
            try:
                o[k] = (str(v).lower() in ("true", "1")) if t is bool else t(v)
            except (TypeError, ValueError):
                pass
        self._apply(o, s.value("output/mode", OUT_SUFFIX), s.value("output/suffix", "_min"),
                    s.value("output/dir", ""))

    def save(self):
        s = self.settings
        for k, v in self.options().items():
            s.setValue(f"options/{k}", v)
        mode, suffix, out_dir = self.output()
        s.setValue("output/mode", mode)
        s.setValue("output/suffix", suffix)
        s.setValue("output/dir", out_dir)

    def reset_defaults(self):
        mode, _, out_dir = self.output()
        self._apply(dict(compressor.DEFAULT_OPTIONS), OUT_SUFFIX, "_min", out_dir)


# =============================================================================
# 空清單時的拖放區
# =============================================================================

class DropZone(QFrame):
    def __init__(self, on_files, on_folder):
        super().__init__()
        self.setObjectName("dropzone")
        self.set_active(False)
        lay = QVBoxLayout(self)
        lay.addStretch(1)
        icon = QLabel("🖼")
        f = icon.font()
        f.setPointSize(40)
        icon.setFont(f)
        icon.setAlignment(Qt.AlignCenter)
        lay.addWidget(icon)
        title = QLabel("把圖片或資料夾拖曳到這裡")
        f = title.font()
        f.setPointSizeF(f.pointSizeF() + 5)
        f.setBold(True)
        title.setFont(f)
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)
        sub = hint_label("支援 PNG、JPG、WebP。拖入資料夾時會包含所有子資料夾。")
        sub.setAlignment(Qt.AlignCenter)
        lay.addWidget(sub)
        lay.addSpacing(14)
        row = QHBoxLayout()
        row.addStretch(1)
        b1 = QPushButton("選擇檔案…")
        b2 = QPushButton("選擇資料夾…")
        for b in (b1, b2):
            b.setMinimumWidth(130)
            b.setMinimumHeight(34)
            row.addWidget(b)
        b1.clicked.connect(on_files)
        b2.clicked.connect(on_folder)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(2)

    def set_active(self, on: bool):
        color = "palette(highlight)" if on else "palette(mid)"
        self.setStyleSheet(f"QFrame#dropzone {{ border: 2px dashed {color}; border-radius: 12px; }}")


# =============================================================================
# 主視窗
# =============================================================================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        ini = os.environ.get("CPIC_SETTINGS_INI")  # 測試用：改用指定的 ini 檔
        self.settings = QSettings(ini, QSettings.IniFormat) if ini else QSettings(ORG, "CompressPicture")
        self.setWindowTitle(f"{APP_NAME}  v{APP_VERSION}")
        self.setMinimumSize(980, 600)
        icon_path = resource_path("icon.ico")
        if os.path.exists(icon_path):
            self.setWindowIcon(QIcon(icon_path))
        self.setAcceptDrops(True)

        workers = max(1, min((os.cpu_count() or 2) - 1, 8))
        self._signal = RunnerSignal(self)
        # QueuedConnection：背景執行緒發出的通知一律排進主執行緒的事件佇列，不會重入
        self._signal.resultsReady.connect(self._drain_results, Qt.QueuedConnection)
        self.runner = JobRunner(workers, notify=self._signal.resultsReady.emit)
        self.running = False
        self.batch: Batch | None = None
        self.written: set[str] = set()  # 這個工作階段寫過的輸出檔（PathCanon 形式）
        self._canon = fileops.PathCanon()
        self.backup_dir = tempfile.mkdtemp(prefix="CompressPicture_backup_")
        # 摘要列要掃整個清單；大批次時每完成一個檔案就重算會變成 O(n²)，改成最多每 0.2 秒一次
        self._summary_timer = QTimer(self)
        self._summary_timer.setSingleShot(True)
        self._summary_timer.setInterval(200)
        self._summary_timer.timeout.connect(self._update_summary)
        # 監看子行程：死在回傳結果途中時，executor 不會發現，要主動救援（見 JobRunner.watchdog）
        self._watchdog_timer = QTimer(self)
        self._watchdog_timer.setInterval(1000)
        self._watchdog_timer.timeout.connect(self._on_watchdog)

        self.model = FileModel(self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(SORT_ROLE)
        self.proxy.setDynamicSortFilter(False)

        self._build_ui()
        self._update_summary()
        self._update_buttons()

        geo = self.settings.value("window/geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            avail = QApplication.primaryScreen().availableGeometry()
            self.resize(min(1280, int(avail.width() * 0.9)), min(860, int(avail.height() * 0.9)))

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        # ---- 左邊：清單
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(14, 12, 10, 8)
        ll.setSpacing(8)

        bar = QHBoxLayout()
        self.btn_add_files = QPushButton("＋ 加入檔案")
        self.btn_add_files.setToolTip("加入圖片檔（Ctrl+O）")
        self.btn_add_folder = QPushButton("＋ 加入資料夾")
        self.btn_add_folder.setToolTip("加入資料夾，包含所有子資料夾（Ctrl+Shift+O）")
        self.btn_remove = QPushButton("移除選取")
        self.btn_remove.setToolTip("從清單移除選取的檔案（Delete），不會刪除檔案")
        self.btn_clear = QPushButton("清空清單")
        self.btn_clear.setToolTip("清空清單，不會刪除檔案")
        for b in (self.btn_add_files, self.btn_add_folder):
            bar.addWidget(b)
        bar.addSpacing(12)
        for b in (self.btn_remove, self.btn_clear):
            bar.addWidget(b)
        bar.addStretch(1)
        self.count_label = hint_label("")
        self.count_label.setWordWrap(False)
        bar.addWidget(self.count_label)
        ll.addLayout(bar)

        self.stack = QStackedWidget()
        self.dropzone = DropZone(self.add_files_dialog, self.add_folder_dialog)
        self.stack.addWidget(self.dropzone)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideMiddle)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(28)
        hh = self.table.horizontalHeader()
        hh.setSortIndicatorClearable(True)
        hh.setSortIndicator(-1, Qt.AscendingOrder)
        self.table.setSortingEnabled(True)
        hh.setHighlightSections(False)
        hh.setStretchLastSection(True)
        for col, w in ((C_STATUS, 110), (C_NAME, 260), (C_FMT, 60), (C_ORIG, 90), (C_NEW, 90), (C_SAVE, 80)):
            hh.resizeSection(col, w)
        hh.setSectionResizeMode(QHeaderView.Interactive)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.table.doubleClicked.connect(lambda idx: self.preview_selected())
        self.table.selectionModel().selectionChanged.connect(lambda *_: self._update_buttons())
        self.stack.addWidget(self.table)
        ll.addWidget(self.stack, 1)

        self.summary = QLabel("")
        self.summary.setTextFormat(Qt.RichText)
        ll.addWidget(self.summary)
        body.addWidget(left, 1)

        # ---- 右邊：設定
        vline = QFrame()
        vline.setFrameShape(QFrame.VLine)
        vline.setFrameShadow(QFrame.Plain)
        pal = vline.palette()
        pal.setColor(QPalette.WindowText, pal.color(QPalette.Mid))
        vline.setPalette(pal)
        body.addWidget(vline)

        self.panel = SettingsPanel(self.settings)
        scroll = QScrollArea()
        scroll.setWidget(self.panel)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFixedWidth(370)
        body.addWidget(scroll)
        root.addLayout(body, 1)

        # ---- 底部：進度與開始
        root.addWidget(separator())
        bottom = QHBoxLayout()
        bottom.setContentsMargins(14, 10, 14, 12)
        self.status_text = QLabel("")
        self.status_text.setMinimumWidth(200)
        bottom.addWidget(self.status_text, 1)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(320)
        self.progress.setTextVisible(True)
        self.progress.setFormat("%v / %m")
        self.progress.hide()
        bottom.addWidget(self.progress)
        bottom.addSpacing(10)
        self.btn_stop = QPushButton("停止")
        self.btn_stop.setMinimumHeight(36)
        self.btn_stop.setToolTip("不再開始新的檔案，等處理中的檔案完成後停止")
        self.btn_start = QPushButton("開始壓縮")
        self.btn_start.setObjectName("primary")
        self.btn_start.setStyleSheet(PRIMARY_QSS)
        self.btn_start.setMinimumHeight(36)
        self.btn_start.setCursor(Qt.PointingHandCursor)
        self.btn_start.setToolTip("Ctrl+Enter")
        bottom.addWidget(self.btn_stop)
        bottom.addWidget(self.btn_start)
        root.addLayout(bottom)

        self.setCentralWidget(central)

        # ---- 連接
        self.btn_add_files.clicked.connect(self.add_files_dialog)
        self.btn_add_folder.clicked.connect(self.add_folder_dialog)
        self.btn_remove.clicked.connect(self.remove_selected)
        self.btn_clear.clicked.connect(self.clear_list)
        self.btn_start.clicked.connect(self.start)
        self.btn_stop.clicked.connect(self.stop)

        def shortcut(seq, fn):
            a = QAction(self)
            a.setShortcut(QKeySequence(seq))
            a.triggered.connect(fn)
            self.addAction(a)
        shortcut("Ctrl+O", self.add_files_dialog)
        shortcut("Ctrl+Shift+O", self.add_folder_dialog)
        shortcut("Ctrl+Return", self.start)
        del_action = QAction(self.table)
        del_action.setShortcut(QKeySequence.Delete)
        del_action.setShortcutContext(Qt.WidgetShortcut)
        del_action.triggered.connect(self.remove_selected)
        self.table.addAction(del_action)
        enter_action = QAction(self.table)
        enter_action.setShortcuts([QKeySequence(Qt.Key_Return), QKeySequence(Qt.Key_Enter)])
        enter_action.setShortcutContext(Qt.WidgetShortcut)
        enter_action.triggered.connect(self.preview_selected)
        self.table.addAction(enter_action)

    # ------------------------------------------------------------------ 加入檔案
    def add_files_dialog(self):
        if self._busy_warning():
            return
        start = self.settings.value("ui/last_dir", os.path.expanduser("~"))
        exts = " ".join(f"*{e}" for e in compressor.SUPPORTED_EXTS)
        files, _ = QFileDialog.getOpenFileNames(self, "加入圖片", start, f"圖片 ({exts});;所有檔案 (*.*)")
        if files:
            self.settings.setValue("ui/last_dir", os.path.dirname(files[0]))
            self.add_paths(files)

    def add_folder_dialog(self):
        if self._busy_warning():
            return
        start = self.settings.value("ui/last_dir", os.path.expanduser("~"))
        d = QFileDialog.getExistingDirectory(self, "加入資料夾", start)
        if d:
            self.settings.setValue("ui/last_dir", d)
            self.add_paths([d])

    def add_paths(self, paths):
        if self._busy_warning():  # 拖放是延後處理的，這裡再擋一次
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            res = fileops.collect(paths)
            added, dup = self.model.add_files(res.files)
        finally:
            QApplication.restoreOverrideCursor()
        parts = [f"已加入 {added} 個檔案"]
        if dup:
            parts.append(f"{dup} 個已在清單中")
        if res.unsupported:
            parts.append(f"略過 {res.unsupported} 個不支援的檔案")
        if not added and not dup and not res.unsupported:
            parts = ["沒有找到圖片"]
        self._set_status("，".join(parts))
        self._update_summary()
        self._update_buttons()

    def _busy_warning(self):
        if self.running:
            self._set_status("壓縮進行中，請等完成或按「停止」後再修改清單")
            return True
        return False

    # ------------------------------------------------------------------ 拖放
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and any(u.isLocalFile() for u in e.mimeData().urls()):
            if self.running:
                e.ignore()
                return
            e.acceptProposedAction()
            self._drop_highlight(True)

    def dragMoveEvent(self, e):
        e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self._drop_highlight(False)

    def dropEvent(self, e):
        self._drop_highlight(False)
        paths = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if paths:
            e.acceptProposedAction()
            self.raise_()
            self.activateWindow()
            # 讓拖放動作先結束，避免大資料夾掃描時檔案總管卡住
            QTimer.singleShot(0, lambda: self.add_paths(paths))

    def _drop_highlight(self, on):
        self.dropzone.set_active(on)
        self.table.setStyleSheet("QTableView { border: 2px dashed palette(highlight); }" if on else "")

    # ------------------------------------------------------------------ 清單操作
    def selected_ids(self):
        rows = self.table.selectionModel().selectedRows()
        return [self.proxy.data(r, ID_ROLE) for r in rows]

    def remove_selected(self):
        if self._busy_warning():
            return
        ids = self.selected_ids()
        if ids:
            self.model.remove_ids(ids)
            self._set_status(f"已從清單移除 {len(ids)} 個檔案（檔案本身沒有被刪除）")
            self._update_summary()
            self._update_buttons()

    def clear_list(self):
        if self._busy_warning() or not self.model.items:
            return
        n = len(self.model.items)
        self.model.clear()
        self._set_status(f"已清空清單（{n} 個檔案，檔案本身沒有被刪除）")
        self._update_summary()
        self._update_buttons()

    def _context_menu(self, pos):
        ids = self.selected_ids()
        if not ids:
            return
        items = [self.model.get(i) for i in ids]
        menu = QMenu(self)
        one = items[0] if len(items) == 1 else None

        a = menu.addAction("比較壓縮前後…")
        a.setShortcut(QKeySequence(Qt.Key_Return))
        a.setEnabled(bool(one and one.status in DONE_STATES))
        a.triggered.connect(self.preview_selected)

        a = menu.addAction("在檔案總管中顯示")
        a.setEnabled(one is not None)
        a.triggered.connect(lambda: self._reveal(one))

        menu.addSeparator()
        a = menu.addAction(f"用目前設定重新壓縮（{len(items)} 個）")
        a.setEnabled(not self.running)
        a.triggered.connect(lambda: self._rerun(ids))

        restorable = [it for it in items if it.backup and it.status != "restored"]
        a = menu.addAction(f"還原原始檔案（{len(restorable)} 個）" if restorable else "還原原始檔案")
        a.setEnabled(bool(restorable) and not self.running)
        a.triggered.connect(lambda: self._restore(restorable))

        menu.addSeparator()
        a = menu.addAction("從清單移除")
        a.setShortcut(QKeySequence.Delete)
        a.setEnabled(not self.running)
        a.triggered.connect(self.remove_selected)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _reveal(self, it: Item):
        path = it.target if it.target and os.path.exists(it.target) else it.path
        if os.path.exists(path):
            subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')  # noqa: S602,S603

    def _rerun(self, ids):
        # 狀態在 _start_items 確認後才會改成「排隊中」；取消的話清單維持原樣
        self._start_items([self.model.get(i) for i in ids if self.model.get(i)])

    def _restore(self, items):
        # 壓縮之後被其他程式改過的檔案，還原會蓋掉那些修改 → 先問
        changed = []
        for it in items:
            try:
                if it.written_sig and fileops.file_sig(it.path) != it.written_sig:
                    changed.append(it)
            except OSError:
                pass  # 檔案不見了：直接還原即可
        if changed:
            names = "\n".join(os.path.basename(it.path) for it in changed[:10])
            more = "\n…" if len(changed) > 10 else ""
            r = QMessageBox.question(
                self, "檔案在壓縮後被修改過",
                f"以下 {len(changed)} 個檔案在壓縮之後又被其他程式修改過，還原會蓋掉那些修改：\n\n"
                f"{names}{more}\n\n仍要還原這些檔案嗎？（選「否」只還原其他檔案）")
            if r != QMessageBox.Yes:
                skip = {it.id for it in changed}
                items = [it for it in items if it.id not in skip]
        ok, failed = 0, []
        for it in items:
            try:
                fileops.restore_backup(it.backup, it.path)
                it.written_sig = fileops.file_sig(it.path)
                it.status = "restored"
                it.new_size = None
                it.message = "已還原為原始檔案"
                ok += 1
            except OSError as e:
                failed.append(f"{os.path.basename(it.path)}：{e}")
            self.model.touch(it.id)
        self._update_summary()
        if failed:
            QMessageBox.warning(self, "還原失敗", "以下檔案無法還原：\n\n" + "\n".join(failed[:20]))
        self._set_status(f"已還原 {ok} 個原始檔案")

    def preview_selected(self):
        ids = self.selected_ids()
        if not ids:
            return
        it = self.model.get(ids[0])
        if not it or it.status not in DONE_STATES:
            self._set_status("這個檔案還沒有壓縮結果，壓縮完成後才能比較")
            return
        original = it.backup if it.backup and os.path.exists(it.backup) else it.path
        result = it.target if it.target and os.path.exists(it.target) else it.path
        from preview import CompareDialog
        dlg = CompareDialog(self, original, result, os.path.basename(it.path),
                            it.size, it.new_size, it.message)
        dlg.exec()

    # ------------------------------------------------------------------ 壓縮流程
    def start(self):
        if self.running:
            return
        if not self.model.items:
            self._set_status("請先加入圖片：把檔案拖進視窗，或按「＋ 加入檔案」")
            return
        todo = [it for it in self.model.items if it.status == "pending"]
        if not todo:
            r = QMessageBox.question(
                self, "重新壓縮",
                "清單中的檔案都已經處理過了。\n\n要用目前的設定重新壓縮全部檔案嗎？\n"
                "（會從原始檔重新壓縮，不會把已壓縮的結果再壓一次）")
            if r != QMessageBox.Yes:
                return
            todo = list(self.model.items)
        self._start_items(todo)

    def _start_items(self, todo):
        if not todo or self.running:
            return
        opts = self.panel.options()
        out_mode, suffix, out_dir = self.panel.output()

        # 確認每個原始檔現在的狀態（只讀檔案資訊，很快）
        sigs, missing = {}, {}
        for it in todo:
            try:
                sigs[it.id] = fileops.file_sig(it.path)
            except OSError:
                missing[it.id] = "找不到檔案或無法讀取（可能已被移動或刪除）"
                continue
            # 之前覆蓋過、但之後檔案被其他程式改過 → 現在的內容才是新的「原始檔」，舊備份不再適用
            if it.backup and (sigs[it.id] != it.written_sig or not os.path.exists(it.backup)):
                it.backup = None
                it.written_sig = None
        # 清單中所有原始檔都不能被輸出檔蓋掉（不只這次要處理的）
        protected = {}
        for it in self.model.items:
            ident = fileops.identity(it.path)
            if ident:
                protected[ident] = it.id
        try:
            targets, problems = fileops.plan_targets(
                [fileops.PlanItem(it.id, it.path, it.rel) for it in todo if it.id not in missing],
                out_mode, suffix, out_dir, protected)
        except ValueError as e:
            QMessageBox.warning(self, "輸出設定有誤", str(e))
            return
        problems.update(missing)

        if out_mode == OUT_OVERWRITE:
            fresh = [it for it in todo if not it.backup and it.id not in problems]
            if fresh and self.settings.value("ui/confirm_overwrite", True, type=bool):
                box = QMessageBox(QMessageBox.Warning, "覆蓋原始檔案",
                                  f"即將直接覆蓋 {len(fresh)} 個原始檔案。\n\n"
                                  "原檔會先暫存備份，在關閉本程式之前，都可以在清單上按右鍵「還原原始檔案」。"
                                  "關閉程式後備份會刪除。",
                                  QMessageBox.Ok | QMessageBox.Cancel, self)
                box.button(QMessageBox.Ok).setText("覆蓋")
                box.button(QMessageBox.Cancel).setText("取消")
                cb = QCheckBox("以後不要再提醒")
                box.setCheckBox(cb)
                if box.exec() != QMessageBox.Ok:
                    return
                if cb.isChecked():
                    self.settings.setValue("ui/confirm_overwrite", False)
        else:
            existing = [t for t in targets.values()
                        if os.path.exists(t) and self._canon(t) not in self.written]
            if existing:
                sample = "\n".join(existing[:5]) + ("\n…" if len(existing) > 5 else "")
                r = QMessageBox.question(
                    self, "輸出檔已存在",
                    f"有 {len(existing)} 個輸出檔已經存在，繼續會覆蓋它們：\n\n{sample}\n\n要繼續嗎？")
                if r != QMessageBox.Yes:
                    return

        self.panel.save()
        jobs = []
        for it in todo:
            if it.id in problems:
                it.status = "error"
                it.message = problems[it.id]
                it.new_size = None
                self.model.touch(it.id)
                continue
            it.status = "queued"
            it.message = ""
            it.target = targets[it.id]
            jobs.append({
                "id": it.id,
                "read_path": it.backup or it.path,  # 重新壓縮時從備份（真正的原檔）讀
                "source_path": it.path,
                "disk_sig": sigs[it.id],
                "target": targets[it.id],
                "overwrite": out_mode == OUT_OVERWRITE,
                "backup": it.backup,
                "backup_dir": self.backup_dir,
                "options": opts,
            })
            self.model.touch(it.id)

        if not jobs:
            self._update_summary()
            self._set_status("沒有可以處理的檔案（請看清單中「失敗」的原因）")
            return
        self.batch = Batch(self.runner, jobs, memory_budget(self.runner.workers))
        self._watchdog_timer.start()
        self.running = True
        self.run_started = time.perf_counter()
        self.progress.setRange(0, self.batch.total)
        self.progress.setValue(0)
        self.progress.show()
        self._set_status("壓縮中…")
        self._update_buttons()
        self._fill()

    def _fill(self):
        b = self.batch
        if b is None:
            return
        started, errors = b.fill()
        for iid in started:
            it = self.model.get(iid)
            if it is not None:
                it.status = "running"
                self.model.touch(iid)
        for res in errors:
            self._apply_result(res)
        if errors:
            self.progress.setValue(b.done)
        if self.running and b.finished:
            if b.stopping:
                self._end_stop()
            else:
                self._finish()

    def _drain_results(self):
        """主執行緒：取出背景執行緒已寫完檔的所有結果。"""
        results = self.runner.drain()
        if not results:
            return
        b = self.batch
        for res in results:
            outcome = b.on_result(res) if b is not None else "ignore"
            if outcome == "retry":
                self._set_item(res["id"], "queued", "子行程意外終止，稍後會單獨重試這個檔案")
            elif outcome == "dropped":
                self._set_item(res["id"], "pending", "")
            elif outcome == "final" or res.get("status") != CRASHED:
                self._apply_result(res)
        if b is not None and self.running:
            self.progress.setValue(b.done)
            if b.stopping:
                self._update_stopping_text()
            self._fill()
        self._schedule_summary()

    def _set_item(self, iid, status, message):
        it = self.model.get(iid)
        if it is not None:
            it.status = status
            it.message = message
            self.model.touch(iid)

    def _on_watchdog(self):
        if self.running and self.runner.watchdog():
            self._set_status("偵測到子行程異常終止，正在單獨重試受影響的檔案…")

    def _apply_result(self, res: dict):
        """把一個（已寫入磁碟的）結果套到清單上。"""
        it = self.model.get(res["id"])
        if it is None:
            return
        try:
            st = res.get("status", "error")
            it.status = st
            it.message = res.get("message", "")
            it.verified = bool(res.get("verified"))
            it.elapsed = res.get("elapsed", 0.0)
            if res.get("format"):
                it.fmt = {"JPEG": "JPEG", "PNG": "PNG", "WEBP": "WebP"}.get(res["format"], res["format"])
            if res.get("backup"):
                it.backup = res["backup"]
            if res.get("written_sig"):
                it.written_sig = tuple(res["written_sig"])
            if st in DONE_STATES:
                if res.get("orig_size"):
                    it.size = res["orig_size"]
                it.new_size = res.get("new_size", it.size)
                if res.get("target"):
                    self.written.add(self._canon(res["target"]))
            else:
                it.new_size = None
        except Exception:  # noqa: BLE001 - 不能讓一筆壞資料打斷整批的進度計算
            traceback.print_exc()
            it.status = "error"
            it.message = "內部錯誤：無法更新這個檔案的狀態"
        self.model.touch(it.id)

    def _schedule_summary(self):
        if not self._summary_timer.isActive():
            self._summary_timer.start()

    def stop(self, force: bool = False):
        """第一次：不再送出新工作，等處理中的檔案做完（安全，不會卡住）。
        第二次（或 force=True）：強制中止處理中的檔案（它們不會被修改）。"""
        b = self.batch
        if not self.running or b is None:
            return
        if not force and not b.stopping:
            for iid in b.stop_dispatch():
                self._set_item(iid, "pending", "")
            if not b.in_flight:
                self._end_stop()
                return
            self.btn_stop.setText("強制停止")
            self.btn_stop.setToolTip("立刻中止處理中的檔案（這些檔案不會被修改，會維持「等待中」）")
            self._update_stopping_text()
            return

        self._set_status("正在強制停止…")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            written = self.runner.kill()
        finally:
            QApplication.restoreOverrideCursor()
        # kill() 返回後不會再有任何寫檔；中止前已寫入磁碟的結果一定要套用，否則清單會和磁碟不一致
        for res in written:
            self._apply_result(res)
        self._end_stop()

    def _update_stopping_text(self):
        n = len(self.batch.in_flight) if self.batch else 0
        self._set_status(f"正在停止：等待 {n} 個處理中的檔案完成…（可按「強制停止」立即中止）")

    def _end_stop(self):
        b, self.batch = self.batch, None
        b.close()
        self._watchdog_timer.stop()
        for iid in list(b.in_flight) + list(b.queue):
            it = self.model.get(iid)
            if it and it.status in ("running", "queued"):
                self._set_item(iid, "pending", "")
        left = sum(1 for i in b.jobs if (it := self.model.get(i)) and it.status == "pending")
        self.running = False
        self.progress.hide()
        self._update_buttons()
        self._update_summary()
        self._set_status(f"已停止。{left} 個檔案尚未處理，再按「開始壓縮」可以繼續。")

    def _finish(self):
        b, self.batch = self.batch, None
        b.close()
        self._watchdog_timer.stop()
        self.running = False
        self.progress.hide()
        self._update_buttons()
        self._update_summary()
        done = [it for it in (self.model.get(i) for i in b.jobs) if it]
        n_err = sum(1 for it in done if it.status == "error")
        msg = f"✓ 完成 {len(done) - n_err} 個檔案，耗時 {time.perf_counter() - self.run_started:.1f} 秒"
        if n_err:
            msg += f"。有 {n_err} 個失敗，滑鼠移到該列可看原因"
        self._set_status(msg)
        QApplication.alert(self)

    # ------------------------------------------------------------------ 狀態顯示
    def _set_status(self, text):
        self.status_text.setText(text)

    def _update_summary(self):
        items = self.model.items
        n = len(items)
        self.count_label.setText(f"共 {n} 個檔案" if n else "")
        self.stack.setCurrentIndex(1 if n else 0)
        done = [it for it in items if it.status in DONE_STATES]
        if not done:
            pending = sum(1 for it in items if it.status == "pending")
            total = sum(it.size for it in items)
            self.summary.setText(f"{pending} 個等待壓縮，共 {human_size(total)}" if n else "")
            return
        orig = sum(it.size for it in done)
        new = sum(it.new_size or 0 for it in done)
        saved = orig - new
        pct = saved / orig * 100 if orig else 0
        n_err = sum(1 for it in items if it.status == "error")
        n_pending = sum(1 for it in items if it.status in ("pending", "queued", "running"))
        parts = [f"已處理 <b>{len(done)}</b> 個：{human_size(orig)} → {human_size(new)}，"
                 f"節省 <b style='color:{GREEN.name()}'>{human_size(saved)}（{pct:.1f}%）</b>"]
        if n_pending:
            parts.append(f"{n_pending} 個未處理")
        if n_err:
            parts.append(f"<span style='color:{RED.name()}'>{n_err} 個失敗</span>")
        self.summary.setText("　·　".join(parts))

    def _update_buttons(self):
        has = bool(self.model.items)
        sel = bool(self.table.selectionModel().selectedRows()) if has else False
        r = self.running
        self.btn_start.setEnabled(not r and has)
        self.btn_start.setText("壓縮中…" if r else "開始壓縮")
        self.btn_stop.setEnabled(r)
        self.btn_stop.setVisible(r)
        if not r:
            self.btn_stop.setText("停止")
            self.btn_stop.setToolTip("不再開始新的檔案，等處理中的檔案完成後停止。"
                                     "尚未處理的檔案會維持「等待中」，再按「開始壓縮」可以繼續")
        for b in (self.btn_add_files, self.btn_add_folder):
            b.setEnabled(not r)
        self.btn_remove.setEnabled(not r and sel)
        self.btn_clear.setEnabled(not r and has)
        self.panel.setEnabled(not r)

    # ------------------------------------------------------------------ 關閉
    def closeEvent(self, e):
        if self.running:
            r = QMessageBox.question(self, "壓縮進行中", "還有檔案正在壓縮，確定要中止並關閉嗎？\n（已完成的檔案不受影響）")
            if r != QMessageBox.Yes:
                e.ignore()
                return
        if self.running:
            self.stop(force=True)  # 對話框開著時批次可能已經結束，所以再判斷一次
        # kill() 會等所有寫檔完成才返回，之後才能刪除備份
        self.runner.kill()
        self.panel.save()
        self.settings.setValue("window/geometry", self.saveGeometry())
        self.settings.sync()
        shutil.rmtree(self.backup_dir, ignore_errors=True)
        super().closeEvent(e)


def install_translations(app) -> bool:
    """載入 Qt 內建的繁中翻譯，讓「是／否」「確定／取消」等標準按鈕顯示中文。"""
    dirs = [QLibraryInfo.path(QLibraryInfo.TranslationsPath)]
    try:
        import PySide6
        dirs.append(os.path.join(os.path.dirname(PySide6.__file__), "translations"))
    except ImportError:
        pass
    for d in dirs:
        tr = QTranslator(app)
        if tr.load("qtbase_zh_TW", d):
            app.installTranslator(tr)
            return True
    return False


def run() -> int:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CompressPicture.App")
        except Exception:  # noqa: BLE001
            pass
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(ORG)
    install_translations(app)
    f = app.font()
    f.setFamilies(["Segoe UI", "Microsoft JhengHei UI", "Microsoft JhengHei"])
    f.setPointSizeF(max(f.pointSizeF(), 9.5))
    app.setFont(f)
    w = MainWindow()
    w.show()
    # 支援把檔案拖到 exe 圖示上開啟
    args = [a for a in sys.argv[1:] if os.path.exists(a)]
    if args:
        QTimer.singleShot(0, lambda: w.add_paths(args))
    code = app.exec()
    stuck = w.runner.stuck
    # 依序釋放：先視窗、再 QApplication，避免 Python 結束時以錯誤順序銷毀而當掉
    import shiboken6
    shiboken6.delete(w)
    app.shutdown()
    if stuck:
        # 極少見：強制停止時有內部執行緒救不回來。Python 結束時會等它而卡住，
        # 所以直接結束行程（設定與備份清理都已在 closeEvent 完成）。
        os._exit(code)
    return code
