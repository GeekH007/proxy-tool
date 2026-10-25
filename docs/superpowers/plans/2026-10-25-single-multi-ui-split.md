# V2.0 主界面「单代理/多代理」分离 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 预设分单代理/多代理两类，界面形态跟随预设切换；多代理池表兼作状态表（速率/连接/测试结果），根治流量换行与窗口高度跳动。

**Architecture:** 全部改动集中在 `V2.0/proxy_tool.py`（单文件应用）与 `V2.0/tests/test_core.py`。数据层先给预设加 `type` 并兼容迁移（Task 1-2 纯逻辑 TDD），UI 层按形态重构主界面两组控件与新增模式（Task 3-6，核心逻辑抽纯函数测、布局靠手测清单），最后统一验收（Task 7）。运行时（入口/分流/共享/SOCKS 网关）一行不动。

**Tech Stack:** Python 3 + tkinter/ttk（clam 主题），unittest。

**Spec:** `docs/superpowers/specs/2026-10-25-single-multi-ui-split-design.md`（含已确认决策与「审查后补充决定」）

## Global Constraints

- 工作线程禁调 `root.after`、WndProc 回调禁碰 tkinter：一律 `self._ui(queue)` + 主线程轮询（现有机制）。
- ttk.Label 无 `fg` 别名，只能 `foreground`。
- 按钮文案不带省略号。
- 绝不截断丢用户数据（迁移冲突改判类型，不删条目）。
- 流量/测试文本不得改变窗口高度（高度只随形态与池大小变化）。
- 运行时行为（HttpServer/RelayServer/SocksServer/分流/注册表/托盘/自启/设置页）不改。
- 测试命令：`python -m unittest discover -s V2.0/tests -v`（cwd = E:\MyXm\Ai\27）。

## Review Focus

1. **旧格式预设迁移**（V1 `{ip,port}` 单地址、V2 无 type 池）→ type 推断不丢地址 → Task 1 测试锁定。
2. **手改配置 type 冲突**（single 却 2 台）→ 改判 multi 不截断 → Task 1 测试锁定。
3. **单行输入畸形值**（无冒号、坏端口、IPv6 字面量）→ None + 明确提示，不崩 → Task 2 测试锁定。
4. **测试在途时改池/切预设** → `_test_gen` 作废旧结果，不写错行 → Task 5 沿用机制 + 手测。
5. **新增模式半途中断**（Esc/切预设/关窗）→ draft 全弃、配置文件无半成品 → Task 6 手测核对 json 未变。

---

### Task 1: 配置模型——预设 `type` 字段与迁移

**Files:**
- Modify: `V2.0/proxy_tool.py:1094-1122`（`_normalize`）、`V2.0/proxy_tool.py:1157-1164`（`save_config`）、`V2.0/proxy_tool.py:1446-1452`（`save_preset` 新建分支）
- Test: `V2.0/tests/test_core.py`（追加 TestPresetTypeMigration 类）

**Interfaces:**
- Produces: `_normalize` 输出的每个 preset 带 `"type": "single" | "multi"`；`save_preset` 新建预设也带 `type`（按 `len(ups)==1` 推断）。后续所有任务读 `_current_preset()["type"]`。

- [ ] **Step 1: 写失败测试**

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest V2.0.tests.test_core.TestPresetTypeMigration -v`（cwd=E:\MyXm\Ai\27）
Expected: FAIL（KeyError "type" / 断言不等）

- [ ] **Step 3: 实现 `_normalize` type 推断**

在 `proxy_tool.py:1113-1114` 的 `presets.append` 处：`type` 取 `p.get("type")`，不在 `("single","multi")` 则按 `len(entries)==1→single 否则 multi`；`single` 且 `len(entries)>=2` 改判 `multi`。fallback 分支（1118 行）与 `save_preset` 新建分支（1451 行）补 `"type"`。

- [ ] **Step 4: 跑全量测试**

Run: `python -m unittest discover -s V2.0/tests -v`
Expected: 全 PASS（含既有测试）

- [ ] **Step 5: Commit**

```bash
git add V2.0/proxy_tool.py V2.0/tests/test_core.py
git commit -m "feat: 预设增加 type 字段——旧配置按台数推断迁移，冲突改判不丢数据"
```

---

### Task 2: 单行地址解析 + 精简错误词

**Files:**
- Modify: `V2.0/proxy_tool.py`（`_CONN_ERR_HINTS` 附近 1167-1183 加两个模块级函数）
- Test: `V2.0/tests/test_core.py`（追加 TestParseUpstreamLine、TestShortConnError）

**Interfaces:**
- Produces: `parse_upstream_line(text: str) -> tuple[str, str] | None`（按最后一个冒号 `rsplit(":", 1)` 拆，两段 strip 后过 `_valid_upstream`；任一环节不合返回 None）；`_short_conn_error(reason) -> str`（返回 2-4 字短词："连不上"/"超时"/"被重置"/"解析失败"/"失败"）。Task 3/5 消费。

- [ ] **Step 1: 写失败测试**

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m unittest V2.0.tests.test_core.TestParseUpstreamLine V2.0.tests.test_core.TestShortConnError -v`
Expected: FAIL（AttributeError: parse_upstream_line）

