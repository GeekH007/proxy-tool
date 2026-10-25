import os, sys, unittest
import json, socket, tempfile, threading, time
from unittest import mock
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import proxy_tool as pt


class FakeClock:  # 用完即焚的时间源：advance 推进
    def __init__(self): self.t = 1000.0
    def __call__(self): return self.t
    def advance(self, s): self.t += s


class TestUpstreamPool(unittest.TestCase):
    def test_round_robin_order(self):
        clk = FakeClock()
        pool = pt.UpstreamPool([("1.1.1.1", 1), ("2.2.2.2", 2)], clock=clk)
        self.assertEqual([pool.order()[0] for _ in range(4)],
                         [("1.1.1.1", 1), ("2.2.2.2", 2)] * 2)

    def test_failover_order_skips_dead_first(self):
        clk = FakeClock()
        pool = pt.UpstreamPool([("1.1.1.1", 1), ("2.2.2.2", 2)], clock=clk)
        for _ in range(3):
            pool.report_fail(("1.1.1.1", 1))
        self.assertEqual(pool.order()[0], ("2.2.2.2", 2))   # 熔断者不作首选
        self.assertIn(("1.1.1.1", 1), pool.order())          # 但仍在列可兜底

    def test_ban_expires_after_30s(self):
        clk = FakeClock()
        pool = pt.UpstreamPool([("1.1.1.1", 1), ("2.2.2.2", 2)], clock=clk)
        for _ in range(3):
            pool.report_fail(("1.1.1.1", 1))
        clk.advance(31)
        self.assertEqual(pool.order()[0], ("1.1.1.1", 1))    # 过期回轮转

    def test_report_ok_resets_failures(self):
        clk = FakeClock()
        pool = pt.UpstreamPool([("1.1.1.1", 1)], clock=clk)
        pool.report_fail(("1.1.1.1", 1)); pool.report_fail(("1.1.1.1", 1))
        pool.report_ok(("1.1.1.1", 1))
        pool.report_fail(("1.1.1.1", 1))                     # 第 1 次而非第 3 次
        self.assertEqual(pool.order()[0], ("1.1.1.1", 1))    # 未达熔断门槛

    def test_set_replaces_and_resets(self):
        clk = FakeClock()
        pool = pt.UpstreamPool([("1.1.1.1", 1)], clock=clk)
        for _ in range(3):
            pool.report_fail(("1.1.1.1", 1))
        pool.set([("3.3.3.3", 3), ("1.1.1.1", 1)])
        self.assertEqual(pool.order()[0], ("3.3.3.3", 3))    # 指针/计数重置

    def test_empty_pool(self):
        self.assertEqual(pt.UpstreamPool([], clock=FakeClock()).order(), [])


