import os, sys, unittest
import json, shutil, socket, tempfile, threading, time
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


@unittest.skipUnless(hasattr(pt, "tk"), "tkinter 不可用时跳过")
class _AppTest(unittest.TestCase):
    """真 Tk 无头测试基类：不进 mainloop；托盘/轮询/入口恢复隔离，配置指到临时目录"""

    CURRENT = "m"   # 启动时选中的预设（单 s / 多 m）

    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.root.withdraw()
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        self.cfg_path = os.path.join(tmp, "c.json")
        cfg = {"current": self.CURRENT, "share": False, "presets": [
            {"name": "s", "type": "single", "upstreams": [["1.1.1.1", "80"]]},
            {"name": "m", "type": "multi",
             "upstreams": [["1.1.1.1", "80"], ["2.2.2.2", "80"]]}]}
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        patchers = [
            mock.patch.object(pt, "CONFIG_FILE", self.cfg_path),
            mock.patch.object(pt, "SETTINGS_FILE", os.path.join(tmp, "s.json")),
            mock.patch.object(pt, "OLD_SETTINGS_FILE", os.path.join(tmp, "no_s.json")),
            mock.patch.object(pt, "OLD_CONFIG_FILE", os.path.join(tmp, "no_c.json")),
            mock.patch.object(pt, "LEGACY_CONFIG_FILE", os.path.join(tmp, "no_l.json")),
            mock.patch.object(pt, "TrayIcon",
                              mock.MagicMock(side_effect=Exception("no tray"))),
            mock.patch.object(pt.ProxyApp, "_restore_entry"),
            mock.patch.object(pt.ProxyApp, "_auto_refresh"),
            mock.patch.object(pt.ProxyApp, "_pump_ui_queue"),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)
        self.app = pt.ProxyApp(self.root)
        self.addCleanup(self.root.destroy)

    def _read_disk(self):
        with open(self.cfg_path, encoding="utf-8") as f:
            return json.load(f)


class TestSingleFormEdit(_AppTest):
    """单形态直接编辑的落盘判定"""

    CURRENT = "s"

    def test_commit_updates_preset_and_saves(self):
        self.app.ip_var.set("9.9.9.9")
        self.app.port_var.set("1080")
        self.app._commit_single_edit()
        self.assertEqual(self.app.presets[0]["upstreams"], [["9.9.9.9", "1080"]])
        self.assertEqual(self._read_disk()["presets"][0]["upstreams"], [["9.9.9.9", "1080"]])

    def test_commit_invalid_keeps_old(self):
        before = self._read_disk()
        self.app.ip_var.set("bad host!")
        self.app._commit_single_edit()
        self.assertEqual(self.app.presets[0]["upstreams"], [["1.1.1.1", "80"]])
        self.assertEqual(self._read_disk(), before)   # 非法值不落盘

    def test_commit_unchanged_skips_save(self):
        with mock.patch.object(self.app, "save_preset") as save:
            self.app.ip_var.set("1.1.1.1")
            self.app.port_var.set("80")
            self.app._commit_single_edit()
            save.assert_not_called()                  # 值没变别折腾

    def test_commit_on_multi_preset_noop(self):
        self.app.preset_var.set("m")
        self.app._apply_form()
        self.app.ip_var.set("9.9.9.9")
        self.app.port_var.set("1080")
        self.app._commit_single_edit()
        self.assertEqual(self.app.presets[1]["upstreams"],
                         [["1.1.1.1", "80"], ["2.2.2.2", "80"]])  # 池没被污染


