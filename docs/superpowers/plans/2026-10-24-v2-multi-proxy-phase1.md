# V2.0 第一阶段 · 多代理入口 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 本机透明转发入口 + 多上游池（round-robin 按连接分流 + 故障切换 + 熔断），系统代理改指向入口。

**Architecture:** `V2.0/proxy_tool.py` 从 V1.0 复制起步（单文件惯例不变）。新增 `UpstreamPool`（纯逻辑选择器）与 `HttpServer`（RelayServer 克隆：绑 127.0.0.1、拨号走池、per-upstream 统计）。配置预设从 `{ip, port}` 升级为 `{upstreams: [[ip, port], ...]}`，旧格式自动迁移。局域网共享不动，上游跟随池[0]。

**Tech Stack:** Python 标准库（tkinter / socket / threading / ctypes），测试用 `unittest`（零第三方依赖）。

**Spec:** `V2.0/多代理方案.md`（本计划 arguing from spec，执行者两个都要读）

## Global Constraints

- 零第三方依赖：纯标准库 + ctypes；测试只用 `unittest`
- 单文件应用：所有代码进 `V2.0/proxy_tool.py`，不拆模块；**V1.0/ 目录一个字节都不改**（稳定版）
- ttk clam 主题、定宽窗口：新增 Label 长 text 必须设 `wraplength=300`；新按钮 `padding=(5, 0)` 压到与 Combobox/Entry 同高（V1 教训）
- 工作线程禁调 `root.after`（用 `_ui()` 投队列）；字节计数在 `self._lock` 锁内入账；隧道接通后上游 socket 必须 `settimeout(None)`（V1 教训）
- 注释、commit message 全中文；commit 用 `feat:`/`fix:`/`test:`/`chore:` 前缀，一行写清"做了什么+为什么"
- Shell 是 Git Bash（win32）；python 命令可用；GUI 无法自动化——凡 GUI 编排以「手测清单」验证并如实标注

## Review Focus

spec 隐含但任务测试未必覆盖、最可能坑用户的五类，每条已挂到 owning task：

1. **入口端口被占**（Clash 等占 7890）：起入口失败必须弹窗明说，绝不静默"已开启"→ Task 4
2. **池清空/全非法后点开启**：必须拒绝并提示，不能写注册表指向空池入口 → Task 5（`validate_upstreams`）
3. **熔断上游 30 秒后要能回轮询**（不能一熔永久出局）→ Task 1 测试
4. **V1 旧配置首启迁移不丢预设**（用户现役 proxy_config.json）→ Task 3 测试
5. **入口运行中切预设/改池**：已建立连接不断、新连接立即走新池 → Task 4 手测

---

### Task 1: UpstreamPool——轮询选择器与熔断

**Files:**
- Create: `V2.0/proxy_tool.py`（`cp V1.0/proxy_tool.py`，本任务只加类）
- Create: `V2.0/proxy_tool.bat`（`cp V1.0/proxy_tool.bat`）
- Create: `V2.0/tests/__init__.py`（空文件）
- Test: `V2.0/tests/test_core.py`

**Interfaces:**
- Produces: `class UpstreamPool`，位于 proxy_tool.py 新区块 `# ---------- 上游池（多代理分流） ----------`（插在 RelayServer 之前）：
  - `__init__(self, upstreams: list, clock=time.monotonic)`，upstream 形如 `("192.130.30.73", 7890)`
  - `set(self, upstreams: list) -> None`——整体替换池（切预设用），重置轮询指针与失败计数
  - `order(self) -> list`——本次连接的尝试顺序：轮询指针推进一格后，健康上游按轮转序在前、熔断中（连续失败 ≥3 且 `clock() < 禁止至`）的殿后；空池返回 `[]`
  - `report_fail(self, up) / report_ok(self, up)`——失败计数与熔断登记 / 清零
  - 线程安全：`order/report_*/set` 全部持锁

- [ ] **Step 1: 复制起步文件 + 测试骨架**

```bash
cp V1.0/proxy_tool.py V2.0/proxy_tool.py
cp V1.0/proxy_tool.bat V2.0/proxy_tool.bat
mkdir -p V2.0/tests && touch V2.0/tests/__init__.py
```

test_core.py 开头（后续任务共用）：

```python
import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import proxy_tool as pt
```

- [ ] **Step 2: 冒烟确认复制件可编译**

Run: `python -m py_compile V2.0/proxy_tool.py`
Expected: 无输出退出 0

