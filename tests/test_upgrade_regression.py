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

class IsolationRegressionTests(unittest.TestCase):
    def test_restore_helper_excludes_target_and_restores_active_survivor(self):
        async def scenario():
            from core import session_agent
            calls = []
            old_sessions = session_agent.SESSIONS.copy()
            old_restore = session_agent.process_manager.restore_foreground
            old_get_pids = session_agent.process_manager.get_pids
            try:
                session_agent.SESSIONS.clear()
                session_agent.SESSIONS.update({
                    'pkg.target': {'session_id': 's1', 'status': 'RECOVERING', '_device_id': 'D'},
                    'pkg.survivor': {'session_id': 's2', 'status': 'ACTIVE', '_device_id': 'D'},
                })

                session_agent.process_manager.restore_foreground = lambda pkg: calls.append(('restore', pkg)) or True
                session_agent.process_manager.get_pids = lambda pkg: {'200'}

                await session_agent._restore_survivors_after_target_operation(
                    'D', 'pkg.target', {'pkg.target': {'100'}, 'pkg.survivor': {'200'}}, 'test'
                )
            finally:
                session_agent.process_manager.restore_foreground = old_restore
                session_agent.process_manager.get_pids = old_get_pids
                session_agent.SESSIONS.clear()
                session_agent.SESSIONS.update(old_sessions)

            self.assertEqual(calls, [('restore', 'pkg.survivor')])

        asyncio.run(scenario())


    def test_dead_survivor_is_not_relaunched_during_restore(self):
        async def scenario():
            from core import session_agent
            calls = []
            old_sessions = session_agent.SESSIONS.copy()
            old_restore = session_agent.process_manager.restore_foreground
            old_get_pids = session_agent.process_manager.get_pids
            try:
                session_agent.SESSIONS.clear()
                session_agent.SESSIONS.update({
                    'pkg.target': {'session_id': 's1', 'status': 'RECOVERING', '_device_id': 'D'},
                    'pkg.survivor': {'session_id': 's2', 'status': 'ACTIVE', '_device_id': 'D'},
                })
                session_agent.process_manager.restore_foreground = lambda pkg: calls.append(pkg) or True
                session_agent.process_manager.get_pids = lambda pkg: set()

                await session_agent._restore_survivors_after_target_operation(
                    'D', 'pkg.target', {'pkg.survivor': {'200'}}, 'test-dead'
                )
            finally:
                session_agent.process_manager.restore_foreground = old_restore
                session_agent.process_manager.get_pids = old_get_pids
                session_agent.SESSIONS.clear()
                session_agent.SESSIONS.update(old_sessions)

            self.assertEqual(calls, [])

        asyncio.run(scenario())

    def test_freeform_failure_still_restores_active_sibling(self):
        async def scenario():
            from core import session_agent
            calls = []
            old_sessions = session_agent.SESSIONS.copy()
            old_activate = session_agent.activate_freeform
            old_restore = session_agent.process_manager.restore_foreground
            old_sleep = session_agent.asyncio.sleep
            try:
                session_agent.SESSIONS.clear()
                session_agent.SESSIONS.update({
                    'pkg.target': {'session_id': 's1', 'status': 'STARTING', '_device_id': 'D'},
                    'pkg.survivor': {'session_id': 's2', 'status': 'ACTIVE', '_device_id': 'D'},
                })
                session_agent.activate_freeform = lambda pkg: (False, None)
                session_agent.process_manager.restore_foreground = lambda pkg: calls.append(pkg) or True

                await session_agent._activate_freeform_and_restore_siblings('pkg.target', 's1')
            finally:
                session_agent.activate_freeform = old_activate
                session_agent.process_manager.restore_foreground = old_restore
                session_agent.SESSIONS.clear()
                session_agent.SESSIONS.update(old_sessions)

            self.assertEqual(calls, ['pkg.survivor'])

        asyncio.run(scenario())
