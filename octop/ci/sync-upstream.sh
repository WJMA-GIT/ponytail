#!/usr/bin/env bash
# sync-upstream.sh —— 把上游改动合并进本 fork 的 main（merge，绝不 reset/force-push）
#
# 由 .github/workflows/sync-upstream.yml 调用；也可以本地跑（用 UPSTREAM_URL 指向本地裸库做演练）。
#
# 环境变量：
#   UPSTREAM_REPO     owner/name，默认 DietrichGebert/ponytail
#   UPSTREAM_URL      可选，覆盖 clone URL（本地演练用，例如 /tmp/x/upstream.git）
#   UPSTREAM_REF      上游分支，默认 main
#   TARGET_BRANCH     本 fork 目标分支，默认 main
#   ADAPT_DIR         我们的适配层目录，默认 octop（上游不许出现同名目录）
#   DRY_RUN           "1" = 只报告是否有更新，不合并
#   GITHUB_STEP_SUMMARY  有则写摘要
#   GH_TOKEN / GITHUB_REPOSITORY  有则用于开 Issue（冲突时）
#
# 退出码：0 = 已最新 / 合并并推送成功 / DRY_RUN；
#         1 = 合并冲突（已 abort，已开 Issue 或打印清单）；
#         2 = 保护性中止（路径重叠、适配层被改动、推送失败且 PR 兜底也失败）

set -uo pipefail

UPSTREAM_REPO="${UPSTREAM_REPO:-DietrichGebert/ponytail}"
UPSTREAM_REF="${UPSTREAM_REF:-main}"
TARGET_BRANCH="${TARGET_BRANCH:-main}"
ADAPT_DIR="${ADAPT_DIR:-octop}"
DRY_RUN="${DRY_RUN:-0}"
UPSTREAM_URL="${UPSTREAM_URL:-https://github.com/${UPSTREAM_REPO}.git}"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/null}"

say() { printf '%s\n' "$*"; printf '%s\n' "$*" >>"$SUMMARY"; }
fatal() { say "## ❌ $*"; exit "${2:-2}"; }

cd "$(git rev-parse --show-toplevel)" || exit 2

say "## 上游同步：\`${UPSTREAM_REPO}@${UPSTREAM_REF}\` → \`${TARGET_BRANCH}\`"
say ""

# ── 1. 取上游 ────────────────────────────────────────────────────────────────
git remote remove upstream >/dev/null 2>&1 || true
git remote add upstream "$UPSTREAM_URL"
if ! git fetch --no-tags --prune upstream "$UPSTREAM_REF" 2>&1 | tail -3; then
  fatal "拉取上游失败（网络或上游分支不存在）：$UPSTREAM_URL $UPSTREAM_REF"
fi

UPSTREAM_SHA="$(git rev-parse --short "upstream/${UPSTREAM_REF}")"
HEAD_SHA="$(git rev-parse --short HEAD)"
BEHIND="$(git rev-list --count "HEAD..upstream/${UPSTREAM_REF}")"
AHEAD="$(git rev-list --count "upstream/${UPSTREAM_REF}..HEAD")"

say "| 项 | 值 |"
say "|---|---|"
say "| 本 fork HEAD | \`${HEAD_SHA}\` |"
say "| 上游 HEAD | \`${UPSTREAM_SHA}\` |"
say "| 落后（上游有我们没有的提交） | ${BEHIND} |"
say "| 领先（我们自己的提交，含适配层） | ${AHEAD} |"
say ""

# ── 2. 路径重叠守卫：上游自己有没有 ADAPT_DIR ─────────────────────────────────
# 约定：上游文件 = 上游拥有并持续改动的文件；我们的改动只放在 ADAPT_DIR 下。
# 判据必须是"上游那棵树里有没有该目录"，**不能**用 `git diff HEAD upstream/main`
# ——那会把"我们有、上游没有"的适配文件也列进来，造成必然误报。
OVERLAP="$(git ls-tree -r --name-only "upstream/${UPSTREAM_REF}" | grep -E "^${ADAPT_DIR}/" || true)"
if [ -n "$OVERLAP" ]; then
  say "### ⚠️ 检测到路径重叠：上游出现了 \`${ADAPT_DIR}/\` 下的文件"
  say '```'
  say "$OVERLAP"
  say '```'
  fatal "上游与适配层路径重叠，已中止（不做任何合并）。需人工改目录名后重跑。"
fi

# ── 3. 无更新：不产生空提交 ─────────────────────────────────────────────────
if [ "$BEHIND" -eq 0 ]; then
  say "✅ 上游无新提交，本次不做任何改动（无空提交、无 push）。"
  exit 0
fi

if [ "$DRY_RUN" = "1" ]; then
  say "🔍 DRY_RUN：有 ${BEHIND} 个上游提交待合并，未执行合并。"
  say ""
  say "<details><summary>待合入的上游提交</summary>"
  say ""
  say '```'
  say "$(git log --oneline --no-decorate "HEAD..upstream/${UPSTREAM_REF}" | head -50)"
  say '```'
  say ""
  say "</details>"
  exit 0
fi

