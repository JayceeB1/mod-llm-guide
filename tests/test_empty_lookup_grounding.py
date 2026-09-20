"""An empty lookup is a no-result, whatever kind of entity was asked for.

Regression found by the Azeroth Control qualification: ``get_quest_info`` on an
unknown quest ID returns ``Quest '' not found.``. Readiness only recognised
``No ...`` / ``Unknown ...`` prefixes and ``Item '...' not found.``, so every
empty quest, zone, dungeon, boss, creature, battleground, faction or recipe
lookup was classified as ``succeeded`` and the answer as ``verified``, and the
model's honest "that quest does not exist" was published with full grounding
instead of the deterministic "I could not find a clear match".

Run from the repository root: ``python -m unittest discover -s tests -v``.
"""

import os
import sys
import unittest
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools'))

from guide_readiness import AnswerReadiness  # noqa: E402
from guide_trace import classify_tool_result  # noqa: E402

# Every "nothing found" shape the engine tools return.
EMPTY = (
    "Item 'Épée du Néant' not found.",
    "Quest '' not found.",
    "Quest 'The Defias Brotherhood' not found.",
    "Zone 'Nowhere' not found. Try zones like: darkshore, westfall, stranglethorn.",
    "Dungeon 'Nowhere' not found. Try names like: Deadmines, Wailing Caverns.",
    "Boss 'Nobody' not found.",
    "Creature 'Nothing' not found in Durotar.",
    "Creature 'Nothing' not found.",
    "Battleground 'Nowhere' not found. Try: warsong gulch, arathi basin.",
    "Faction 'Nobody' not found. Try: Argent Dawn, Timbermaw Hold.",
    "Recipe for 'Nothing' not found. Try a different name or check if it's a trainer-only recipe.",
    # Already recognised before the fix: kept as controls.
    "No NPC named 'Nobody' found in Durotar.",
    "No trainers found for 'swords'. Try: swords, maces.",
    "No mining nodes found in Nowhere. This zone may not have mining deposits.",
    "Please specify a quest name to look up.",
    "",
)

FOUND = (
    "Quest: [[quest:65:The Defias Brotherhood:0]] (level 10) given by [[npc:234:Gryan Stoutmantle]].",
    "Item: [[item:40244:The Impossible Dream:4]] (Epic/Purple)",
    "Zone: Durotar. Level 1-10. Notable NPCs: [[npc:3143:Orgnil Soulscar]].",
    "Dungeon: Deadmines, level 15-20, entrance in Westfall.",
)


def readiness_for(result):
    readiness = AnswerReadiness()
    readiness.record('get_quest_info', {'quest_id': 99999999}, result,
                     SimpleNamespace(snapshot=None, character_summary=''))
    return readiness


class EmptyLookups(unittest.TestCase):
    def test_every_empty_result_is_a_no_result_in_the_trace(self):
        for text in EMPTY:
            with self.subTest(result=text):
                self.assertEqual(classify_tool_result('get_quest_info', {}, text), 'no-result')

    def test_every_empty_result_leaves_the_answer_unverified(self):
        for text in EMPTY:
            with self.subTest(result=text):
                readiness = readiness_for(text)
                self.assertTrue(readiness.blocked())
                self.assertFalse(readiness.checks[next(iter(readiness.checks))][0])

    def test_a_found_result_is_still_a_success(self):
        for text in FOUND:
            with self.subTest(result=text):
                self.assertEqual(classify_tool_result('get_quest_info', {}, text), 'succeeded')
                self.assertFalse(readiness_for(text).blocked())

    def test_an_unknown_quest_id_ends_in_the_deterministic_no_match_answer(self):
        readiness = readiness_for("Quest '' not found.")
        honest = 'Quest 99999999 does not exist in the game database.'
        # The model was honest, but the engine must not publish it as a verified lookup.
        self.assertEqual(readiness.finalize(honest),
                         'I could not find a clear match. Which name or location do you mean?')

    def test_one_empty_lookup_does_not_erase_a_grounded_answer(self):
        readiness = AnswerReadiness()
        executor = SimpleNamespace(snapshot=None, character_summary='')
        readiness.record('get_quest_info', {'quest_name': 'Nothing'}, "Quest 'Nothing' not found.", executor)
        readiness.record('get_quest_info', {'quest_name': 'Defias'}, FOUND[0], executor)
        self.assertFalse(readiness.blocked())


if __name__ == '__main__':
    unittest.main()
