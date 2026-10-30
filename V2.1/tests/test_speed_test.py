import json
from pathlib import Path
import sys
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import speed_test as speed


def test_parallel_rate_uses_group_wall_clock_and_invalid_samples_have_no_speed():
    streams = [{"bytes": 1024, "complete": True}, {"bytes": 3072, "complete": True}]
    result = speed.summarize("parallel", streams, 2)
    assert result["KiB_per_s"] == 2
    streams[1]["complete"] = False
    result = speed.summarize("partial", streams, 2)
    assert result["complete"] is False
    assert result["KiB_per_s"] is None
    assert result["MiB_per_s"] is None


def test_access_check_has_no_speed_label():
    row = {"label": "access", "complete": True, "access_check": True, "MiB_per_s": None}
    assert speed.result_line(row) == "access：通过"


@pytest.mark.parametrize("kind", ["proxy_403", "target_403", "truncated", "wrong_length", "too_long", "ok", "chunked"])
def test_download_distinguishes_rejection_and_partial_transfers(kind):
    connection = mock.Mock()
    response = connection.getresponse.return_value
    response.status = 403 if kind == "target_403" else 200
    response.getheader.return_value = None if kind == "chunked" else ("5" if kind == "wrong_length" else "4")
    if kind == "proxy_403":
        connection.connect.side_effect = OSError("Tunnel connection failed: 403 Forbidden")
    response.read1.side_effect = {
        "truncated": [b"ab", b""], "too_long": [b"abcd", b"e"],
    }.get(kind, [b"abcd", b""])
    with mock.patch.object(speed.http.client, "HTTPSConnection", return_value=connection):
        result = speed.download(("127.0.0.1", 7890), 4, object())
    assert result["complete"] == (kind in {"ok", "chunked"})
    connection.set_tunnel.assert_called_once_with(speed.TARGET, 443)
    connection.connect.assert_called_once()
    connection.close.assert_called_once()
    if not result["complete"]:
        assert speed.summarize("failure", [result], 1)["KiB_per_s"] is None
    if kind == "proxy_403":
        assert result["last_phase"] == "connect_tls"
        assert result["connect_tls_seconds"] is None
        connection.request.assert_not_called()
    elif kind in {"ok", "chunked"}:
        assert result["last_phase"] == "complete"
        assert 0 <= result["connect_tls_seconds"] <= result["headers_seconds"] <= result["first_byte_seconds"]


def test_config_read_honors_current_preset_disabled_nodes_and_does_not_write(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"current": "multi", "presets": [
        {"name": "other", "upstreams": [["192.0.2.1", "7890"]]},
        {"name": "multi", "upstreams": [["192.0.2.2", "7890"], ["192.0.2.3", "7890"]],
         "upstream_options": {"192.0.2.2:7890": {"enabled": False}}},
    ]}), encoding="utf-8")
    before = config.read_bytes()
    with mock.patch.object(speed.pt, "save_config", side_effect=AssertionError("must not write")):
        upstreams, source = speed.read_upstreams({"config_path": str(config)}, None)
    assert upstreams == [("192.0.2.3", 7890)]
    assert source == str(config)
    assert config.read_bytes() == before


def test_missing_custom_config_does_not_silently_test_a_different_preset(tmp_path):
    with pytest.raises(ValueError, match="没有找到"):
        speed.read_upstreams({"config_path": str(tmp_path / "missing.json")}, None)


def test_temporary_server_stops_even_when_measurement_fails():
    server = mock.Mock()
    server._srv.getsockname.return_value = ("127.0.0.1", 12345)
    with mock.patch.object(speed.pt, "HttpServer", return_value=server), \
            mock.patch.object(speed, "measure", side_effect=RuntimeError("test failure")):
        with pytest.raises(RuntimeError):
            speed.measure_pool([("127.0.0.1", 7890)], 4, 3000000, object())
    server.stop.assert_called_once()


