"""Background HTTPS health checks without external network or system settings."""
import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import proxy_tool as pt


def eventually(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.005)
    return predicate()


class TestHttpsHealthState(unittest.TestCase):
    A, B = ('a', 1), ('b', 2)

    def setUp(self):
        self.now = 100.0
        self.pool = pt.UpstreamPool([self.A, self.B], clock=lambda: self.now)

    def report(self, up, success, info=None):
        token = self.pool.begin_health_probe(up)
        self.assertIsNotNone(token)
        return self.pool.finish_health_probe(up, token, success,
                                             (123 if success else TimeoutError()) if info is None else info)

    def test_three_complete_failures_degrade_then_two_successes_restore(self):
        self.assertEqual(self.pool.health_snapshot()[self.A]['state'], 'unknown')
        for count in (1, 2):
            self.report(self.A, False)
            state = self.pool.health_snapshot()[self.A]
            self.assertEqual(state['state'], 'suspect')
            self.assertEqual(state['failures'], count)
            self.assertFalse(state['degraded'])
        self.report(self.A, False)
        self.assertEqual(self.pool.health_snapshot()[self.A]['state'], 'degraded')
        self.assertEqual(self.pool.order()[0], self.B)
        self.report(self.A, True)
        state = self.pool.health_snapshot()[self.A]
        self.assertEqual((state['state'], state['successes'], state['degraded']), ('recovering', 1, True))
        self.assertEqual(self.pool.order()[0], self.B)
        self.report(self.A, True)
        state = self.pool.health_snapshot()[self.A]
        self.assertEqual((state['state'], state['successes'], state['degraded']), ('healthy', 2, False))

    def test_tcp_success_never_clears_https_degradation(self):
        for _ in range(3):
            self.report(self.A, False)
        for _ in range(3):
            self.pool.report_fail(self.A)
        self.pool.report_ok(self.A)
        token = self.pool.begin_attempt(self.A)
        self.pool.finish_attempt(self.A, token, True)
        self.pool.release_attempt(token)
        state = self.pool.health_snapshot()[self.A]
        self.assertEqual(state['state'], 'degraded')
        self.assertFalse(state['tcp_degraded'])
        self.assertEqual(state['tcp_failures'], 0)
        self.assertEqual(self.pool.order()[0], self.B)

    def test_recovery_failure_stays_degraded(self):
        for _ in range(3):
            self.report(self.A, False)
        self.report(self.A, True)
        self.report(self.A, False)
        state = self.pool.health_snapshot()[self.A]
        self.assertEqual(state['state'], 'degraded')
        self.assertEqual(state['successes'], 0)

    def test_all_https_degraded_still_allow_parallel_user_connections(self):
        for up in (self.A, self.B):
            for _ in range(3):
                self.report(up, False)
        checks = [self.pool.begin_health_probe(up) for up in (self.A, self.B)]
        attempts = [self.pool.acquire_attempt(set()) for _ in range(16)]
        self.assertTrue(all(attempt is not None for attempt in attempts))
        self.assertEqual({up for up, _ in attempts}, {self.A, self.B})
        for up, token in attempts:
            self.pool.finish_attempt(up, token, True)
            self.pool.release_attempt(token)
        for up, token in zip((self.A, self.B), checks):
            self.pool.cancel_health_probe(up, token)
        self.assertEqual(self.pool._loads, {})
        self.assertTrue(all(row['degraded'] for row in self.pool.health_snapshot().values()))

    def test_preset_change_rejects_old_result_even_for_same_address(self):
        token = self.pool.begin_health_probe(self.A)
        self.pool.set([self.A, self.B])
        self.assertFalse(self.pool.finish_health_probe(self.A, token, False, TimeoutError()))
        self.assertEqual(self.pool.health_snapshot()[self.A]['state'], 'unknown')

    def test_preserve_health_keeps_existing_state_but_rejects_inflight_results(self):
        for _ in range(3):
            self.report(self.A, False)
            self.pool.report_fail(self.A)
        token = self.pool.begin_health_probe(self.A)
        self.pool.set([self.A, ('new', 3)], preserve_health=True)
        state = self.pool.health_snapshot()[self.A]
        self.assertEqual(state['state'], 'degraded')
        self.assertEqual(state['failures'], 3)
        self.assertEqual(state['tcp_failures'], 3)
        self.assertTrue(state['tcp_degraded'])
        self.assertFalse(state['checking'])
        self.assertFalse(self.pool.finish_health_probe(self.A, token, True, 1))
        self.assertEqual(self.pool.health_snapshot()[self.A]['state'], 'degraded')
        self.assertEqual(self.pool.health_snapshot()[('new', 3)]['state'], 'unknown')
        self.assertNotIn(self.B, self.pool.health_snapshot())

    def test_preserve_health_same_ordered_addresses_is_noop(self):
        token = self.pool.begin_health_probe(self.A)
        generation, _ = self.pool.health_targets()
        for _ in range(20):
            self.pool.set([self.A, self.B], preserve_health=True)
        self.assertEqual(self.pool.health_targets()[0], generation)
        self.assertTrue(self.pool.health_snapshot()[self.A]['checking'])
        self.assertTrue(self.pool.finish_health_probe(self.A, token, True, 42))
        self.assertEqual(self.pool.health_snapshot()[self.A]['state'], 'healthy')

    def test_removed_node_and_duplicate_result_do_not_recreate_state(self):
        token = self.pool.begin_health_probe(self.A)
        self.assertTrue(self.pool.finish_health_probe(self.A, token, False, TimeoutError()))
        self.assertFalse(self.pool.finish_health_probe(self.A, token, False, TimeoutError()))
        self.assertEqual(self.pool.health_snapshot()[self.A]['failures'], 1)
        removed = self.pool.begin_health_probe(self.A)
        self.pool.set([self.B])
        self.assertFalse(self.pool.finish_health_probe(self.A, removed, False, TimeoutError()))
        self.assertNotIn(self.A, self.pool.health_snapshot())

    def test_snapshot_is_copy_with_separate_tcp_and_monotonic_check_time(self):
        self.report(self.A, True, 42)
        self.pool.report_fail(self.A)
        snapshot = self.pool.health_snapshot()
        self.assertEqual(snapshot[self.A]['last_checked'], 100.0)
        self.assertEqual(snapshot[self.A]['latency_ms'], 42)
        self.assertEqual(snapshot[self.A]['state'], 'healthy')
        self.assertEqual(snapshot[self.A]['tcp_failures'], 1)
        snapshot[self.A]['state'] = 'corrupted'
        self.assertEqual(self.pool.health_snapshot()[self.A]['state'], 'healthy')

    def test_cancelled_probe_cannot_submit_late_result(self):
        token = self.pool.begin_health_probe(self.A)
        self.assertTrue(self.pool.health_snapshot()[self.A]['checking'])
        self.assertIsNone(self.pool.begin_health_probe(self.A))
        self.pool.cancel_health_probe(self.A, token)
        self.assertFalse(self.pool.finish_health_probe(self.A, token, True, 1))
        self.assertFalse(self.pool.health_snapshot()[self.A]['checking'])

    def test_transitions_are_logged_once_per_state_change(self):
        with mock.patch.object(pt, '_diagnostic', create=True) as diagnostic:
            self.report(self.A, False)
            self.report(self.A, False)
            self.report(self.A, False)
            self.report(self.A, True)
            self.report(self.A, True)
        self.assertEqual([call.kwargs['state'] for call in diagnostic.call_args_list],
                         ['suspect', 'degraded', 'recovering', 'healthy'])
        self.assertTrue(all(call.args == ('health_transition',) for call in diagnostic.call_args_list))


