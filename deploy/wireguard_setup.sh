#!/usr/bin/env bash
# 自建 WireGuard 服务端（共用一份客户端配置）— 服务器上一键脚本
# 用法：把本文件传到服务器，然后  sudo bash wireguard_setup.sh
#
# 说明：
#   - 服务端网段 10.8.0.0/24（避开服务器上连对方平台的 client 接口 10.66.66.x）
#   - 监听端口 51820/udp
#   - 所有用户共用一份客户端配置（单 [Peer]），密钥在服务器生成，脚本不含密钥
set -euo pipefail

# root 检查：需要写 /etc/wireguard、装包、systemctl、防火墙，必须以 root 运行
if [ "$(id -u)" -ne 0 ]; then
  echo "✗ 需要 root 权限，请用 sudo 运行：sudo bash $(basename "$0")" >&2
  exit 1
fi

echo "==> [1/6] 安装 WireGuard"
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y wireguard
elif command -v yum >/dev/null 2>&1; then
  yum install -y wireguard-tools
else
  echo "✗ 无法识别包管理器，请手动安装 wireguard 后重跑"; exit 1
fi

echo "==> [2/6] 生成服务端密钥"
mkdir -p /etc/wireguard && cd /etc/wireguard && umask 077
wg genkey | tee server.key | wg pubkey > server.pub

echo "==> [3/6] 生成共用客户端密钥"
wg genkey | tee client.key | wg pubkey > client.pub

echo "==> [4/6] 写 /etc/wireguard/wg0.conf"
cat > /etc/wireguard/wg0.conf <<EOF
[Interface]
Address = 10.8.0.1/24
ListenPort = 51820
PrivateKey = $(cat /etc/wireguard/server.key)
[Peer]
PublicKey = $(cat /etc/wireguard/client.pub)
AllowedIPs = 10.8.0.2/32
EOF
chmod 600 /etc/wireguard/wg0.conf

echo "==> [5/6] 开启 IP 转发并启动"
grep -q '^net.ipv4.ip_forward' /etc/sysctl.conf || echo 'net.ipv4.ip_forward = 1' >> /etc/sysctl.conf
sysctl -p -q || true
systemctl enable wg-quick@wg0
systemctl start wg-quick@wg0

echo "==> [6/6] 防火墙放行 51820/udp"
if command -v ufw >/dev/null 2>&1; then
  ufw allow 51820/udp
else
  echo "  （无 ufw，请自行放行 51820/udp，如 iptables / firewall-cmd）"
fi

echo
echo "========== WireGuard 服务端就绪 =========="
echo "服务端公钥  : $(cat /etc/wireguard/server.pub)"
echo "共用客户端私钥: $(cat /etc/wireguard/client.key)"
echo "共用客户端公钥: $(cat /etc/wireguard/client.pub)"
echo
echo "客户端配置模板见项目 deploy/wireguard_client.conf.template"
echo "（把上面「服务端公钥」和「共用客户端私钥」填入模板占位符后分发给所有用户）"
echo "=========================================="