def test_buffer_comparison_retains_keepalive_and_nodelay_and_restores_after_failure():
    server, sock = mock.Mock(), mock.Mock()
    server._srv.getsockname.return_value = ("127.0.0.1", 12345)

    def tune(s):
        s.setsockopt(speed.pt.socket.SOL_SOCKET, speed.pt.socket.SO_RCVBUF, 262144)
        s.setsockopt(speed.pt.socket.SOL_SOCKET, speed.pt.socket.SO_SNDBUF, 262144)
        s.setsockopt(speed.pt.socket.SOL_SOCKET, speed.pt.socket.SO_KEEPALIVE, 1)
        s.setsockopt(speed.pt.socket.IPPROTO_TCP, speed.pt.socket.TCP_NODELAY, 1)
        s.ioctl(123, (1, 60000, 10000))

    server.start.side_effect = lambda: speed.pt._tune_sock(sock)
    with mock.patch.object(speed.pt, "_tune_sock", tune), \
            mock.patch.object(speed.pt, "HttpServer", return_value=server), \
            mock.patch.object(speed, "measure", side_effect=RuntimeError("test failure")):
        with pytest.raises(RuntimeError):
            speed.measure_pool([("127.0.0.1", 7890)], 4, 3000000, object(), system_buffers=True)
        assert speed.pt._tune_sock is tune
    assert sock.setsockopt.call_args_list == [
        mock.call(speed.pt.socket.SOL_SOCKET, speed.pt.socket.SO_KEEPALIVE, 1),
        mock.call(speed.pt.socket.IPPROTO_TCP, speed.pt.socket.TCP_NODELAY, 1),
    ]
    sock.ioctl.assert_called_once_with(123, (1, 60000, 10000))
    server.stop.assert_called_once()


def test_diagnosis_compares_equal_workload_and_repeats_controls():
    ups = [("192.0.2.1", 7890), ("192.0.2.2", 7890)]
    records = []
    context = object()
    with mock.patch.object(speed, "measure", side_effect=lambda *a, **kw: {"label": a[0]}) as direct, \
            mock.patch.object(speed, "measure_pool", side_effect=lambda *a, **kw: {"label": kw["label"]}) as pool, \
            mock.patch.object(speed.time, "sleep"):
        speed.diagnose(ups, ("127.0.0.1", 7890), 4, 2000000, context, records.append)
    assert records[0]["comparison_kind"] == records[-1]["comparison_kind"] == "direct"
    for call in direct.call_args_list:
        assert call.args[2:5] == (4, 2000000, context)
    for call in [direct.call_args_list[0], direct.call_args_list[-1]]:
        assert call.kwargs["routes"].count(ups[0]) == 2
        assert call.kwargs["routes"].count(ups[1]) == 2
    for call in pool.call_args_list:
        assert call.args[1:] == (4, 2000000, context)
    multi = [call for call in pool.call_args_list if call.args[0] == ups]
    assert [call.kwargs["system_buffers"] for call in multi] == [False, True, False]


def test_direct_mixed_measurement_keeps_each_streams_route():
    routes = [("192.0.2.1", 7890), ("192.0.2.2", 7890)] * 2

    def transfer(proxy, size, context, gate):
        gate.wait(timeout=2)
        return {"bytes": size, "complete": True, "proxy": proxy}

    with mock.patch.object(speed, "download", side_effect=transfer):
        row = speed.measure("mixed", None, 4, 2000000, object(), routes=routes)
    assert row["bytes"] == 8000000
    assert row["complete"] is True
    assert [s["proxy"] for s in row["streams"]] == routes


def test_access_check_retries_transient_failure_but_preserves_first_error():
    first = {"complete": False, "error": {"type": "SSLError", "message": "unexpected EOF"}}
    second = {"complete": True, "error": None}
    with mock.patch.object(speed, "download", side_effect=[first, second]), \
            mock.patch.object(speed.time, "sleep"):
        stream, attempts = speed.check_access(("127.0.0.1", 7890), object())
    assert stream is second
    assert attempts == [first, second]


def test_access_check_does_not_retry_forbidden_clients():
    failure = {"complete": False, "error": {"type": "OSError", "message": "Tunnel connection failed: 403 Forbidden"}}
    with mock.patch.object(speed, "download", return_value=failure) as download:
        stream, attempts = speed.check_access(("127.0.0.1", 7890), object())
    assert stream is failure
    assert len(attempts) == 1
    download.assert_called_once()
