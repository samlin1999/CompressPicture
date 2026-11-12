"""比較壓縮前後：左右並排、同步縮放與平移。"""
from __future__ import annotations

from PIL import Image, ImageOps
from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import (QDialog, QGraphicsPixmapItem, QGraphicsScene, QGraphicsView,
                               QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget)

from compressor import to_rgba8
from fileops import human_size

MIN_ZOOM, MAX_ZOOM = 0.02, 64.0


def load_qimage(path: str):
    with Image.open(path) as im:
        im.seek(0)
        im = ImageOps.exif_transpose(im)
        rgba = to_rgba8(im) if im.mode != "CMYK" else im.convert("RGB").convert("RGBA")
        w, h = rgba.size
        data = rgba.tobytes()
    return QImage(data, w, h, 4 * w, QImage.Format_RGBA8888).copy()


def checker_brush() -> QBrush:
    pm = QPixmap(16, 16)
    pm.fill(QColor("#ffffff"))
    p = QPainter(pm)
    p.fillRect(0, 0, 8, 8, QColor("#d4d4d4"))
    p.fillRect(8, 8, 8, 8, QColor("#d4d4d4"))
    p.end()
    return QBrush(pm)


class ImageView(QGraphicsView):
    zoomChanged = Signal()

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.item = QGraphicsPixmapItem()
        self.scene().addItem(self.item)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setRenderHint(QPainter.SmoothPixmapTransform, True)
        self.setFrameShape(QGraphicsView.NoFrame)
        self.setBackgroundBrush(QColor("#2b2b2b"))
        self._checker = checker_brush()

    def set_image(self, img: QImage):
        self.item.setPixmap(QPixmap.fromImage(img))
        self.setSceneRect(QRectF(self.item.boundingRect()))
        self._update_mode()

    def zoom(self) -> float:
        return self.transform().m11()

    def set_zoom(self, z: float):
        z = max(MIN_ZOOM, min(MAX_ZOOM, z))
        self.setTransform(QTransform.fromScale(z, z))
        self._update_mode()
        self.zoomChanged.emit()

    def fit(self):
        r = self.item.boundingRect()
        if r.isEmpty():
            return
        vw, vh = self.viewport().width() - 4, self.viewport().height() - 4
        z = min(vw / r.width(), vh / r.height(), 1.0)  # 小圖不放大
        self.set_zoom(z)
        self.centerOn(self.item)
        self.zoomChanged.emit()

    def _update_mode(self):
        # 放大時用最近鄰，才看得到真實像素（壓縮瑕疵）；縮小時平滑
        smooth = self.zoom() < 1.0
        self.item.setTransformationMode(Qt.SmoothTransformation if smooth else Qt.FastTransformation)

    def wheelEvent(self, e):
        steps = e.angleDelta().y() / 120
        if steps:
            z = max(MIN_ZOOM, min(MAX_ZOOM, self.zoom() * (1.25 ** steps)))
            f = z / self.zoom()
            self.scale(f, f)
            self._update_mode()
            self.zoomChanged.emit()
        e.accept()

    def drawBackground(self, painter, rect):
        super().drawBackground(painter, rect)
        r = self.mapFromScene(self.item.sceneBoundingRect()).boundingRect()
        painter.save()
        painter.resetTransform()
        painter.fillRect(r, self._checker)
        painter.restore()


