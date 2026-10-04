import asyncio
import unittest

from core import agent_client, process_manager, username_scanner


class UpgradeRegressionTests(unittest.TestCase):
    def test_package_validation(self):
        self.assertTrue(process_manager.is_valid_package_name('com.roblox.client'))
        self.assertFalse(process_manager.is_valid_package_name('com.roblox;rm -rf /'))
        self.assertFalse(process_manager.is_valid_package_name(''))

    def test_username_result_distinguishes_stale_cache(self):
        username_scanner._cache['com.roblox.client'] = {
            'username': 'PlayerA',
            'read_ok': False,
            'scanned_at': 123.0,
        }
        result = username_scanner.get_cached_identity('com.roblox.client')
        self.assertEqual(result['username'], 'PlayerA')
        self.assertFalse(result['read_ok'])

    def test_command_id_cache_prevents_duplicate_handler_execution(self):
        async def scenario():
            sent = []
            calls = {'count': 0}

            class FakeWS:
                async def send(self, payload):
                    sent.append(payload)

            async def fake_handler(msg, device_id):
                calls['count'] += 1
                return {
                    'type': 'COMMAND_RESULT',
                    'command': msg['type'],
                    'device_id': device_id,
                    'ok': True,
                    'reason': 'DONE',
                }

            old_handlers = agent_client._COMMAND_HANDLERS.copy()
            old_cache = agent_client._COMMAND_RESULT_CACHE.copy()
            try:
                agent_client._COMMAND_HANDLERS = {'PING': fake_handler}
                agent_client._COMMAND_RESULT_CACHE.clear()
                msg = {'type': 'PING', 'command_id': 'cmd-1'}
                ws = FakeWS()
                await agent_client._handle_incoming_command(ws, msg, 'dev-1', set())
                await agent_client._handle_incoming_command(ws, msg, 'dev-1', set())
            finally:
                agent_client._COMMAND_HANDLERS = old_handlers
                agent_client._COMMAND_RESULT_CACHE.clear()
                agent_client._COMMAND_RESULT_CACHE.update(old_cache)

            self.assertEqual(calls['count'], 1)
            self.assertEqual(len(sent), 3)  # ACK + initial result + cached result

        asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