- [ ] **Step 3: 写失败测试**（test_core.py，全部用注入 clock，不 sleep）

```python
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
```

- [ ] **Step 4: 跑测试确认失败**

Run: `cd V2.0 && python -m unittest tests.test_core -v`
Expected: 6 个测试 ERROR/FAIL（`UpstreamPool` 不存在）

- [ ] **Step 5: 实现 UpstreamPool**

要点：`_idx` 轮询指针从 `order()` 推进（`_idx = (_idx + 1) % len`，取该位为首选）；`_fails: dict` 计数、`_banned_until: dict` 登记 `clock() + 30`；`order()` = 健康集按 `(_idx+i) % n` 序 + 熔断集按同序殿后。30 与 3 写成类常量 `BAN_AFTER = 3` / `BAN_SECS = 30`。

- [ ] **Step 6: 跑测试确认全过**

Run: `cd V2.0 && python -m unittest tests.test_core -v`
Expected: 6 passed

- [ ] **Step 7: Commit**

```bash
git add V2.0/proxy_tool.py V2.0/proxy_tool.bat V2.0/tests
git commit -m "feat: UpstreamPool 上游池——round-robin 按连接轮询，连续 3 败熔断 30 秒殿后兜底（V2.0 从 V1.0 复制起步）"
```

---

### Task 2: HttpServer——透明转发入口 + 故障切换 + per-upstream 统计

**Files:**
- Modify: `V2.0/proxy_tool.py`（新增 `HttpServer` 类，插在 SocksServer 之后）
- Test: `V2.0/tests/test_core.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `UpstreamPool.order()/report_fail()/report_ok()`
- Produces: `class HttpServer`（供 Task 4 的 ProxyApp 使用）：
  - `__init__(self, listen_port: int, pool: UpstreamPool)`；`start()` 绑 **`("127.0.0.1", listen_port)`**（绝不 0.0.0.0），占用抛 OSError；`stop()` 同 RelayServer（掐监听 + `_live` 全断）
  - `_pump`：按 `pool.order()` 逐台 `socket.create_connection(up, timeout=5)`；失败 `report_fail` 下一台；成功 `report_ok`、`settimeout(None)`、双向 `_pipe`；全败 `client.close()`
  - 统计：`self.stats: dict[up -> {"conns", "bd", "bu"}]`（锁内更新；`set` 换池后旧 up 的条目保留供 UI 显示残留计数）
  - `snapshot(self) -> (int, dict)`——`(总连接数, {up: (conns, bd, bu)})`，UI 每 2 秒轮询
  - `_pipe` 结构同 RelayServer._pipe，但 dev 参数换成对应 up 的 stats 条目（同 `{conns, bd, bu}` 形状，直接复用锁内入账写法）

- [ ] **Step 1: 写失败测试**（真 socket，全走 127.0.0.1 高位端口，测试自起假上游线程）

```python
import socket, threading, time

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
        self.dead_port = _free_port()          # 模块级 helper：bind 后关掉，拿到没人听的端口
        addrs = [self.ups[0].addr, self.ups[1].addr, ("127.0.0.1", self.dead_port)]
        self.pool = pt.UpstreamPool([addrs[0], addrs[1]])
        self.srv = pt.HttpServer(_free_port(), self.pool)   # 端口自选避免并行撞
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
        _, resp = self._connect_entry(b"ping")
        time.sleep(0.3)
        _, stats = self.srv.snapshot()
        (conns, bd, bu) = stats[self.ups[0].addr]
        self.assertEqual(bu, 4); self.assertEqual(bd, 2)    # 上行=ping 4B，下行=ok 2B
        self.assertEqual(conns, 0)                          # 连接已结束
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd V2.0 && python -m unittest tests.test_core -v`
Expected: 新增 5 个 FAIL/ERROR（`HttpServer` 不存在）

- [ ] **Step 3: 实现 HttpServer**

克隆 RelayServer 骨架改四处：bind 地址 127.0.0.1；`self.upstream` 换成 `self.pool`；拨号循环按 `pool.order()` 带 report_fail/ok（`create_connection` 捕 OSError 继续，循环外才掐 client）；`devices` 换 `stats`。`snapshot` 返回形状按 Interfaces。

- [ ] **Step 4: 跑测试确认全过**

Run: `cd V2.0 && python -m unittest tests.test_core -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add V2.0/proxy_tool.py V2.0/tests/test_core.py
git commit -m "feat: HttpServer 本机透明转发入口——绑 127.0.0.1，按池序拨号失败顺延，per-upstream 连接/字节统计（明文 GET 与 CONNECT 都走字节搬运，零协议解析）"
```

---

### Task 3: 配置结构升级——presets 带 upstreams 列表 + entry_port 设置

**Files:**
- Modify: `V2.0/proxy_tool.py`（`_normalize` :836、`load_settings` :804、`save_settings` 调用处、`DEFAULT_ENTRY_PORT` 常量区）
- Test: `V2.0/tests/test_core.py`（追加）

**Interfaces:**
- Produces:
  - 预设新形态：`{"name": str, "upstreams": [["ip","port"], ...]}`（ip/port 均字符串，与 V1 存储一致）；`save_config` 签名不变（presets 整体落盘）
  - `_normalize`：优先读 `upstreams`；无此键则由旧 `ip`/`port` 迁移成单元素列表；条目非法（host 校验同 `_valid_host` 逻辑、端口 1-65535）整条丢弃；预设空列表 → 默认 `[["127.0.0.1","7890"]]`
  - `load_settings` 新键 `entry_port`（默认 `DEFAULT_ENTRY_PORT = 7890`，非法回退默认）
  - 模块级 `def _valid_upstream(ip: str, port: str) -> bool`（host 校验逻辑从 ProxyApp._valid_host 提出共用，ProxyApp._valid_host 改为薄委托）

- [ ] **Step 1: 写失败测试**

```python
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

    def test_settings_entry_port_default_and_fallback(self):
        self.assertEqual(pt.load_settings.__wrapped__ if False else
                         pt.DEFAULT_ENTRY_PORT, 7890)  # 常量锁定；文件读写回退由手测覆盖
