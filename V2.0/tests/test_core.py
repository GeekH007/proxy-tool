import os, sys, unittest
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


if __name__ == "__main__":
    unittest.main()