- [ ] **Step 3: 实现两个函数**

`_short_conn_error`：短词映射表 `{10061:"连不上", 10060:"超时", 11001:"解析失败", 10054:"被重置"}`；`isinstance(reason, TimeoutError)` 或 str 含 "timed out" → "超时"；其余 "失败"。不改既有 `_friendly_conn_error`（单形态完整文案仍用它）。

- [ ] **Step 4: 跑全量测试**

Run: `python -m unittest discover -s V2.0/tests -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add V2.0/proxy_tool.py V2.0/tests/test_core.py
git commit -m "feat: 单行 IP:端口 解析与测试失败精简短词"
```

---

### Task 3: 多代理形态——池表 5 列状态化 + 单行添加

**Files:**
- Modify: `V2.0/proxy_tool.py:1252-1291`（输入区/池表/状态行布局）、`proxy_tool.py:1377-1400`（`_refresh_pool_box`/`add_upstream`）、`proxy_tool.py:1585-1604`（`_entry_status_text`）
- Test: 全量回归 + 手测清单（本任务以 UI 为主，无可抽的厚逻辑）

**Interfaces:**
- Consumes: Task 1 `["type"]`、Task 2 `parse_upstream_line`。
- Produces: `ProxyApp._apply_form(self)`（按 `_current_preset()["type"]` 显隐单/多两组控件，grid/grid_remove）；池表列 id 固定 `("addr","down","up","conns","test")`，标题「上游/↓/↑/连接/测试」；`self.add_var`（单行输入框）；`ProxyApp._set_pool_row(self, iid, values)`（整行覆写）；单形态速率行 `self.entry_speed_var`。Task 4/5/6 依赖这些名字。

- [ ] **Step 1: 重构主界面为两组控件 + `_apply_form`**

`__init__` 中（1252-1291 行区域）：
- 新建 `single_frm`（沿用 `ip_entry`/`port_entry` 两格，标签「上游地址/上游端口」）与 `multi_frm`（`add_entry` 单行框 width=24 + 「添加」「测试」按钮 + 池表 Treeview 5 列 + 侧边「移除」按钮沿用现有选中行交互）。
- 池表 `show="tree headings"` 不变，`height=4`，`addr` 列宽 150、其余按标题宽。
- `up_status_label`（1288-1291 行）删除；单形态状态框加 `entry_speed_var` 一行 Label（无 wraplength）。
- `_apply_form`：multi → `multi_frm.grid()`、`single_frm.grid_remove()`、`entry_speed` 行隐藏；single 反之。`on_preset_selected`（1349 行）与 `__init__` 末尾调用。

- [ ] **Step 2: `add_upstream` 改单行解析**

用 `parse_upstream_line(self.add_var.get())`；None 则 `messagebox.showwarning("提示", "格式：IP:端口（如 192.130.30.73:7890）")`；成功后清空 `add_var`，其余逻辑（去重、`_test_gen+=1`、`_sync_pools`、`save_preset`）沿用。

- [ ] **Step 3: `_entry_status_text` 改写池表行**

保留 1590-1591 的「端口已改待重开」guard 与 2 秒差分采样；每台上游的差分结果不再拼 var，而是 `_set_pool_row(str(i), (f"{ip}:{port}", ↓, ↑, str(c), test列保留原值))`——注意从 `self.pool_box.set(iid, "down", val)` 按列更新，别覆掉「测试」列。单形态时把池[0]（`self._pool_addrs()[0]` 在 stats 中的条目）写 `entry_speed_var` 为 `↓ x ↑ y · N连`（单行，无换行）。

