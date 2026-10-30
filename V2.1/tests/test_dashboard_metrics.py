import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import proxy_tool as pt


class TestDashboardMetrics(unittest.TestCase):
    def setUp(self):
        self.app = object.__new__(pt.ProxyApp)
        self.app.metric_down = mock.Mock()
        self.app.metric_up = mock.Mock()
        self.app.metric_conns = mock.Mock()
        self.app._metric_prev = None
        self.app._entry = object()

    def test_aggregates_upstreams_using_elapsed_time(self):
        with mock.patch.object(pt.time, 'monotonic', side_effect=[100, 102]):
            self.app._update_metrics(3, {'a': (1, 100, 50), 'b': (2, 200, 100)})
            self.app.metric_down.set.assert_called_with('—')
            self.app._update_metrics(4, {'a': (2, 1124, 1074), 'b': (2, 1224, 1124)})
        self.app.metric_conns.set.assert_called_with('4')
        self.app.metric_down.set.assert_called_with('1.0 KB/s')
        self.app.metric_up.set.assert_called_with('1.0 KB/s')

    def test_stopping_or_resetting_counters_cannot_display_negative_rate(self):
        self.app._metric_prev = (100, 999999, 999999)
        with mock.patch.object(pt.time, 'monotonic', return_value=102):
            self.app._update_metrics(1, {'a': (1, 10, 10)})
        self.app.metric_down.set.assert_called_with('—')
        self.app._entry = None
        self.app._update_metrics(0, {})
        self.assertIsNone(self.app._metric_prev)
        self.app.metric_conns.set.assert_called_with('0')
