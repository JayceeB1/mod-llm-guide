"""A request that names what it asks about must not depend on the session.

Regression found by the Azeroth Control live qualification: as soon as a
character had any Session History, ``route_question`` ran the conversation
resolver (an LLM call) on EVERY request. When the model answered ``clarify`` or
``unsupported`` for a question that named everything it needed, the request
ended before any tool ran, and the refusal was stored as a new memory that fed
the next request. Only 5 of 14 independent questions reached the tools.

The resolver is now consulted only when the request really depends on an
earlier turn. Memory stays enabled and is still stored for every request.

Run from the repository root: ``python -m unittest discover -s tests -v``.
"""

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools'))

import guide_conversation as gc  # noqa: E402
import llm_guide_bridge as bridge_module  # noqa: E402


def ready(request, constraints=''):
    return dict(request=request, topic='request', constraints=constraints,
                status='ready', question='')


def clarify(request, question='Dans quelle zone ?'):
    return dict(request=request, topic='clarification', constraints='',
                status='clarify', question=question)


def memory(question, response='Voici ce que j\'ai trouvé.', context=None):
    return {'question': question, 'response': response,
            'context': context if context is not None else ready(question)}


PAST_QUESTIONS = (
    'Où est mon maître de classe le plus proche ?',
    'Qui donne la quête The Defias Brotherhood ?',
    'Que peux-tu me dire sur l\'objet Hearthstone ?',
    'À quel niveau un guerrier apprend-il Whirlwind ?',
    'Comment gagner de la réputation avec Argent Dawn ?',
)


def five_memories(**overrides):
    rows = [memory(question) for question in PAST_QUESTIONS]
    rows[-1] = dict(rows[-1], **overrides)
    return rows


# Questions that name what they ask about: they must never need the session.
STANDALONE = (
    'Où est mon maître de classe le plus proche ?',
    'Où est l\'aubergiste le plus proche ?',
    'Que peux-tu me dire sur l\'objet Hearthstone ?',
    'Qui donne la quête The Defias Brotherhood ?',
    'À quel niveau un guerrier apprend-il Whirlwind ?',
    'Qu\'est-ce que je peux apprendre chez mon maître à mon niveau actuel ?',
    'Qu\'est-ce que je peux miner dans les Badlands ?',
    'Quelle est la tranche de niveau et l\'entrée de Blackrock Depths ?',
    'Qu\'est-ce qu\'Edwin VanCleef peut lâcher ?',
    'Comment gagner de la réputation avec Argent Dawn ?',
    'Que devrais-je faire ensuite ? Quelles quêtes sont disponibles pour moi '
    'et où est mon maître de classe ?',
    'Où puis-je obtenir l\'Épée du Néant impossible ?',
    'What is quest 99999999? Use the exact quest ID.',
    'Where is the nearest flight master?',
    'Où est la banque à Orgrimmar ?',
    'Where can I find the innkeeper in Orgrimmar?',
    'Où est le forgeron de Stormwind ?',
    'Which trainer teaches Cold Weather Flying?',
)

# Requests that cannot be understood without an earlier turn.
FOLLOW_UPS = (
    'Et l\'aubergiste ?',
    'Et pour un mage ?',
    'Et pour Stormwind ?',
    'Et le second ?',
    'Combien coûte celui-là ?',
    'Lequel est le plus proche ?',
    'Cette quête est-elle facile ?',
    'Où est ce PNJ ?',
    'Où puis-je l\'acheter ?',
    'Que peut-il m\'apprendre ?',
    'Où est-ce que je peux en acheter ?',
    'Où se trouve celui qui la donne ?',
    'Hearthstone ?',
    'Durotar',
    'Le premier',
    'Where can I buy it?',
    'And what about the innkeeper?',
    'What about the second one?',
    'How do I get there?',
    'Is it worth doing?',
)