- [ ] **Step 4: 跑全量测试**

Run: `python -m unittest discover -s V2.0/tests -v`
Expected: 全 PASS

- [ ] **Step 5: 手测**

`python V2.0/proxy_tool.py`（或 pythonw）核对：选「默认」预设出现池表+单行添加框；添加 `192.130.30.99:7890` 入池、重复添加不加、移除剩 1 台被拦；开启代理后池表 ↓/↑/连接 2 秒刷新，**窗口高度全程不变**；地址栏无 wraplength 换行。

- [ ] **Step 6: Commit**

```bash
git add V2.0/proxy_tool.py
git commit -m "feat: 多代理形态——池表 5 列兼作状态表，单行 IP:端口 添加，速率不再折行撑窗"
```

---

### Task 4: 单代理形态——直接编辑 + 状态框

**Files:**
- Modify: `V2.0/proxy_tool.py:1616-1640`（`_on_addr_changed` 附近）、`on_preset_selected`（1349 行）
- Test: 全量回归 + 手测

**Interfaces:**
- Consumes: Task 3 `_apply_form`、`entry_speed_var`。
- Produces: `ProxyApp._commit_single_edit(self)`（单形态地址生效入口）。

- [ ] **Step 1: 实现直接编辑语义**

单形态下 `ip_entry`/`port_entry` 即当前预设上游的值：`on_preset_selected`/`_apply_form` 时 `ip_var.set(ip)`、`port_var.set(port)`（多形态时保持清空）。绑定 `<Return>` 与 `<FocusOut>` → `_commit_single_edit`：两格均合法（`_valid_upstream`）则 `cur["upstreams"]=[[ip, port]]`、`_test_gen+=1`、清 `test_result_var`、`_sync_pools()`、`save_preset()`；不合法则什么都不做（红框已在 `_on_addr_changed`）。注意：`FocusOut` 触发时若值未变化则跳过落盘（对比 `cur["upstreams"][0]`）。

- [ ] **Step 2: 跑全量测试**

Run: `python -m unittest discover -s V2.0/tests -v`
Expected: 全 PASS

- [ ] **Step 3: 手测**

切到「91」预设（单形态）：两格带出 `192.130.30.91/7890`；改成非法值回车→红框、配置文件未变；改成合法值回车→json 落盘、入口运行中热切生效；状态框速率单行显示。

- [ ] **Step 4: Commit**

```bash
git add V2.0/proxy_tool.py
git commit -m "feat: 单代理形态——地址端口直接编辑即存，回到 V1 式简洁界面"
```

---

### Task 5: 测试结果进池表 / 状态框

**Files:**
- Modify: `V2.0/proxy_tool.py:1711-1750`（`test_connectivity`/`_test_worker`/`_show_test_result`）
- Test: 全量回归 + 手测

**Interfaces:**
- Consumes: Task 2 `_short_conn_error`/`_friendly_conn_error`；Task 3 池表列 `test`、`_apply_form`。
- Produces: `_show_test_result` 内部按形态分流渲染（表列 or `test_result_var`），签名不变；`_test_worker` 失败时回传 `reason` 原始对象（成功回传毫秒 int），渲染层按形态决定短词/完整文案。

- [ ] **Step 1: 重构测试结果展示**

`_test_worker`：成功 `info = round((t)*1000)`（int）；失败保留 `reason` 对象。`_show_test_result`：按当前形态——multi：找到 `name` 对应池表 iid（遍历 `_pool_addrs()`），`pool_box.set(iid, "test", f"✓ {ms}ms"` 或 `f"✗ {短词}")`，颜色不做（表内纯文本）；single：`test_result_var.set(f"✓ {ms}ms")` 或 `f"✗ {短词}（{_friendly_conn_error(reason)}）"`。`_test_gen` 作废机制、按钮「测试中…」恢复、收齐 total 条逻辑全部沿用。

- [ ] **Step 2: 跑全量测试**

Run: `python -m unittest discover -s V2.0/tests -v`
Expected: 全 PASS

- [ ] **Step 3: 手测**

多形态点「测试」→ 每行 test 列出 `✓ xxxms`/`✗ 短词`；测试中改地址→旧结果作废、按钮恢复；单形态点「测试」→ 状态区一行 `✓ 210ms` 或 `✗ 超时（3 秒无响应（超时））`；窗口高度全程不动。

