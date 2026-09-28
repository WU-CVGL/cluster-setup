"""A service restart refreshes credentials without sending a notification."""
import importlib.util
from datetime import datetime
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, call, patch


class StartupRefreshTest(unittest.TestCase):
    def load_application(self):
        # Replace external integration imports; no credentials, network, or app loop.
        stubs = {}
        for module_name, class_name in [
            ("alert_config", "Config"),
            ("alert_MessageNotifier", "MessageNotifier"),
            ("alert_APIHandler", "APIHandler"),
            ("alert_DataProcessor", "DataProcessor"),
        ]:
            module = ModuleType(module_name)
            setattr(module, class_name, Mock())
            stubs[module_name] = module
        spec = importlib.util.spec_from_file_location(
            "watchdog_under_test", Path(__file__).with_name("alert_response_handler_v02.py")
        )
        module = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, stubs):
            spec.loader.exec_module(module)
        return module.MainApplication

    def test_silent_refresh_success_and_failure_do_not_notify(self):
        cls = self.load_application()
        app = cls.__new__(cls)
        app.config = SimpleNamespace(slack_webhook_url="unused")
        app.message_notifier = Mock()
        app.renew_det_token = Mock()
        app.update_det_token_to_prometheus = Mock()
        app.self_check = Mock()
        self.assertTrue(app.auto_update(notify=False))
        app.update_det_token_to_prometheus.assert_called_once()
        app.message_notifier.send_slack_warning.assert_not_called()
        app.renew_det_token.side_effect = RuntimeError("login unavailable")
        self.assertFalse(app.auto_update(notify=False))
        app.update_det_token_to_prometheus.assert_called_once()
        app.message_notifier.send_slack_warning.assert_not_called()

    def test_scheduled_refresh_retains_notification(self):
        cls = self.load_application()
        app = cls.__new__(cls)
        app.config = SimpleNamespace(slack_webhook_url="unused")
        app.message_notifier = Mock()
        app.renew_det_token = Mock()
        app.update_det_token_to_prometheus = Mock()
        app.self_check = Mock()
        app.auto_update()
        app.message_notifier.send_slack_warning.assert_called_once()

    def test_thursday_startup_skips_duplicate_then_next_thursday_notifies(self):
        cls = self.load_application()
        app = cls.__new__(cls)
        app.config = SimpleNamespace(
            alert_min=0, alert_update_day=3, is_debug=False,
            time_function_enabled_3090=True, time_function_enabled_update=True,
        )
        app.auto_update = Mock(return_value=True)
        times = iter([
            datetime(2026, 10, 1, 12, 5),  # startup on Thursday
            datetime(2026, 10, 1, 12, 5),  # same-day first loop
            datetime(2026, 10, 2, 12, 5),  # Friday resets the schedule
            datetime(2026, 10, 8, 12, 5),  # next Thursday sends notification
        ])
        clock = Mock()
        clock.now.side_effect = lambda: next(times)
        sleeps = 0

        def stop_after_three_loops(_):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 3:
                raise StopIteration

        run_globals = cls.run.__globals__
        with patch.dict(run_globals, {"datetime": clock}), patch.object(
            run_globals["time"], "sleep", side_effect=stop_after_three_loops
        ), patch("builtins.print"):
            with self.assertRaises(StopIteration):
                app.run()
        self.assertEqual(app.auto_update.call_args_list, [call(notify=False), call()])

    def test_failed_thursday_startup_keeps_scheduled_retry(self):
        cls = self.load_application()
        app = cls.__new__(cls)
        app.config = SimpleNamespace(
            alert_min=0, alert_update_day=3, is_debug=False,
            time_function_enabled_3090=True, time_function_enabled_update=True,
        )
        app.auto_update = Mock(side_effect=[False, True])
        clock = Mock()
        clock.now.return_value = datetime(2026, 10, 1, 12, 5)
        run_globals = cls.run.__globals__
        with patch.dict(run_globals, {"datetime": clock}), patch.object(
            run_globals["time"], "sleep", side_effect=StopIteration
        ), patch("builtins.print"):
            with self.assertRaises(StopIteration):
                app.run()
        self.assertEqual(app.auto_update.call_args_list, [call(notify=False), call()])


if __name__ == "__main__":
    unittest.main()