class TestHealthMonitorLifecycle(unittest.TestCase):
    def keep(self, monitor):
        def cleanup():
            monitor.stop()
            self.assertTrue(eventually(lambda: not monitor._threads))
        self.addCleanup(cleanup)
        return monitor

    def test_bounded_workers_and_one_check_per_node(self):
        upstreams = [('node', n) for n in range(12)]
        pool = pt.UpstreamPool(upstreams)
        release = threading.Event()
        lock = threading.Lock()
        counts = {}
        active = maximum = 0
        def probe(host, port):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
                counts[(host, port)] = counts.get((host, port), 0) + 1
            release.wait(2)
            with lock:
                active -= 1
            return True, 10
        monitor = self.keep(pt.HealthMonitor(pool, probe))
        self.addCleanup(release.set)
        monitor.start()
        self.assertTrue(eventually(lambda: active == 2))
        for _ in range(20):
            monitor.start()
            monitor.wake()
        self.assertEqual(len(monitor._threads), 2)
        self.assertEqual(sum(counts.values()), 2)
        self.assertTrue(all(worker.daemon for worker in monitor._threads))
        release.set()
        self.assertTrue(eventually(lambda: len(counts) == len(upstreams)))
        self.assertLessEqual(maximum, 2)
        self.assertTrue(all(count == 1 for count in counts.values()))

    def test_stop_is_nonblocking_and_drops_inflight_result(self):
        up = ('a', 1)
        pool = pt.UpstreamPool([up])
        started, release = threading.Event(), threading.Event()
        def probe(*_):
            started.set()
            release.wait(2)
            return False, TimeoutError()
        monitor = self.keep(pt.HealthMonitor(pool, probe))
        self.addCleanup(release.set)
        monitor.start()
        self.assertTrue(started.wait(2))
        before = time.monotonic()
        monitor.stop()
        self.assertLess(time.monotonic() - before, .25)
        self.assertFalse(pool.health_snapshot()[up]['checking'])
        release.set()
        self.assertTrue(eventually(lambda: not monitor._threads))
        self.assertEqual(pool.health_snapshot()[up]['state'], 'unknown')
        self.assertIsNone(pool.health_snapshot()[up]['last_checked'])

    def test_wake_does_not_bypass_node_interval(self):
        pool = pt.UpstreamPool([('a', 1)])
        calls = []
        def probe(*_):
            calls.append(time.monotonic())
            return True, 1
        monitor = self.keep(pt.HealthMonitor(pool, probe, interval=.15))
        monitor.start()
        self.assertTrue(eventually(lambda: len(calls) == 1))
        for _ in range(20):
            monitor.wake()
        time.sleep(.03)
        self.assertEqual(len(calls), 1)
        self.assertTrue(eventually(lambda: len(calls) >= 2))
        self.assertGreaterEqual(calls[1] - calls[0], .14)

    def test_repeated_remark_sync_does_not_bypass_interval(self):
        up = ('a', 1)
        pool = pt.UpstreamPool([up])
        calls = []
        def probe(*_):
            calls.append(time.monotonic())
            return False, TimeoutError()
        monitor = self.keep(pt.HealthMonitor(pool, probe, interval=.3))
        monitor.start()
        self.assertTrue(eventually(lambda: pool.health_snapshot()[up]['failures'] == 1))
        for _ in range(100):
            pool.set([up], preserve_health=True)
            monitor.wake()
        time.sleep(.03)
        self.assertEqual(len(calls), 1)
        self.assertEqual(pool.health_snapshot()[up]['failures'], 1)
        self.assertTrue(eventually(lambda: len(calls) >= 2))
        self.assertGreaterEqual(calls[1] - calls[0], .29)

    def test_member_change_preserves_retained_node_due_time(self):
        a, b = ('a', 1), ('b', 2)
        pool = pt.UpstreamPool([a])
        calls = {a: [], b: []}
        def probe(host, port):
            calls[(host, port)].append(time.monotonic())
            return True, 3
        monitor = self.keep(pt.HealthMonitor(pool, probe, interval=.3))
        monitor.start()
        self.assertTrue(eventually(lambda: pool.health_snapshot()[a]['state'] == 'healthy'))
        pool.set([a, b], preserve_health=True)
        monitor.wake()
        self.assertTrue(eventually(lambda: pool.health_snapshot()[b]['state'] == 'healthy'))
        time.sleep(.03)
        self.assertEqual(len(calls[a]), 1)
        self.assertEqual(len(calls[b]), 1)
        self.assertTrue(eventually(lambda: len(calls[a]) >= 2))
        self.assertGreaterEqual(calls[a][1] - calls[a][0], .29)

    def test_invalidated_retained_probe_does_not_immediately_repeat(self):
        a, b = ('a', 1), ('b', 2)
        pool = pt.UpstreamPool([a])
        started, release = threading.Event(), threading.Event()
        calls = {a: [], b: []}
        def probe(host, port):
            up = (host, port)
            calls[up].append(time.monotonic())
            if up == a and len(calls[a]) == 1:
                started.set()
                release.wait(2)
                return False, TimeoutError()
            return True, 4
        monitor = self.keep(pt.HealthMonitor(pool, probe, interval=.3))
        self.addCleanup(release.set)
        monitor.start()
        self.assertTrue(started.wait(2))
        pool.set([a, b], preserve_health=True)
        monitor.wake()
        self.assertTrue(eventually(lambda: pool.health_snapshot()[b]['state'] == 'healthy'))
        release.set()
        time.sleep(.03)
        self.assertEqual(len(calls[a]), 1)
        self.assertEqual(pool.health_snapshot()[a]['state'], 'unknown')
        self.assertTrue(eventually(lambda: pool.health_snapshot()[a]['state'] == 'healthy'))
        self.assertEqual(pool.health_snapshot()[a]['failures'], 0)
        self.assertGreaterEqual(calls[a][1] - calls[a][0], .29)

    def test_same_address_new_preset_drops_old_probe_and_checks_again(self):
        up = ('a', 1)
        pool = pt.UpstreamPool([up])
        started, release = threading.Event(), threading.Event()
        calls = []
        def probe(*_):
            calls.append(1)
            if len(calls) == 1:
                started.set()
                release.wait(2)
                return False, TimeoutError()
            return True, 77
        monitor = self.keep(pt.HealthMonitor(pool, probe, interval=.05))
        self.addCleanup(release.set)
        monitor.start()
        self.assertTrue(started.wait(2))
        pool.set([up])
        monitor.wake()
        release.set()
        self.assertTrue(eventually(lambda: pool.health_snapshot()[up]['state'] == 'healthy'))
        self.assertEqual(pool.health_snapshot()[up]['failures'], 0)
        self.assertEqual(pool.health_snapshot()[up]['latency_ms'], 77)
        self.assertEqual(len(calls), 2)

    def test_restarts_do_not_spawn_extra_probes_while_old_probe_is_blocked(self):
        up = ('a', 1)
        pool = pt.UpstreamPool([up])
        started, release = threading.Event(), threading.Event()
        calls = []
        def probe(*_):
            calls.append(1)
            if len(calls) == 1:
                started.set()
                release.wait(2)
                return False, TimeoutError()
            return True, 9
        monitor = self.keep(pt.HealthMonitor(pool, probe))
        self.addCleanup(release.set)
        monitor.start()
        self.assertTrue(started.wait(2))
        for _ in range(10):
            monitor.stop()
            monitor.start()
        self.assertLessEqual(len(monitor._threads), 2)
        self.assertEqual(len(calls), 1)
        release.set()
        self.assertTrue(eventually(lambda: pool.health_snapshot()[up]['state'] == 'healthy'))
        self.assertEqual(len(calls), 2)

    def test_probe_exception_becomes_failed_check(self):
        up = ('a', 1)
        pool = pt.UpstreamPool([up])
        monitor = self.keep(pt.HealthMonitor(pool, mock.Mock(side_effect=TimeoutError())))
        monitor.start()
        self.assertTrue(eventually(lambda: pool.health_snapshot()[up]['state'] == 'suspect'))
        self.assertEqual(pool.health_snapshot()[up]['reason'], '超时')

    def test_default_probe_reuses_multi_target_checker(self):
        up = ('a', 1)
        pool = pt.UpstreamPool([up])
        monitor = self.keep(pt.HealthMonitor(pool))
        with mock.patch.object(pt, '_probe_upstream', return_value=(True, 5)) as probe:
            monitor.start()
            self.assertTrue(eventually(lambda: pool.health_snapshot()[up]['state'] == 'healthy'))
            monitor.stop()
            self.assertTrue(eventually(lambda: not monitor._threads))
        probe.assert_called_once_with(*up)

    def test_empty_pool_waits_for_config_and_wake(self):
        pool = pt.UpstreamPool([])
        probe = mock.Mock(return_value=(True, 4))
        monitor = self.keep(pt.HealthMonitor(pool, probe))
        monitor.start()
        probe.assert_not_called()
        pool.set([('a', 1)])
        monitor.wake()
        self.assertTrue(eventually(lambda: pool.health_snapshot()[('a', 1)]['state'] == 'healthy'))

    def test_no_worker_start_failure_is_reported_to_caller(self):
        monitor = self.keep(pt.HealthMonitor(pt.UpstreamPool([]), mock.Mock()))
        with mock.patch.object(pt.threading.Thread, 'start', side_effect=RuntimeError('no threads')):
            with mock.patch.object(pt, '_diagnostic', create=True) as diagnostic:
                with self.assertRaises(RuntimeError):
                    monitor.start()
        self.assertFalse(monitor._running)
        self.assertFalse(monitor._threads)
        diagnostic.assert_called_once()
        self.assertEqual(diagnostic.call_args.args, ('health_start_error',))

    def test_one_worker_keeps_running_and_records_partial_start_failure(self):
        up = ('a', 1)
        pool = pt.UpstreamPool([up])
        monitor = self.keep(pt.HealthMonitor(pool, mock.Mock(return_value=(True, 1))))
        real_start = threading.Thread.start
        starts = []
        def start(worker):
            starts.append(worker)
            if len(starts) > 1:
                raise RuntimeError('only one thread available')
            return real_start(worker)
        with mock.patch.object(pt.threading.Thread, 'start', new=start):
            with mock.patch.object(pt, '_diagnostic', create=True) as diagnostic:
                monitor.start()
                self.assertTrue(eventually(lambda: pool.health_snapshot()[up]['state'] == 'healthy'))
                self.assertEqual(len(monitor._threads), 1)
                self.assertTrue(any(call.args == ('health_start_error',) for call in diagnostic.call_args_list))