- [ ] **Step 4: Commit**

```bash
git add V2.0/proxy_tool.py
git commit -m "feat: 测试结果按形态展示——多代理写池表行内，单代理状态框短词+完整原因"
```

---

### Task 6: 新增预设弹窗（选类型）+ 缓冲语义

**Files:**
- Modify: `V2.0/proxy_tool.py:1461-1499`（`on_add_or_save`/`_enter_add_mode`/`_exit_add_mode`/`_cancel_add_mode`）
- Test: 全量回归 + 手测

**Interfaces:**
- Consumes: Task 3 `_apply_form`、`add_var`、池表；Task 4 `_commit_single_edit`。
- Produces: `ProxyApp._open_new_preset_dialog(self)`；`ProxyApp._enter_add_mode(self, ptype: str, name: str)`；`self._draft: dict | None`（`{"name","type","upstreams"}`，非 None 即处于新增模式）。

- [ ] **Step 1: 弹窗与 draft 状态机**

「新增」→ `_open_new_preset_dialog`：Toplevel（`transient(root)`+`grab_set`），Radiobutton 单代理（默认）/多代理 + 名称 Entry + 确定/取消。确定 → `_enter_add_mode(ptype, name)`：`self._draft = {"name": name, "type": ptype, "upstreams": []}`；主界面按 draft 类型进入空形态（multi：池表清空显示 draft；single：两格空白）；`save_preset_btn` 文案「存预设」、`delete_preset_btn`「取消」、开关/共享禁用（沿用现有 1478 行清单，加测试按钮不禁——draft 里测试池为空会提示，无妨）。

- [ ] **Step 2: draft 期间的读写分离**

多形态 draft：`add_upstream`/`remove_upstream` 操作 `self._draft["upstreams"]` 且**不调 `save_preset`/`save_config`**，只刷池表；移除允许删到 0（保存时拦）。单形态 draft：两格编辑只改 var。`on_add_or_save` 的「存预设」：校验（single：两格合法；multi：draft ≥1 台，否则 `messagebox.showwarning("提示", "至少添加一台上游")`）→ `self.presets.append(draft)` + `preset_var.set(name)` + `save_preset()` + `_apply_form()` + `_sync_pools()` + `_exit_add_mode(restore=False)`。`_exit_add_mode`：`self._draft=None`、恢复按钮文案/状态、restore=True 时还原进入前预设名与形态。Esc/切预设路径沿用现有绑定（1207、1350-1351）改调新逻辑。

- [ ] **Step 3: 跑全量测试**

Run: `python -m unittest discover -s V2.0/tests -v`
Expected: 全 PASS

- [ ] **Step 4: 手测**

新增「多代理」预设：弹窗选类型→逐行加 2 台（**期间检查配置 json：未新增预设、无半成品**）→存预设→json 出现带 `"type": "multi"` 的新预设；Esc 中途丢弃→json 无痕、界面回原预设；新增「单代理」：填地址→存→json 带 `"type": "single"`。

- [ ] **Step 5: Commit**

```bash
git add V2.0/proxy_tool.py
git commit -m "feat: 新增预设弹窗选单/多类型——draft 缓冲，存才落盘，Esc 全弃"
```

---

### Task 7: 收尾——验收、手测清单、spec 标注

**Files:**
- Modify: `docs/superpowers/specs/2026-10-25-single-multi-ui-split-design.md`（状态行改「已实现」）
- Create: 无

- [ ] **Step 1: 全量测试**

Run: `python -m unittest discover -s V2.0/tests -v`
Expected: 全 PASS

- [ ] **Step 2: 交付手测清单给用户**

按 spec 手测一遍：① 删 `%APPDATA%\ProxyToolV2\proxy_config.json` 用旧 V1 配置首启→三预设类型推断正确；② 单形态全流程（编辑/红框/落盘/测试/开关代理）；③ 多形态全流程（添加/去重/移除/速率刷新/**高度稳定**/测试行内结果）；④ 形态随预设切换；⑤ 新增弹窗两条路径 + Esc；⑥ 共享/入口/托盘/自启不回归。

- [ ] **Step 3: spec 标注 + 提交**

```bash
git add docs/superpowers/specs/2026-10-25-single-multi-ui-split-design.md
git commit -m "docs: 单/多代理分离 spec 标注已实现"
```