class Gate(unittest.TestCase):
    def test_a_standalone_question_never_needs_the_session(self):
        for question in STANDALONE:
            with self.subTest(question=question):
                self.assertFalse(gc.needs_conversation_context(question, five_memories()))
                self.assertEqual(gc.replayable_history(question, five_memories()), [])

    def test_a_follow_up_keeps_the_conversation(self):
        for question in FOLLOW_UPS:
            with self.subTest(question=question):
                recent = five_memories()
                self.assertTrue(gc.needs_conversation_context(question, recent))
                self.assertEqual(gc.replayable_history(question, recent), recent)

    def test_nothing_to_consult_without_memories(self):
        for question in (*STANDALONE, *FOLLOW_UPS):
            with self.subTest(question=question):
                self.assertFalse(gc.needs_conversation_context(question, []))
                self.assertEqual(gc.replayable_history(question, []), [])

    def test_a_short_reply_answers_the_last_clarification(self):
        recent = five_memories(context=clarify('Où est l\'aubergiste ?'))
        for reply in ('Dans Durotar', 'Je suis à Razor Hill', 'Razor Hill'):
            with self.subTest(reply=reply):
                self.assertTrue(gc.needs_conversation_context(reply, recent))

    def test_a_full_question_after_a_clarification_still_stands_alone(self):
        # The failure loop: a stored clarification must not drag the next
        # independent question back into the resolver.
        recent = five_memories(context=clarify('Où est l\'aubergiste ?'))
        for question in ('Où est la banque à Orgrimmar ?',
                         'Where is the nearest flight master?',
                         'Qui donne la quête The Defias Brotherhood ?'):
            with self.subTest(question=question):
                self.assertFalse(gc.needs_conversation_context(question, recent))

    def test_an_unsupported_capability_is_treated_like_a_clarification(self):
        recent = five_memories(context=dict(clarify('Route ?'), status='unsupported'))
        self.assertTrue(gc.needs_conversation_context('Dans Durotar', recent))
        self.assertFalse(gc.needs_conversation_context('Où est la banque à Orgrimmar ?', recent))

    def test_legacy_memories_without_a_stored_context_are_handled(self):
        recent = five_memories(context=None)
        recent[-1]['context'] = None
        self.assertFalse(gc.needs_conversation_context('Où est la banque à Orgrimmar ?', recent))
        self.assertTrue(gc.needs_conversation_context('Et l\'aubergiste ?', recent))


class FakeLLM:
    """Records every provider call the engine makes and answers deterministically."""

    def __init__(self, plan, context=None, answer='Frang est près de Razor Hill.'):
        self.plan = plan
        self.context = context
        self.answer = answer
        self.calls = []

    def __call__(self, question, system_prompt=None, memories_recent=None, routing=False):
        kind = 'context' if routing == 'context' else 'routing' if routing else 'answer'
        self.calls.append(SimpleNamespace(
            kind=kind, question=question, system_prompt=system_prompt or '',
            memories=list(memories_recent or [])))
        if kind == 'context':
            return json.dumps([{'tool_name': 'resolve_request', 'tool_input': self.context}]), 10, False
        if kind == 'routing':
            return json.dumps(self.plan), 10, False
        return self.answer, 10, True

    def kinds(self):
        return [call.kind for call in self.calls]


INNKEEPER = [{'tool_name': 'find_service_npc',
              'tool_input': {'service_type': 'innkeeper', 'zone': 'Durotar'}}]
MINING = [{'tool_name': 'get_zone_mining', 'tool_input': {'zone': 'Badlands'}}]