def _free_port():
    """拿一个当前没人监听的回环端口（bind 0 分配后立刻释放）"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeUpstream(threading.Thread):  # 假 HTTP 上游：accept 后收什么记什么，回了"ok"
    def __init__(self):
        super().__init__(daemon=True)
        self.received, self.srv = [], socket.socket()
        self.srv.bind(("127.0.0.1", 0)); self.srv.listen(8)
        self.addr = self.srv.getsockname()
    def run(self):
        while True:
            try: c, _ = self.srv.accept()
            except OSError: return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()
    def _serve(self, c):
        try:
            data = c.recv(4096)
            if data: self.received.append(data)
            c.sendall(b"ok")
        except OSError: pass
        finally: c.close()


class TestHttpServer(unittest.TestCase):
    def setUp(self):
        self.ups = [FakeUpstream() for _ in range(2)]
        for u in self.ups: u.start()
        self.dead_port = _free_port()
        self.pool = pt.UpstreamPool([self.ups[0].addr, self.ups[1].addr])
        self.srv = pt.HttpServer(_free_port(), self.pool)
        self.srv.start()
    def tearDown(self):
        self.srv.stop()
        for u in self.ups: u.srv.close()

    def _connect_entry(self, payload=b"ping"):  # 连入口→发→读到回包或 EOF
        c = socket.create_connection(("127.0.0.1", self.srv.listen_port), timeout=5)
        c.sendall(payload)
        try: resp = c.recv(4096)
        except OSError: resp = b""
        return c, resp

    def test_binds_loopback_only(self):
        self.assertEqual(self.srv._srv.getsockname()[0], "127.0.0.1")

    def test_connections_round_robin(self):
        for _ in range(2): self._connect_entry()
        time.sleep(0.3)
        self.assertEqual(len(self.ups[0].received), 1)
        self.assertEqual(len(self.ups[1].received), 1)

    def test_failover_to_second_upstream(self):
        self.pool.set([("127.0.0.1", self.dead_port), self.ups[1].addr])
        _, resp = self._connect_entry(b"via-b")
        self.assertEqual(resp, b"ok")                       # 死首选 → 落到第二台
        self.assertIn(b"via-b", self.ups[1].received[-1])

    def test_all_dead_closes_client(self):
        self.pool.set([("127.0.0.1", self.dead_port), ("127.0.0.1", self.dead_port)])
        _, resp = self._connect_entry()
        self.assertEqual(resp, b"")                         # 全挂 → 客户端被掐

    def test_stats_accounting(self):
        c, resp = self._connect_entry(b"ping")
        c.close()  # 不关客户端，client→up 方向的搬运线程读不到 EOF，连接数不会归零
        time.sleep(0.3)
        _, stats = self.srv.snapshot()
        (conns, bd, bu) = stats[self.ups[0].addr]
        self.assertEqual(bu, 4); self.assertEqual(bd, 2)    # 上行=ping 4B，下行=ok 2B
        self.assertEqual(conns, 0)                          # 连接已结束


class TestConfigMigration(unittest.TestCase):
    def test_legacy_preset_migrates(self):
        cfg = pt._normalize({"presets": [{"name": "73", "ip": "192.130.30.73", "port": "7890"}]})
        self.assertEqual(cfg["presets"][0]["upstreams"], [["192.130.30.73", "7890"]])

    def test_legacy_single_config_migrates(self):
        cfg = pt._normalize({"ip": "192.130.30.73", "port": "7890"})
        self.assertEqual(cfg["presets"][0]["upstreams"], [["192.130.30.73", "7890"]])

    def test_new_format_roundtrip(self):
        src = {"current": "双上游", "share": False,
               "presets": [{"name": "双上游",
                            "upstreams": [["192.130.30.73", "7890"], ["192.130.30.91", "7890"]]}]}
        cfg = pt._normalize(src)
        self.assertEqual(cfg["presets"][0]["upstreams"], src["presets"][0]["upstreams"])
        self.assertEqual(cfg["current"], "双上游")

    def test_mixed_presets(self):
        cfg = pt._normalize({"presets": [
            {"name": "新", "upstreams": [["1.1.1.1", "80"]]},
            {"name": "旧", "ip": "2.2.2.2", "port": "8080"}]})
        self.assertEqual([p["upstreams"] for p in cfg["presets"]],
                         [[["1.1.1.1", "80"]], [["2.2.2.2", "8080"]]])

    def test_bad_entries_dropped(self):
        cfg = pt._normalize({"presets": [{"name": "x", "upstreams":
                         [["1.1.1.1", "80"], ["坏 地址", "80"], ["1.1.1.1", "99999"]]}]})
        self.assertEqual(cfg["presets"][0]["upstreams"], [["1.1.1.1", "80"]])

    def test_entry_port_constant(self):
        self.assertEqual(pt.DEFAULT_ENTRY_PORT, 7890)  # 文件读写的回退由 Task 5 手测覆盖


class TestToAddrPairs(unittest.TestCase):
    def test_convert(self):
        self.assertEqual(pt._to_addr_pairs([["1.1.1.1", "80"], ["h.com", "8080"]]),
                         [("1.1.1.1", 80), ("h.com", 8080)])

    def test_empty(self):
        self.assertEqual(pt._to_addr_pairs([]), [])


class TestValidateUpstreams(unittest.TestCase):
    def test_empty(self):
        self.assertIn("至少", pt.validate_upstreams([]))

    def test_bad_entry(self):
        self.assertIsNotNone(pt.validate_upstreams([("1.1.1.1", 80), ("bad host", 80)]))

    def test_all_good(self):
        self.assertIsNone(pt.validate_upstreams([("1.1.1.1", 80)]))


class TestV1Coexistence(unittest.TestCase):
    """V2 必须独立存配置：V1 读不懂 upstreams 格式，共享文件会被 V1 退出时覆写成默认值"""

    def test_config_dir_is_v2_specific(self):
        self.assertTrue(pt.CONFIG_DIR.rstrip("\\/").endswith("ProxyToolV2"))

    def test_first_boot_migrates_from_v1_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.path.join(tmp, "ProxyTool", "proxy_config.json")
            os.makedirs(os.path.dirname(old))
            with open(old, "w", encoding="utf-8") as f:
                json.dump({"current": "73",
                           "presets": [{"name": "73", "ip": "192.130.30.73", "port": "7890"}]}, f)
            new_file = os.path.join(tmp, "ProxyToolV2", "proxy_config.json")
            with mock.patch.object(pt, "CONFIG_FILE", new_file), \
                 mock.patch.object(pt, "OLD_CONFIG_FILE", old), \
                 mock.patch.object(pt, "LEGACY_CONFIG_FILE", os.path.join(tmp, "无.json")):
                cfg = pt.load_config()
            self.assertEqual(cfg["presets"][0]["upstreams"], [["192.130.30.73", "7890"]])
            self.assertTrue(os.path.exists(new_file))  # 读到旧格式 → 迁到 V2 位置并升级

    def test_settings_fallback_to_v1_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.path.join(tmp, "ProxyTool", "settings.json")
            os.makedirs(os.path.dirname(old))
            with open(old, "w", encoding="utf-8") as f:
                json.dump({"share_port": 8000, "socks_port": 8002, "socks_user": "u"}, f)
            new_file = os.path.join(tmp, "ProxyToolV2", "settings.json")
            with mock.patch.object(pt, "SETTINGS_FILE", new_file), \
                 mock.patch.object(pt, "OLD_SETTINGS_FILE", old):
                st = pt.load_settings()
            self.assertEqual(st["share_port"], 8000)   # 旧共享端口带上
            self.assertEqual(st["entry_port"], 7890)   # 旧文件没有的新键走默认


class TestParseEntryAddr(unittest.TestCase):
    def test_loopback_with_port(self):
        self.assertEqual(pt._parse_entry_addr("127.0.0.1:7890"), 7890)

    def test_external_and_garbage(self):
        self.assertIsNone(pt._parse_entry_addr("192.130.30.73:7890"))
        self.assertIsNone(pt._parse_entry_addr("127.0.0.1:xx"))
        self.assertIsNone(pt._parse_entry_addr(""))


class TestPresetTypeMigration(unittest.TestCase):
    def test_no_type_inferred_by_count(self):
        cfg = pt._normalize({"presets": [
            {"name": "a", "upstreams": [["1.1.1.1", "80"]]},
            {"name": "b", "upstreams": [["1.1.1.1", "80"], ["2.2.2.2", "80"]]}]})
        self.assertEqual([p["type"] for p in cfg["presets"]], ["single", "multi"])

    def test_single_with_two_upstreams_reclassified(self):
        cfg = pt._normalize({"presets": [{"name": "x", "type": "single",
            "upstreams": [["1.1.1.1", "80"], ["2.2.2.2", "80"]]}]})
        self.assertEqual(cfg["presets"][0]["type"], "multi")
        self.assertEqual(len(cfg["presets"][0]["upstreams"]), 2)  # 不截断

    def test_invalid_type_value_inferred(self):
        cfg = pt._normalize({"presets": [{"name": "x", "type": "singel",
            "upstreams": [["1.1.1.1", "80"]]}]})
        self.assertEqual(cfg["presets"][0]["type"], "single")

    def test_multi_with_one_stays_multi(self):
        cfg = pt._normalize({"presets": [{"name": "x", "type": "multi",
            "upstreams": [["1.1.1.1", "80"]]}]})
        self.assertEqual(cfg["presets"][0]["type"], "multi")

    def test_v1_single_address_preset_migrates(self):
        cfg = pt._normalize({"presets": [{"name": "旧", "ip": "1.1.1.1", "port": "80"}]})
        self.assertEqual(cfg["presets"][0], {"name": "旧", "type": "single",
                                             "upstreams": [["1.1.1.1", "80"]]})

    def test_fallback_default_carries_type(self):
        cfg = pt._normalize(None)
        self.assertEqual(cfg["presets"][0]["type"], "single")

    def test_save_config_roundtrips_type(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "c.json")
            pt.save_config("x", [{"name": "x", "type": "multi",
                                  "upstreams": [["1.1.1.1", "80"]]}], False, path)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f)["presets"][0]["type"], "multi")


class TestParseUpstreamLine(unittest.TestCase):
    def test_normal(self):
        self.assertEqual(pt.parse_upstream_line("192.130.30.73:7890"),
                         ("192.130.30.73", "7890"))

    def test_spaces_trimmed(self):
        self.assertEqual(pt.parse_upstream_line(" 1.1.1.1 : 80 "), ("1.1.1.1", "80"))

    def test_no_colon_rejected(self):
        self.assertIsNone(pt.parse_upstream_line("1.1.1.1"))

    def test_bad_port_rejected(self):
        self.assertIsNone(pt.parse_upstream_line("1.1.1.1:0"))
        self.assertIsNone(pt.parse_upstream_line("1.1.1.1:abc"))
        self.assertIsNone(pt.parse_upstream_line("1.1.1.1:99999"))

    def test_ipv6_literal_rejected_as_today(self):
        self.assertIsNone(pt.parse_upstream_line("[::1]:7890"))


class TestShortConnError(unittest.TestCase):
    def test_known_winerror(self):
        e = OSError(); e.winerror = 10061
        self.assertEqual(pt._short_conn_error(e), "连不上")
        e2 = OSError(); e2.winerror = 10060
        self.assertEqual(pt._short_conn_error(e2), "超时")

    def test_timeout_type(self):
        self.assertEqual(pt._short_conn_error(socket.timeout("timed out")), "超时")

    def test_unknown_falls_back(self):
        self.assertEqual(pt._short_conn_error(ValueError("x")), "失败")


if __name__ == "__main__":
    unittest.main()
