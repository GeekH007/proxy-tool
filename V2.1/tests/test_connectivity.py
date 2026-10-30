import http.client
import os
import socket
import ssl
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
import proxy_tool as pt


def response(status):
    result = mock.MagicMock()
    result.__enter__.return_value = result
    result.status = status
    return result


class TestConnectivityProbe(unittest.TestCase):
    def test_https_uses_selected_proxy_and_closes_connection(self):
        connection = mock.Mock()
        reply = response(204)
        connection.getresponse.return_value = reply
        with mock.patch.object(pt.http.client, 'HTTPSConnection', return_value=connection) as create:
            ok, elapsed = pt._probe_upstream('192.0.2.1', 7890)
        self.assertTrue(ok)
        self.assertIsInstance(elapsed, int)
        self.assertEqual(create.call_args.args, ('192.0.2.1', 7890))
        self.assertEqual(create.call_args.kwargs['timeout'], 6)
        context = create.call_args.kwargs['context']
        self.assertTrue(context.check_hostname)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        connection.set_tunnel.assert_called_once_with('www.google.com', port=443)
        connection.request.assert_called_once()
        self.assertEqual(connection.request.call_args.args, ('GET', '/generate_204'))
        self.assertEqual(connection.request.call_args.kwargs['headers']['Host'], 'www.google.com')
        reply.__exit__.assert_called_once()
        connection.close.assert_called_once()

    def test_first_site_http_error_falls_back_to_second_https_site(self):
        first, second = mock.Mock(), mock.Mock()
        first.getresponse.return_value = response(403)
        second.getresponse.return_value = response(204)
        with mock.patch.object(pt.http.client, 'HTTPSConnection', side_effect=[first, second]):
            ok, _ = pt._probe_upstream('192.0.2.1', 7890)
        self.assertTrue(ok)
        second.set_tunnel.assert_called_once_with('www.gstatic.com', port=443)
        first.close.assert_called_once()
        second.close.assert_called_once()

    def test_proxy_bypass_environment_cannot_turn_test_into_direct_access(self):
        connection = mock.Mock()
        connection.getresponse.return_value = response(204)
        with mock.patch.dict(os.environ, {'NO_PROXY': '*', 'HTTPS_PROXY': 'http://wrong:1'}):
            with mock.patch.object(pt.http.client, 'HTTPSConnection', return_value=connection) as create:
                self.assertTrue(pt._probe_upstream('chosen-proxy', 7890)[0])
        self.assertEqual(create.call_args.args, ('chosen-proxy', 7890))

    def test_all_failed_preserves_both_site_reasons(self):
        first, second = mock.Mock(), mock.Mock()
        first.request.side_effect = socket.timeout('timed out')
        second.getresponse.return_value = response(502)
        with mock.patch.object(pt.http.client, 'HTTPSConnection', side_effect=[first, second]):
            ok, error = pt._probe_upstream('proxy', 7890)
        self.assertFalse(ok)
        text = pt._friendly_conn_error(error)
        self.assertIn('Google HTTPS', text)
        self.assertIn('Gstatic HTTPS', text)
        self.assertIn('超时', text)
        self.assertIn('HTTP 502', text)
        self.assertEqual(pt._short_conn_error(error), '测试未通')
        self.assertEqual(len(error.attempts), 2)

    def test_certificate_error_remains_failure_and_has_explanation(self):
        connection = mock.Mock()
        connection.request.side_effect = ssl.SSLCertVerificationError('certificate verify failed')
        with mock.patch.object(pt.http.client, 'HTTPSConnection', return_value=connection):
            ok, error = pt._probe_upstream('proxy', 7890)
        self.assertFalse(ok)
        self.assertEqual(pt._short_conn_error(error), '证书错误')
        self.assertIn('证书校验失败', pt._friendly_conn_error(error))
        self.assertEqual(connection.close.call_count, 2)

    def test_proxy_refused_tunnel_is_not_just_generic_failure(self):
        error = OSError('Tunnel connection failed: 403 Forbidden')
        self.assertEqual(pt._short_conn_error(error), '隧道拒绝')
        self.assertIn('403 Forbidden', pt._friendly_conn_error(error))

    def test_unexpected_200_body_is_not_false_positive(self):
        connection = mock.Mock()
        connection.getresponse.return_value = response(200)
        with mock.patch.object(pt.http.client, 'HTTPSConnection', return_value=connection):
            ok, error = pt._probe_upstream('proxy', 7890)
        self.assertFalse(ok)
        self.assertEqual(pt._short_conn_error(error), 'HTTP200')

    def test_malformed_http_response_has_protocol_error(self):
        error = http.client.BadStatusLine('not HTTP')
        self.assertEqual(pt._short_conn_error(error), '协议错误')


class TestConnectivityPresentation(unittest.TestCase):
    """Exercise callbacks without creating a Tk window or changing system settings."""
    def setUp(self):
        self.app = object.__new__(pt.ProxyApp)
        self.app._test_gen = 7
        self.app._test_results = {}
        self.app._manual_test_times = {}
        self.app._pool = pt.UpstreamPool([('a', 1), ('b', 2)])
        self.app._current_preset = mock.Mock(return_value={'type': 'multi'})
        self.app._pool_addrs = mock.Mock(return_value=[('a', 1), ('b', 2)])
        self.app.pool_box = mock.Mock()
        self.app.pool_box.exists.return_value = True
        self.app.test_btn = mock.Mock()
        self.app.test_result_var = mock.Mock()
        self.app.test_result_label = mock.Mock()
        self.app.root = mock.Mock()

    def test_partial_failure_keeps_success_and_details_per_node(self):
        error = pt._ConnectivityFailure([('Google HTTPS', pt._ProbeHttpError(403))])
        self.app._show_test_result(7, 'a:1', False, error, 2)
        self.app._show_test_result(7, 'b:2', True, 160, 2)
        self.app.pool_box.set.assert_any_call('0', 'test', '✗ HTTP403')
        self.app.pool_box.set.assert_any_call('1', 'test', '✓ 160 ms')
        self.assertIn('1/2 上游通过', self.app.test_result_var.set.call_args.args[0])
        with mock.patch.object(pt.messagebox, 'showinfo') as show:
            self.app.show_test_details()
        text = show.call_args.args[1]
        self.assertIn('a:1', text)
        self.assertIn('HTTP 403', text)
        self.assertIn('b:2', text)
        self.assertIn('通过，160 ms', text)

    def test_stale_results_and_details_are_cleared_on_address_change(self):
        self.app._test_results = {'a:1': (False, OSError('old failure'))}
        self.app._invalidate_test_results()
        self.app._show_test_result(7, 'a:1', True, 10, 1)
        self.assertEqual(self.app._test_results, {})

    def test_worker_always_posts_result_on_ui_queue(self):
        callbacks = []
        self.app._ui = callbacks.append
        self.app._show_test_result = mock.Mock()
        with mock.patch.object(pt, '_probe_upstream', return_value=(True, 123)) as probe:
            self.app._test_worker('a', 1, 7, 1)
        probe.assert_called_once_with('a', 1)
        self.app._show_test_result.assert_not_called()
        callbacks[0]()
        self.app._show_test_result.assert_called_once_with(7, 'a:1', True, 123, 1)

    def test_unexpected_probe_error_still_returns_result(self):
        self.app._ui = lambda callback: callback()
        self.app._show_test_result = mock.Mock()
        error = ValueError('unexpected')
        with mock.patch.object(pt, '_probe_upstream', side_effect=error):
            self.app._test_worker('a', 1, 7, 1)
        self.app._show_test_result.assert_called_once_with(7, 'a:1', False, error, 1)
