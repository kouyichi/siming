#!/usr/bin/env bash
# 一键启动本地自托管 Sure 财务 OS 并导入 siming.db 账本（幂等，可重复执行）
# 前置：若 dockerd 走代理且当前节点拉 ghcr.io 限速（~8KB/s），请先在代理面板切一个高速节点再执行。
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/deploy/sure/docker-compose.live.yml"
SECRETS_ENV="$HOME/.config/siming/secrets/sure.env"

if [[ ! -f "$SECRETS_ENV" ]]; then
  echo "缺少 $SECRETS_ENV，正在自动生成 0600 密钥文件…"
  mkdir -p "$(dirname "$SECRETS_ENV")"
  chmod 700 "$(dirname "$SECRETS_ENV")"
  (
    umask 077
    cat > "$SECRETS_ENV" <<EOF
SECRET_KEY_BASE=$(openssl rand -hex 64)
POSTGRES_USER=sure
POSTGRES_PASSWORD=$(openssl rand -hex 24)
POSTGRES_DB=sure_production
EOF
  )
fi

echo "==> 1/3 拉取并启动 Sure（绑定 127.0.0.1:3032）"
docker compose -p siming-sure -f "$COMPOSE_FILE" up -d

echo "==> 2/3 等待 Sure 健康检查 http://127.0.0.1:3032/up ..."
for i in $(seq 1 60); do
  if curl -fsS --noproxy '*' -o /dev/null http://127.0.0.1:3032/up 2>/dev/null; then
    echo "Sure 已就绪！"
    break
  fi
  sleep 2
done

echo "==> 3/3 幂等导入 siming.db 全部账本流水"
"$ROOT_DIR/.pixi/envs/default/bin/python" "$ROOT_DIR/tools/sure_import.py"

echo "==> 完成！浏览器打开 http://127.0.0.1:3032 ，登录账号密码位于 ~/.config/siming/secrets/sure-login.txt（0600）。"
