import os, sys, unittest
import socket, threading, time
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


if __name__ == "__main__":
    unittest.main()
