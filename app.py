"""FishingHelper desktop app. The optional WoW addon only records after /fh start."""
from __future__ import annotations

import json
import ctypes
import os
import shutil
import sys
import threading
import time
import webbrowser
import winreg
from pathlib import Path

import librosa
import numpy as np
import sounddevice as sd
import win32api
import win32con
import win32gui
from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import (QApplication, QCheckBox, QComboBox, QFileDialog,
    QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSpinBox, QTextEdit, QVBoxLayout, QWidget)
from pynput.keyboard import Controller, KeyCode

from bridge import merge_events, read_events, summarize
from live_bridge import read_visible_packet


ROOT = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent))
HOME = Path(os.environ.get('APPDATA', Path.home())) / 'FishingHelper'
HOME.mkdir(parents=True, exist_ok=True)
CONFIG = HOME / 'config.json'
NOISE = HOME / 'noise.npy'
VB_CABLE_URL = 'https://download.vb-audio.com/Download_CABLE/VBCABLE_Driver_Pack45.zip'

DEFAULTS = {
    'threshold': 0.41, 'rms_gate': 0.016, 'hook_delay': 0.0,
    'rest_delay': 1.5, 'cast_vk': 97, 'hook_vk': 98,
    'shield_vk': 99, 'shield_minutes': 28, 'shield_enabled': False,
    'background': True, 'source': 'virtual', 'device_name': '',
    'device_id': None,
    'macro_vk': 119, 'combat_vk': 120, 'turn_vk': 0, 'turn_ms': 0,
    'combat_enabled': False, 'noise_enabled': False, 'wow_root': '',
    'template_path': '',
}


def normalize_wow_root(path):
    root = Path(path).expanduser()
    if root.name.lower() == '_retail_': root = root.parent
    return root if (root / '_retail_').is_dir() else None


def find_wow_roots():
    candidates = []
    for letter in 'CDEFGHIJKLMNOPQRSTUVWXYZ':
        drive = Path(f'{letter}:/')
        if not drive.is_dir(): continue
        for relative in ('World of Warcraft', 'Games/World of Warcraft',
                         'Program Files/World of Warcraft',
                         'Program Files (x86)/World of Warcraft'):
            candidates.append(drive / relative)
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        key_path = r'SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall'
        try:
            with winreg.OpenKey(hive, key_path) as base:
                for index in range(winreg.QueryInfoKey(base)[0]):
                    try:
                        with winreg.OpenKey(base, winreg.EnumKey(base, index)) as key:
                            name = winreg.QueryValueEx(key, 'DisplayName')[0]
                            if 'World of Warcraft' in name:
                                candidates.append(Path(winreg.QueryValueEx(key, 'InstallLocation')[0]))
                    except (OSError, ValueError):
                        continue
        except OSError:
            pass
    found = []
    for path in candidates:
        root = normalize_wow_root(path)
        if root and root not in found: found.append(root)
    return found


def suggested_wow_root():
    found = find_wow_roots()
    return str(found[0]) if found else ''


def load_config():
    result = DEFAULTS.copy()
    try: result.update(json.loads(CONFIG.read_text(encoding='utf-8')))
    except (OSError, ValueError): pass
    if not result['wow_root']: result['wow_root'] = suggested_wow_root()
    return result


def save_config(config):
    temp = CONFIG.with_suffix('.tmp')
    temp.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(CONFIG)


def template_file(settings):
    candidates = [settings.get('template_path'),
                  Path(sys.executable).resolve().parent / 'fishbobber.ogg',
                  ROOT / 'fishbobber.ogg']
    for candidate in candidates:
        if candidate and Path(candidate).is_file(): return Path(candidate)
    raise FileNotFoundError('未找到鱼漂声音模板；请在界面中选择自己的 .ogg 或 .wav 文件。')


def audio_inputs():
    return [(i, d['name']) for i, d in enumerate(sd.query_devices())
            if d['max_input_channels'] > 0]


def is_cable(name):
    lower = name.lower()
    return ('cable output' in lower or 'vb-audio' in lower or
            'virtual cable' in lower)


