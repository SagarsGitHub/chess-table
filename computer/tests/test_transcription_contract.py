"""Host side of the shared host/Aina transcription contract.

Normalization only cleans bounded speech artifacts. The fixture's ``recognized``
field describes Aina's syntax recognition, not legality in any chess position.
Legal resolution remains covered by test_move_expression.
"""

import json
from pathlib import Path
import unittest

from chess_table_host.move_expression import normalize_move_transcription


CONTRACT_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "chess_transcription_contract.json"


class TranscriptionContractTests(unittest.TestCase):
    def test_normalization_matches_shared_contract(self):
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        self.assertEqual(contract["version"], 1)
        identifiers = set()
        for case in contract["cases"]:
            with self.subTest(case=case["id"], text=case["input"]):
                self.assertNotIn(case["id"], identifiers)
                identifiers.add(case["id"])
                self.assertIsInstance(case["recognized"], bool)
                self.assertEqual(normalize_move_transcription(case["input"]), case["normalized"])


if __name__ == "__main__":
    unittest.main()
