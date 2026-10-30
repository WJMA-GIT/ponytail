# ponytail for Octop

**一个** Octop 插件（`kind: tool`）承载 **两件事**，封装的是 **upstream ponytail v5.3.3**（commit `568510c`）：

| 能力 | 说明 |
|---|---|
| 6 个工具 | 规则 / 档位 / 技能 / 命令 / 技术债扫描 / 自检 |
| 常驻注入 | `PonytailMiddleware` 每轮把规则追加到系统提示（等价上游 `pre_llm_call`） |

### 为什么一个 `kind: tool` 插件能同时注册 middleware

`PluginContext` 的两个方法是**硬校验**，不是分类：`tool()` 在 kind≠tool 时抛
`ValueError`（`octop_harness/plugins/context.py:47-48`），`middleware()` 在 kind≠hook
时抛（`context.py:63-64`）。但**下游完全不按 kind 过滤**：`to_loaded()` 原样复制
`_tools` / `_middleware`（`context.py:152-160`），`all_tools()`（`registry.py:126-130`）
与 `build_middleware_chain()`（`registry.py:146-154`）只读这两个列表；只有
`all_channels()` 会看 kind（`registry.py:137`）。

所以插件先按文档 API 调 `ctx.middleware(...)`（未来 Octop 放开就直接用），
被 `ValueError` 拒绝时，把同样形状的注册对象追加到 `ctx._middleware`（`register_middleware()`）。
这条兼容路径**当场校验、写日志、并由 `ponytail_doctor` 的 `auto_injection` 字段暴露**，
不会静默失效（值：`documented` / `compatibility` / `unavailable`）。

实测（本机 Octop）：p1 = `kind: tool` 里调 `ctx.middleware()` →
`ValueError: plugin 'probe-p1' is kind='tool', not hook`（context.py:64）；
合并后的插件 `plugged` 后 `PluginRegistry` 里是 `('ponytail', 'tool', 6, 1)` —— 6 个工具 + 1 个 middleware，
`build_middleware_chain()` 里能看到 `PonytailMiddleware`。

## 为什么是这个形状

ponytail 不是一个 CLI、不是库、也不是服务——它是「提示词 + 技能包」。它唯一可编程的
入口是给 Hermes Agent 写的 `__init__.py`（纯标准库）。所以适配层：

* **不调用任何子进程**（上游没有可执行入口）；
* **不修改上游任何文件**：每个插件目录里带一份 `_vendor/ponytail/`，是上游文件的
  **逐字节副本**（`CHECKSUMS.sha256` 校验），由 `tools/sync_upstream.py` 从本仓库根目录同步；
* **不改行为**：`ponytail_rules` 的返回值与上游 `build_injected_context` 逐字一致，
  技能正文与上游 `_filter_skill_body_for_mode` 逐字一致，档位优先级与上游
  （env > `config.json:defaultMode` > `full`）一致。

只有一个插件目录，所以 `_vendor/ponytail/` 在磁盘上也只有一份（装好后约 128 KB；
拆成两个包时是 84 KB × 2 + 代码 ≈ 236 KB）。

## 安装

```bash
octop plugin install ./octop/plugin --force
octop plugin list          # 应看到 ponytail (tool)
```

装一个就够：工具与常驻注入都在这个包里。

装完如果 `octop run` 正在跑，执行 `POST /api/plugins/reload` 或重启服务。

## 6 个工具

| 工具 | 用途 | 典型调用 |
|---|---|---|
| `ponytail_rules` | 取规则全文（与上游逐字一致） | `ponytail_rules(level="ultra")` |
| `ponytail_mode` | 查看 / 切换档位（写入上游兼容的 `config.json`） | `ponytail_mode(action="set", level="lite")` |
| `ponytail_skill` | 取技能正文（按档位过滤） | `ponytail_skill(name="ponytail-review", include_prompt=True)` |
| `ponytail_command` | 取斜杠命令定义（已替换 `{{args}}`） | `ponytail_command(name="ponytail-audit")` |
| `ponytail_shortcut_debt` | 扫描仓库里的 `shortcut:` 注释 | `ponytail_shortcut_debt(root=".", limit=20)` |
| `ponytail_doctor` | 自检：vendor 校验和 / 技能数 / 配置路径 / 档位来源 | `ponytail_doctor()` |

错误不会抛异常，而是返回结构化文本：

```
status: error
kind: invalid_argument
message: unknown level: 'extreme'
valid: off, lite, full, ultra
```

`kind` 取值：`dependency_missing`（vendor 缺失/损坏）、`invalid_argument`（档位/技能/命令名）、
`config_unwritable`（写不了配置）、`invalid_path`（扫描目录不存在）、`upstream_error`。

## 常驻注入

优先级 90（在 `BootstrapMiddleware` 的 100 之前），`wrap_model_call` /
`awrap_model_call` 把规则追加到 **本次请求** 的 system message，不写回 checkpoint。

与上游 `_pre_llm_call` 等价的行为：

| 会话状态 | 行为 |
|---|---|
| 历史里没有 PONYTAIL 标记，模式 ≠ off | 注入该档位规则 |
| 历史里没有标记，模式 = off | 不注入（没有要撤销的东西） |
| 历史里已有当前档位的标记 | 不注入（避免同一份规则重复） |
| 历史里是**别的**档位 | 注入；若当前是 off 则注入 `PONYTAIL MODE OFF` 撤销 |

档位每次请求重新解析，所以 `ponytail_mode(action="set")` 改完立即生效。

## 档位与配置

优先级与上游一致：`PONYTAIL_DEFAULT_MODE` 环境变量 > `config.json:defaultMode` > 内置 `full`。
配置目录同样是上游的 `<XDG_CONFIG_HOME|~/.config>/ponytail`（Windows `%APPDATA%\ponytail`）。

## 同步上游

```bash
python3 octop/tools/sync_upstream.py           # 刷新 _vendor（从本仓库根目录拷贝）
python3 octop/tools/sync_upstream.py --check   # 只校验，漂移则退出码 1
python3 octop/tools/make_fixtures.py           # 用未修改的上游重生成行为基线
```

上游版本升级流程：合并上游 → `sync_upstream.py` → `make_fixtures.py` → 跑测试 →
更新 `plugin.yaml` 的版本号与 `ponytail_runtime.py` 里的 `PINNED_COMMIT`。

## 测试

```bash
python3 -m unittest discover -s octop/tests -v                       # 纯 stdlib
/app/.venv/bin/python -m unittest discover -s octop/tests -v         # 含插件契约（共 35 项）
/app/.venv/bin/python octop/tools/verify_install.py                  # 装完后的真调用
```

## 打包

```bash
python3 octop/tools/build_zips.py --out /tmp/octop-dist
```

ZIP 内是唯一一个带 `plugin.yaml` 的根目录（`ponytail-octop/`）。

## 许可

适配层代码随本 fork 发布；其中 `_vendor/ponytail/**` 是上游 MIT 代码的逐字节副本，
`LICENSE` 已一并附带。分发时保留上游版权声明（见根目录 `LICENSE` 与 `FORK.md`）。
