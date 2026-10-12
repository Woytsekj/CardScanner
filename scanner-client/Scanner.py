import os
import sys
import cv2
import numpy as np
import requests
import traceback
import re
import time
from datetime import datetime, timezone

from PySide6.QtCore import Qt, QTimer, QEvent
from PySide6.QtGui import QImage, QPixmap, QFont, QKeyEvent, QIcon
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox, QPushButton,
    QSpinBox, QVBoxLayout, QWidget
)

import scanner_engine as eng


LOCAL_OWNER_FALLBACK = [
    x.strip() for x in os.environ.get(
        "MTG_SCANNER_OWNERS", "Me,Trade Binder,Friend"
    ).split(",") if x.strip()
]
if not LOCAL_OWNER_FALLBACK:
    LOCAL_OWNER_FALLBACK = ["Me"]


def fetch_people():
    if not eng.SERVER_URL or not eng.SCANNER_API_KEY:
        return LOCAL_OWNER_FALLBACK
    try:
        response = requests.get(
            f"{eng.SERVER_URL}/api/people",
            headers=eng.api_headers(),
            timeout=eng.REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        people = [str(x).strip() for x in data.get("people", []) if str(x).strip()]
        return people or LOCAL_OWNER_FALLBACK
    except Exception as exc:
        print(f"[PEOPLE] Could not refresh from server: {exc}")
        return LOCAL_OWNER_FALLBACK


def _compact_collector(value):
    value = str(value or "").strip().lower().replace(" ", "")
    if not value:
        return ""
    if "/" in value:
        value = value.split("/", 1)[0]
    if value.isdigit():
        return value.lstrip("0") or "0"
    return value


def _scryfall_artist_search(query, limit=40):
    """Search Scryfall by artist on explicit artist-mode/manual search.

    Results are cached by normalized artist query for the lifetime of this
    scanner process, so typing does not repeatedly hit Scryfall for the same
    query. This is only used for Manual Add artist mode; normal name/collector
    search remains entirely local.
    """
    q = " ".join(str(query or "").strip().split())
    if len(q) < 3:
        return []
    key = q.casefold()
    cache = getattr(_scryfall_artist_search, "cache", {})
    if key in cache:
        return cache[key]
    headers = {
        "User-Agent": eng.SCRYFALL_USER_AGENT,
        "Accept": "application/json",
    }
    try:
        response = requests.get(
            "https://api.scryfall.com/cards/search",
            params={"q": f'artist:"{q}"', "unique": "prints", "order": "name"},
            headers=headers,
            timeout=eng.SCRYFALL_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        results = []
        for raw in data.get("data", [])[:limit]:
            results.append({
                "id": raw.get("id"),
                "name": raw.get("name"),
                "set": str(raw.get("set") or "").upper(),
                "rarity": str(raw.get("rarity") or "").capitalize(),
                "tcg_id": raw.get("tcgplayer_id"),
                "collector_number": raw.get("collector_number"),
                "artist": raw.get("artist") or "",
                "image_url": ((raw.get("image_uris") or {}).get("normal") if isinstance(raw.get("image_uris"), dict) else None),
                "finishes": raw.get("finishes") or [],
            })
        cache[key] = results
        _scryfall_artist_search.cache = cache
        print(f"[MANUAL ARTIST] Scryfall returned {len(results)} printing(s) for artist query '{q}'.")
        return results
    except Exception as exc:
        print(f"[MANUAL ARTIST] Artist search failed for '{q}': {exc}")
        cache[key] = []
        _scryfall_artist_search.cache = cache
        return []


def search_local_cards(query, limit=40):
    """Search the full local printing catalog by name + optional collector number.

    Queries such as ``Forest 304`` are interpreted as card name ``Forest`` and
    collector number ``304``. A set symbol is optional; all matching printings
    are returned and ranked by name/collector exactness.
    """
    raw = str(query or "").strip()
    if not raw:
        return []

    tokens = raw.lower().replace("#", " ").split()
    number_query = None
    name_tokens = []
    for token in tokens:
        cleaned = re.sub(r"[^0-9a-z/\-]", "", token)
        if re.fullmatch(r"\d{1,4}(?:/\d{1,4})?", cleaned):
            number_query = _compact_collector(cleaned)
        elif cleaned:
            name_tokens.append(cleaned)
    name_query = " ".join(name_tokens).strip()

    results = []
    for card in eng.all_hashes_flat:
        name = str(card.get("name") or "")
        set_code = str(card.get("set") or "")
        collector = str(card.get("collector_number") or "")
        tcg_id = str(card.get("tcg_id") or "")
        ln = name.lower()
        ls = set_code.lower()
        lc = _compact_collector(collector)
        lt = tcg_id.lower()

        if name_query:
            nwords = [w for w in name_query.split() if w]
            if not all(w in ln for w in nwords):
                continue
        if number_query and lc != number_query:
            continue

        score = 0
        if name_query:
            if ln == name_query:
                score += 0
            elif ln.startswith(name_query):
                score += 5
            elif name_query in ln:
                score += 12
            else:
                score += 25
        if number_query:
            score += 0
        else:
            raw_l = raw.lower()
            if raw_l == ls:
                score += 30
            elif raw_l in ls:
                score += 40
            elif not name_query and raw_l == lt:
                score += 50
        results.append((score, len(name), ln, ls, lc, card))

    results.sort(key=lambda x: x[:5])
    return [x[5] for x in results[:limit]]


class ManualAddDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Manually Add Card")
        self.resize(1050, 720)
        self.setMinimumSize(900, 620)
        self.selected_card = None
        self.image_cache = {}
        self.setStyleSheet("""
            QDialog, QWidget { background:#111; color:#f1f1f1; }
            QLabel { color:#f1f1f1; }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPushButton {
                min-height:44px; border-radius:7px; border:1px solid #454545;
                background:#242424; color:#f1f1f1; padding:0 12px; font-size:17px;
            }
            QListWidget { background:#181818; border:1px solid #3a3a3a; font-size:16px; }
            QListWidget::item { padding:6px; min-height:54px; }
            QListWidget::item:selected { background:#295c4b; }
            QPushButton#primary { background:#208a5b; border-color:#2ca86f; font-weight:700; }
        """)
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(10)

        title = QLabel("Search your local card catalog")
        title.setFont(QFont("Sans Serif", 20, QFont.Weight.Bold))
        root.addWidget(title)

        search_row = QHBoxLayout()
        self.search_mode = QComboBox()
        self.search_mode.addItem("Card name / collector #", "name")
        self.search_mode.addItem("Artist", "artist")
        self.search_mode.setFixedWidth(250)
        search_row.addWidget(self.search_mode)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Type card name + collector number (e.g. Forest 304). Set symbol is optional…")
        self.search.textChanged.connect(self.schedule_search)
        self.search.returnPressed.connect(lambda: self.accept_selection())
        search_row.addWidget(self.search, 1)
        root.addLayout(search_row)
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(350)
        self.search_timer.timeout.connect(self.run_scheduled_search)
        self.search_mode.currentIndexChanged.connect(self.on_search_mode_changed)

        content = QHBoxLayout()
        self.results = QListWidget()
        self.results.setMinimumWidth(600)
        self.results.setMinimumHeight(360)
        self.results.itemDoubleClicked.connect(self.accept_selection)
        self.results.currentItemChanged.connect(self.show_selection)
        content.addWidget(self.results, 3)

        preview = QVBoxLayout()
        preview.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        self.image_label = QLabel("Select a card to preview its image.")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setFixedSize(300, 420)
        self.image_label.setStyleSheet("background:#181818; border:1px solid #3a3a3a; border-radius:8px; color:#aaa;")
        preview.addWidget(self.image_label, alignment=Qt.AlignCenter)
        self.selected_label = QLabel("No card selected.")
        self.selected_label.setWordWrap(True)
        self.selected_label.setAlignment(Qt.AlignCenter)
        self.selected_label.setFont(QFont("Sans Serif", 16, QFont.Weight.Bold))
        preview.addWidget(self.selected_label)
        content.addLayout(preview, 1)
        root.addLayout(content, 1)

        controls = QHBoxLayout()
        finish_block = QVBoxLayout()
        finish_block.addWidget(QLabel("Finish"))
        self.finish_combo = QComboBox()
        self.finish_combo.addItem("Normal", "normal")
        self.finish_combo.addItem("Foil", "foil")
        finish_block.addWidget(self.finish_combo)
        controls.addLayout(finish_block, 1)

        qty_block = QVBoxLayout()
        qty_block.addWidget(QLabel("Quantity"))
        self.qty_spin = QSpinBox()
        self.qty_spin.setRange(1, 9999)
        self.qty_spin.setValue(1)
        qty_block.addWidget(self.qty_spin)
        controls.addLayout(qty_block, 1)
        root.addLayout(controls)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.add_button = QPushButton("Add Card")
        self.add_button.setObjectName("primary")
        self.add_button.setEnabled(False)
        self.add_button.clicked.connect(self.accept_selection)
        buttons.addButton(self.add_button, QDialogButtonBox.AcceptRole)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.update_results("")
        self.search.setFocus()

    def on_search_mode_changed(self):
        if self.search_mode.currentData() == "artist":
            self.search.setPlaceholderText("Type artist name, e.g. Rob Alexander…")
        else:
            self.search.setPlaceholderText("Type card name + collector number (e.g. Forest 304). Set symbol is optional…")
        self.schedule_search(self.search.text())

    def schedule_search(self, text):
        self.search_timer.stop()
        self.search_timer.start()

    def run_scheduled_search(self):
        text = self.search.text()
        mode = self.search_mode.currentData()
        self.update_results(text, mode=mode)

    def update_results(self, text, mode=None):
        mode = mode or self.search_mode.currentData()
        self.results.clear()
        self.selected_card = None
        self.add_button.setEnabled(False)
        self.image_label.setPixmap(QPixmap())
        self.image_label.setText("Select a card to preview its image.")
        self.selected_label.setText("No card selected.")
        if not str(text).strip():
            return

        cards = _scryfall_artist_search(text, 40) if mode == "artist" else search_local_cards(text, 40)
        for card in cards:
            collector = card.get("collector_number") or "?"
            artist = card.get("artist")
            label = f"{card.get('name','Unknown')}  ·  {card.get('set','?')} #{collector}  ·  {card.get('rarity','?')}"
            if artist:
                label += f"  ·  {artist}"
            tcg = card.get("tcg_id")
            if tcg:
                label += f"  ·  TCGplayer {tcg}"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, card)
            self.results.addItem(item)
        if self.results.count():
            self.results.setCurrentRow(0)

    def _set_card_image(self, card):
        sid = str(card.get("id") or "").strip()
        if not sid:
            self.image_label.setPixmap(QPixmap())
            self.image_label.setText("No Scryfall ID/image available.")
            return
        if sid in self.image_cache:
            self._show_pixmap(self.image_cache[sid])
            return
        try:
            image_url = card.get("image_url")
            cached = eng._read_scryfall_cache() if hasattr(eng, "_read_scryfall_cache") else {}
            for prints in cached.values():
                if isinstance(prints, list):
                    for p in prints:
                        if isinstance(p, dict) and str(p.get("id")) == sid:
                            image_url = p.get("image_url")
                            break
                if image_url:
                    break
            if not image_url:
                sf = eng._request_scryfall_card(sid)
                image_url = ((sf.get("image_uris") or {}).get("normal") if isinstance(sf.get("image_uris"), dict) else None)
            if not image_url:
                self.image_label.setPixmap(QPixmap())
                self.image_label.setText("No image available.")
                return
            response = requests.get(image_url, headers={"User-Agent": eng.SCRYFALL_USER_AGENT}, timeout=eng.SCRYFALL_TIMEOUT)
            response.raise_for_status()
            pix = QPixmap()
            pix.loadFromData(response.content)
            self.image_cache[sid] = pix
            self._show_pixmap(pix)
        except Exception as exc:
            print(f"[MANUAL IMAGE] Could not load image for {sid}: {exc}")
            self.image_label.setPixmap(QPixmap())
            self.image_label.setText("Image unavailable.")

    def _show_pixmap(self, pix):
        if pix and not pix.isNull():
            self.image_label.setText("")
            self.image_label.setPixmap(pix.scaled(self.image_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))
        else:
            self.image_label.setPixmap(QPixmap())
            self.image_label.setText("No image available.")

    def show_selection(self, current, previous):
        card = current.data(Qt.UserRole) if current else None
        self.selected_card = card
        if not card:
            self.selected_label.setText("No card selected.")
            self.add_button.setEnabled(False)
            return
        collector = card.get("collector_number") or "?"
        self.selected_label.setText(
            f"Selected: {card.get('name','Unknown')} · {card.get('set','?')} #{collector} · {card.get('rarity','?')}"
        )
        self.add_button.setEnabled(True)
        self._set_card_image(card)

    def accept_selection(self):
        if not self.selected_card:
            return
        self.accept()

    def values(self):
        return self.selected_card, str(self.finish_combo.currentData()), int(self.qty_spin.value())


class ScannerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("MTG Card Scanner")
        self.resize(1280, 900)
        self.setMinimumSize(1000, 720)
        self.setStyleSheet("""
            QMainWindow, QWidget { background:#111; color:#f1f1f1; }
            QLabel { color:#f1f1f1; }
            QComboBox, QPushButton, QSpinBox, QDoubleSpinBox {
                min-height:48px; border-radius:7px; border:1px solid #454545;
                background:#242424; color:#f1f1f1; padding:0 14px; font-size:18px;
            }
            QComboBox:hover, QPushButton:hover { border-color:#6a6a6a; }
            QPushButton#primary { background:#208a5b; border-color:#2ca86f; font-weight:700; }
            QPushButton#danger { background:#5b2929; border-color:#8c4444; }
            QPushButton:disabled { color:#777; background:#1b1b1b; }
            QFrame#panel { background:#1a1a1a; border:1px solid #353535; border-radius:10px; }
        """)

        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_KeyCompression, False)
        self.capture = eng.open_camera()
        if not self.capture.isOpened():
            raise RuntimeError(
                f"Could not open camera source {eng.describe_camera_source()}. "
                "For phone mode, make sure scrcpy is running and the V4L2 device is readable."
            )

        eng.load_local_data()
        if eng.SERVER_URL and eng.SCANNER_API_KEY:
            eng.flush_pending_queue()
        self.latest_frame = None
        self.latest_warped = None
        self.pending_card = None
        self.pending_price = None
        self.pending_finish = "normal"
        self.pending_quantity = 1
        self.is_foil_mode = False
        self.scan_busy = False
        self.scan_block_until = 0.0
        self.space_latched = False
        self.preview_zoom = 1.0
        self.status_text = "Align a card in the frame, then press Scan Card."

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(12)

        self.video_label = QLabel(alignment=Qt.AlignCenter)
        self.video_label.setMinimumHeight(560)
        self.video_label.setStyleSheet("background:#090909; border:1px solid #3a3a3a; border-radius:10px;")
        root.addWidget(self.video_label, 1)

        panel = QFrame()
        panel.setObjectName("panel")
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(16, 14, 16, 14)
        panel_layout.setSpacing(10)

        self.status_label = QLabel(self.status_text)
        self.status_label.setWordWrap(True)
        self.status_label.setFont(QFont("Sans Serif", 21, QFont.Weight.Bold))
        self.status_label.setMinimumHeight(58)
        panel_layout.addWidget(self.status_label)

        controls = QHBoxLayout()
        controls.setSpacing(10)

        owner_block = QVBoxLayout()
        owner_title = QLabel("Store cards for")
        owner_title.setStyleSheet("color:#aaa; font-size:15px;")
        self.owner_combo = QComboBox()
        self.owner_combo.setEditable(False)
        owner_block.addWidget(owner_title)
        owner_block.addWidget(self.owner_combo)
        controls.addLayout(owner_block, 2)

        finish_block = QVBoxLayout()
        finish_title = QLabel("Finish")
        finish_title.setStyleSheet("color:#aaa; font-size:15px;")
        self.finish_combo = QComboBox()
        self.finish_combo.addItem("Normal", False)
        self.finish_combo.addItem("Foil", True)
        self.finish_combo.currentIndexChanged.connect(self.on_finish_changed)
        finish_block.addWidget(finish_title)
        finish_block.addWidget(self.finish_combo)
        controls.addLayout(finish_block, 1)

        zoom_block = QVBoxLayout()
        zoom_title = QLabel("Preview zoom")
        zoom_title.setStyleSheet("color:#aaa; font-size:15px;")
        zoom_row = QHBoxLayout()
        self.zoom_label = QLabel("1.0×")
        self.zoom_label.setMinimumWidth(55)
        self.zoom_spin = QDoubleSpinBox()
        self.zoom_spin.setRange(1.0, 3.0)
        self.zoom_spin.setSingleStep(0.1)
        self.zoom_spin.setValue(1.0)
        self.zoom_spin.valueChanged.connect(self.on_zoom_changed)
        zoom_row.addWidget(self.zoom_label)
        zoom_row.addWidget(self.zoom_spin, 1)
        zoom_block.addWidget(zoom_title)
        zoom_block.addLayout(zoom_row)
        controls.addLayout(zoom_block, 1)

        panel_layout.addLayout(controls)

        buttons = QHBoxLayout()
        buttons.setSpacing(10)
        self.refresh_people_button = QPushButton("Refresh People")
        self.refresh_people_button.clicked.connect(self.refresh_people)
        buttons.addWidget(self.refresh_people_button)

        self.manual_add_button = QPushButton("Manual Add")
        self.manual_add_button.clicked.connect(self.manual_add_card)
        buttons.addWidget(self.manual_add_button)

        self.zoom_out_button = QPushButton("− Zoom")
        self.zoom_out_button.clicked.connect(lambda: self.change_zoom(-0.1))
        buttons.addWidget(self.zoom_out_button)
        self.zoom_in_button = QPushButton("+ Zoom")
        self.zoom_in_button.clicked.connect(lambda: self.change_zoom(0.1))
        buttons.addWidget(self.zoom_in_button)

        self.scan_button = QPushButton("Scan Card")
        self.scan_button.setObjectName("primary")
        self.scan_button.clicked.connect(self.scan_card)
        buttons.addWidget(self.scan_button, 2)

        self.save_button = QPushButton("Save Card")
        self.save_button.setObjectName("primary")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save_pending)
        buttons.addWidget(self.save_button, 1)

        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setObjectName("danger")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_pending)
        buttons.addWidget(self.cancel_button, 1)
        panel_layout.addLayout(buttons)

        self.hotkey_label = QLabel("Hotkeys: Space = Scan · Y = Save · N = Cancel · F = Normal/Foil · +/− = Zoom")
        self.hotkey_label.setStyleSheet("color:#bdbdbd; font-size:14px;")
        panel_layout.addWidget(self.hotkey_label)

        self.connection_label = QLabel()
        self.connection_label.setStyleSheet("color:#9ad8b8; font-size:14px;")
        panel_layout.addWidget(self.connection_label)
        root.addWidget(panel)

        self.refresh_people()
        self.update_connection_status()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_frame)
        self.timer.start(30)

    def refresh_people(self):
        current = self.owner_combo.currentText()
        people = fetch_people()
        self.owner_combo.blockSignals(True)
        self.owner_combo.clear()
        self.owner_combo.addItems(people)
        if current in people:
            self.owner_combo.setCurrentText(current)
        self.owner_combo.blockSignals(False)
        self.set_status(f"Owner list refreshed — storing scans for {self.owner_combo.currentText()}.")

    def update_connection_status(self):
        if eng.SERVER_URL and eng.SCANNER_API_KEY:
            mode = "Cloudflare Access" if eng.CF_ACCESS_CLIENT_ID and eng.CF_ACCESS_CLIENT_SECRET and eng.SERVER_URL.startswith("https://") else "Server"
            self.connection_label.setText(f"{mode} mode: {eng.SERVER_URL} · offline queue enabled")
        else:
            self.connection_label.setText("Local mode: scans stay on this computer")
            self.connection_label.setStyleSheet("color:#c4c4c4; font-size:14px;")

    def set_status(self, text):
        self.status_text = str(text)
        self.status_label.setText(self.status_text)

    def on_finish_changed(self):
        self.is_foil_mode = bool(self.finish_combo.currentData())
        if self.pending_card is None:
            self.set_status("Finish: Foil." if self.is_foil_mode else "Finish: Normal.")

    def on_zoom_changed(self, value):
        self.preview_zoom = float(value)
        self.zoom_label.setText(f"{self.preview_zoom:.1f}×")

    def change_zoom(self, delta):
        value = max(1.0, min(3.0, self.preview_zoom + float(delta)))
        self.zoom_spin.setValue(round(value, 1))

    def update_frame(self):
        ret, frame = self.capture.read()
        if not ret:
            self.set_status("Camera frame unavailable. Check the camera connection.")
            return
        self.latest_frame = frame.copy()
        display = frame.copy()
        warped, corners = self.detect_card(display)
        self.latest_warped = warped
        if corners is not None and self.pending_card is None:
            cv2.polylines(display, [np.int32(corners)], True, (80, 210, 150), 4)
        if self.pending_card is not None:
            cv2.rectangle(display, (10, 10), (display.shape[1]-10, display.shape[0]-10), (70, 180, 230), 5)
        display = self.apply_preview_zoom(display)
        self.show_cv_frame(display)

    def apply_preview_zoom(self, frame):
        z = float(self.preview_zoom)
        if z <= 1.01:
            return frame
        h, w = frame.shape[:2]
        crop_w = max(2, int(w / z))
        crop_h = max(2, int(h / z))
        x1 = max(0, (w - crop_w) // 2)
        y1 = max(0, (h - crop_h) // 2)
        cropped = frame[y1:y1+crop_h, x1:x1+crop_w]
        return cv2.resize(cropped, (w, h), interpolation=cv2.INTER_LINEAR)

    def detect_card(self, frame):
        """Detect the actual card rectangle instead of the surrounding tray/foam.

        The old detector chose the largest external contour after heavy morphology.
        In this physical setup that often connected the card, sleeve shadow, and foam
        into one large blob. This detector looks for real 4-corner quadrilaterals from
        Canny/threshold edges, scores rectangularity + MTG aspect ratio + center position,
        and prefers a strong inner card candidate over a larger enclosing contour.
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        # Light denoise only; large morphology made the tray become one contour.
        blur = cv2.GaussianBlur(gray, (5, 5), 0)
        edges = cv2.Canny(blur, 45, 135)
        edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)

        # A modest adaptive threshold is a fallback for dark/sleeved borders.
        thresh = cv2.adaptiveThreshold(blur, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY_INV, 31, 7)
        mask = cv2.bitwise_or(edges, thresh)

        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        h, w = frame.shape[:2]
        frame_area = float(h * w)
        frame_center = np.array([w / 2.0, h / 2.0], dtype=np.float32)
        frame_diag = max(1.0, float(np.hypot(w, h)))
        target_aspect = 63.0 / 88.0  # width / height for a normal MTG card
        candidates = []

        def angle_cos(a, b, c):
            ba = a - b
            bc = c - b
            den = max(1e-6, float(np.linalg.norm(ba) * np.linalg.norm(bc)))
            return abs(float(np.dot(ba, bc)) / den)

        for contour in contours:
            area = float(cv2.contourArea(contour))
            if area < frame_area * 0.06 or area > frame_area * 0.75:
                continue
            peri = cv2.arcLength(contour, True)
            if peri <= 0:
                continue
            approx = cv2.approxPolyDP(contour, 0.018 * peri, True)
            if len(approx) != 4 or not cv2.isContourConvex(approx):
                # A second approximation is useful on slightly rounded card sleeves.
                approx = cv2.approxPolyDP(contour, 0.028 * peri, True)
            if len(approx) != 4 or not cv2.isContourConvex(approx):
                continue

            pts = eng.order_points(approx.reshape(4, 2).astype(np.float32))
            tl, tr, br, bl = pts
            width_top = float(np.linalg.norm(tr - tl))
            width_bottom = float(np.linalg.norm(br - bl))
            height_left = float(np.linalg.norm(bl - tl))
            height_right = float(np.linalg.norm(br - tr))
            card_w = (width_top + width_bottom) / 2.0
            card_h = (height_left + height_right) / 2.0
            if card_w <= 10 or card_h <= 10:
                continue
            aspect = min(card_w, card_h) / max(card_w, card_h)
            # Expected width/height ratio is 0.716; accept a little perspective.
            if aspect < 0.63 or aspect > 0.80:
                continue
            if card_w >= card_h:
                # Landscape candidate is not our target phone orientation.
                continue

            polygon_area = float(cv2.contourArea(pts.astype(np.float32)))
            box_area = max(1.0, card_w * card_h)
            rectangularity = min(1.0, polygon_area / box_area)
            if rectangularity < 0.72:
                continue

            angles = [angle_cos(pts[(i - 1) % 4], pts[i], pts[(i + 1) % 4]) for i in range(4)]
            right_angle_score = max(0.0, 1.0 - (sum(angles) / 4.0) / 0.30)
            center = pts.mean(axis=0)
            center_score = max(0.0, 1.0 - float(np.linalg.norm(center - frame_center)) / frame_diag * 2.2)
            area_ratio = area / frame_area
            # Prefer realistic card sizes, but do not hard-code one camera resolution.
            size_score = 1.0 if 0.12 <= area_ratio <= 0.48 else max(0.0, 1.0 - abs(area_ratio - 0.30) / 0.45)
            aspect_score = max(0.0, 1.0 - abs((card_w / card_h) - target_aspect) / 0.18)

            # Strong edges around the card perimeter beat soft tray/foam boundaries.
            edge_band = cv2.Canny(gray, 60, 160)
            edge_vals = []
            for a, b in zip(pts, np.roll(pts, -1, axis=0)):
                steps = max(20, int(np.linalg.norm(b - a)))
                xs = np.linspace(a[0], b[0], steps).astype(np.int32)
                ys = np.linspace(a[1], b[1], steps).astype(np.int32)
                valid = (xs >= 0) & (xs < w) & (ys >= 0) & (ys < h)
                if np.any(valid):
                    edge_vals.append(float(edge_band[ys[valid], xs[valid]].mean()) / 255.0)
            edge_score = min(1.0, (sum(edge_vals) / len(edge_vals)) * 2.2) if edge_vals else 0.0

            score = (
                rectangularity * 0.27
                + right_angle_score * 0.17
                + aspect_score * 0.19
                + edge_score * 0.19
                + center_score * 0.10
                + size_score * 0.08
            )
            candidates.append({
                "score": score,
                "area": area,
                "pts": pts,
                "center": center,
                "rectangularity": rectangularity,
            })

        if not candidates:
            # Last-resort fallback: use a tighter morphology pass, but still score
            # quadrilaterals rather than blindly taking the external contour.
            kernel = np.ones((5, 5), np.uint8)
            fallback = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=2)
            fallback_contours, _ = cv2.findContours(fallback, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
            for contour in fallback_contours:
                area = float(cv2.contourArea(contour))
                if area < frame_area * 0.08 or area > frame_area * 0.70:
                    continue
                peri = cv2.arcLength(contour, True)
                approx = cv2.approxPolyDP(contour, 0.02 * peri, True)
                if len(approx) != 4:
                    continue
                pts = eng.order_points(approx.reshape(4, 2).astype(np.float32))
                tl, tr, br, bl = pts
                cw = (np.linalg.norm(tr-tl) + np.linalg.norm(br-bl)) / 2
                ch = (np.linalg.norm(bl-tl) + np.linalg.norm(br-tr)) / 2
                if ch <= cw * 1.25:
                    continue
                aspect_score = max(0.0, 1.0 - abs((cw/ch) - target_aspect) / 0.20)
                candidates.append({
                    "score": aspect_score * 0.65 + (area / frame_area) * 0.25,
                    "area": area,
                    "pts": pts,
                    "center": pts.mean(axis=0),
                    "rectangularity": 0.75,
                })

        if not candidates:
            # Rounded-corner/sleeved-card fallback: a real card contour may not simplify
            # cleanly to four points. Use a rotated rectangle ONLY as a last resort,
            # and only for candidates with plausible MTG-card geometry and center position.
            for contour in contours:
                area = float(cv2.contourArea(contour))
                area_ratio = area / frame_area
                if area_ratio < 0.10 or area_ratio > 0.58:
                    continue
                rect = cv2.minAreaRect(contour)
                (cx, cy), (rw, rh), _ = rect
                if rw <= 0 or rh <= 0:
                    continue
                cw, ch = (rw, rh) if rw < rh else (rh, rw)
                ar = cw / max(ch, 1.0)
                if not (0.62 <= ar <= 0.82):
                    continue
                center = np.array([cx, cy], dtype=np.float32)
                center_score = max(0.0, 1.0 - float(np.linalg.norm(center - frame_center)) / frame_diag * 2.0)
                aspect_score = max(0.0, 1.0 - abs(ar - target_aspect) / 0.18)
                size_score = 1.0 if 0.14 <= area_ratio <= 0.52 else max(0.0, 1.0 - abs(area_ratio - 0.32) / 0.40)
                pts = eng.order_points(cv2.boxPoints(rect).astype(np.float32))
                candidates.append({
                    "score": aspect_score * 0.45 + center_score * 0.30 + size_score * 0.25,
                    "area": area,
                    "pts": pts,
                    "center": center,
                    "rectangularity": 0.70,
                })
                # Keep only a few fallback candidates; normal contour detection is preferred.
                if len(candidates) >= 5:
                    break

        if not candidates:
            return None, None

        candidates.sort(key=lambda c: c["score"], reverse=True)
        best = candidates[0]

        # If the top candidate is a large enclosing shape and a good smaller
        # quadrilateral sits inside it, prefer the inner candidate.
        for alt in candidates[1:8]:
            if alt["area"] >= best["area"] * 0.45:
                continue
            inside = cv2.pointPolygonTest(best["pts"].astype(np.float32), tuple(alt["center"]), False) >= 0
            if inside and alt["score"] >= best["score"] - 0.06:
                best = alt
                break

        best_pts = best["pts"]
        width = max(2, int(round((np.linalg.norm(best_pts[1]-best_pts[0]) + np.linalg.norm(best_pts[2]-best_pts[3])) / 2.0)))
        height = max(2, int(round((np.linalg.norm(best_pts[3]-best_pts[0]) + np.linalg.norm(best_pts[2]-best_pts[1])) / 2.0)))
        # Normalize every card to a consistent portrait size for OCR/hash matching.
        target_w = 720
        target_h = max(960, int(round(target_w * height / max(width, 1))))
        dst = np.float32([[0,0],[target_w-1,0],[target_w-1,target_h-1],[0,target_h-1]])
        matrix = cv2.getPerspectiveTransform(best_pts, dst)
        warped = cv2.warpPerspective(frame, matrix, (target_w, target_h), flags=cv2.INTER_CUBIC)
        return warped, best_pts

    def show_cv_frame(self, frame):
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w = rgb.shape[:2]
        qimg = QImage(rgb.data, w, h, 3*w, QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg)
        self.video_label.setPixmap(pixmap.scaled(self.video_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def scan_card(self):
        if self.scan_busy or self.pending_card is not None:
            return
        if time.monotonic() < self.scan_block_until:
            return
        self.scan_block_until = time.monotonic() + 0.65
        if self.latest_warped is None or not hasattr(self.latest_warped, "shape"):
            self.set_status("No card detected. Place a single card inside the frame.")
            return
        self.scan_busy = True
        self.space_latched = True
        self.scan_button.blockSignals(True)
        self.scan_button.setEnabled(False)
        self.set_status("Analyzing card…")
        QApplication.processEvents()
        try:
            match = eng.identify_card_hybrid(self.latest_warped, self.is_foil_mode)
            if not match:
                self.set_status("No match found. Adjust the card and try again, or use Manual Add.")
                return
            tcg_raw = match.get("tcg_id")
            tcg_id = None
            if tcg_raw:
                try:
                    tcg_id = str(int(float(tcg_raw)))
                except (TypeError, ValueError):
                    pass
            print(f"[CARD] {match.get('name')} · {match.get('set')} #{match.get('collector_number','?')} · TCGplayer ID={tcg_id or 'MISSING'}")
            prices = eng.get_remote_prices(tcg_id) if tcg_id else None
            card_pricing = None
            if prices is None and match.get("id"):
                card_pricing = eng.get_remote_card_pricing(match)
                if card_pricing:
                    prices = {"normal": card_pricing.get("normal"), "foil": card_pricing.get("foil")}
                    tcg_id = card_pricing.get("tcgplayer_id") or tcg_id
            if prices is None:
                prices = eng.tcg_prices.get(tcg_id, {}) if tcg_id else {}

            finish = eng.normalized_finish(self.is_foil_mode)
            finishes = set(str(f).lower() for f in (card_pricing or {}).get("finishes", []))
            # Some exact printings are foil-only (for example etched commander printings).
            # Treat an explicit foil-only Scryfall printing as foil so it cannot be stored as normal.
            if finish == "normal" and finishes and "nonfoil" not in finishes and ("foil" in finishes or "etched" in finishes):
                finish = "foil"
                self.set_status("Foil-only printing detected; using Foil price and inventory bucket.")
                QApplication.processEvents()

            chosen = prices.get(finish)
            if chosen is None:
                chosen = prices.get("normal") if finish == "foil" else prices.get("foil")
            price = f"{chosen:.2f}" if isinstance(chosen, (float, int)) else "N/A"
            self.pending_card = match
            self.pending_price = price
            cn = f" #{match.get('collector_number')}" if match.get('collector_number') else ""
            self.set_status(f"Found {match['name']} · {finish.title()} · {match['set']}{cn} · ${price}.\nConfirm with Save Card or Cancel.")
            self.save_button.setEnabled(True)
            self.cancel_button.setEnabled(True)
        except Exception as exc:
            print("[SCAN ERROR]", exc)
            traceback.print_exc()
            self.set_status(f"Scanner error: {exc}. Press Space to try again.")
        finally:
            self.scan_busy = False
            # Keep Space latched until the physical key-release event arrives.
            # This prevents queued keypresses generated during OCR from starting
            # another recognition pass.
            # Do not allow keypresses that accumulated while EasyOCR was
            # running to trigger another scan immediately after completion.
            # The UI thread is blocked during recognition, so a held Space can
            # otherwise be delivered after scan_busy becomes False.
            self.scan_block_until = time.monotonic() + 1.10
            if self.pending_card is None:
                self.scan_button.setEnabled(True)
                self.scan_button.blockSignals(False)

    def manual_add_card(self):
        if self.pending_card is not None:
            QMessageBox.information(self, "Pending card", "Save or cancel the current pending card before manually adding another card.")
            return
        dialog = ManualAddDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        card, finish, quantity = dialog.values()
        if not card:
            return

        self.scan_busy = True
        try:
            self.set_status(f"Looking up price for {card.get('name', 'card')}…")
            QApplication.processEvents()
            price_data = eng.get_remote_card_pricing(card) if eng.SERVER_URL and eng.SCANNER_API_KEY else None
            if price_data is None:
                tcg_id = card.get("tcg_id")
                price_data = eng.get_remote_prices(str(tcg_id)) if tcg_id else None
            if price_data is None:
                tcg_id = card.get("tcg_id")
                price_data = eng.tcg_prices.get(str(tcg_id), {}) if tcg_id else {}

            chosen = price_data.get(finish) if price_data else None
            if chosen is None and price_data:
                chosen = price_data.get("normal") if finish == "foil" else price_data.get("foil")
            price = f"{chosen:.2f}" if isinstance(chosen, (float, int)) else "N/A"
            self.pending_card = card
            self.pending_price = price
            self.pending_finish = finish
            self.pending_quantity = quantity
            cn = f" #{card.get('collector_number')}" if card.get('collector_number') else ""
            price_text = f"${price}" if price != "N/A" else "Price unavailable"
            self.set_status(
                f"Manual card: {card.get('name')} · {finish.title()} · {card.get('set')}{cn} · Qty {quantity} · {price_text}.\n"
                "Confirm with Save Card or Cancel."
            )
            self.save_button.setEnabled(True)
            self.cancel_button.setEnabled(True)
        except Exception as exc:
            traceback.print_exc()
            self.set_status(f"Manual add error: {exc}")
        finally:
            self.scan_busy = False

    def save_pending(self):
        if not self.pending_card:
            return
        owner = self.owner_combo.currentText().strip() or "Me"
        finish = getattr(self, "pending_finish", eng.normalized_finish(self.is_foil_mode))
        quantity = max(1, int(getattr(self, "pending_quantity", 1)))
        result = eng.save_scan(self.pending_card, self.pending_price, owner, finish, quantity=quantity)
        if result.get("mode") == "server":
            self.set_status(f"Saved to server · {self.pending_card['name']} · {finish.title()} · {owner} · +{quantity} (total {result.get('quantity','?')})")
        elif result.get("mode") == "queued":
            self.set_status(f"Server unavailable — queued locally. {self.pending_card['name']} · {owner} · +{quantity}")
        else:
            self.set_status(f"Saved locally · {self.pending_card['name']} · {finish.title()} · {owner} · +{quantity}")
        self.clear_pending()

    def cancel_pending(self):
        self.clear_pending()
        self.set_status("Scan cancelled. Ready for the next card.")

    def clear_pending(self):
        self.pending_card = None
        self.pending_price = None
        self.pending_finish = eng.normalized_finish(self.is_foil_mode)
        self.pending_quantity = 1
        self.save_button.setEnabled(False)
        self.cancel_button.setEnabled(False)
        self.scan_button.blockSignals(False)
        self.scan_button.setEnabled(True)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.KeyRelease:
            if event.key() == Qt.Key_Space:
                self.space_latched = False
            return super().eventFilter(obj, event)
        if event.type() == QEvent.KeyPress:
            if event.isAutoRepeat():
                return True
            # Never let the scanner's global hotkeys interfere with a modal
            # dialog. This is important for Manual Add: spaces, y/n/f, etc.
            # must behave as normal text while typing in the search field.
            active_modal = QApplication.activeModalWidget()
            if active_modal is not None:
                return super().eventFilter(obj, event)

            # Also protect text-entry widgets in the main window.
            if isinstance(obj, QLineEdit):
                return super().eventFilter(obj, event)

            key = event.key()
            modifiers = event.modifiers()
            # Ignore modified shortcuts (Ctrl/Alt/Meta) so standard desktop
            # shortcuts remain available. Shift is harmless for these keys.
            if not (modifiers & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)):
                if key == Qt.Key_Space:
                    # Latch Space for the entire recognition operation. Key events
                    # can queue while EasyOCR blocks the Qt event loop; without this,
                    # one physical press can become several scans when the loop wakes.
                    if self.space_latched or self.scan_busy or time.monotonic() < self.scan_block_until:
                        self.space_latched = True
                        return True
                    self.space_latched = True
                    self.scan_card()
                    return True
                if key in (Qt.Key_Y, Qt.Key_Return, Qt.Key_Enter):
                    if self.pending_card is not None:
                        self.save_pending()
                        return True
                if key == Qt.Key_N:
                    if self.pending_card is not None:
                        self.cancel_pending()
                        return True
                if key == Qt.Key_F:
                    if self.pending_card is None and not self.scan_busy:
                        next_index = 1 if not self.is_foil_mode else 0
                        self.finish_combo.setCurrentIndex(next_index)
                        return True
                if key in (Qt.Key_Plus, Qt.Key_Equal):
                    if not self.scan_busy:
                        self.change_zoom(0.1)
                        return True
                if key in (Qt.Key_Minus, Qt.Key_Underscore):
                    if not self.scan_busy:
                        self.change_zoom(-0.1)
                        return True
        return super().eventFilter(obj, event)

    def closeEvent(self, event):
        self.capture.release()
        event.accept()


def main():
    app = QApplication(sys.argv)
    try:
        window = ScannerWindow()
        app.installEventFilter(window)
    except Exception as exc:
        QMessageBox.critical(None, "MTG Scanner", str(exc))
        return 1
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
