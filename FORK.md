# Fork 说明（WJMA-GIT/ponytail）

- **上游**：<https://github.com/DietrichGebert/ponytail>（默认分支 `main`）
- **本 fork**：<https://github.com/WJMA-GIT/ponytail>（默认分支 `main`）
- **同步方式**：`.github/workflows/sync-upstream.yml` 每日合并上游 `main`（git merge，非 force push）

## 许可证与合规

| 项 | 内容 |
|---|---|
| 上游许可证 | **MIT License**（见根目录 `LICENSE`，版权行 `Copyright (c) 2026 DietrichGebert`） |
| fork 是否合规 | 合规。MIT 明确允许复制、修改、再分发，包括商业使用 |
| 二次分发条件 | 必须**保留版权声明与许可声明**（MIT 原文要求 "The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software"） |
| 我们这边的义务 | ① 不改写、不删除 `LICENSE` 里的版权行；② 分发任何衍生作品（含打包/镜像/二进制）时随附 MIT 许可全文；③ 本 fork 无需开源我们的改动（MIT 非 copyleft），但已公开的 fork 天然公开 |
| 免责 | MIT 附带 "AS IS" 免责条款，上游不提供担保 |

> **不要**把 `LICENSE` 替换成别的许可证，也不要删掉版权行——这是 MIT 下唯一必须做的事。

## 路径约定（务必遵守）

| 类别 | 路径 | 谁维护 |
|---|---|---|
| 上游文件 | 除 `octop/` 外的一切（`commands/`、`hooks/`、`docs/`、`.xxx-plugin/`、`README.md`、`LICENSE` …） | 上游；我们**只读** |
| 我们的适配层 | `octop/` | 我们；上游没有这个目录 |

适配代码**只放 `octop/`**，不要塞进上游文件。同步工作流对此有自动守卫（见 `octop/README.md`）。

本文件（`FORK.md`）与 `octop/` 一样属于我们的层，上游不会创建同名文件，因此不会冲突。

## 分支策略

| 分支 | 角色 |
|---|---|
| `main` | 唯一长期分支。= 上游 `main` + 我们的 `octop/` 层 |
| `sync/upstream-<时间戳>` | 仅在 `main` 推送被分支保护拒绝时由工作流临时创建，用于开 PR |
| 其他 | 一次性修复可开短分支 + PR 回 `main`，合完即删 |

- **不要**把本地工作直接 rebase 到上游再 force push `main`：会重写别人已拉取的历史，也会打断工作流的合并基线。
- 建议在 `main` 上开分支保护：**禁止 force push、禁止删除分支**；不要强制要求 PR 审核
  （会让每日自动同步改走"开 PR"路径，需要人工点合）。