def make_bridge(llm, memories=None):
    bridge = object.__new__(bridge_module.LLMBridge)
    bridge.provider = 'ollama'
    bridge.routing_enabled = True
    bridge.conversation_enabled = True
    bridge.memory_enabled = True
    bridge.routing_max_calls = 3
    bridge.followup_limit = 8
    bridge.request_timeout = 120
    bridge.answer_target_words = 60
    bridge.distance_unit = 'yards'
    bridge.system_prompt = 'You are a helpful WoW guide.'
    bridge.conversation_context = None
    bridge.api_clients = []
    bridge.tool_executor = mock.MagicMock()
    bridge.tool_executor.default_zone = 'Durotar'
    bridge.tool_executor.finalize_answer.side_effect = lambda response, *a, **k: response
    bridge.remaining_timeout = lambda: None
    bridge.execute_traced_tool = lambda name, arguments: f'{name} result'
    bridge.call_llm = llm
    bridge.mark_processing = lambda cursor, request_id: True
    bridge.fetch_memories = lambda cursor, guid: memories or {'recent': [], 'older_topics': []}
    bridge.grounding_state = lambda question, clarified: 'verified'
    bridge.saved = []
    bridge.save_response = lambda cursor, request_id, response, tokens, trace=None: bridge.saved.append(response) or True
    bridge.generate_summary = lambda question, response: 'legacy summary'
    bridge.stored = []
    bridge.store_memory = lambda cursor, guid, name, summary, question=None, response=None: bridge.stored.append(
        dict(summary=summary, question=question, response=response))
    return bridge


class Routing(unittest.TestCase):
    """The three behaviours the live qualification exposed."""

    def route(self, question, recent, llm):
        bridge = make_bridge(llm)
        history = gc.replayable_history(question, recent)
        return bridge, bridge.route_question(question, 'Heroproof is in Durotar.', history)

    def test_a_standalone_question_after_five_memories_goes_straight_to_the_tools(self):
        question = 'Où est l\'aubergiste le plus proche ?'
        # If the resolver ran it would refuse: this is the trap the old wiring fell into.
        llm = FakeLLM(INNKEEPER, context=clarify(question))
        bridge, (lookup, clarification, _tokens) = self.route(question, five_memories(), llm)
        self.assertIsNone(clarification)
        self.assertIn('find_service_npc', lookup)
        self.assertEqual(llm.kinds(), ['routing'])
        self.assertEqual(llm.calls[0].memories, [])

    def test_the_trap_catches_the_old_wiring(self):
        # Characterisation of the defect: handing every memory to route_question
        # runs the resolver and ends the request with a clarification.
        question = 'Où est l\'aubergiste le plus proche ?'
        llm = FakeLLM(INNKEEPER, context=clarify(question))
        bridge = make_bridge(llm)
        lookup, clarification, _tokens = bridge.route_question(question, 'ctx', five_memories())
        self.assertEqual(clarification, 'Dans quelle zone ?')
        self.assertEqual(lookup, '')
        self.assertEqual(llm.kinds(), ['context'])

    def test_a_real_follow_up_uses_the_conversation_context(self):
        question = 'Et l\'aubergiste ?'
        resolved = ready('Où est l\'aubergiste le plus proche ?')
        llm = FakeLLM(INNKEEPER, context=resolved)
        recent = five_memories()
        bridge, (lookup, clarification, _tokens) = self.route(question, recent, llm)
        self.assertIsNone(clarification)
        self.assertEqual(llm.kinds(), ['context', 'routing'])
        # The resolver saw the earlier turns as data...
        self.assertEqual(llm.calls[0].question, question)
        self.assertIn(PAST_QUESTIONS[-1], llm.calls[0].system_prompt)
        # ...and routing worked on the standalone rewrite, with the history.
        self.assertEqual(llm.calls[1].question, resolved['request'])
        self.assertEqual(llm.calls[1].memories, recent)
        self.assertEqual(bridge.conversation_context, resolved)
        self.assertIn('find_service_npc', lookup)

    def test_a_follow_up_can_still_be_clarified_by_the_resolver(self):
        llm = FakeLLM(INNKEEPER, context=clarify('Et le second ?', 'Lequel des deux ?'))
        _bridge, (lookup, clarification, _tokens) = self.route('Et le second ?', five_memories(), llm)
        self.assertEqual(clarification, 'Lequel des deux ?')
        self.assertEqual(lookup, '')
        self.assertEqual(llm.kinds(), ['context'])

    def test_a_topic_change_ignores_the_old_context(self):
        old = five_memories(context=ready('Quelles armes en bronze ?', constraints='crafted only'))
        question = 'Qu\'est-ce que je peux miner dans les Badlands ?'
        llm = FakeLLM(MINING, context=ready(question, constraints='crafted only'))
        bridge, (lookup, clarification, _tokens) = self.route(question, old, llm)
        self.assertIsNone(clarification)
        self.assertIn('get_zone_mining', lookup)
        self.assertEqual(llm.kinds(), ['routing'])
        self.assertEqual(llm.calls[0].memories, [])
        # The resolved intent is fresh: no constraint leaked from the old goal.
        self.assertEqual(bridge.conversation_context['request'], question)
        self.assertEqual(bridge.conversation_context['constraints'], '')

    def test_routing_is_the_same_with_or_without_memories_for_a_standalone_question(self):
        question = 'Où est la banque à Orgrimmar ?'
        plan = [{'tool_name': 'find_service_npc', 'tool_input': {'service_type': 'banker', 'zone': 'Orgrimmar'}}]
        with_memories = FakeLLM(plan, context=clarify(question))
        without = FakeLLM(plan)
        self.route(question, five_memories(), with_memories)
        self.route(question, [], without)
        self.assertEqual([(c.kind, c.question, c.memories) for c in with_memories.calls],
                         [(c.kind, c.question, c.memories) for c in without.calls])