def wow_window():
    for title in ('World of Warcraft', '魔兽世界'):
        hwnd = win32gui.FindWindow(None, title)
        if hwnd: return hwnd
    return None


def send_key(vk, background, duration=0.05):
    if not vk: return False
    if background:
        hwnd = wow_window()
        if not hwnd: return False
        win32api.PostMessage(hwnd, win32con.WM_KEYDOWN, vk, 0)
        time.sleep(duration)
        win32api.PostMessage(hwnd, win32con.WM_KEYUP, vk, 0)
    else:
        hwnd = wow_window()
        if not hwnd: return False
        if win32gui.GetForegroundWindow() != hwnd:
            try: win32gui.SetForegroundWindow(hwnd)
            except Exception: return False
            time.sleep(0.1)
        if win32gui.GetForegroundWindow() != hwnd: return False
        keyboard = Controller()
        key = KeyCode.from_vk(vk)
        keyboard.press(key)
        time.sleep(duration)
        keyboard.release(key)
    return True


def game_foreground():
    hwnd = wow_window()
    return bool(hwnd and win32gui.GetForegroundWindow() == hwnd)


def combat_beacon():
    """Top-left red/green addon marker is available only while game is visible."""
    if not game_foreground(): return None
    try:
        left, top = win32gui.ClientToScreen(wow_window(), (0, 0))
        dc = ctypes.windll.user32.GetDC(0)
        try: pixel = ctypes.windll.gdi32.GetPixel(dc, left + 5, top + 5)
        finally: ctypes.windll.user32.ReleaseDC(0, dc)
        red, green, blue = pixel & 255, (pixel >> 8) & 255, (pixel >> 16) & 255
        if red > 180 and green < 100 and blue < 100: return True
        if green > 140 and red < 100 and blue < 100: return False
    except Exception:
        pass
    return None


def volume_level(rms):
    """Show input level on a familiar 0–100 scale (−60 to 0 dBFS)."""
    return max(0, min(100, round((20 * np.log10(max(rms, 1e-5)) + 60) * 100 / 60)))


class IdleMeter(QObject):
    level = pyqtSignal(float)

    def __init__(self):
        super().__init__()
        self.stop_event = threading.Event()
        self.thread = None

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread.is_alive(): self.thread.join(timeout=1)

    def start(self, device_id):
        self.stop()
        if device_id is None: return
        self.stop_event = threading.Event()
        stop_event = self.stop_event
        self.thread = threading.Thread(target=self._run, args=(device_id, stop_event), daemon=True)
        self.thread.start()

    def _run(self, device_id, stop_event):
        last_emit = 0
        def callback(samples, frames, timestamp, status):
            nonlocal last_emit
            now = time.monotonic()
            if now - last_emit >= 0.1:
                self.level.emit(float(np.sqrt(np.mean(samples[:, 0] ** 2))))
                last_emit = now
        try:
            with sd.InputStream(samplerate=22050, channels=1, device=device_id,
                                blocksize=1024, callback=callback):
                stop_event.wait()
        except Exception:
            self.level.emit(0.0)


