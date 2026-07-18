#!/bin/bash
# cb-weekly 一键部署: 创建 GitHub 仓库 + 推送 + 启用 Pages
set -e
echo "=== cb-weekly 部署 ==="
cd ~/cb-weekly/.claude/worktrees/cb-weekly-deploy 2>/dev/null || cd ~/cb-weekly

gh auth login

REPO="cb-weekly"
gh repo create "$REPO" --public --source . --remote origin --push 2>/dev/null || {
  echo "仓库已存在, 设置 remote 并推送..."
  git remote remove origin 2>/dev/null || true
  ACCOUNT=$(git config user.name)
  git remote add origin "https://github.com/${ACCOUNT}/${REPO}.git"
  git push -u origin main
}

gh api -X POST "/repos/$(git config user.name)/${REPO}/pages" \
  -f "source[branch]=main" -f "source[path]=/" 2>/dev/null || echo "Pages 可能已启用或需在 Settings 手动操作"

gh workflow run weekly-cb-run 2>/dev/null && echo "✓ 已触发首次运行" || echo "请到 Actions 页面手动触发首次 run"

echo ""
echo "=== 部署完成! ==="
echo "面板地址: https://$(git config user.name).github.io/${REPO}/"
echo "Actions:   https://github.com/$(git config user.name)/${REPO}/actions"