class Wiring(unittest.TestCase):
    """process_request must apply the gate to routing, the answer and the trace."""

    REQUEST = (44, 2004, 'Heroproof', 'Heroproof is a level 55 Troll Warrior in Durotar. Horde.',
               None, None, None, None, '', None)

    def process(self, question, memories, llm):
        bridge = make_bridge(llm, memories)
        request = self.REQUEST[:4] + (question,) + self.REQUEST[5:]
        with mock.patch.object(bridge_module, 'reverify_followup',
                               side_effect=lambda response, *a, **k: (response, False)):
            bridge.process_request(mock.MagicMock(), request)
        return bridge

    @staticmethod
    def session(context=None):
        return {'recent': five_memories(), 'older_topics': ['Hearthstone', 'Whirlwind']}

    def test_a_standalone_question_ignores_the_session_end_to_end(self):
        question = 'Où est l\'aubergiste le plus proche ?'
        llm = FakeLLM(INNKEEPER, context=clarify(question))
        bridge = self.process(question, self.session(), llm)
        self.assertEqual(llm.kinds(), ['routing', 'answer'])
        for call in llm.calls:
            self.assertEqual(call.memories, [], call.kind)
            self.assertNotIn('Previously discussed topics', call.system_prompt, call.kind)
        self.assertEqual(bridge.saved, ['Frang est près de Razor Hill.'])
        # Nothing was replayed, and the trace says so truthfully.
        self.assertEqual(bridge.trace.memory_context_entries, 0)
        self.assertTrue(bridge.trace.memory_enabled)

    def test_memory_is_still_stored_for_a_standalone_question(self):
        question = 'Où est l\'aubergiste le plus proche ?'
        bridge = self.process(question, self.session(), FakeLLM(INNKEEPER, context=clarify(question)))
        self.assertEqual(len(bridge.stored), 1)
        self.assertEqual(bridge.stored[0]['question'], question)
        stored = gc.decode_context(bridge.stored[0]['summary'])
        self.assertEqual(stored['request'], question)
        self.assertEqual(stored['status'], 'ready')

    def test_a_follow_up_replays_the_session_end_to_end(self):
        question = 'Et l\'aubergiste ?'
        llm = FakeLLM(INNKEEPER, context=ready('Où est l\'aubergiste le plus proche ?'))
        bridge = self.process(question, self.session(), llm)
        self.assertEqual(llm.kinds(), ['context', 'routing', 'answer'])
        self.assertEqual(len(llm.calls[1].memories), 5)
        self.assertEqual(len(llm.calls[2].memories), 5)
        self.assertIn('Previously discussed topics: Hearthstone, Whirlwind', llm.calls[2].system_prompt)
        self.assertEqual(bridge.trace.memory_context_entries, 5)
        self.assertEqual(len(bridge.stored), 1)


if __name__ == '__main__':
    unittest.main()
