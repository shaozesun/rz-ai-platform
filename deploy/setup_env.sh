#!/usr/bin/env bash
# 生成新版 compose 必填密钥并写入 deploy/.env（幂等，已存在的 key 不覆盖）
# 用法：在服务器 /opt/rz-ai/deploy 下执行  bash setup_env.sh
# 说明：新版新增 langfuse，compose 需要 LANGFUSE_DB_PASSWORD/NEXTAUTH_SECRET/SALT，
#       服务器旧版没有这三个变量，跑一次本脚本即补全（写进 deploy/.env）。
set -euo pipefail

ENV_FILE="$(cd "$(dirname "$0")" && pwd)/.env"
touch "$ENV_FILE"

append() {
  local key="$1" value="$2"
  if grep -q "^${key}=" "$ENV_FILE"; then
    echo "  $key 已存在，跳过"
  else
    echo "$key=$value" >> "$ENV_FILE"
    echo "  $key 已写入"
  fi
}

echo "==> 写入 deploy/.env: $ENV_FILE"
append LANGFUSE_DB_PASSWORD "$(openssl rand -base64 24)"
append LANGFUSE_NEXTAUTH_SECRET "$(openssl rand -base64 32)"
append LANGFUSE_SALT "$(openssl rand -base64 32)"
echo "==> 完成。deploy/.env 已含 LANGFUSE 变量，可继续 docker compose up -d --build"
