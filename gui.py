# -*- coding: utf-8 -*-
# pylint: disable=C0114,C0115,C0116,W3101,C0412,W0718,I1101,E0611
"""
Lepu Pulse Oximeter BLE app
"""
import os
import os.path as osp
import sys
import json
import logging
import asyncio
from collections import deque
from datetime import datetime
from functools import wraps
from enum import IntEnum
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget,
                             QVBoxLayout, QHBoxLayout,
                             QLabel, QFrame, QGraphicsDropShadowEffect)
from PyQt6.QtCore import (Qt, QTimer, pyqtSignal, QObject, pyqtProperty,
                          QPropertyAnimation, QRect)
from PyQt6.QtGui import (QPainter, QPen, QColor, QLinearGradient,
                         QBrush, QPainterPath, QIcon, QPixmap)
from PyQt6.QtSvgWidgets import QSvgWidget
from PyQt6.QtSvg import QSvgRenderer
import qasync
from bleak import (BleakScanner, BleakClient, BLEDevice, AdvertisementData,
                   BleakGATTCharacteristic)
from protocol import split_packets, parse_protocol

# ── BLE Constants ────────────────────────────────────────────────────────────
NAME_PREFIX = "PF-10AW"
NUS_TX_CHAR = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
NUS_RX_CHAR = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"

# ── Theme ─────────────────────────────────────────────────
C_BG = "#F0F4F8"
C_CARD = "#FFFFFF"
C_TEAL = "#1ABCA8"       # main
C_TEAL_DARK = "#0E9E8C"
C_BLUE = "#2979FF"       # SpO2
C_ORANGE = "#FF8C00"       # PI
C_GREEN = "#00C853"       # PR
C_TEXT = "#1A2B3C"
C_SUBTEXT = "#7A8FA6"
C_BORDER = "#E2EAF0"
C_PPG = "#1ABCA8"
C_DANGER = "#FF4444"
# C_BLE_ON = "#26D2BE"
C_BLE_ON = "#0082FC"
C_BLE_OFF = "#BDC2CE"
C_BTN_TRUE = "#2979FF"
C_BTN_FALSE = "#E5E5EA"


RESC = osp.join(osp.dirname(__file__), 'resc')

LOGGER = logging.getLogger("app")


def set_windows_appusermodelid(user_id: str):
    """Make sure correct icon is used on Windows 7 taskbar"""
    if os.name == 'nt':
        from ctypes import windll
        try:
            return windll.shell32.SetCurrentProcessExplicitAppUserModelID(user_id)
        except AttributeError:
            return "SetCurrentProcessExplicitAppUserModelID not found"

# ── PPG Wave Widget ───────────────────────────────────────────────────────────


