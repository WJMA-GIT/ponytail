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

## 版本策略：插件版本 = 上游版本

`plugin.yaml` 的 `version` **就是上游 ponytail 的版本**（当前 `5.3.3`），不加任何后缀。
这个值不是手写的：`octop/tools/sync_upstream.py` 从**上游自己的根 `plugin.yaml`** 读取，
并同时重写三处：

| 落点 | 内容 |
|---|---|
| `octop/plugin/plugin.yaml` → `version` | 上游版本 |
| `octop/plugin/ponytail_runtime.py` → `UPSTREAM_VERSION` / `PINNED_COMMIT` / `CONTENT_COMMIT` | 版本 + 同步点 + vendored 字节的最后一次上游提交 |
| `octop/plugin/_vendor/ponytail/UPSTREAM_VERSION` | 同上，供运行时自检 |

上游升到 5.3.4 时，合并上游 → 跑一次 sync，版本号自动变成 `5.3.4`；
`sync_upstream.py --check` 会把这些一致性检查一起做（版本、vendored 字节、provenance），
**没有需要手工维护的常量**。

### 适配层自己修了 bug、上游还没升版怎么办

版本号必须与上游一致，所以**不能**用 `5.3.3.x` 这种自造后缀。区分办法：

| 载体 | 做法 |
|---|---|
| Release tag | `octop-5.3.3`（版本部分固定）；适配层重发时用 `octop-5.3.3+build.2` —— `+` 是 SemVer 构建元数据，版本号本身仍是 `5.3.3` |
| ZIP 文件名 | 恒为 `ponytail-octop-5.3.3.zip`（跟 `plugin.yaml` 一致） |
| 包内 `BUILD` 文件 | 打包时写入 `<适配提交>-<UTC 时间戳>`，不进 git；`ponytail_doctor` 会显示 `adapter_build:` |
| Release notes | 写明适配提交与改动 |

代价与风险（说清楚）：

- Octop 里看到的插件版本永远是 `5.3.3`，**看不出是两个不同构建** —— 所以 `ponytail_doctor`
  的 `adapter_build` 行是唯一可靠的运行期区分手段，报障时必须让用户带上它。
- 同名 ZIP（不同 tag）意味着下载地址不同但文件名一样，运维脚本若按文件名缓存会拿错版本 ——
  脚本请按 tag URL 取，不要缓存本地同名文件。
- 选择「覆盖同名 Release」会更糟：旧构建不可追溯，手里装着旧包的人无法自证版本。
  所以这里明确采用「新 tag + 同名 ZIP + BUILD 标记」。

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
# 从本地 checkout
octop plugin install ./octop/plugin --force
# 或从 Release（版本号与上游一致）
octop plugin install https://github.com/WJMA-GIT/ponytail/releases/download/octop-5.3.3/ponytail-octop-5.3.3.zip --force