class Engine(QObject):
    message = pyqtSignal(str)
    meter = pyqtSignal(float, float)
    counts = pyqtSignal(int, int)
    done = pyqtSignal()

    def __init__(self):
        super().__init__()
        self.stop_event = threading.Event()
        self.thread = None

    def stop(self):
        self.stop_event.set()
        sd.stop()

    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def start(self, settings, device_id):
        if self.running(): return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, args=(settings.copy(), device_id), daemon=True)
        self.thread.start()

    def _wait(self, seconds):
        return not self.stop_event.wait(max(0, seconds))

    def _run(self, settings, device_id):
        casts = hooks = 0
        sr = 22050
        try:
            y, _ = librosa.load(str(template_file(settings)), sr=sr, mono=True)
            duration = max(0.6, len(y) / sr)
            size = int(duration * sr)
            template = np.zeros(size, dtype=np.float32)
            template[:min(len(y), size)] = y[:size]
            template -= template.mean()
            target = np.abs(np.fft.rfft(template * np.hanning(size)))
            target_norm = np.linalg.norm(target)
            if target_norm == 0: raise RuntimeError('鱼漂声音模板为空')
            noise = np.load(NOISE) if settings['noise_enabled'] and NOISE.exists() else None
            if noise is not None and noise.shape != target.shape: noise = None
            shield_at = time.monotonic()
            last_hit = time.monotonic()
            warned = False
            self.message.emit('引擎已启动；游戏音频输入已连接。')
            while not self.stop_event.is_set():
                if settings['shield_enabled'] and time.monotonic() - shield_at >= settings['shield_minutes'] * 60:
                    self.message.emit('正在补盾。')
                    if not send_key(settings['shield_vk'], settings['background']):
                        self.message.emit('找不到游戏窗口，补盾未发送。')
                    shield_at = time.monotonic()
                    if not self._wait(5): break
                state = combat_beacon() if settings['combat_enabled'] else None
                if state is True:
                    self.message.emit('插件标记显示进入战斗，暂停钓鱼。')
                    last_macro = 0
                    while not self.stop_event.is_set() and combat_beacon() is True:
                        if time.monotonic() - last_macro >= 4:
                            send_key(settings['combat_vk'], settings['background'])
                            last_macro = time.monotonic()
                        if not self._wait(0.4): break
                    if self.stop_event.is_set(): break
                    if settings['turn_vk'] and settings['turn_ms']:
                        send_key(settings['turn_vk'], settings['background'], settings['turn_ms'] / 1000)
                    self.message.emit('战斗标记消失，恢复钓鱼。')
                    if not self._wait(1): break
                if not send_key(settings['cast_vk'], settings['background']):
                    self.message.emit('找不到游戏窗口；已暂停，请打开游戏后重启。')
                    break
                casts += 1
                self.counts.emit(casts, hooks)
                if not self._wait(1.5): break
                deadline = time.monotonic() + 22
                while time.monotonic() < deadline and not self.stop_event.is_set():
                    if settings['combat_enabled'] and combat_beacon() is True: break
                    try:
                        rec = sd.rec(size, samplerate=sr, channels=1, device=device_id)
                        sd.wait()
                    except Exception as exc:
                        raise RuntimeError(f'音频设备采样失败：{exc}') from exc
                    if self.stop_event.is_set(): break
                    audio = rec[:, 0].astype(np.float64)
                    audio -= audio.mean()
                    rms = float(np.sqrt(np.mean(audio * audio)))
                    spectrum = np.abs(np.fft.rfft(audio * np.hanning(size)))
                    if noise is not None: spectrum = np.maximum(spectrum - noise, 0)
                    denom = np.linalg.norm(spectrum) * target_norm
                    score = float(np.dot(spectrum, target) / denom) if denom > 0 else 0.0
                    self.meter.emit(score, rms)
                    if rms >= settings['rms_gate'] and score >= settings['threshold']:
                        if not self._wait(settings['hook_delay']): break
                        if send_key(settings['hook_vk'], settings['background']):
                            hooks += 1
                            self.counts.emit(casts, hooks)
                            self.message.emit(f'检测到鱼漂声，已发送收竿键（相似度 {score:.2f}）。')
                            last_hit = time.monotonic()
                            warned = False
                        break
                if not warned and time.monotonic() - last_hit > 90:
                    self.message.emit('长时间未检测到有效鱼漂声；请检查游戏背景声音、输入声道和阈值。')
                    warned = True
                if not self._wait(settings['rest_delay']): break
        except Exception as exc:
            self.message.emit(f'已停止：{exc}')
        finally:
            self.done.emit()