```

（最后一行只锁常量；`load_settings` 读真实文件，单测不便注入路径，入口端口非法回退的行为并入 Task 5 手测清单。）

- [ ] **Step 2: 跑测试确认失败**

Run: `cd V2.0 && python -m unittest tests.test_core -v`
Expected: 新增 6 个 FAIL（`upstreams` 键不存在 / `_valid_upstream` 未定义）

- [ ] **Step 3: 实现**

`_valid_upstream` = 现 `_valid_host` 的循环体 + 端口 `str.isascii() and str.isdecimal() and 1<=int<=65535`；`_normalize` 按 Interfaces 重写 presets 构造（保留 current/share 逻辑不动）；`DEFAULT_ENTRY_PORT = 7890` 加进常量区并注释"7890 若被 Clash 等占用，设置页可改"；`load_settings` 加 entry_port 三行（模式抄 socks_port）。

- [ ] **Step 4: 跑测试确认全过**

Run: `cd V2.0 && python -m unittest tests.test_core -v`
Expected: 17 passed

- [ ] **Step 5: Commit**

```bash
git add V2.0/proxy_tool.py V2.0/tests/test_core.py
git commit -m "feat: 预设升级为上游组 upstreams 列表——旧 {ip,port} 自动迁移单元素组，非法条目丢弃；settings 新增 entry_port(默认 7890)"
```

---

### Task 4: apply 接入入口生命周期 + 共享跟随池[0]

**Files:**
- Modify: `V2.0/proxy_tool.py`（`ProxyApp.__init__` :917、`_read_upstream` :1259、`_sync_relay_upstream` :1266、`apply` :1277、`refresh_status` :1204）

**Interfaces:**
- Consumes: Task 2 `HttpServer(listen_port, pool)`、Task 3 预设 `upstreams` 形态
- Produces:
  - `self._entry: HttpServer | None`、`self._pool: UpstreamPool`（`__init__` 用当前预设 upstreams 构造）
  - `def _pool_addrs(self) -> list[tuple]`——当前预设 `upstreams` → `[("ip", int(port)), ...]`（GUI 内多处用）
  - `def _sync_pools(self)`——入口在跑则 `self._entry.pool.set(self._pool_addrs())`；随后执行原 `_sync_relay_upstream` 的共享跟随（共享上游 = 池[0]，即 `_read_upstream` 改为返回 `self._pool_addrs()[0]`；`_sync_relay_upstream` 内部改调 `_sync_pools`，所有旧调用点不换名不改动）
  - `apply(enable)`：enable 时先 `self._pool.set(self._pool_addrs())`，起 `HttpServer(self.settings["entry_port"], self._pool)`（已起则复用；**OSError 弹"入口端口被占用（改设置里的入口端口）"并 return，不写注册表**），再 `set_system_proxy(f"127.0.0.1:{entry_port}")`；disable 时 `disable_system_proxy()` + `self._entry.stop()` 置 None
  - `refresh_status`：已开启且 server 以 `127.0.0.1:` 开头时，状态行显示 `● 已开启  本机入口 :{端口}（N 台上游）`；N 取 `len(self._pool_addrs())`

- [ ] **Step 1: 单测补 `_pool_addrs` 纯逻辑**（ProxyApp 不可无头实例化，只测模块级可剥离的部分：把 `upstreams 字符串对 → (ip,int)` 的转换写成模块级 `def _to_addr_pairs(upstreams: list) -> list`，GUI 方法调用它）

```python
class TestToAddrPairs(unittest.TestCase):
    def test_convert(self):
        self.assertEqual(pt._to_addr_pairs([["1.1.1.1", "80"], ["h.com", "8080"]]),
                         [("1.1.1.1", 80), ("h.com", 8080)])
    def test_empty(self):
        self.assertEqual(pt._to_addr_pairs([]), [])
