"""Contract checks for the shared ingress, trusted console control and schema.

The C++ module cannot be compiled here, so these tests pin the observable
source contract that the worldserver build and the live proof then exercise:
one submit helper for every ingress, a console-only control command, origin
aware delivery, and a schema migration that never touches the base file.
"""

import base64
import hashlib
import os
import re
import sys
import unittest
from unittest.mock import patch

TOOLS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.dirname(TOOLS)
sys.path.insert(0, TOOLS)

from llm_guide_bridge import (  # noqa: E402
    LLMBridge, WEB_INGRESS_COLUMNS, WEB_INGRESS_KEYS,
)

SCRIPT = os.path.join(ROOT, 'src', 'LLMGuideScript.cpp')
BASE_SQL = os.path.join(ROOT, 'data', 'sql', 'characters', 'base',
                        'llm_guide_queue.sql')
UPDATE_SQL = os.path.join(ROOT, 'data', 'sql', 'characters', 'updates',
                          'llm_guide_queue_web_ingress.sql')

# sha256 of base/llm_guide_queue.sql at upstream 4b89056 (LF-normalized).
UPSTREAM_BASE_SQL_SHA256 = (
    'd738c8282c7a8bbd31e09c05ff96d56f91f8acce047670a365c209909b3c4f14')


def read(path):
    with open(path, encoding='utf-8') as handle:
        return handle.read().replace('\r\n', '\n')


def function_body(source, signature):
    start = source.index(signature)
    brace = source.index('{', start)
    depth = 0
    for index in range(brace, len(source)):
        if source[index] == '{':
            depth += 1
        elif source[index] == '}':
            depth -= 1
            if depth == 0:
                return source[brace:index + 1]
    raise AssertionError('unbalanced braces after ' + signature)


class BaseSchemaTests(unittest.TestCase):
    def test_base_sql_is_byte_identical_to_upstream(self):
        # AzerothCore re-applies a module SQL file whose hash changes, and the
        # base file drops llm_guide_memory: editing it would erase history.
        with open(BASE_SQL, 'rb') as handle:
            data = handle.read().replace(b'\r\n', b'\n')
        self.assertEqual(hashlib.sha256(data).hexdigest(),
                         UPSTREAM_BASE_SQL_SHA256)

    def test_update_file_sorts_after_the_base_file(self):
        self.assertLess(os.path.basename(BASE_SQL),
                        os.path.basename(UPDATE_SQL))

    def test_update_file_guards_every_alter(self):
        sql = read(UPDATE_SQL)
        statements = re.findall(r"'ALTER TABLE `llm_guide_queue` ADD[^']*(?:''[^']*)*'",
                                sql)
        self.assertEqual(len(statements),
                         len(WEB_INGRESS_COLUMNS) + len(WEB_INGRESS_KEYS))
        self.assertEqual(sql.count("'DO 0'"), len(statements))
        self.assertEqual(sql.count('PREPARE llm_guide_stmt FROM'),
                         len(statements))
        self.assertNotIn('DROP ', sql.upper().replace('DROP TABLE IF EXISTS', ''))
        for column in WEB_INGRESS_COLUMNS:
            self.assertIn(f"COLUMN_NAME = '{column}'", sql)
            self.assertIn(f'ADD COLUMN `{column}`', sql)
        for key in WEB_INGRESS_KEYS:
            self.assertIn(f"INDEX_NAME = '{key}'", sql)

    def test_update_file_and_bridge_define_the_same_columns(self):
        sql = read(UPDATE_SQL)
        for column, definition in WEB_INGRESS_COLUMNS.items():
            expected = definition.replace("'", "''")
            self.assertIn(f'ADD COLUMN `{column}` {expected}', sql)
        for key, definition in WEB_INGRESS_KEYS.items():
            self.assertIn(f'ADD {definition}', sql)


class FakeCursor:
    def __init__(self):
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append((' '.join(sql.split()), params))

    def fetchone(self):
        last = self.statements[-1][0]
        if last.startswith('SELECT DATA_TYPE'):
            return ('mediumtext',)
        return None

    def fetchall(self):
        return []

    def close(self):
        pass


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def commit(self):
        pass

    def close(self):
        pass


