"""Run flipper checks with temporary databases and external networking disabled."""
import contextlib
import logging
import os
import socket
import tempfile
import unittest
from unittest.mock import patch


def main():
    logging.disable(logging.CRITICAL)
    connect, connect_ex = socket.socket.connect, socket.socket.connect_ex
    def offline(original):
        def wrapped(sock, address):
            # Windows asyncio uses a loopback socket pair internally.
            if isinstance(address, tuple) and address[0] not in ('127.0.0.1','::1','localhost'):
                raise OSError('External network disabled by test runner')
            return original(sock,address)
        return wrapped
    with tempfile.TemporaryDirectory(prefix='flipper-checks-') as folder, contextlib.ExitStack() as stack:
        # Preserve OS variables needed by Windows multiprocessing, but never
        # inherit live credentials, proxies or trading settings in the suite.
        excluded = {'ADMIN_IDS', 'DEFAULT_PROFIT_GOAL', 'AUTO_DETECT_PROXY',
                    'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'NO_PROXY'}
        environment = {key: value for key, value in os.environ.items()
                       if not key.upper().startswith(('FLIPPER_', 'FUNPAY_', 'TELEGRAM_'))
                       and key.upper() not in excluded}
        environment.update(FLIPPER_DB_PATH=os.path.join(folder, 'flipper.db'),
                           AUTO_DETECT_PROXY='false')
        stack.enter_context(patch.dict(os.environ, environment, clear=True))
        stack.enter_context(patch.object(socket.socket,'connect',offline(connect)))
        stack.enter_context(patch.object(socket.socket,'connect_ex',offline(connect_ex)))
        suite=unittest.defaultTestLoader.loadTestsFromNames([
            'test_engineering_fixes','test_database_concurrency',
            'test_auto_flipper','test_safe_flipper','test_client_contracts',
            'auto_flipper.test_exit_policy','auto_flipper.test_capital_store','test_manual_route','test_pilot_integration',
            'resale_intelligence.models.test_models','resale_intelligence.models.test_risk_gate'])
        result=unittest.TextTestRunner(verbosity=1).run(suite)
        return 0 if result.wasSuccessful() else 1


if __name__ == '__main__':
    raise SystemExit(main())