octop plugin list          # 应看到 ponytail (tool) v5.3.3
```

装一个就够：工具与常驻注入都在这个包里。

装完如果 `octop run` 正在跑，执行 `POST /api/plugins/reload` 或重启服务。

## 技能清单随注入下发（不写工作区）

官方把 6 个 `SKILL.md` 列进技能清单，模型一轮就看见名字与描述。Octop 插件不能改
工作区，也不该把技能复制给每个 agent，所以这里**走同一条注入通道**：规则后面追加
一段技能清单，模型不必先想到"有个工具"才去调。

清单长这样（描述取自各 `SKILL.md` frontmatter 的**第一句**，逐字，不会与上游漂移）：

```
PONYTAIL SKILLS（正文用 ponytail_skill(name=...) 取）
- ponytail — Lazy senior dev mode: the smallest change that fully solves the task, …｜写代码或评审前的总则与档位
- ponytail-audit — Quality audit of a whole repo: …｜整仓安全与质量审计
- ponytail-debt — List every `shortcut:` comment …｜列 shortcut: 技术债台账
- ponytail-gain — Show ponytail's measured savings …｜看基准省下的数字
- ponytail-help — Quick reference for ponytail levels, skills and commands.｜查档位/技能/命令速查
- ponytail-review — Quality review of a change: …｜评审 diff / PR / 上一个提交
命令形式用 ponytail_command(name=..., command_args=...)，拿到的是已渲染 {{args}} 的 prompt（只有 ponytail 模板带占位符）。
```

**成本**（`tiktoken` cl100k_base 实测）：清单 **977 字符 / 327 token**；注入总量从
2859 字符（639 token）涨到 **3838 字符 / 967 token**。也就是每次模型调用多 327 token
（+51%）。名字 + 极短描述的极限压法是 ~60 token，但那就退化成"知道有这回事"，
失去"知道该用哪个"——不值。

**off 档**：规则不注入时，清单也一起不注入，只留 `PONYTAIL MODE OFF` 撤销标记。
理由：档位关了还塞技能清单，等于用上下文把用户按回 ponytail 流程，与 OFF 冲突；
工具仍在，用户显式要求时照样能调。`ponytail_doctor` 的 `skills_injected` 会照实显示
`no (mode=off)`。

**已废弃**：早期版本用 `install_skills.py` 把 6 个 `SKILL.md` 复制到
`<workspace>/skills/`（目的是拿到 `/ponytail-review` 这种点名触发）。该做法会污染
工作区、卸载易残留，已移除脚本与相关测试。代价是不再支持 `/ponytail-review` 点名——
需要时直接说"用 ponytail-review 审这个 diff"，或调 `ponytail_command`。

## 6 个工具

| 工具 | 用途 | 典型调用 |
|---|---|---|
| `ponytail_rules` | 取规则全文（与上游逐字一致） | `ponytail_rules(level="ultra")` |
| `ponytail_mode` | 查看 / 切换档位（写入上游兼容的 `config.json`） | `ponytail_mode(action="set", level="lite")` |
| `ponytail_skill` | 取技能正文（按档位过滤） | `ponytail_skill(name="ponytail-review", include_prompt=True)` |
| `ponytail_command` | 取斜杠命令定义（已替换 `{{args}}`） | `ponytail_command(name="ponytail", command_args="ultra")` |
| `ponytail_shortcut_debt` | 扫描仓库里的 `shortcut:` 注释 | `ponytail_shortcut_debt(root=".", limit=20)` |
| `ponytail_doctor` | 自检：vendor 校验和 / 技能数 / 配置路径 / 档位来源 | `ponytail_doctor()` |

参数说明走 `Annotated[type, "..."]`：平台的 `StructuredTool.from_function` 用
`parse_docstring=False`，docstring 里的 `Args:` **不会**进 schema，只有 `Annotated`
的说明会被模型看到。`octop/tests/test_plugin_contract.py` 会断言 10 个参数每个都有
schema description，漏一个就红。

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

**幂等判定只看 `system_message`**（注入就写在那里），不扫对话历史。这条是 5.3.3
里修掉的 bug，见下面的 changelog：

| 会话状态 | 行为 |
|---|---|
| `system_message` 里没有标记，模式 ≠ off | 注入该档位规则 + 技能清单 |
| `system_message` 里已有当前档位的标记 | 不注入（避免同一份规则重复追加） |
| `system_message` 里是**别的**档位 | 注入（覆盖旧档位，不累积） |
| 模式 = off，`system_message` 干净且历史无标记 | 不注入 |
| 模式 = off，`system_message` 或历史里有标记 | 注入 `PONYTAIL MODE OFF` |

最后一行是刻意的**不对称**：注入要严格（历史里提到过标记不算已注入，否则规则会
静默消失），撤销要宽松（多发一行 OFF 只是几十 token，漏发会让模型继续按旧规则行事）。
撤销判定只做 `is None` 判断、不与标记值比较，所以"历史里已经有过 OFF"不会挡住这次撤销。

档位每次请求重新解析，所以 `ponytail_mode(action="set")` 改完立即生效。

## changelog（适配层）

### 5.3.3 · 注入被历史消息里的标记"毒化"

**症状**：某些线程里规则注入静默消失（注入长度 0），新开的线程却正常（3838 字符）。
同一份插件、同一个档位，差别只在"这个线程的对话历史里有没有出现过
`PONYTAIL MODE ACTIVE — level: full`"。

**真因**：`augment()` 的幂等判定扫的是**全部历史消息**：

```python
seen = injected_mode(request.messages)   # ← 错：历史正文也算
if seen == mode: return request          # 于是每轮都跳过注入
```

`injected_mode()` 的正则匹配任意消息正文。于是**只要线程里有人贴过一次这个标记**
——工具自检输出、排障时贴的验证结果、分析文字里的引用，甚至任务书本身——此后该
线程每轮都认为"已注入过"，规则与技能清单一起消失。团队线程实测 10 处命中。

**修复**：只拿 `system_message` 判定（注入的落点），另加 off 档的放宽分支（见上表）：

```python
system_message = getattr(request, "system_message", None)
seen = injected_mode([system_message] if system_message is not None else [])
if seen == mode: return request
if mode == "off":
    if (seen or injected_mode(request.messages)) is None: return request
```

**回归测试**：`test_marker_quoted_in_history_does_not_block_injection`（历史含标记 →
仍然注入）、`test_skips_when_same_mode_already_present`（system 已含 → 不重复）、
`test_off_with_stale_marker_in_history_still_cancels`（off + 历史残留 → 仍撤销）、
`test_off_with_marker_in_system_message_still_cancels`。

## 档位与配置

优先级与上游一致：`PONYTAIL_DEFAULT_MODE` 环境变量 > `config.json:defaultMode` > 内置 `full`。
配置目录同样是上游的 `<XDG_CONFIG_HOME|~/.config>/ponytail`（Windows `%APPDATA%\ponytail`）。

## 同步上游

```bash
python3 octop/tools/sync_upstream.py           # 刷新 _vendor（从本仓库根目录拷贝）
python3 octop/tools/sync_upstream.py --check   # 只校验，漂移则退出码 1
python3 octop/tools/make_fixtures.py           # 用未修改的上游重生成行为基线
```

上游版本升级流程（**不需要手工改任何版本号**）：

```bash
git merge upstream/main
python3 octop/tools/sync_upstream.py     # 自动写入上游版本 + 两类 commit
python3 octop/tools/make_fixtures.py     # 重生成行为基线
python3 -m unittest discover -s octop/tests
```

`sync_upstream.py --check` 会在版本、vendored 字节或 provenance 落后时报错并给出修复命令。

## 测试

```bash
python3 -m unittest discover -s octop/tests -v                       # 纯 stdlib
/app/.venv/bin/python -m unittest discover -s octop/tests -v         # 含插件契约（共 35 项）
/app/.venv/bin/python octop/tools/verify_install.py                  # 装完后的真调用
```

## 打包

```bash
python3 octop/tools/build_zips.py --out /tmp/octop-dist
# 适配层重发时给个可追溯的构建标记（写进包内 BUILD 文件）
python3 octop/tools/build_zips.py --out /tmp/octop-dist --build-label adapter-<sha7>
```

ZIP 内是唯一一个带 `plugin.yaml` 的根目录（`ponytail-octop/`）。

## 许可

适配层代码随本 fork 发布；其中 `_vendor/ponytail/**` 是上游 MIT 代码的逐字节副本，
`LICENSE` 已一并附带。分发时保留上游版权声明（见根目录 `LICENSE` 与 `FORK.md`）。
