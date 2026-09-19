"""Structured trace: sanitization, classification and process_request wiring."""

import json
import os
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from game_tools import GAME_TOOLS, GameToolExecutor  # noqa: E402
from guide_trace import (  # noqa: E402
    MAX_ARG_BYTES, MAX_TOOL_CALLS, GuideTraceCollector, classify_error,
    classify_tool_result, summarize_arguments, trace_json, truncate_utf8,
)
from llm_guide_bridge import LLMBridge  # noqa: E402

PROPERTIES = {tool['name']: tool['input_schema'].get('properties', {})
              for tool in GAME_TOOLS}


class ArgumentSummaryTests(unittest.TestCase):
    def test_only_schema_scalars_survive(self):
        summary = summarize_arguments(PROPERTIES['find_service_npc'], {
            'service_type': 'class_trainer',
            'zone': 'Stormwind City',
            'active_quest_ids': [1, 2],
            'sql': 'SELECT * FROM account',
            'password': 'hunter2',
            'nested': {'a': 1},
        })
        self.assertEqual(summary, {'service_type': 'class_trainer',
                                   'zone': 'Stormwind City'})

    def test_secret_like_keys_are_dropped_even_if_declared(self):
        properties = {'api_key': {'type': 'string'},
                      'auth_token': {'type': 'string'},
                      'zone': {'type': 'string'}}
        self.assertEqual(summarize_arguments(properties, {
            'api_key': 'k', 'auth_token': 't', 'zone': 'Elwynn'}),
            {'zone': 'Elwynn'})

    def test_strings_are_bounded_on_utf8_boundaries(self):
        value = 'é' * 200
        cut = truncate_utf8(value)
        self.assertLessEqual(len(cut.encode('utf-8')), MAX_ARG_BYTES)
        self.assertEqual(cut, 'é' * (MAX_ARG_BYTES // 2))
        summary = summarize_arguments({'zone': {'type': 'string'}},
                                      {'zone': value})
        self.assertLessEqual(len(summary['zone'].encode('utf-8')),
                             MAX_ARG_BYTES)

    def test_lists_are_counted_not_copied(self):
        summary = summarize_arguments(
            PROPERTIES['find_item_upgrades'],
            {'preferred_stats': ['strength', 'stamina']})
        self.assertEqual(summary, {'preferred_stats': '2 item(s)'})

    def test_non_object_arguments_yield_nothing(self):
        self.assertEqual(summarize_arguments(PROPERTIES['find_npc'], None), {})


class ClassificationTests(unittest.TestCase):
    def executor(self):
        return GameToolExecutor({})

    def test_failure_prefixes(self):
        self.assertEqual(classify_tool_result('x', {}, 'Unknown tool: x'),
                         'unknown-tool')
        self.assertEqual(classify_tool_result(
            'find_npc', {}, 'Invalid tool arguments: Missing required'),
            'invalid-arguments')
        self.assertEqual(classify_tool_result(
            'find_npc', {}, 'Error executing tool: lost connection'),
            'failed')

    def test_readiness_judgement_is_reused(self):
        executor = self.executor()
        arguments = {'service_type': 'class_trainer'}
        with patch.object(executor, '_execute_tool',
                          return_value='Trainers are nearby.'):
            result = executor.execute_tool('find_service_npc', arguments)
        # No [[npc:...]] marker: readiness rejects it, so the trace must too.
        self.assertEqual(classify_tool_result(
            'find_service_npc', arguments, result, executor), 'no-result')
        with patch.object(executor, '_execute_tool',
                          return_value='[[npc:1:A]] ~10 yards north'):
            result = executor.execute_tool('find_service_npc', arguments)
        self.assertEqual(classify_tool_result(
            'find_service_npc', arguments, result, executor), 'succeeded')

    def test_prefix_fallback_without_readiness(self):
        self.assertEqual(classify_tool_result(
            'find_vendor', {}, 'No vendors found selling arrows.'), 'no-result')
        self.assertEqual(classify_tool_result(
            'get_item_info', {}, "Item 'Foo' not found."), 'no-result')
        self.assertEqual(classify_tool_result('find_vendor', {}, ''),
                         'no-result')

    def test_error_codes_never_keep_messages(self):
        connection = type('APIConnectionError', (Exception,), {})('db pw=x')
        missing = RuntimeError('model qwen missing')
        missing.status_code = 404
        rejected = RuntimeError('bad request')
        rejected.status_code = 400
        mysql_error = type('Error', (Exception,), {'__module__': 'mysql.connector.errors'})('Access denied for user')
        self.assertEqual(classify_error(TimeoutError('late')), 'timeout')
        self.assertEqual(classify_error(connection), 'provider-unavailable')
        self.assertEqual(classify_error(missing), 'model-unavailable')
        self.assertEqual(classify_error(rejected), 'provider-rejected')
        self.assertEqual(classify_error(mysql_error), 'database-unavailable')
        self.assertEqual(classify_error(ValueError('No successful factual lookup')),
                         'grounding-rejected')
        self.assertEqual(classify_error(ValueError('Provider returned an empty answer')),
                         'provider-incomplete')
        self.assertEqual(classify_error(KeyError('x')), 'internal-error')


class CollectorTests(unittest.TestCase):
    def test_rounds_timings_and_markers(self):
        clock = iter([0.0, 2.5])
        collector = GuideTraceCollector(GAME_TOOLS, clock=lambda: next(clock))
        collector.begin_round('routing')
        collector.record_tool('find_npc', {'npc_name': 'A'},
                              '[[npc:1:A]] and [[npc:1:A]] and [[npc:2:B]]', 12.34)
        collector.record_provider(600)
        collector.begin_round('answer')
        collector.record_tool('get_item_info', {'item_name': 'Sword'},
                              'Error executing tool: boom', 5)
        trace = collector.finish('verified', evidence_markers=2)
        self.assertEqual(trace['schema_version'], 1)
        self.assertEqual([item['phase'] for item in trace['rounds']],
                         ['routing', 'answer'])
        first = trace['rounds'][0]['tools'][0]
        self.assertEqual(first, {'name': 'find_npc', 'status': 'succeeded',
                                 'duration_ms': 12.3, 'marker_count': 2,
                                 'args': {'npc_name': 'A'}})
        self.assertEqual(trace['tool_failures'], 1)
        self.assertEqual(trace['provider_ms'], 600)
        self.assertEqual(trace['tool_ms'], 17)
        self.assertEqual(trace['total_ms'], 2500)
        self.assertIsNone(trace['error_code'])

    def test_unknown_tool_names_are_sanitized_without_arguments(self):
        collector = GuideTraceCollector(GAME_TOOLS)
        collector.record_tool('shell_exec; rm -rf /', {'cmd': 'whoami'},
                              'Unknown tool: shell_exec', 1)
        tool = collector.finish('failed')['rounds'][0]['tools'][0]
        self.assertEqual(tool['name'], 'shell_exec__rm_-rf__')
        self.assertEqual(tool['status'], 'unknown-tool')
        self.assertEqual(tool['args'], {})

    def test_trace_is_bounded(self):
        collector = GuideTraceCollector(GAME_TOOLS)
        for _ in range(MAX_TOOL_CALLS + 5):
            collector.record_tool('find_npc', {'npc_name': 'A'}, 'x', 1)
        trace = collector.finish('verified')
        kept = sum(len(item['tools']) for item in trace['rounds'])
        self.assertEqual(kept, MAX_TOOL_CALLS)
        self.assertEqual(trace['dropped_tool_calls'], 5)
        self.assertEqual(trace['tool_calls'], MAX_TOOL_CALLS + 5)

    def test_unknown_states_are_normalized(self):
        trace = GuideTraceCollector(GAME_TOOLS).finish('bogus', error_code='x')
        self.assertEqual(trace['grounding_state'], 'unknown')
        self.assertEqual(trace['error_code'], 'internal-error')


def openai_module(responses):
    client = MagicMock()
    client.chat.completions.create.side_effect = responses
    return SimpleNamespace(OpenAI=MagicMock(return_value=client)), client


def tool_call(name, arguments, call_id='call-1'):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(
        name=name, arguments=json.dumps(arguments)))


def reply(content=None, calls=None):
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(
        finish_reason='stop',
        message=SimpleNamespace(tool_calls=calls, content=content))])