class PPGWidget(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(160)
        self.wave_data = deque(maxlen=300)
        self.setStyleSheet("background: transparent;")

    def push(self, points: list):
        self.wave_data.extend(points)
        self.update()

    def paintEvent(self, event):  # pylint: disable=C0103, W0613
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        pad_l, pad_r, pad_t, pad_b = 8, 8, 12, 12

        # 背景
        painter.fillRect(self.rect(), QColor(C_CARD))

        data = list(self.wave_data)
        if len(data) < 2:
            painter.setPen(QColor(C_SUBTEXT))
            painter.drawText(self.rect(),
                             Qt.AlignmentFlag.AlignCenter,
                             "等待波形数据...")
            return

        # 网格线
        grid_pen = QPen(QColor("#EEF3F8"), 1, Qt.PenStyle.DotLine)
        painter.setPen(grid_pen)
        for i in range(1, 4):
            y = pad_t + (h - pad_t - pad_b) * i / 4
            painter.drawLine(pad_l, int(y), w - pad_r, int(y))

        # wave path
        plot_w = w - pad_l - pad_r
        plot_h = h - pad_t - pad_b
        step = plot_w / max(len(data) - 1, 1)

        path = QPainterPath()
        for i, val in enumerate(data):
            x = pad_l + i * step
            y = pad_t + plot_h - (val / 127.0) * plot_h
            if i == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)

        # 渐变填充
        grad = QLinearGradient(0, pad_t, 0, h - pad_b)
        grad.setColorAt(0.0, QColor(26, 188, 168, 100))
        grad.setColorAt(1.0, QColor(26, 188, 168, 0))

        fill_path = QPainterPath(path)
        fill_path.lineTo(pad_l + (len(data) - 1) * step, h - pad_b)
        fill_path.lineTo(pad_l, h - pad_b)
        fill_path.closeSubpath()
        painter.fillPath(fill_path, QBrush(grad))

        # wave line
        wave_pen = QPen(QColor(C_PPG), 2.0, Qt.PenStyle.SolidLine,
                        Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(wave_pen)
        painter.drawPath(path)

        painter.end()

# ── Card Widget ───────────────────────────────────────────────────────────


class MetricCard(QFrame):
    def __init__(self, title, unit, color, parent=None, icon=None):
        super().__init__(parent)
        self.color = color
        self.setFixedHeight(150)
        self.setStyleSheet('QFrame {'
                           f'background: {C_CARD};'
                           'border-radius: 16px;'
                           f'border: 1.5px solid {C_BORDER};'
                           '}'
                           'QLabel {'
                           "border: none;"
                           '}')
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(18)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 18))
        self.setGraphicsEffect(shadow)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(2)

        # 标题行
        title_row = QHBoxLayout()
        if icon is None:
            dot = QLabel("●")
            dot.setStyleSheet(f"color: {color};"
                              "font-size: 10px;")
            title_row.addWidget(dot)
        elif icon.endswith('.svg'):
            icon_svg = QSvgWidget(icon)
            icon_svg.setFixedSize(22, 20)
            icon_svg.setStyleSheet("background: transparent;")
            title_row.addWidget(icon_svg)
        else:
            icon_label = QLabel()
            icon_label.setPixmap(QPixmap(icon))
            icon_label.setStyleSheet("border: none;")
            title_row.addWidget(icon_label)
        self.title_lbl = QLabel(title)
        self.title_lbl.setStyleSheet(f"color: {C_SUBTEXT};"
                                     "font-size: 13px;"
                                     "font-weight: 600;")

        title_row.addWidget(self.title_lbl)
        title_row.addStretch()
        layout.addLayout(title_row)

        # 数值
        self.value_lbl = QLabel("--")
        self.value_lbl.setStyleSheet(f"color: {color};"
                                     "font-size: 38px;"
                                     "font-weight: 700;")
        layout.addWidget(self.value_lbl)

        # 单位
        self.unit_lbl = QLabel(unit)
        self.unit_lbl.setStyleSheet(f"color: {C_SUBTEXT};"
                                    "font-size: 12px;")
        layout.addWidget(self.unit_lbl)

    def set_value(self, val):
        self.value_lbl.setText(str(val) if val is not None else "--")

    def set_unit(self, val):
        self.unit_lbl.setText(str(val) if val is not None else "--")

# ── Battery ────────────────────────────────────────────────────────────────


class BatteryWidget(QLabel):
    LEVELS = ["🪫", "🔋", "🔋", "🔋"]
    TEXTS = ["25%", "50%", "75%", "100%"]

    def set_level(self, level: int):
        if 0 <= level <= 3:
            self.setText(f"{self.LEVELS[level]} {self.TEXTS[level]}")
            self.setStyleSheet(f"color: {C_TEAL}; font-size: 13px;")

# ── BLE Worker ──────────────────────────────────────────────────────────────


class BleSt(IntEnum):
    IDLE = 0
    SCANNING = 1
    CONNECTING = 2
    CONNECTED = 3
    DISCONNECTED = 4


