"""Read the addon's visible, one-way pixel telemetry. No game memory access."""
from __future__ import annotations

import ctypes
import time

import win32gui

CELL_COUNT = 128
CELL_SIZE = 4
DATA_X = 20
PACKET_BYTES = CELL_COUNT * 3 // 2


def decode_colors(colors):
    if len(colors) != CELL_COUNT:
        return None
    nibbles = []
    for red, green, blue in colors:
        for channel in (red, green, blue):
            nibbles.append(max(0, min(15, round((channel - 8) / 16))))
    raw = bytes((nibbles[i] << 4) | nibbles[i + 1]
                for i in range(0, len(nibbles), 2))
    if raw[:3] != b'FH\x02':
        return None
    length = raw[3]
    if length > PACKET_BYTES - 11:
        return None
    session = int.from_bytes(raw[4:8], 'big')
    sequence = int.from_bytes(raw[8:10], 'big')
    body = raw[11:11 + length]
    if sum(body) % 256 != raw[10]:
        return None
    try:
        timestamp, kind, payload = body.decode('utf-8').split('|', 2)
        event = {'time': int(timestamp), 'kind': kind,
                 'source': 'live', 'session': session, 'sequence': sequence}
        parts = payload.split('|')
        if kind == 'loot' and len(parts) >= 3:
            event.update(item_id=int(parts[0]), quantity=int(parts[1]), name=parts[2])
        elif kind == 'achievement' and len(parts) >= 4:
            event.update(achievement_id=int(parts[0]), completed=int(parts[1]),
                         total=int(parts[2]), name=parts[3],
                         criterion=parts[4] if len(parts) > 4 else '')
        elif kind == 'error':
            event['message'] = payload
        return event
    except (UnicodeError, ValueError):
        return None


def read_visible_packet(hwnd):
    """Return None if WoW is covered, minimized, or telemetry is not visible."""
    if not hwnd or win32gui.IsIconic(hwnd):
        return None
    try:
        left, top = win32gui.ClientToScreen(hwnd, (0, 0))
        dc = ctypes.windll.user32.GetDC(0)
        try:
            colors = []
            for index in range(CELL_COUNT):
                pixel = ctypes.windll.gdi32.GetPixel(
                    dc, left + DATA_X + index * CELL_SIZE + CELL_SIZE // 2, top + 2)
                if pixel == 0xFFFFFFFF:
                    return None
                colors.append((pixel & 255, (pixel >> 8) & 255,
                               (pixel >> 16) & 255))
        finally:
            ctypes.windll.user32.ReleaseDC(0, dc)
        return decode_colors(colors)
    except Exception:
        return None