STYLE = """
QWidget {background:#0c0a08;color:#ede4d3;font:13px 'Microsoft YaHei';}
QGroupBox {border:1px solid #4a3925;border-radius:8px;margin-top:12px;padding:17px 12px 12px;background:#17130f;font-weight:bold;color:#f3d58f;}
QGroupBox::title {subcontrol-origin:margin;left:14px;padding:0 7px;}
QPushButton {background:#30261a;border:1px solid #806238;border-radius:6px;padding:8px 12px;color:#f3d58f;font-weight:bold;}
QPushButton:hover {background:#493820;border-color:#d7b36a;}
QPushButton:disabled {color:#766d5d;border-color:#514737;}
QLineEdit,QComboBox,QSpinBox,QTextEdit {background:#0f0c09;border:1px solid #4a3925;border-radius:5px;padding:5px;color:#ede4d3;}
QComboBox QAbstractItemView {background:#20180f;color:#ede4d3;}
QProgressBar {background:#0f0c09;border:1px solid #4a3925;border-radius:5px;text-align:center;color:#ede4d3;min-height:20px;}
QProgressBar::chunk {background:#a87932;border-radius:4px;}
QCheckBox {spacing:8px;}
QLabel#title {font:24px 'Microsoft YaHei';font-weight:bold;color:#f3d58f;}
QLabel#hint {color:#aa9a80;}
QLabel#metric {font:19px 'Microsoft YaHei';font-weight:bold;color:#d7b36a;}
"""


class KeyBindButton(QPushButton):
    def __init__(self, vk):
        super().__init__()
        self.vk = vk
        self.waiting = False
        self.setText(self._label(vk))
        self.clicked.connect(self.begin_capture)

    @staticmethod
    def _label(vk):
        if 96 <= vk <= 105: return f'Num {vk - 96}'
        if 112 <= vk <= 123: return f'F{vk - 111}'
        if 65 <= vk <= 90 or 48 <= vk <= 57: return chr(vk)
        if vk in (37, 38, 39, 40): return {37:'←',38:'↑',39:'→',40:'↓'}[vk]
        return '关闭' if vk == 0 else f'VK {vk}'

    def begin_capture(self):
        self.waiting = True
        self.setText('请按键… Esc 取消')
        self.grabKeyboard()

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.vk = 0
            self.setText('关闭')
            return
        super().mousePressEvent(event)

    def keyPressEvent(self, event):
        if not self.waiting:
            return super().keyPressEvent(event)
        key = event.key()
        vk = None
        if key == Qt.Key_Escape:
            self.waiting = False; self.releaseKeyboard(); self.setText(self._label(self.vk)); return
        if event.modifiers() & Qt.KeypadModifier and Qt.Key_0 <= key <= Qt.Key_9:
            vk = 96 + key - Qt.Key_0
        elif Qt.Key_F1 <= key <= Qt.Key_F12:
            vk = 112 + key - Qt.Key_F1
        elif Qt.Key_A <= key <= Qt.Key_Z or Qt.Key_0 <= key <= Qt.Key_9:
            vk = key
        elif key in (Qt.Key_Left, Qt.Key_Up, Qt.Key_Right, Qt.Key_Down):
            vk = {Qt.Key_Left:37, Qt.Key_Up:38, Qt.Key_Right:39, Qt.Key_Down:40}[key]
        if vk is not None:
            self.vk = vk
            self.waiting = False
            self.releaseKeyboard()
            self.setText(self._label(vk))

    def value(self):
        return self.vk