class BleWorker(QObject):
    data_received = pyqtSignal(dict)
    status_changed = pyqtSignal(dict)

    def __init__(self):
        super().__init__()
        self._running = True
        self._client = None
        self._main_task = None
        self._loop = None

    def stop(self):
        self._running = False
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._cancel_tasks)

    def _cancel_tasks(self):
        if self._client and self._client.is_connected:
            self._loop.create_task(self._client.disconnect())
        if self._main_task and not self._main_task.done():
            self._main_task.cancel()

    def filter(self, device: BLEDevice, ad: AdvertisementData) -> bool:  # pylint: disable=unused-argument
        return device.name and device.name.startswith(NAME_PREFIX)

    def on_notify(self, character: BleakGATTCharacteristic, data: bytearray):  # pylint: disable=unused-argument
        for pack in split_packets(data):
            ret = parse_protocol(pack)
            if ret is not None:
                # print(ret)
                self.data_received.emit(ret)

    async def run(self):
        self._loop = asyncio.get_running_loop()
        self._main_task = asyncio.current_task()
        self._running = True
        while self._running:
            self.status_changed.emit({'status': BleSt.SCANNING})
            try:
                device = await BleakScanner.find_device_by_filter(
                    self.filter,
                    timeout=10
                )
                if device is None:
                    self.status_changed.emit({'status': BleSt.IDLE})
                    await asyncio.sleep(3)
                    continue
                if not self._running:
                    break

                self.status_changed.emit({'status': BleSt.CONNECTING})
                event = asyncio.Event()

                def on_disconnect(c):
                    event.set()
                    self.status_changed.emit({'status': BleSt.DISCONNECTED})

                async with BleakClient(device, disconnected_callback=on_disconnect) as client:
                    self._client = client
                    self.status_changed.emit({'status': BleSt.CONNECTED,
                                              'name': device.name,
                                              'address': device.address})
                    await client.start_notify(NUS_TX_CHAR, self.on_notify)
                    await event.wait()
                    try:
                        await client.stop_notify(NUS_TX_CHAR)
                    except Exception:
                        pass
            except asyncio.CancelledError:
                self._running = False
                break
            except Exception as e:
                self.status_changed.emit({'status': BleSt.SCANNING,
                                          'error': str(e)})
                try:
                    await asyncio.sleep(3)
                except asyncio.CancelledError:
                    self._running = False
                    break


class DynamicSvgWidget(QWidget):
    def __init__(self, path, parent=None):
        super().__init__(parent)
        with open(path, encoding='utf-8') as file:
            svg_data = file.read()
        self.svg = svg_data
        self.color = "#26D2BE"

    def set_icon_color(self, color_str):
        self.color = color_str
        self.update()  # 触发重绘

    def paintEvent(self, event):  # pylint: disable=C0103
        painter = QPainter(self)
        svg = self.svg.replace('currentColor', self.color)
        renderer = QSvgRenderer(svg.encode('utf-8'))
        renderer.render(painter)


class Switch(QWidget):
    toggled = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._checked = False
        self._offset = 0.0
        self.setMinimumSize(40, 20)
        self.anim = QPropertyAnimation(self, b"offset")
        self.anim.setDuration(180)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # -------- property --------
    def get_offset(self):
        return self._offset

    def set_offset(self, v):
        self._offset = v
        self.update()

    offset = pyqtProperty(float, fget=get_offset, fset=set_offset)

    # -------- toggle --------
    def mousePressEvent(self, event):  # pylint: disable=C0103,W0613
        self._checked = not self._checked
        self.toggled.emit(self._checked)
        self.anim.stop()

        start = self._offset
        end = 1.0 if self._checked else 0.0

        self.anim.setStartValue(start)
        self.anim.setEndValue(end)
        self.anim.start()

    # -------- paint --------
    def paintEvent(self, event):  # pylint: disable=C0103,W0613
        w = self.width()
        h = self.height()

        radius = h / 2
        margin = h * 0.1
        knob_size = h - margin * 2

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 背景
        bg = QColor(C_BTN_TRUE) if self._checked else QColor(C_BTN_FALSE)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)

        painter.drawRoundedRect(0, 0, w, h, radius, radius)

        # thumb 位置（关键：按比例）
        x = margin + self._offset * (w - knob_size - margin * 2)
        y = margin

        painter.setBrush(QColor("white"))
        painter.drawEllipse(QRect(int(x),
                                  int(y),
                                  int(knob_size),
                                  int(knob_size)))

# ── main windows ─────────────────────────────────────────────────────────────