class BridgeMigrationTests(unittest.TestCase):
    def test_bridge_adds_ingress_columns_and_keys(self):
        cursor = FakeCursor()
        bridge = LLMBridge({})
        with patch('mysql.connector.connect',
                   return_value=FakeConnection(cursor)):
            bridge._ensure_table_exists()
        alters = [sql for sql, _ in cursor.statements
                  if sql.startswith('ALTER TABLE `llm_guide_queue` ADD')]
        for column, definition in WEB_INGRESS_COLUMNS.items():
            self.assertIn(
                f'ALTER TABLE `llm_guide_queue` ADD COLUMN `{column}` '
                + ' '.join(definition.split()), alters)
        for definition in WEB_INGRESS_KEYS.values():
            self.assertIn(
                f'ALTER TABLE `llm_guide_queue` ADD {definition}', alters)


class IngressSourceContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = read(SCRIPT)

    def test_limits_are_shared(self):
        self.assertIn('MAX_GUIDE_QUESTION_BYTES = 500', self.source)
        self.assertIn('MAX_GUIDE_QUESTION_LINES = 8', self.source)
        validator = function_body(
            self.source, 'static char const* ValidateGuideQuestion(')
        self.assertIn('MAX_GUIDE_QUESTION_BYTES', validator)
        self.assertIn('MAX_GUIDE_QUESTION_LINES', validator)
        self.assertIn('IsValidUtf8', validator)

    def test_every_ingress_uses_one_submit_helper(self):
        legacy = function_body(
            self.source,
            'static bool SubmitQuestion(Player* player, const std::string& '
            'questionStr, bool isWhisper)')
        self.assertIn('SubmitGuideRequest(player, questionStr, options)', legacy)
        self.assertNotIn('INSERT INTO', legacy)
        control = function_body(
            self.source, 'static bool HandleControlSubmit(')
        self.assertIn('SubmitGuideRequest(player, question, options, &reason)',
                      control)
        self.assertEqual(self.source.count('INSERT INTO llm_guide_queue'), 1)
        helper = function_body(self.source, 'static bool SubmitGuideRequest(')
        self.assertIn('ValidateGuideQuestion(questionStr)', helper)
        self.assertIn('BuildCharacterContext(player)', helper)
        self.assertIn('BuildGuideSnapshot(player', helper)
        self.assertIn('playerCooldowns', helper)
        self.assertIn('GetMaxPendingPerPlayer', helper)

    def test_control_commands_are_console_only(self):
        self.assertIn(
            '{ "submit", HandleControlSubmit, SEC_CONSOLE, Console::Yes }',
            self.source)
        self.assertIn(
            '{ "clear", HandleControlClear, SEC_CONSOLE, Console::Yes }',
            self.source)
        self.assertIn('{ "agctl", controlTable }', self.source)
        for handler in ('HandleControlSubmit(', 'HandleControlClear('):
            body = function_body(self.source, f'static bool {handler}')
            self.assertIn('if (!handler->IsConsole())', body)

    def test_control_output_never_contains_question_text(self):
        emit = function_body(self.source, 'static void EmitGuideControl(')
        self.assertIn('GUIDE_CONTROL_PREFIX', emit)
        self.assertIn('IsGuideRequestToken(request)', emit)
        control = function_body(
            self.source, 'static bool HandleControlSubmit(')
        for call in re.findall(r'EmitGuideControl\(([^;]*)\);', control):
            self.assertNotIn('question', call)

    def test_web_submission_is_silent_in_game(self):
        control = function_body(
            self.source, 'static bool HandleControlSubmit(')
        self.assertIn('options.origin = GuideRequestOrigin::Web;', control)
        self.assertIn('options.emitPlayerFeedback = false;', control)
        self.assertIn('player->GetGUID().GetCounter() != guid', control)

    def test_world_delivery_only_consumes_in_game_rows(self):
        update = function_body(self.source, 'void OnUpdate(uint32 diff)')
        self.assertIn(
            "WHERE status IN ('complete', 'error') AND origin = 'ingame' LIMIT 5",
            update)
        self.assertIn("WHERE status = 'delivered' AND origin = 'ingame'", update)

    def test_control_payload_bound_matches_question_limit(self):
        # Largest unpadded base64url of a 500-byte question is 667 chars.
        encoded = base64.urlsafe_b64encode(b'x' * 500).rstrip(b'=')
        self.assertEqual(len(encoded), 667)
        self.assertIn('(MAX_GUIDE_QUESTION_BYTES * 4 + 2) / 3', self.source)


if __name__ == '__main__':
    unittest.main()
