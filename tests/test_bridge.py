import tempfile
import unittest
from pathlib import Path

from bridge import merge_events, read_events, summarize
from live_bridge import CELL_COUNT, decode_colors


def painted_packet(body, session=123, sequence=7):
    body = body.encode('utf-8')
    packet = (b'FH\x02' + bytes([len(body)]) + session.to_bytes(4, 'big')
              + sequence.to_bytes(2, 'big') + bytes([sum(body) % 256]) + body)
    packet += bytes(CELL_COUNT * 3 // 2 - len(packet))
    nibbles = [n for byte in packet for n in (byte >> 4, byte & 15)]
    return [tuple(8 + 16 * nibbles[i + offset] for offset in range(3))
            for i in range(0, CELL_COUNT * 3, 3)]


class BridgeTests(unittest.TestCase):
    def test_pixel_event_and_checksum(self):
        colors = painted_packet('1770000000|loot|456|2|达拉然铜币')
        event = decode_colors(colors)
        self.assertEqual((event['session'], event['sequence']), (123, 7))
        self.assertEqual((event['name'], event['quantity']), ('达拉然铜币', 2))
        colors[20] = (8, 8, 8)
        self.assertIsNone(decode_colors(colors))

    def test_disk_reconciliation_and_achievement(self):
        with tempfile.TemporaryDirectory() as folder:
            path = (Path(folder) / '_retail_' / 'WTF' / 'Account' / 'user'
                    / 'SavedVariables' / 'FishingHelperBridge.lua')
            path.parent.mkdir(parents=True)
            path.write_text('FishingHelperBridgeDB = {events = {\n'
                '"FH2|123|1|1770000000|start|",\n'
                '"FH2|123|7|1770000001|loot|456|2|达拉然铜币",\n'
                '"FH2|123|8|1770000002|achievement|2094|3|22|铜币成就|新铜币",\n'
                '}}', encoding='utf-8')
            disk = read_events(folder)
            live = decode_colors(painted_packet('1770000001|loot|456|2|达拉然铜币'))
            merged = merge_events(disk, [live])
            stats = summarize(merged)
            self.assertEqual(len(merged), 3)
            self.assertEqual(stats['items'][(456, '达拉然铜币')], 2)
            self.assertEqual(stats['achievements'][2094]['completed'], 3)


if __name__ == '__main__':
    unittest.main()
