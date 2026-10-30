# ponytail for Octop

Two Octop plugins wrapping **upstream ponytail v5.3.3** (commit `568510c`):

| 目录 | Octop kind | 作用 |
|---|---|---|
| `tool/` | `tool` | 把 ponytail 的规则 / 技能 / 命令 / 技术债扫描暴露成 6 个可调用工具 |
| `hook/` | `hook` | 每轮把规则注入系统提示（等价上游 `pre_llm_call`），**ponytail 的原生语义** |

## 为什么是这个形状

ponytail 不是一个 CLI、不是库、也不是服务——它是「提示词 + 技能包」。它唯一可编程的
入口是给 Hermes Agent 写的 `__init__.py`（纯标准库）。所以适配层：

* **不调用任何子进程**（上游没有可执行入口）；
* **不修改上游任何文件**：每个插件目录里带一份 `_vendor/ponytail/`，是上游文件的
  **逐字节副本**（`CHECKSUMS.sha256` 校验），由 `tools/sync_upstream.py` 从本仓库根目录同步；
* **不改行为**：`ponytail_rules` 的返回值与上游 `build_injected_context` 逐字一致，
  技能正文与上游 `_filter_skill_body_for_mode` 逐字一致，档位优先级与上游
  （env > `config.json:defaultMode` > `full`）一致。

`tool/` 与 `hook/` 各自独立安装，所以 `ponytail_runtime.py` 与 `_vendor/` 各存一份；
`tests/test_runtime.py::test_both_plugins_ship_identical_runtime_and_vendor` 保证两份一致。

## 安装

```bash
# 工具版
octop plugin install ./octop/tool --force
# 常驻注入版（建议同时装）
octop plugin install ./octop/hook --force

octop plugin list          # 应看到 ponytail (tool) / ponytail-hook (hook)
```

装完如果 `octop run` 正在跑，执行 `POST /api/plugins/reload` 或重启服务。

## 工具版：6 个工具

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

## Hook 版：常驻注入

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
python3 -m unittest discover -s octop/tests -v                       # 纯 stdlib：24 项
/app/.venv/bin/python -m unittest discover -s octop/tests -v         # 含插件契约：33 项
/app/.venv/bin/python octop/tools/verify_install.py                  # 装完后的真调用
```

## 打包

```bash
python3 octop/tools/build_zips.py --out /tmp/octop-dist
```

ZIP 内是唯一一个带 `plugin.yaml` 的根目录（`ponytail-octop-tool/`、`ponytail-octop-hook/`）。

## 许可

适配层代码随本 fork 发布；其中 `_vendor/ponytail/**` 是上游 MIT 代码的逐字节副本，
`LICENSE` 已一并附带。分发时保留上游版权声明（见根目录 `LICENSE` 与 `FORK.md`）。