# ── 4. 合并（merge --no-ff，绝不 reset --hard / force push） ─────────────────
PRE_MERGE_HEAD="$(git rev-parse HEAD)"
say "执行：\`git merge --no-ff upstream/${UPSTREAM_REF}\`"
if ! git merge --no-ff --no-edit \
      -m "merge: upstream ${UPSTREAM_REPO}@${UPSTREAM_SHA}" \
      "upstream/${UPSTREAM_REF}"; then

  CONFLICTS="$(git diff --name-only --diff-filter=U | sort -u)"
  say ""
  say "### ❌ 合并冲突（未合并、未推送，工作区已回滚到合并前状态）"
  say ""
  say "冲突文件（$(printf '%s\n' "$CONFLICTS" | grep -c . ) 个）："
  say '```'
  say "${CONFLICTS:-（无法列出，见上方 git 输出）}"
  say '```'
  say ""
  # runner 是一次性的，保留冲突态没有意义：abort 掉工作区，改为"开 Issue 交接给维护者"。
  git merge --abort >/dev/null 2>&1 || true

  BODY="$(mktemp)"
  {
    echo "上游 \`${UPSTREAM_REPO}@${UPSTREAM_REF}\` 有新提交（\`${UPSTREAM_SHA}\`），自动合并到 \`${TARGET_BRANCH}\` 时冲突。"
    echo
    echo "工作流已中止，**没有推送任何东西**，\`${TARGET_BRANCH}\` 保持合并前状态。"
    echo
    echo "### 冲突文件"
    echo
    echo '```'
    echo "$CONFLICTS"
    echo '```'
    echo
    echo "### 人工处理步骤"
    echo
    echo '```bash'
    echo "git fetch upstream            # 或 git fetch https://github.com/${UPSTREAM_REPO}.git"
    echo "git checkout ${TARGET_BRANCH}"
    echo "git merge --no-ff upstream/${UPSTREAM_REF}"
    echo "# 逐个解决冲突；冲突里属于上游逻辑的以 upstream 为准，属于我们适配层的保留我们的"
    echo "git add <file> && git commit"
    echo "git push origin ${TARGET_BRANCH}"
    echo '```'
    echo
    echo "> 约定：适配代码只放在 \`${ADAPT_DIR}/\`，不要塞进上游文件，否则每次上游同步都会冲突。"
  } >"$BODY"

  if [ -n "${GH_TOKEN:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ]; then
    EXISTING="$(gh issue list --repo "$GITHUB_REPOSITORY" --state open \
      --search '"[sync-upstream] 上游合并冲突" in:title' --json number --jq '.[0].number' 2>/dev/null || true)"
    if [ -n "${EXISTING:-}" ] && [ "$EXISTING" != "null" ]; then
      gh issue comment "$EXISTING" --repo "$GITHUB_REPOSITORY" --body-file "$BODY" >/dev/null 2>&1 \
        && say "已在 Issue #${EXISTING} 追加评论（同一问题不重复开单）。"
    else
      NEW_URL="$(gh issue create --repo "$GITHUB_REPOSITORY" \
        --title "[sync-upstream] 上游合并冲突 $(date -u +%Y-%m-%d)" \
        --label "sync-upstream" --body-file "$BODY" 2>/dev/null \
        || gh issue create --repo "$GITHUB_REPOSITORY" \
             --title "[sync-upstream] 上游合并冲突 $(date -u +%Y-%m-%d)" --body-file "$BODY" 2>/dev/null)"
      [ -n "${NEW_URL:-}" ] && say "已开 Issue：${NEW_URL}"
    fi
  fi
  rm -f "$BODY"
  exit 1
fi

# ── 5. 合并后守卫：这次 merge 不许碰适配层 ───────────────────────────────────
TOUCHED_ADAPT="$(git diff --name-only "${PRE_MERGE_HEAD}" HEAD | grep -E "^${ADAPT_DIR}/" || true)"
if [ -n "$TOUCHED_ADAPT" ]; then
  say "### ⚠️ 本次合并改动了适配层文件，已回滚"
  say '```'
  say "$TOUCHED_ADAPT"
  say '```'
  git reset --hard "${PRE_MERGE_HEAD}" >/dev/null 2>&1 || true
  fatal "上游改动触及 ${ADAPT_DIR}/，已 reset --hard 回合并前状态（这一步只在本地/CI 工作区生效，未推送）。"
fi

MERGED="$(git rev-list --count "${PRE_MERGE_HEAD}..HEAD")"
say "合并产生 ${MERGED} 个提交（含 1 个 merge commit）。"
say ""
say "<details><summary>本次合入的上游提交</summary>"
say ""
say '```'
say "$(git log --oneline --no-decorate "${PRE_MERGE_HEAD}..HEAD" | head -50)"
say '```'
say ""
say "</details>"

# ── 6. 推送（失败则退回 PR） ─────────────────────────────────────────────────
if git push origin "HEAD:${TARGET_BRANCH}" 2>&1 | tail -3; then
  say ""
  say "✅ 已推送到 \`${TARGET_BRANCH}\`（merge commit，未 force push）。"
  exit 0
fi

say ""
say "直接推送被拒（多半是分支保护要求走 PR），改用 PR 兜底。"
SYNC_BRANCH="sync/upstream-$(date -u +%Y%m%d-%H%M)"
if ! git push origin "HEAD:${SYNC_BRANCH}" 2>&1 | tail -3; then
  fatal "推送 ${SYNC_BRANCH} 也失败，请检查分支保护与 token 权限。"
fi
if [ -n "${GH_TOKEN:-}" ] && [ -n "${GITHUB_REPOSITORY:-}" ]; then
  PR_URL="$(gh pr create --repo "$GITHUB_REPOSITORY" --base "$TARGET_BRANCH" --head "$SYNC_BRANCH" \
    --title "sync: 上游 ${UPSTREAM_SHA}" \
    --body "自动合并上游 ${UPSTREAM_REPO}@${UPSTREAM_REF}（merge，非 force push）。" 2>&1 | tail -1)"
  say "已开 PR：${PR_URL}"
  say ""
  say "⚠️ \`${TARGET_BRANCH}\` 仍未更新，需要人工合入该 PR。"
  exit 0
fi
fatal "推送分支成功但无法开 PR（缺少 GH_TOKEN）。分支：${SYNC_BRANCH}"