```

- [ ] **Step 2: 跑 → 失败 → 实现 `_to_addr_pairs` → 跑 → 过**

Run: `cd V2.0 && python -m unittest tests.test_core -v` → FAIL → 实现 → 19 passed

- [ ] **Step 3: 按 Interfaces 改 ProxyApp 五处**（GUI 编排，无法自动化；改动对照上面 Produces）

- [ ] **Step 4: 手测清单（用户执行，如实回报结果）**

1. 启动 V2.0（`cd V2.0 && start proxy_tool.bat`），旧配置载入预设显示正常
2. 池=73 单台 → 点开启：注册表 ProxyServer = `127.0.0.1:7890`，浏览器能上网（透明转发走 73）
3. 开启状态下改入口端口为 7893（设置页）→ 重开代理生效
4. 用 netstat 占住 7890 后点开启 → 弹"端口被占用"，注册表不写入
5. 点关闭：注册表 ProxyEnable=0，`netstat` 无 7890 监听
6. 局域网共享照常开启，手机走 7891 上网（共享跟池[0]=73，V1 行为不坏）

- [ ] **Step 5: Commit**

```bash
git add V2.0/proxy_tool.py V2.0/tests/test_core.py
git commit -m "feat: 开关代理接入本机入口——开启=起 HttpPool 入口+系统代理指 127.0.0.1:entry_port，端口被占弹窗不写注册表；共享上游跟随池[0] 保持 V1 行为"
```

---

### Task 5: UI 池管理 + 状态行分列 + 逐台测试

**Files:**
- Modify: `V2.0/proxy_tool.py`（主界面 `__init__` :977-1005 输入区、`on_preset_selected` :1050、`save_preset` :1076、`delete_preset` :1058、`test_connectivity` :1301、`_test_worker` :1312、`_show_test_result` :1325、设置页 `open_settings` :1500）

**Interfaces:**
- Consumes: Task 4 `self._pool / _pool_addrs / _sync_pools`、Task 2 `snapshot()`
- Produces:
  - 模块级 `def validate_upstreams(pairs: list) -> str | None`——空列表返回"至少需要一台上游"；逐对 `_valid_upstream` 不过返回"上游 {ip}:{port} 格式不对"；全过返回 None（apply 的门槛 + Review Focus #2）
  - 主界面：IP/端口输入框保留作「添加上游」入口 + `添加` 按钮（padding=(5,0)）；`tk.Listbox(height=3)` 显示池每行 `ip:port` + `移除` 按钮（删选中行；删空不许——至少留一台）
  - 预设读写：`on_preset_selected/save_preset/delete_preset` 的 `p["ip"]/p["port"]` 访问全部换成池 Listbox ↔ `p["upstreams"]` 同步；切预设/增删上游后调 `_sync_pools()`
  - 状态行（`refresh_status` 轮询内）：入口在跑时追加一行 `wraplength=300` 的 Label：每台 `ip:port ↓速率 n连接`（差分采样复用 V1 `_speed_text` 模式，采样基点按 up 地址分桶存 `self._up_bytes_prev`）
  - 测试按钮：`test_connectivity` 遍历 `self._pool_addrs()` 逐台后台测（线程数=池大小），结果 `"73: ✓ 220ms ｜ 91: ✗ 连不上"`（`_test_worker` 加 ip 参数，结果经 `_ui()` 汇总）
  - 设置页：新增「入口端口」行（entry_var），`on_save` 校验并写入 settings；改了入口端口且代理开着 → 状态行红字提示"重新点一次开关代理"

- [ ] **Step 1: 写 `validate_upstreams` 失败测试**

```python
class TestValidateUpstreams(unittest.TestCase):
    def test_empty(self):
        self.assertIn("至少", pt.validate_upstreams([]))
    def test_bad_entry(self):
        self.assertIsNotNone(pt.validate_upstreams([("1.1.1.1", 80), ("bad host", 80)]))
    def test_all_good(self):
        self.assertIsNone(pt.validate_upstreams([("1.1.1.1", 80)]))
