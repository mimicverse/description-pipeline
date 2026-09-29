"""The compatibility import must retain the owned STA session contract."""

import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from tools.solidworks_export import native_swapi
from description_pipeline.sources.solidworks.errors import EnvironmentError_


class ComThreadingTests(unittest.TestCase):
    def test_sessions_do_not_share_proxies_across_threads(self):
        sessions = []

        def create():
            session = SimpleNamespace(app=object(), connect=Mock(), close=Mock())
            sessions.append(session)
            return session

        backend = native_swapi.SolidWorksBackend(session_factory=create)
        results, errors = {}, []

        def worker(name):
            try:
                with backend.session():
                    results[name] = backend._app_obj()
                    results[name + "_again"] = backend._app_obj()
            except BaseException as error:
                errors.append(error)

        for name in ("a", "b"):
            thread = threading.Thread(target=worker, args=(name,))
            thread.start()
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(sessions), 2)
        self.assertIs(results["a"], results["a_again"])
        self.assertIs(results["b"], results["b_again"])
        self.assertIsNot(results["a"], results["b"])
        for session in sessions:
            session.close.assert_called_once()

    def test_active_proxy_is_not_returned_to_another_thread(self):
        session = SimpleNamespace(app=object(), connect=Mock(), close=Mock())
        backend = native_swapi.SolidWorksBackend(session_factory=lambda: session)
        errors = []

        def other_thread():
            try:
                backend._app_obj()
            except Exception as error:
                errors.append(error)

        with backend.session():
            backend._app_obj()
            thread = threading.Thread(target=other_thread)
            thread.start()
            thread.join(2)
        self.assertEqual(len(errors), 1)
        assert isinstance(errors[0], EnvironmentError_)
        self.assertEqual(errors[0].code, "cad_thread_mismatch")


if __name__ == "__main__":
    unittest.main()