def cache_data(*keys):
    """ cache data"""
    cache = {}

    def decorator(func):
        @wraps(func)
        def wrapper(self, data: dict):
            nonlocal cache
            current = {k: data.get(k) for k in keys}
            if cache == current:
                return
            cache = current
            return func(self, data)

        def cache_clear():
            nonlocal cache
            cache.clear()
        wrapper.clear = cache_clear
        return wrapper
    return decorator


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("乐普血氧监测")
        self.setMinimumSize(520, 720)
        self.resize(560, 780)
        self.setStyleSheet(f"QMainWindow {{ background: {C_BG}; }}")
        self.logger = LOGGER
        self.logger.setLevel(logging.DEBUG)
        self.logger.handlers.clear()
        self.file_handler = logging.FileHandler("ble.log", encoding="utf-8")
        self.file_handler.setLevel(logging.ERROR)
        self.logger.addHandler(self.file_handler)
        self.is_logging = False
        self._build_ui()
        self._start_ble()

    def _build_ui(self):
        root = QWidget()
        root.setStyleSheet(f"background: {C_BG};")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(16)

        # ── Top bar ──────────────────────────────────────────────────────────
        topbar = QFrame()
        topbar.setFixedHeight(64)
        topbar.setStyleSheet(f"""
            QFrame {{
                background: {C_TEAL};
                border-radius: 16px;
            }}
        """)
        shadow = QGraphicsDropShadowEffect()
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(26, 188, 168, 80))
        topbar.setGraphicsEffect(shadow)

        top_layout = QHBoxLayout(topbar)
        top_layout.setContentsMargins(20, 0, 20, 0)

        title = QLabel("血氧监测")
        title.setStyleSheet("color: white; font-size: 18px; font-weight: 700;")
        top_layout.addWidget(title)
        top_layout.addStretch()

        self.log_text = QLabel("日志开关")
        self.log_text.setStyleSheet('color: white;'
                                    'font-size: 15px;'
                                    'font-weight: 500;')
        top_layout.addWidget(self.log_text)
        self.log_switch = Switch()
        self.log_switch.setFixedSize(36, 20)
        top_layout.addWidget(self.log_switch)

        layout.addWidget(topbar)

        # ── status bar ───────────────────────────────────────────────────────
        status_frame = QFrame()
        status_frame.setFixedHeight(44)
        status_frame.setStyleSheet('QFrame {'
                                   f'background: {C_CARD};'
                                   'border-radius: 10px;'
                                   f'border: 1px solid {C_BORDER};'
                                   '}'
                                   'QLabel {'
                                   "border: none;"
                                   '}')
        status_layout = QHBoxLayout(status_frame)
        status_layout.setContentsMargins(16, 0, 16, 0)
        self.status_icon = DynamicSvgWidget(osp.join(RESC, "ble_icon.svg"))
        self.status_icon.setFixedSize(24, 24)
        self.status_icon.set_icon_color(C_BLE_OFF)
        status_layout.addWidget(self.status_icon)
        self.status_name = QLabel("当前设备未连接")
        self.status_name.setStyleSheet(f"color: {C_SUBTEXT};"
                                       "font-size: 13px;")
        self.status_name.setFixedWidth(250)
        status_layout.addWidget(self.status_name)
        self.battery_lbl = BatteryWidget("🔋 --")
        self.battery_lbl.setStyleSheet("color: rgba(255,255,255,200);"
                                       "font-size: 13px;"
                                       "margin-left: 12px;")
        status_layout.addWidget(self.battery_lbl)
        self.battery_lbl.hide()
        status_layout.addStretch()

        self.probe_lbl = QLabel("")
        self.probe_lbl.setStyleSheet(f"color: {C_DANGER};"
                                     "font-size: 13px;"
                                     "font-weight: 600;")
        status_layout.addWidget(self.probe_lbl)

        layout.addWidget(status_frame)

        # ── Cards ────────────────────────────────────────────────────────────
        cards_row1 = QHBoxLayout()
        cards_row1.setSpacing(12)
        self.spo2_card = MetricCard("SpO2  血氧饱和度",
                                    "%",
                                    C_BLUE,
                                    icon=osp.join(RESC, "ic_spo2.svg"))
        self.pr_card = MetricCard("PR  心率",
                                  "次/分",
                                  C_GREEN,
                                  icon=osp.join(RESC, "iv_icon_ecg_hr.svg"))
        cards_row1.addWidget(self.spo2_card)
        cards_row1.addWidget(self.pr_card)
        layout.addLayout(cards_row1)

        cards_row2 = QHBoxLayout()
        cards_row2.setSpacing(12)
        self.pi_card = MetricCard("PI  灌注指数",
                                  "%",
                                  C_ORANGE,
                                  icon=osp.join(RESC, "ic_label_pi.svg"))
        self.extra_card = MetricCard("工作模式",
                                     "",
                                     C_TEAL,
                                     icon=osp.join(RESC, "ic_label_po.svg"))
        self.extra_card.set_value("--")
        cards_row2.addWidget(self.pi_card)
        cards_row2.addWidget(self.extra_card)
        layout.addLayout(cards_row2)

        # ── PPG Wave ─────────────────────────────────────────────────────────
        wave_frame = QFrame()
        wave_frame.setStyleSheet('QFrame {'
                                 f'background: {C_CARD};'
                                 'border-radius: 16px;'
                                 f'border: 1.5px solid {C_BORDER};'
                                 '}'
                                 'QLabel {'
                                 "border: none;"
                                 '}')
        shadow2 = QGraphicsDropShadowEffect()
        shadow2.setBlurRadius(18)
        shadow2.setOffset(0, 4)
        shadow2.setColor(QColor(0, 0, 0, 18))
        wave_frame.setGraphicsEffect(shadow2)

        wave_layout = QVBoxLayout(wave_frame)
        wave_layout.setContentsMargins(16, 14, 16, 14)
        wave_layout.setSpacing(8)

        wave_header = QHBoxLayout()
        icon_svg = QSvgWidget(osp.join(RESC, "ic_label_ecg.svg"))
        icon_svg.setFixedSize(22, 20)
        icon_svg.setStyleSheet("background: transparent;")
        wave_header.addWidget(icon_svg)
        wave_title = QLabel("PPG 波形")
        wave_title.setStyleSheet(f"color: {C_TEAL};"
                                 "font-size: 14px;"
                                 "font-weight: 700;")
        wave_header.addWidget(wave_title)
        wave_header.addStretch()
        self.wave_time_lbl = QLabel("")
        self.wave_time_lbl.setStyleSheet(f"color: {C_SUBTEXT};"
                                         "font-size: 11px;")
        wave_header.addWidget(self.wave_time_lbl)
        wave_layout.addLayout(wave_header)

        wave_body = QHBoxLayout()

        # Scale Y
        scale_y = QVBoxLayout()
        scale_list = ["0", "32", "64", "96", "127"]
        scale_list_r = list(reversed(scale_list))
        for label in scale_list_r:
            lbl = QLabel(label)
            lbl.setStyleSheet(f"color: {C_SUBTEXT};"
                              "font-size: 10px;")
            scale_y.addWidget(lbl)
            if label is not scale_list_r[-1]:
                scale_y.addStretch()
        wave_body.addLayout(scale_y)
        self.ppg_widget = PPGWidget()
        self.ppg_widget.setMinimumHeight(180)
        wave_body.addWidget(self.ppg_widget, 1)
        wave_layout.addLayout(wave_body)

        layout.addWidget(wave_frame)

        # ── Bottom timestamp ─────────────────────────────────────────────────
        self.time_lbl = QLabel("--:--:--")
        self.time_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.time_lbl.setStyleSheet(f"color: {C_SUBTEXT}; "
                                    "font-size: 12px;")
        layout.addWidget(self.time_lbl)

        self.timer = QTimer()
        self.timer.timeout.connect(self._update_time)
        self.timer.start(1000)

    def _update_time(self):
        self.time_lbl.setText(datetime.now().strftime("%H:%M:%S"))

    def _start_ble(self):
        self.ble_worker = BleWorker()
        self.ble_worker.data_received.connect(self._on_data_received)
        self.ble_worker.status_changed.connect(self._on_status_changed)
        self.log_switch.toggled.connect(self._on_log_switch)
        loop = asyncio.get_event_loop()
        loop.create_task(self.ble_worker.run())

    def _on_status_changed(self, info: dict):
        status = info['status']
        if status == BleSt.CONNECTED:
            self._on_connected(info)
        elif status == BleSt.DISCONNECTED:
            self._on_disconnected(info)
        else:
            pass

    def _on_connected(self, info: dict):
        self.logger.info("Connected to %s, device name %s",
                         info['address'],
                         info['name'])
        self.battery_lbl.show()
        self.battery_lbl.setStyleSheet("color: rgba(255,255,255,200);"
                                       "font-size: 13px;"
                                       "margin-left: 12px;"
                                       "border: none;")
        self.status_icon.set_icon_color(C_BLE_ON)
        self.status_name.setText(f"{info['name']} [{info['address']}]")
        self.status_name.setStyleSheet(f"color: {C_TEAL}; "
                                       "font-size: 13px; "
                                       "font-weight: 600;")
        self.extra_card.set_value("")

    def _on_disconnected(self, info: dict):  # pylint: disable=unused-argument
        self.logger.info("Disconnected")
        self.battery_lbl.hide()
        self.battery_lbl.setStyleSheet("color: rgba(255,255,255,200);"
                                       "font-size: 13px;"
                                       "margin-left: 12px;"
                                       "border: none;")
        self.status_icon.set_icon_color(C_BLE_OFF)
        self.status_name.setText("当前设备未连接")
        self.status_name.setStyleSheet(f"color: {C_ORANGE}; "
                                       "font-size: 13px;")
        self.spo2_card.set_value(None)
        self.pr_card.set_value(None)
        self.pi_card.set_value(None)
        self.extra_card.set_value("--")
        self.extra_card.set_unit("")
        self.probe_lbl.setText("")
        self._on_fw_battery.clear()
        self._on_rt_data_param.clear()
        self._on_work_status_data.clear()

    def _on_log_switch(self, value: bool):
        self.file_handler.setLevel(logging.DEBUG if value else logging.ERROR)
        self.is_logging = value

    def _get_range_value(self, value: int, div=None) -> int | None:
        if not value:
            return None
        if 1 <= value < 255:
            return value if div is None else value / div
        return None

    def _on_rt_data_wave(self, data: dict):
        wave = data.get("wave_rev_data", [])
        timestamp = data['timestamp']
        self.ppg_widget.push(wave)
        dt = datetime.fromtimestamp(timestamp)
        self.wave_time_lbl.setText(dt.strftime("%H:%M:%S"))

    @cache_data('spo2', 'pr', 'pi', 'battery_level')
    def _on_rt_data_param(self, data: dict):
        if data.get("is_probe_off"):
            self.probe_lbl.setText("⚠ Finger out")
            self.spo2_card.set_value(None)
            self.pr_card.set_value(None)
            self.pi_card.set_value(None)
            return
        if data.get("is_pulse_searching"):
            self.probe_lbl.setText("🔍 Pulse searching...")
            return
        self.probe_lbl.setText("")
        self.spo2_card.set_value(self._get_range_value(data.get("spo2")))
        self.pr_card.set_value(self._get_range_value(data.get("pr")))
        self.pi_card.set_value(self._get_range_value(data.get("pi"), 10))
        self._on_fw_battery(data)

    @cache_data('battery_level')
    def _on_fw_battery(self, data: dict):
        self.battery_lbl.set_level(data.get("battery_level", 0))

    @cache_data('mode', 'step', 'para1', 'para2')
    def _on_work_status_data(self, data: dict):
        mode = data['mode']
        if mode == 1:
            self.extra_card.set_value("点测模式")
            step = data['step']
            if step == 1:  # 1: 准备测量
                pass
            elif step == 2:  # 2: 测量中 para1 倒计时
                if data['para1'] == 0:
                    self.extra_card.set_unit("测量完成")
                else:
                    self.extra_card.set_unit(f"正在测量({data['para1']})...")
            elif step == 3:  # 3: 测量结果 para1 血氧 para2 PR
                self.extra_card.set_unit(f"spo2 {data['para1']}"
                                         ' | '
                                         f"PR {data['para2']}")
        elif mode == 2:
            self.extra_card.set_value("连续模式")
            self.extra_card.set_unit("")
        else:
            self.extra_card.set_value(None)

    def log_to_file(self, data, *keys):
        if not self.is_logging:
            return
        dump_keys = 'name', 'timestamp', *keys
        value = {k: data[k] for k in dump_keys if k in data}
        value['timestamp'] = int(value['timestamp'] * 1000)
        self.logger.info(json.dumps(value, ensure_ascii=False))

    def _on_data_received(self, data: dict):
        name = data.get("name")
        if name == "EventPC60FwRtDataWave":
            self._on_rt_data_wave(data)
            self.log_to_file(data, 'wave_rev_data')
        elif name == "EventPC60FwRtDataParam":
            self._on_rt_data_param(data)
            self.log_to_file(data,
                             'spo2', 'pr', 'pi', 'is_probe_off',
                             'is_pulse_searching', 'battery_level')
        elif name == "EventPC60FwBattery":
            self._on_fw_battery(data)
        elif name == "WORK_STATUS_DATA":
            self._on_work_status_data(data)
        else:
            print(f"Unknown event: {name}")

    def closeEvent(self, event):   # pylint: disable=C0103
        self.ble_worker.stop()
        event.accept()


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    app = QApplication(sys.argv)
    icon = QIcon(osp.join(RESC, "ic_launcher.png"))
    app.setWindowIcon(icon)
    app.setStyle("Fusion")
    loop = qasync.QEventLoop(app)
    asyncio.set_event_loop(loop)
    window = MainWindow()
    window.setWindowIcon(icon)
    set_windows_appusermodelid('com.lepu.ble')
    window.show()

    with loop:
        loop.run_forever()


if __name__ == "__main__":
    main()