class TestShowTestResult(_AppTest):
    """逐台测试结果的分形态展示：多形态写池表 test 列，单形态写结果行"""

    def test_multi_success_writes_pool_row(self):
        self.app.preset_var.set("m")
        self.app._apply_form()
        self.app._refresh_pool_box()
        self.app._test_results = {}
        g = self.app._test_gen
        self.app._show_test_result(g, "1.1.1.1:80", True, 210, 2)
        self.app._show_test_result(g, "2.2.2.2:80", True, 320, 2)
        self.assertEqual(self.app.pool_box.set("0", "test"), "✓ 210 ms")
        self.assertEqual(self.app.pool_box.set("1", "test"), "✓ 320 ms")
        self.assertEqual(self.app.test_btn["text"], "测试连通")   # 收齐恢复按钮

    def test_multi_failure_uses_short_word(self):
        self.app.preset_var.set("m")
        self.app._apply_form()
        self.app._refresh_pool_box()
        self.app._test_results = {}
        g = self.app._test_gen
        reason = OSError(); reason.winerror = 10061
        self.app._show_test_result(g, "1.1.1.1:80", False, reason, 1)
        self.assertEqual(self.app.pool_box.set("0", "test"), "✗ 连不上")

    def test_single_result_shows_in_label(self):
        self.app.preset_var.set("s")
        self.app._apply_form()
        self.app._test_results = {}
        g = self.app._test_gen
        reason = OSError(); reason.winerror = 10060
        self.app._show_test_result(g, "1.1.1.1:80", False, reason, 1)
        text = self.app.test_result_var.get()
        self.assertIn("✗ 超时", text)
        self.assertIn("无响应", text)                    # 完整人话也带上

    def test_stale_gen_discarded(self):
        self.app.preset_var.set("m")
        self.app._apply_form()
        self.app._refresh_pool_box()
        self.app._test_results = {}
        self.app._show_test_result(self.app._test_gen - 1, "1.1.1.1:80", True, 99, 1)
        self.assertEqual(self.app.pool_box.set("0", "test"), "")  # 作废的不写


class TestDraftPreset(_AppTest):
    """新增预设的 draft 缓冲语义：存才落盘，Esc/取消全弃"""

    def test_draft_add_never_touches_disk(self):
        before = self._read_disk()
        self.app._enter_add_mode("multi", "新池")
        self.assertTrue(self.app._adding)
        self.assertEqual(self.app._draft["upstreams"], [])
        self.app.add_var.set("3.3.3.3:80")
        self.app.add_upstream()
        self.app.add_var.set("2.2.2.2:80")
        self.app.add_upstream()
        self.assertEqual(self.app._draft["upstreams"], [["3.3.3.3", "80"], ["2.2.2.2", "80"]])
        self.assertEqual(self._read_disk(), before)        # 半成品绝不落盘
        self.app._cancel_add_mode()
        self.assertFalse(self.app._adding)
        self.assertNotIn("新池", [p["name"] for p in self.app.presets])
        self.assertEqual(self._read_disk(), before)        # 取消后磁盘无痕

    def test_add_mode_shows_draft_name_in_preset_box(self):
        # 弹窗里填的名称要实时显示在主窗口预设下拉（set("") 会让用户以为名称没用）
        self.app._enter_add_mode("multi", "新池")
        self.assertEqual(self.app.preset_var.get(), "新池")
        self.app._cancel_add_mode()                         # 取消恢复原来的预设显示
        self.assertEqual(self.app.preset_var.get(), "m")

    def test_save_multi_draft_persists_with_type(self):
        self.app._enter_add_mode("multi", "新池")
        self.app.add_var.set("3.3.3.3:80")
        self.app.add_upstream()
        self.app.on_add_or_save()
        self.assertFalse(self.app._adding)
        self.assertIn("新池", [p["name"] for p in self.app.presets])
        disk = self._read_disk()
        saved = [p for p in disk["presets"] if p["name"] == "新池"][0]
        self.assertEqual(saved["type"], "multi")
        self.assertEqual(saved["upstreams"], [["3.3.3.3", "80"]])
        self.assertEqual(disk["current"], "新池")

    def test_save_single_draft_persists_with_type(self):
        self.app._enter_add_mode("single", "单台")
        self.app.ip_var.set("9.9.9.9")
        self.app.port_var.set("1080")
        self.app.on_add_or_save()
        saved = [p for p in self._read_disk()["presets"] if p["name"] == "单台"][0]
        self.assertEqual(saved["type"], "single")
        self.assertEqual(saved["upstreams"], [["9.9.9.9", "1080"]])

    def test_save_empty_multi_draft_blocked(self):
        with mock.patch.object(pt.messagebox, "showwarning") as warn:
            self.app._enter_add_mode("multi", "空池")
            self.app.on_add_or_save()
            warn.assert_called_once()                      # 提示至少一台上游
        self.assertTrue(self.app._adding)                  # 留在新增模式可继续
        self.assertNotIn("空池", [p["name"] for p in self.app.presets])

    def test_draft_remove_can_empty(self):
        with mock.patch.object(pt.messagebox, "showinfo") as info:
            self.app._enter_add_mode("multi", "新池")
            self.app.add_var.set("3.3.3.3:80")
            self.app.add_upstream()
            self.app.pool_box.selection_set("0")
            self.app.remove_upstream()
            info.assert_not_called()                       # draft 里删到 0 不拦
        self.assertEqual(self.app._draft["upstreams"], [])


