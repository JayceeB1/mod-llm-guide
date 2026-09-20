"""A tool call written as the answer is never published, whatever else succeeded.

Regression found by the Azeroth Control qualification. On the ollama lane the
last round carries no tool catalog. When the model still wants another lookup it
writes the call as text (``<tool_call><function=get_item_info>...``). Readiness
saw one unrelated lookup that had succeeded (``get_character_context``, or the
first quest lookup), called the answer verified, appended "Some requested
details remain unverified." and published the raw markup to the player. The
qualification corpus scores this as ``raw_no_tool_leak`` and ``product_safety``.

Run from the repository root: ``python -m unittest discover -s tests -v``.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools'))

from game_tools import GameToolExecutor  # noqa: E402
from guide_readiness import UNFINISHED_ANSWER, leaks_tool_call  # noqa: E402
from llm_guide_bridge import LLMBridge  # noqa: E402

# The two answers the live qualification published on 2026-09-20.
NO_RESULT_LEAK = (
    '<tool_call> <function=get_item_info> <parameter=item_name> Épée de la Mort '
    '</parameter> </function> </tool_call>')
QUEST_LEAK = (
    '<tool_call> <function=find_available_quests> <parameter=zone> stormwind '
    '</parameter> <parameter=faction> defias_brotherhood </parameter> </function> </tool_call>')

LEAKS = (
    NO_RESULT_LEAK,
    QUEST_LEAK,
    'Je cherche encore. <tool_call><function=find_npc><parameter=npc_name>Gryan</parameter></function></tool_call>',
    '<TOOL_CALL>\n<FUNCTION=get_item_info>\n</FUNCTION>\n</TOOL_CALL>',
    'Voici le résultat </function>',
    '<function=get_quest_info>',
)

CLEAN = (
    'Frang est près de Razor Hill. [[npc:3143:Orgnil Soulscar]]',
    'The function of this vendor is to sell reagents, and a tool call is not needed.',
    'Level < 10 and > 5 only; see <the map> in Durotar.',
    'Le forgeron vend des armes [[item:37:Worn Axe:1]] (niveau 1).',
    '',
)


def executor():
    return GameToolExecutor({})


def executor_of_a_request():
    """As process_request builds it: the request carries the character summary."""
    ex = executor()
    ex.begin_request(None, 'Heroproof is a level 55 Troll Warrior in Durotar.')
    return ex


def bridge_for(tool_executor):
    bridge = object.__new__(LLMBridge)
    bridge.tool_executor = tool_executor
    return bridge


class Detection(unittest.TestCase):
    def test_every_shape_of_tool_call_markup_is_a_leak(self):
        for text in LEAKS:
            with self.subTest(text=text[:60]):
                self.assertTrue(leaks_tool_call(text))

    def test_ordinary_answers_are_not(self):
        for text in CLEAN:
            with self.subTest(text=text[:60]):
                self.assertFalse(leaks_tool_call(text))

    def test_nothing_to_detect_in_a_missing_answer(self):
        self.assertFalse(leaks_tool_call(None))


class Publication(unittest.TestCase):
    def lookup(self, tool_executor, name, result, **arguments):
        with patch.object(tool_executor, '_execute_tool', return_value=result):
            return tool_executor.execute_tool(name, arguments)

    def test_a_leak_is_replaced_even_when_an_unrelated_lookup_succeeded(self):
        # The live failure: five empty item searches and one successful character lookup.
        ex = executor_of_a_request()
        for name in ('Épée du Néant', 'Void Sword', "Épée de l'Abîme", 'Néant'):
            self.lookup(ex, 'get_item_info', f"Item '{name}' not found.", item_name=name)
        self.lookup(ex, 'get_character_context', '{"version": "1", "summary": "Heroproof is a level 55 Warrior."}')
        self.assertFalse(ex.readiness.blocked())  # readiness alone would have let the text through
        answer = ex.finalize_answer(NO_RESULT_LEAK, True)
        self.assertEqual(answer, UNFINISHED_ANSWER)
        self.assertFalse(leaks_tool_call(answer))
        self.assertNotIn('unverified', answer)

    def test_a_leak_is_replaced_after_lookups_that_found_something(self):
        ex = executor()
        self.lookup(ex, 'get_quest_info', 'Quest: [[quest:65:The Defias Brotherhood:0]] (level 10)')
        answer = ex.finalize_answer(QUEST_LEAK, True)
        self.assertEqual(answer, UNFINISHED_ANSWER)

    def test_a_leak_is_replaced_before_any_lookup_ran(self):
        self.assertEqual(executor().finalize_answer(NO_RESULT_LEAK, True), UNFINISHED_ANSWER)

    def test_a_leak_is_replaced_when_the_answer_needs_no_evidence(self):
        self.assertEqual(executor().finalize_answer(NO_RESULT_LEAK, False), UNFINISHED_ANSWER)

    def test_readiness_being_disabled_does_not_let_a_leak_through(self):
        ex = executor()
        ex.readiness_enabled = False
        self.assertEqual(ex.finalize_answer(NO_RESULT_LEAK, True), UNFINISHED_ANSWER)

    def test_a_partial_answer_with_trailing_markup_is_not_published(self):
        text = LEAKS[2]
        self.assertEqual(executor().finalize_answer(text, False), UNFINISHED_ANSWER)

    def test_the_replacement_asks_for_a_narrower_question(self):
        self.assertIn('name one specific', UNFINISHED_ANSWER)
        self.assertNotIn('<', UNFINISHED_ANSWER)

    def test_a_grounded_answer_is_unchanged(self):
        ex = executor()
        self.lookup(ex, 'get_quest_info', 'Quest: [[quest:65:The Defias Brotherhood:0]] (level 10)')
        text = 'The Defias Brotherhood is a level 10 quest. [[quest:65:The Defias Brotherhood:0]]'
        answer = ex.finalize_answer(text, True)
        self.assertIn('level 10 quest', answer)
        self.assertFalse(ex.readiness.unfinished)

    def test_a_readiness_limitation_is_unchanged(self):
        ex = executor()
        self.lookup(ex, 'find_service_npc', 'Found [[npc:1:A]] in Darkshire')
        answer = ex.finalize_answer('The innkeeper is [[npc:1:A]].', True)
        self.assertIn('Distance is unavailable', answer)
        self.assertFalse(ex.readiness.unfinished)


class Trace(unittest.TestCase):
    QUESTION = 'Où puis-je obtenir l\'Épée du Néant impossible ?'

    def test_a_replaced_leak_is_a_clarification_never_verified(self):
        ex = executor_of_a_request()
        with patch.object(ex, '_execute_tool', return_value='{"version": "1", "summary": "ok"}'):
            ex.execute_tool('get_character_context', {})
        ex.finalize_answer(NO_RESULT_LEAK, True)
        self.assertEqual(bridge_for(ex).grounding_state(self.QUESTION, False), 'clarification')

    def test_without_a_leak_the_same_lookups_stay_verified(self):
        # The control that proves the state above comes from the leak, not from the lookups.
        ex = executor_of_a_request()
        with patch.object(ex, '_execute_tool', return_value='{"version": "1", "summary": "ok"}'):
            ex.execute_tool('get_character_context', {})
        ex.finalize_answer('You are a level 55 Warrior.', True)
        self.assertEqual(bridge_for(ex).grounding_state(self.QUESTION, False), 'verified')

    def test_a_new_request_starts_without_the_flag(self):
        ex = executor()
        ex.finalize_answer(NO_RESULT_LEAK, True)
        self.assertTrue(ex.readiness.unfinished)
        ex.begin_request(None)
        self.assertFalse(ex.readiness.unfinished)


if __name__ == '__main__':
    unittest.main()