class Window(QWidget):
    def __init__(self):
        super().__init__()
        self.settings = load_config()
        self.engine = Engine()
        self.idle_meter = IdleMeter()
        self.idle_meter.level.connect(self.show_idle_volume)
        self.live_events = []
        self.last_live_key = None
        self.last_live_seen = 0.0
        self.engine.message.connect(self.log)
        self.engine.meter.connect(self.show_meter)
        self.engine.counts.connect(self.show_counts)
        self.engine.done.connect(self.finished)
        self.setWindowTitle('FishingHelper · 艾泽拉斯钓鱼助手')
        self.resize(780, 850)
        self.setStyleSheet(STYLE)
        self._build()
        self.refresh_devices()
        self.sync_bridge()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.sync_bridge)
        self.timer.start(5000)
        self.live_timer = QTimer(self)
        self.live_timer.timeout.connect(self.poll_live_bridge)
        self.live_timer.start(250)
        self.idle_meter.start(self.device.currentData())

    def _build(self):
        outer = QVBoxLayout(self)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        content = QWidget(); page = QVBoxLayout(content)
        scroll.setWidget(content); outer.addWidget(scroll)
        title = QLabel('艾泽拉斯钓鱼助手'); title.setObjectName('title')
        page.addWidget(title)
        page.addWidget(QLabel('声音识别 · 游戏记录 · 可选战斗宏'))

        box = QGroupBox('音频输入')
        form = QFormLayout(box)
        self.source = QComboBox(); self.source.addItem('VB-CABLE 虚拟音轨', 'virtual'); self.source.addItem('实际输入设备', 'physical')
        self.source.setCurrentIndex(0 if self.settings['source'] == 'virtual' else 1)
        self.source.currentIndexChanged.connect(self.refresh_devices)
        form.addRow('来源', self.source)
        self.device = QComboBox(); form.addRow('设备', self.device)
        self.device.currentIndexChanged.connect(self.restart_idle_meter)
        row = QHBoxLayout()
        self.cable_status = QLabel(); row.addWidget(self.cable_status, 1)
        refresh = QPushButton('刷新声道'); refresh.clicked.connect(self.refresh_devices); row.addWidget(refresh)
        download = QPushButton('下载 VB-CABLE Pack45'); download.clicked.connect(self.download_cable); row.addWidget(download)
        form.addRow(row)
        sample_row = QHBoxLayout()
        self.template_name = QLabel(self.settings.get('template_path') or '默认鱼漂模板')
        sample_row.addWidget(self.template_name, 1)
        choose_sample = QPushButton('选择鱼漂声音'); choose_sample.clicked.connect(self.choose_template); sample_row.addWidget(choose_sample)
        form.addRow('识别模板', sample_row)
        meter_row = QHBoxLayout()
        self.input_level = QProgressBar(); self.input_level.setRange(0, 100)
        self.input_level.setValue(0); self.input_level.setFormat('%v / 100')
        meter_row.addWidget(self.input_level, 1)
        self.input_level_text = QLabel('等待输入')
        meter_row.addWidget(self.input_level_text)
        form.addRow('当前音轨音量', meter_row)
        page.addWidget(box)

        controls = QGroupBox('钓鱼控制')
        grid = QGridLayout(controls)
        self.background = QCheckBox('后台运行  ?')
        self.background.setChecked(self.settings['background'])
        self.background.setToolTip('想在切到其他窗口后继续钓鱼？请先在魔兽世界的声音设置里开启“背景声音”。')
        grid.addWidget(self.background, 0, 0)
        self.shield = QCheckBox('定时补盾'); self.shield.setChecked(self.settings['shield_enabled']); grid.addWidget(self.shield, 0, 1)
        self.noise = QCheckBox('喷泉降噪'); self.noise.setChecked(self.settings['noise_enabled']); grid.addWidget(self.noise, 0, 2)
        self.combat = QCheckBox('战斗时按一键宏'); self.combat.setChecked(self.settings['combat_enabled']); grid.addWidget(self.combat, 1, 0, 1, 2)
        self.combat.setToolTip('仅游戏在前台、插件左上角状态标记可见时生效。')
        self.fields = {}
        specs = [('抛竿键 VK', 'cast_vk', 1, 255), ('收竿键 VK', 'hook_vk', 1, 255),
                 ('补盾键 VK', 'shield_vk', 1, 255), ('补盾间隔（分钟）', 'shield_minutes', 1, 120),
                 ('插件启动宏键 VK', 'macro_vk', 1, 255), ('战斗宏键 VK', 'combat_vk', 1, 255),
                 ('转向键 VK（0 为关闭）', 'turn_vk', 0, 255), ('转向时长（毫秒）', 'turn_ms', 0, 3000)]
        for n, (label, key, low, high) in enumerate(specs):
            if key.endswith('_vk'):
                widget = KeyBindButton(int(self.settings[key]))
            else:
                widget = QSpinBox(); widget.setRange(low, high); widget.setValue(int(self.settings[key]))
            self.fields[key] = widget
            grid.addWidget(QLabel(label), 2 + n // 2, (n % 2) * 2)
            grid.addWidget(widget, 2 + n // 2, (n % 2) * 2 + 1)
        page.addWidget(controls)

        tuning = QGroupBox('声音识别')
        tune = QFormLayout(tuning)
        self.threshold = QSpinBox(); self.threshold.setRange(10, 98); self.threshold.setSuffix(' %'); self.threshold.setValue(round(self.settings['threshold'] * 100))
        self.rms = QSpinBox(); self.rms.setRange(1, 100); self.rms.setValue(max(1, min(100, round(self.settings['rms_gate'] * 1000))))
        self.rms.setToolTip('1–100，数值越高，越不容易被轻微的背景声音触发。通常先保持默认值。')
        tune.addRow('相似度阈值', self.threshold); tune.addRow('触发音量（1–100）', self.rms)
        noise_button = QPushButton('录制 3 秒环境噪音'); noise_button.clicked.connect(self.capture_noise); tune.addRow(noise_button)
        page.addWidget(tuning)

        bridge = QGroupBox('游戏内记录')
        bf = QFormLayout(bridge)
        root_row = QHBoxLayout(); self.wow_root = QLineEdit(self.settings['wow_root']); root_row.addWidget(self.wow_root)
        detect = QPushButton('自动查找'); detect.clicked.connect(self.auto_find_wow); root_row.addWidget(detect)
        browse = QPushButton('选择游戏目录'); browse.clicked.connect(self.choose_wow); root_row.addWidget(browse)
        bf.addRow('WoW 目录', root_row)
        actions = QHBoxLayout()
        for label, fn in [('安装插件', self.install_addon), ('发送插件启动宏键', self.send_start_macro), ('同步记录', self.sync_bridge)]:
            b = QPushButton(label); b.clicked.connect(fn); actions.addWidget(b)
        bf.addRow(actions)
        self.actual = QLabel('尚无游戏记录。'); self.actual.setWordWrap(True); bf.addRow(self.actual)
        self.link_status = QLabel('实时连接：等待游戏内 /fh start。')
        self.link_status.setObjectName('hint')
        bf.addRow(self.link_status)
        self.achievement_view = QLabel('达拉然钓鱼成就：等待插件数据。')
        self.achievement_view.setWordWrap(True)
        bf.addRow(self.achievement_view)
        page.addWidget(bridge)

        self.metrics = QLabel('抛竿 0  ·  收竿指令 0  ·  声音 0.0000'); self.metrics.setObjectName('metric'); page.addWidget(self.metrics)
        runrow = QHBoxLayout(); self.start_button = QPushButton('开始钓鱼'); self.start_button.clicked.connect(self.start)
        self.stop_button = QPushButton('停止'); self.stop_button.clicked.connect(self.stop); self.stop_button.setEnabled(False)
        macro_button = QPushButton('执行战斗宏'); macro_button.clicked.connect(self.send_combat_macro)
        runrow.addWidget(self.start_button); runrow.addWidget(self.stop_button); runrow.addWidget(macro_button); page.addLayout(runrow)
        self.log_area = QTextEdit(); self.log_area.setReadOnly(True); self.log_area.setMinimumHeight(130); page.addWidget(self.log_area)
        self._casts = self._hooks = 0; self._score = self._volume = 0.0

    def log(self, message):
        self.log_area.append(time.strftime('%H:%M:%S  ') + message)

    def refresh_devices(self):
        try: devices = audio_inputs()
        except Exception as exc:
            self.cable_status.setText(f'设备枚举失败：{exc}'); return
        cables = [(i, n) for i, n in devices if is_cable(n)]
        virtual = self.source.currentData() == 'virtual'
        choices = cables if virtual else devices
        self.device.clear()
        for i, name in choices: self.device.addItem(name, i)
        old = self.settings.get('device_name', '')
        old_id = self.settings.get('device_id')
        match = next((j for j, (_, name) in enumerate(choices) if name == old), None)
        if match is None: match = next((j for j, (i, _) in enumerate(choices) if i == old_id), 0)
        if choices: self.device.setCurrentIndex(match)
        self.cable_status.setText('已检测到 VB-CABLE 输入端。' if cables else '未检测到 VB-CABLE；可下载 Pack45 安装后刷新，或改用实际音轨。')
        self.restart_idle_meter()

    def restart_idle_meter(self):
        if hasattr(self, 'device') and hasattr(self, 'input_level') and not self.engine.running():
            self.idle_meter.start(self.device.currentData())

    def show_idle_volume(self, rms):
        if not self.engine.running():
            self._volume = rms
            self.update_volume_display(rms)

    def update_volume_display(self, rms):
        level = volume_level(rms)
        self.input_level.setValue(level)
        self.input_level_text.setText(f'{level} / 100')
        self._metric()

    def download_cable(self):
        QMessageBox.information(self, '安装 VB-CABLE', '即将从 VB-Audio 官网下载 Pack45。下载后请解压，以管理员身份运行安装程序，重启 Windows 后刷新声道。')
        webbrowser.open(VB_CABLE_URL)

    def current_settings(self):
        s = self.settings.copy()
        for key, widget in self.fields.items(): s[key] = widget.value()
        s.update(background=self.background.isChecked(), shield_enabled=self.shield.isChecked(),
                 noise_enabled=self.noise.isChecked(), combat_enabled=self.combat.isChecked(),
                 source=self.source.currentData(), device_name=self.device.currentText(),
                 device_id=self.device.currentData(),
                 template_path=self.settings.get('template_path', ''),
                 threshold=self.threshold.value() / 100, rms_gate=self.rms.value() / 1000,
                 wow_root=self.wow_root.text().strip())
        return s

    def start(self):
        if self.device.currentData() is None:
            QMessageBox.warning(self, '缺少音频输入', '请刷新设备、安装 VB-CABLE，或选择实际输入设备。'); return
        self.settings = self.current_settings(); save_config(self.settings)
        self.idle_meter.stop()
        self.engine.start(self.settings, self.device.currentData())
        self.start_button.setEnabled(False); self.stop_button.setEnabled(True)

    def stop(self):
        self.engine.stop(); self.stop_button.setEnabled(False)

    def finished(self):
        self.start_button.setEnabled(True); self.stop_button.setEnabled(False)
        self.log('引擎已停止。')
        self.restart_idle_meter()

    def show_meter(self, score, volume):
        self._score, self._volume = score, volume
        self.update_volume_display(volume)

    def show_counts(self, casts, hooks):
        self._casts, self._hooks = casts, hooks; self._metric()

    def _metric(self):
        self.metrics.setText(f'抛竿指令 {self._casts}  ·  收竿指令 {self._hooks}  ·  相似度 {self._score:.2f}  ·  音量 {volume_level(self._volume)} / 100')

    def capture_noise(self):
        if self.engine.running():
            QMessageBox.information(self, '正在运行', '请先停止钓鱼，再录制环境声。'); return
        if self.device.currentData() is None: return
        self.idle_meter.stop()
        try:
            y, _ = librosa.load(str(template_file(self.current_settings())), sr=22050, mono=True)
            size = int(max(0.6, len(y) / 22050) * 22050)
            chunks = []
            for _ in range(max(1, int(3 * 22050 / size))):
                sample = sd.rec(size, samplerate=22050, channels=1, device=self.device.currentData()); sd.wait()
                audio = sample[:, 0] - sample[:, 0].mean()
                chunks.append(np.abs(np.fft.rfft(audio * np.hanning(size))))
            np.save(NOISE, np.mean(chunks, axis=0))
            self.noise.setChecked(True)
            self.log('已录制环境噪音；喷泉降噪已开启。请在没有鱼漂声时录制。')
        except Exception as exc:
            QMessageBox.warning(self, '录制失败', str(exc))
        finally:
            self.restart_idle_meter()

    def choose_wow(self):
        path = QFileDialog.getExistingDirectory(self, '选择 World of Warcraft 根目录', self.wow_root.text() or str(Path.home()))
        if path:
            root = normalize_wow_root(path)
            if root:
                self.wow_root.setText(str(root)); self.sync_bridge()
            else:
                QMessageBox.warning(self, '目录不正确', '请选择 World of Warcraft 根目录或其中的 _retail_ 文件夹。')

    def choose_template(self):
        path, _ = QFileDialog.getOpenFileName(self, '选择鱼漂声音模板', '', '音频文件 (*.ogg *.wav)')
        if path:
            self.settings['template_path'] = path
            self.template_name.setText(Path(path).name)
            save_config(self.current_settings())

    def auto_find_wow(self):
        paths = [str(path) for path in find_wow_roots()]
        if not paths:
            QMessageBox.information(self, '未找到游戏', '没有在常见目录或 Windows 安装记录中找到 WoW。请手动选择游戏目录。')
            return
        selected, ok = QInputDialog.getItem(self, '选择 WoW 安装位置', '检测到的游戏目录：', paths, 0, False)
        if ok and selected:
            self.wow_root.setText(selected); self.sync_bridge()

    def install_addon(self):
        root = normalize_wow_root(self.wow_root.text().strip())
        if not root:
            QMessageBox.warning(self, '目录不正确', '请选择包含 _retail_ 文件夹的 World of Warcraft 根目录。'); return
        self.wow_root.setText(str(root))
        game = root / '_retail_'
        target = game / 'Interface' / 'AddOns' / 'FishingHelperBridge'
        try:
            target.mkdir(parents=True, exist_ok=True)
            for source in (ROOT / 'addon' / 'FishingHelperBridge').iterdir():
                shutil.copy2(source, target / source.name)
        except OSError as exc:
            QMessageBox.warning(self, '安装失败', str(exc)); return
        self.log(f'插件已安装到 {target}；若游戏已打开，请 /reload。')
        save_config(self.current_settings())

    def send_start_macro(self):
        s = self.current_settings()
        if send_key(s['macro_vk'], s['background']):
            self.log('已发送插件启动宏键。游戏宏内容应为 /fh start。')
        else:
            self.log('找不到游戏窗口；请在游戏中手动按插件启动宏键。')

    def send_combat_macro(self):
        s = self.current_settings()
        if send_key(s['combat_vk'], s['background']): self.log('已发送战斗宏键。')
        else: self.log('找不到游戏窗口，战斗宏未发送。')

    def sync_bridge(self):
        root = self.wow_root.text().strip()
        stats = summarize(merge_events(read_events(root) if root else [], self.live_events))
        if not stats['last_sync']:
            self.actual.setText('还没有钓鱼记录。在游戏里使用 /fh start 后，获得的物品会实时显示。')
            self.achievement_view.setText('达拉然钓鱼成就：等待插件数据。')
            return
        top = stats['items'].most_common(5)
        items = '、'.join(f'{name} ×{count}' for (_, name), count in top) or '无'
        rate = stats['catches'] / stats['cast_ok'] * 100 if stats['cast_ok'] else 0
        self.actual.setText(f"游戏记录：成功出杆 {stats['cast_ok']} · 失败出杆 {stats['cast_fail']} · 确认渔获 {stats['catches']} · 估计成功率 {rate:.1f}%\n物品：{items}")
        achievements = stats['achievements']
        if achievements:
            lines = [f"{entry['name']}：{entry['completed']}/{entry['total']}"
                     for _, entry in sorted(achievements.items())]
            self.achievement_view.setText('达拉然钓鱼成就\n' + '\n'.join(lines))

    def poll_live_bridge(self):
        packet = read_visible_packet(wow_window())
        if packet is None:
            self.link_status.setText('实时连接：等待游戏左上角的数据条可见；遮挡期间的记录会在 /reload 或退出后补齐。')
            return
        self.last_live_seen = time.monotonic()
        self.link_status.setText('实时连接：已接入游戏插件。')
        key = (packet['session'], packet['sequence'])
        if key == self.last_live_key:
            return
        self.last_live_key = key
        self.live_events.append(packet)
        if len(self.live_events) > 3000:
            self.live_events = self.live_events[-3000:]
        if packet['kind'] == 'loot':
            self.log(f"游戏获得：{packet['name']} ×{packet['quantity']}")
        elif packet['kind'] == 'achievement' and packet.get('criterion'):
            self.log(f"成就进度：{packet['name']} {packet['completed']}/{packet['total']}，新完成 {packet['criterion']}")
        elif packet['kind'] == 'cast_fail':
            self.log('游戏提示：这次出杆未成功。')
        self.sync_bridge()

    def closeEvent(self, event):
        save_config(self.current_settings())
        self.idle_meter.stop()
        self.engine.stop()
        super().closeEvent(event)


if __name__ == '__main__':
    app = QApplication(sys.argv)
    window = Window(); window.show()
    sys.exit(app.exec_())