class CompareDialog(QDialog):
    def __init__(self, parent, original_path, result_path, name, orig_size, new_size, message=""):
        super().__init__(parent)
        self.setWindowTitle(f"比較壓縮前後 — {name}")
        self.resize(1200, 760)
        self.setWindowFlag(Qt.WindowMaximizeButtonHint, True)

        self.img_orig = load_qimage(original_path)
        self.img_new = load_qimage(result_path)
        self.showing_original = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 10, 10, 10)

        if message:
            info = QLabel(message)
            info.setWordWrap(True)
            lay.addWidget(info)

        views = QHBoxLayout()
        views.setSpacing(6)
        self.left = ImageView()
        self.right = ImageView()
        self.left.set_image(self.img_orig)
        self.right.set_image(self.img_new)
        dims = f"{self.img_orig.width()} × {self.img_orig.height()} px"
        ratio = ""
        if orig_size and new_size is not None:
            r = (orig_size - new_size) / orig_size * 100
            ratio = f"（−{r:.1f}%）" if r > 0 else "（相同）"
        self.cap_left = QLabel(f"<b>原圖</b>　{human_size(orig_size)}　·　{dims}")
        self.cap_right_text = f"<b>壓縮後</b>　{human_size(new_size)} {ratio}"
        self.cap_right = QLabel(self.cap_right_text)
        views.addWidget(self._column(self.cap_left, self.left))
        views.addWidget(self._column(self.cap_right, self.right))
        lay.addLayout(views, 1)

        bar = QHBoxLayout()
        self.zoom_label = QLabel("")
        self.zoom_label.setMinimumWidth(70)
        b_fit = QPushButton("符合視窗")
        b_100 = QPushButton("實際大小 100%")
        b_400 = QPushButton("放大 400%")
        self.b_hold = QPushButton("按住：右側顯示原圖（空白鍵）")
        self.b_hold.setToolTip("按住時右邊暫時換成原圖，放開恢復。快速切換最容易看出差異。")
        b_close = QPushButton("關閉")
        bar.addWidget(QLabel("縮放："))
        bar.addWidget(self.zoom_label)
        bar.addWidget(b_fit)
        bar.addWidget(b_100)
        bar.addWidget(b_400)
        bar.addSpacing(16)
        bar.addWidget(self.b_hold)
        bar.addStretch(1)
        hint = QLabel("滾輪縮放 · 拖曳移動 · 左右同步")
        hint.setStyleSheet("color: gray;")
        bar.addWidget(hint)
        bar.addSpacing(10)
        bar.addWidget(b_close)
        lay.addLayout(bar)

        b_fit.clicked.connect(self.fit)
        b_100.clicked.connect(lambda: self._set_zoom(1.0))
        b_400.clicked.connect(lambda: self._set_zoom(4.0))
        self.b_hold.pressed.connect(lambda: self._show_original(True))
        self.b_hold.released.connect(lambda: self._show_original(False))
        b_close.clicked.connect(self.accept)
        for b in (b_fit, b_100, b_400, self.b_hold, b_close):
            b.setFocusPolicy(Qt.NoFocus)  # 空白鍵留給「按住比對」

        self._syncing = False
        for src, dst in ((self.left, self.right), (self.right, self.left)):
            src.horizontalScrollBar().valueChanged.connect(lambda _, s=src, d=dst: self._sync(s, d))
            src.verticalScrollBar().valueChanged.connect(lambda _, s=src, d=dst: self._sync(s, d))
            src.zoomChanged.connect(lambda s=src, d=dst: self._sync(s, d))

    def _column(self, caption, view):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(4)
        v.addWidget(caption)
        v.addWidget(view, 1)
        return w

    def showEvent(self, e):
        super().showEvent(e)
        self.fit()

    def fit(self):
        self.left.fit()
        self._sync(self.left, self.right)

    def _set_zoom(self, z):
        center = self.left.mapToScene(self.left.viewport().rect().center())
        self.left.set_zoom(z)
        self.left.centerOn(center)
        self._sync(self.left, self.right)

    def _sync(self, src: ImageView, dst: ImageView):
        if self._syncing:
            return
        self._syncing = True
        try:
            if dst.transform() != src.transform():
                dst.setTransform(src.transform())
                dst._update_mode()
            dst.horizontalScrollBar().setValue(src.horizontalScrollBar().value())
            dst.verticalScrollBar().setValue(src.verticalScrollBar().value())
            self.zoom_label.setText(f"{src.zoom() * 100:.0f}%")
        finally:
            self._syncing = False

    def _show_original(self, on: bool):
        if on == self.showing_original:
            return
        self.showing_original = on
        h, v = self.right.horizontalScrollBar().value(), self.right.verticalScrollBar().value()
        self.right.item.setPixmap(QPixmap.fromImage(self.img_orig if on else self.img_new))
        self.right.horizontalScrollBar().setValue(h)
        self.right.verticalScrollBar().setValue(v)
        self.cap_right.setText("<b style='color:#d97706'>原圖（放開返回壓縮後）</b>" if on else self.cap_right_text)

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Space and not e.isAutoRepeat():
            self._show_original(True)
            return
        super().keyPressEvent(e)

    def keyReleaseEvent(self, e):
        if e.key() == Qt.Key_Space and not e.isAutoRepeat():
            self._show_original(False)
            return
        super().keyReleaseEvent(e)
