import json
import unittest

from scripts.clash_jp_fixed_ports import _json_output


class ClashFixedPortCliTests(unittest.TestCase):
    def test_json_output_is_ascii_safe_and_round_trips_unicode(self):
        payload = {"ok": True, "node": "🇯🇵日本"}

        rendered = _json_output(payload)

        self.assertTrue(rendered.isascii())
        self.assertEqual(json.loads(rendered), payload)


if __name__ == "__main__":
    unittest.main()