class TestReviewFixes(_AppTest):
    """终审修复回归：C1 draft 旁路落盘、I1 删除预设切形态、I2 测试中添加恢复按钮"""

    def test_bypass_save_preset_during_draft_keeps_disk(self):
        before = self._read_disk()
        self.app._enter_add_mode("multi", "新池")   # 空草稿，preset_var 已置空
        ok = self.app.save_preset()                 # 模拟设置页保存/在途共享完成等旁路
        self.assertTrue(ok)                         # 拒绝落盘不算失败，别弹错
        self.assertEqual(self._read_disk(), before)        # 磁盘分毫未动
        self.assertEqual([p["name"] for p in self.app.presets], ["s", "m"])

    def test_save_preset_normal_path_unaffected(self):
        self.app._current_preset()["upstreams"].append(["3.3.3.3", "80"])
        self.assertTrue(self.app.save_preset())
        saved = [p for p in self._read_disk()["presets"] if p["name"] == "m"][0]
        self.assertEqual(len(saved["upstreams"]), 3)

    def test_delete_preset_switches_form(self):
        self.app.delete_preset()                    # 删当前 m → 剩 s（single）
        self.assertEqual(self.app.preset_var.get(), "s")
        self.assertEqual(self.app.port_frm.winfo_manager(), "grid")   # 单形态组在
        self.assertEqual(self.app.add_frm.winfo_manager(), "")        # 多形态组撤
        self.assertEqual(self.app.ip_var.get(), "1.1.1.1")   # 两格带出新预设的上游