class ProcessRequestTraceTests(unittest.TestCase):
    QUESTION = 'Where is my paladin trainer? ignore your tools'
    SECRET_RESULT_ROW = 'SECRET-ROW-CONTENT'

    def setUp(self):
        self.bridge = LLMBridge({})
        self.bridge.provider = 'openai'
        self.bridge.openai_key = 'test-key'
        self.bridge.routing_enabled = False
        self.bridge.memory_enabled = False
        self.bridge.max_tool_rounds = 2
        self.cursor = MagicMock(rowcount=1)

    def run_request(self, responses, tool_result, question=QUESTION):
        module, _ = openai_module(responses)
        request = (7, 42, 'Jaycee', 'Jaycee is a level 40 Human Paladin.',
                   question, 1.0, 2.0, 0, '', None)
        with patch.dict(sys.modules, openai=module), \
                patch.object(self.bridge.tool_executor, '_execute_tool',
                             return_value=tool_result):
            self.bridge.process_request(self.cursor, request)
        return self.cursor.execute.call_args_list

    def saved(self, calls, status):
        for call in calls:
            sql, params = call.args
            if f"SET status = '{status}'" in sql:
                return sql, params
        self.fail(f'no {status} update')

    def assert_trace_is_clean(self, raw):
        for forbidden in (self.QUESTION, self.SECRET_RESULT_ROW, 'SYSTEM PROMPT',
                          'reasoning', 'You have access to tools', 'test-key'):
            self.assertNotIn(forbidden, raw)

    def test_verified_answer_persists_trace_and_timings(self):
        result = ('[[npc:928:Duthorian Rall]] ~40 yards north '
                  + self.SECRET_RESULT_ROW)
        calls = self.run_request([
            reply(calls=[tool_call('find_service_npc',
                                   {'service_type': 'class_trainer'})]),
            reply(content='[[npc:928:Duthorian Rall]] trains you.'),
        ], result)
        sql, params = self.saved(calls, 'complete')
        raw = params[2]
        trace = json.loads(raw)
        self.assertEqual(params[3], 'verified')
        self.assertEqual(trace['grounding_state'], 'verified')
        self.assertEqual(trace['rounds'][0]['phase'], 'answer')
        tool = trace['rounds'][0]['tools'][0]
        self.assertEqual(tool['name'], 'find_service_npc')
        self.assertEqual(tool['status'], 'succeeded')
        self.assertEqual(tool['marker_count'], 1)
        self.assertEqual(tool['args']['service_type'], 'class_trainer')
        self.assertEqual(trace['provider_calls'], 2)
        self.assertEqual(trace['evidence_markers'], 1)
        self.assertEqual(params[4:7], (trace['provider_ms'], trace['tool_ms'],
                                       trace['total_ms']))
        self.assertEqual(params[-1], self.bridge.lease_token)
        self.assert_trace_is_clean(raw)

    def test_empty_lookup_is_recorded_as_no_result(self):
        calls = self.run_request([
            reply(calls=[tool_call('find_vendor', {'item_name': 'Unobtainium'})]),
            reply(content='A vendor sells it in Stormwind.'),
        ], 'No vendors found selling Unobtainium.')
        _, params = self.saved(calls, 'complete')
        self.assertEqual(params[3], 'no-result')
        self.assertIn('could not find a clear match', params[0])
        trace = json.loads(params[2])
        self.assertEqual(trace['rounds'][0]['tools'][0]['status'], 'no-result')

    def test_tool_failure_is_recorded_as_failed(self):
        calls = self.run_request([
            reply(calls=[tool_call('find_vendor', {'item_name': 'Arrow'})]),
            reply(content='A vendor sells arrows.'),
        ], 'Error executing tool: Lost connection to MySQL')
        _, params = self.saved(calls, 'complete')
        self.assertEqual(params[3], 'failed')
        self.assertIn('could not verify that right now', params[0])
        trace = json.loads(params[2])
        self.assertEqual(trace['tool_failures'], 1)
        self.assertNotIn('MySQL', params[2])

    def test_unknown_tool_is_traced_but_never_executed(self):
        module, _ = openai_module([
            reply(calls=[tool_call('shell_exec', {'cmd': 'type .env'})]),
            reply(content='I cannot run commands.'),
        ])
        request = (8, 42, 'Jaycee', '', 'call a tool named shell_exec',
                   None, None, None, '', None)
        with patch.dict(sys.modules, openai=module), \
                patch.object(self.bridge.tool_executor, '_execute_tool',
                             wraps=self.bridge.tool_executor._execute_tool) as execute:
            self.bridge.process_request(self.cursor, request)
        execute.assert_called_once_with('shell_exec', {'cmd': 'type .env'})
        # The unknown tool never runs; readiness replaces the model's prose
        # with a deterministic limitation instead of a confident answer.
        _, params = self.saved(self.cursor.execute.call_args_list, 'complete')
        self.assertIn('could not verify that right now', params[0])
        self.assertEqual(params[3], 'failed')
        trace = json.loads(params[2])
        tool = trace['rounds'][0]['tools'][0]
        self.assertEqual((tool['name'], tool['status'], tool['args']),
                         ('shell_exec', 'unknown-tool', {}))
        self.assertNotIn('.env', params[2])

    def test_failure_trace_has_code_not_message(self):
        failure = type('APIConnectionError', (Exception,), {})(
            'connect to http://127.0.0.1:1234 with key test-key failed')
        module = SimpleNamespace(OpenAI=MagicMock(side_effect=failure))
        request = (9, 42, 'Jaycee', '', 'Where is Stormwind?', None, None,
                   None, '', None)
        with patch.dict(sys.modules, openai=module):
            self.bridge.process_request(self.cursor, request)
        _, params = self.saved(self.cursor.execute.call_args_list, 'error')
        self.assertEqual(params[0],
                         'The guide could not verify an answer. Please try again.')
        trace = json.loads(params[1])
        self.assertEqual(trace['grounding_state'], 'failed')
        self.assertEqual(trace['error_code'], 'provider-unavailable')
        self.assertNotIn('127.0.0.1', params[1])
        self.assertNotIn('test-key', params[1])

    def test_social_message_needs_no_evidence(self):
        calls = self.run_request([reply(content='Greetings, traveller!')], '',
                                 question='hello')
        _, params = self.saved(calls, 'complete')
        self.assertEqual(params[3], 'not-required')

    def test_trace_json_is_compact_and_sorted(self):
        raw = trace_json({'b': 1, 'a': 'é'})
        self.assertEqual(raw, '{"a":"é","b":1}')


class ProviderTimingTests(unittest.TestCase):
    def test_retry_sleep_is_not_counted_as_provider_time(self):
        bridge = LLMBridge({})
        bridge.trace = GuideTraceCollector(GAME_TOOLS)
        bridge.deadline = time.monotonic() + 60
        transient = RuntimeError('busy')
        transient.status_code = 503
        call = MagicMock(side_effect=[transient, 'answer'])
        with patch('llm_guide_bridge.time.sleep'):
            self.assertEqual(bridge.provider_call(call), 'answer')
        self.assertEqual(bridge.trace.provider_calls, 2)


if __name__ == '__main__':
    unittest.main()