```

- [ ] **Step 2: 跑 → 失败 → 实现 → 跑 → 22 passed**

Run: `cd V2.0 && python -m unittest tests.test_core -v`

- [ ] **Step 3: 按 Interfaces 改 UI 七处**（GUI，手测验证；Listbox/按钮尺寸对齐既有 clam 风格）

- [ ] **Step 4: 手测清单（用户执行，如实回报）**

1. 添加 73、91 两台（91 先随便填）→ Listbox 两行；重启程序池还在
2. 删到只剩一台时「移除」被拦
3. 清空 IP 点添加 → 红框；非法地址进不了池
4. 点开启后池为空（构造：手改配置 JSON）→ 拒绝且注册表不写
5. 状态行两台各自显示 连接/速率（跑个下载）
6. 测试按钮分列显示两台结果；改坏 91 端口 → 91 行显 ✗
7. 窗口宽度不变（新 Label 都带 wraplength=300），三态进出新增模式无抖动

- [ ] **Step 5: Commit**

```bash
git add V2.0/proxy_tool.py V2.0/tests/test_core.py
git commit -m "feat: 主界面多上游管理——Listbox 池+添加/移除，状态行按上游分列速率，测试按钮逐台 generate_204 分列结果；设置页入口端口"
```

---

### Task 6: 端到端验收 + V2 快捷方式 + 推送

**Files:**
- Modify: 无代码（除非验收暴露缺陷，修在 V2.0/ 内）

- [ ] **Step 1: 全量回归**

Run: `cd V2.0 && python -m unittest tests.test_core -v`
Expected: 22 passed；`python -m py_compile V2.0/proxy_tool.py` 无输出

- [ ] **Step 2: 验收清单（对照 spec 第四节，用户执行）**

1. 单上游（仅 73）：批量下载速度与 V1 直连无明显差异（入口转发不拖速）
2. 故障切换：池=[73, 坏端口]，浏览器持续访问不中断（Review Focus #5 的运行态验证）
3. 旧配置首启迁移：V1 时代 presets 全部还在（Review Focus #4）
4. 共享不回归：手机 7891/7892 照常（Task 4 手测 6 复验）
5. 翻倍验收（91 配好后补做）：两台并发批量下载 ≥ 单台 1.6 倍——spec 验收线

- [ ] **Step 3: 桌面加 V2 快捷方式（不覆盖 V1 的）**

```bash
powershell -NoProfile -Command '$s = (New-Object -ComObject WScript.Shell).CreateShortcut("C:\Users\10307\Desktop\开发\proxy_tool_v2.lnk"); $s.TargetPath = "E:\MyXm\Ai\27\V2.0\proxy_tool.bat"; $s.WorkingDirectory = "E:\MyXm\Ai\27\V2.0"; $s.Save()'
```

- [ ] **Step 4: 更新 spec 验收状态 + 推送**

`V2.0/多代理方案.md` 头部标注"第一阶段已实现（验收 1-4 过，5 待 91 配置）"；commit（`chore: V2.0 第一阶段验收收尾`）；推送：`https_proxy=http://192.130.30.73:7890 git push github main`（直连必被 reset，直接带代理跑）

---

## Self-Review 结论

- **Spec 覆盖**：2.1 透明转发→Task 2；2.2 绑 127.0.0.1→Task 2 测试 `test_binds_loopback_only`；2.3 轮询/切换/熔断→Task 1+2；2.4 已接受代价不实现；三节 UI/配置→Task 3+5；共享跟随→Task 4；验收→Task 6。无缺口。
- **比例**：计划 6 任务/约 24 步，对应约 +400 行代码与测试，相当。
- **GUI 局限明示**：Task 4/5 的编排步骤无法自动化，以手测清单收口——执行者不得把手测步骤勾掉当完成。