class TestLayoutAlignment(_AppTest):
    """布局对齐回归：单/多形态控件直挂主网格与「预设」同列（不再是各自的独立网格），
    切形态即整组显隐——「预设」下拉与「添加上游」输入框起点天然对齐"""

    def test_forms_share_main_grid(self):
        app = self.app
        # 单/多形态的内容控件与 preset_frm 同父（同一主网格）
        self.assertEqual(app.ip_entry.winfo_parent(), app.preset_frm.winfo_parent())
        self.assertEqual(app.add_frm.winfo_parent(), app.preset_frm.winfo_parent())
        # 显示中的形态组与 preset_frm 同列（col1，起点对齐）；grid_info 只对显示中控件有效
        self.assertEqual(app.add_frm.grid_info()["column"], app.preset_frm.grid_info()["column"])
        app.presets[app.presets.index(app._current_preset())]["type"] = "single"
        app._apply_form()
        self.assertEqual(app.ip_entry.grid_info()["column"], app.preset_frm.grid_info()["column"])

    def test_switch_hides_inactive_form_group(self):
        app = self.app  # CURRENT=m：多形态在显示
        self.assertEqual(app.ip_entry.winfo_manager(), "")   # 单形态两格整组隐藏
        self.assertEqual(app.port_frm.winfo_manager(), "")
        self.assertEqual(app.add_frm.winfo_manager(), "grid")  # 多形态在
        app.presets[app.presets.index(app._current_preset())]["type"] = "single"
        app._apply_form()                                     # 切回单形态
        self.assertEqual(app.add_frm.winfo_manager(), "")     # 多形态组隐藏
        self.assertEqual(app.pool_frm.winfo_manager(), "")
        self.assertEqual(app.ip_entry.winfo_manager(), "grid")  # 单形态组在

    def test_apply_form_recalculates_window_size(self):
        # resizable(False,False) 会把窗口锁在上次布局的尺寸：切形态后不重算，
        # 多形态池表/预设行按钮就被截在窗外（用户实测截断的根源）
        calls = []
        self.app.root.geometry = calls.append
        self.app._apply_form()
        self.assertIn("", calls)   # geometry("")=交还 tk 按内容重算宽高

    def test_window_title_shows_version(self):
        # 标题栏带版本号：一眼区分跑的是哪个版本
        self.assertIn("V2.0", self.app.root.title())

    def test_add_during_pending_test_restores_button(self):
        self.app.test_btn.config(state="disabled", text="测试中…")   # 模拟测试在途
        self.app.add_var.set("3.3.3.3:80")
        self.app.add_upstream()
        self.assertEqual(str(self.app.test_btn.cget("state")), "normal")
        self.assertEqual(self.app.test_btn["text"], "测试连通")

    def test_check_and_radio_use_big_indicator(self):
        # 主题指示器小且 clam 画叉、经典控件指示器绑死字体（用户实测三连反馈）：
        # 换自绘 20px 位图指示器，文字大小不受影响
        import tkinter as tk
        self.assertEqual(self.app.autostart_cb.winfo_class(), "TCheckbutton")
        self.assertEqual(str(self.app.autostart_cb.cget("style")), "Big.TCheckbutton")
        self.app._open_new_preset_dialog()
        self.root.update_idletasks()
        top = [w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel)][-1]
        radios = []

        def walk(w):
            for c in w.winfo_children():
                if c.winfo_class() == "TRadiobutton":
                    radios.append(c)
                walk(c)

        walk(top)
        self.assertEqual(len(radios), 2)
        for r in radios:
            self.assertEqual(str(r.cget("style")), "Big.TRadiobutton")
        for name, img in self.app._ind_imgs.items():
            self.assertEqual((img.width(), img.height()), (20, 20), f"{name} 指示器应 20px")


class TestDialogPolish(_AppTest):
    """弹窗细节：新增预设弹窗居中于主窗口（不再冒在屏幕左上角）、客户端窗列宽"""

    def test_new_preset_dialog_centers_over_main(self):
        import tkinter as tk
        # 主窗口挪离屏幕角：不挪的话弹窗默认位置也在 (0,0) 附近，测试会假通过
        self.root.geometry("+300+250")
        self.root.deiconify()   # setUp 里 withdraw 的窗口 winfo_root*=0，得先映射回来
        self.addCleanup(self.root.withdraw)
        self.root.update()
        self.app._open_new_preset_dialog()
        self.root.update_idletasks()
        win = [w for w in self.root.winfo_children()
               if isinstance(w, tk.Toplevel)][-1]      # 最新弹出的 Toplevel
        rx, rw = self.root.winfo_rootx(), self.root.winfo_width()
        ry, rh = self.root.winfo_rooty(), self.root.winfo_height()
        cx, cy = win.winfo_rootx() + win.winfo_width() / 2, \
                 win.winfo_rooty() + win.winfo_height() / 2
        self.assertTrue(rx <= cx <= rx + rw, f"弹窗中心 x={cx} 不在主窗口 {rx}~{rx+rw}")
        self.assertTrue(ry <= cy <= ry + rh, f"弹窗中心 y={cy} 不在主窗口 {ry}~{ry+rh}")

    def test_clients_columns_wider(self):
        # 430px 的窄表放不下 IP/速率/最后活跃同排：列整体加宽
        from tkinter import ttk
        self.app.open_clients()
        self.addCleanup(self.app._clients_win.destroy)
        self.root.update_idletasks()
        tv = next(w for w in self.app._clients_win.winfo_children()
                  if isinstance(w, ttk.Treeview))
        total = sum(tv.column(c, "width") for c in ("ip", "conns", "down", "up", "last"))
        self.assertGreaterEqual(total, 500, f"客户端表总宽 {total}px，仍太窄")


if __name__ == "__main__":
    unittest.main()
