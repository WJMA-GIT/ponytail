# `octop/` —— 适配层（属于本 fork，不属于上游）

本目录是本仓库**唯一**允许放"我们自己代码"的地方。上游 `DietrichGebert/ponytail` 没有这个目录，
所以上游 → 本 fork 的自动同步永远不会碰它。

## 放什么

| 子目录 | 用途 |
|---|---|
| `octop/ci/` | 本仓库自用的 CI 辅助脚本（目前只有 `sync-upstream.sh`，供 `.github/workflows/sync-upstream.yml` 调用） |
| 后续适配代码 | 由开发同学按需新增，例如把 ponytail 接进 Octop 专家体系的胶水层 |

## 不放什么（重要）

- ❌ **不要改上游已有的文件**（`README.md`、`commands/`、`hooks/`、`docs/`、各 `.xxx-plugin/` 目录里的文件……）。
  一旦改了，上游下次同步就会冲突，而且冲突点会随上游演进来回漂移。
- ❌ 不要在上游文件里"顺手加两行我们的逻辑"。要包一层就新增文件，放在 `octop/`。
- ❌ 不要把 `octop/` 改名为上游也已经存在的目录名（如 `docs/`、`hooks/`）。

## 为什么这么定

同步工作流用的是 **git merge**（不是 `reset --hard`、不是 force push），它会保留双方历史。
merge 的唯一冲突来源就是"同一文件的同一段被两边都改过"。只要我们的改动**只落在上游没有的路径**上，
自动合并就永远是快进式的、零冲突。

工作流里有两道自动守卫，出错会直接中止而不是硬合：

1. **合并前**：如果上游新增了 `octop/` 下的文件（路径重叠），立即中止（退出码 2），不做任何合并；
2. **合并后**：如果本次 merge 的改动里出现了 `octop/` 下的路径，回滚到合并前并中止（退出码 2）。

## 冲突了怎么办

工作流会：保留 `main` 原样（不做任何推送）→ 把冲突文件清单写进 Run Summary → 开一个
`[sync-upstream] 上游合并冲突` 的 Issue（同一问题未关闭时改成追加评论）→ 以失败状态结束。

人工处理：

```bash
git fetch https://github.com/DietrichGebert/ponytail.git main:upstream-main
git checkout main
git merge --no-ff upstream-main
# 冲突里属于上游逻辑的以 upstream 为准；属于我们适配的保留我们的，并把逻辑搬进 octop/
git add <file> && git commit && git push origin main
```
