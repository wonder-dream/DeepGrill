#!/bin/sh
# 把 Cloudflare 公布的出口网段写成 nginx 的一份配置，做两件事（决策 77 / 94 / 95）：
#
#   ① realip：`allow`/`deny` 与 `$binary_remote_addr` 看的都是 **TCP 对端地址**，
#      而经 CF 进来的对端永远是 CF 的边缘节点、不是访客 —— 于是按 IP 的白名单
#      全都不成立、按 IP 的限流会把全站算成一个来源。realip 在**只信任 CF 网段**
#      的前提下把 `$remote_addr` 换成 `CF-Connecting-IP`。
#   ② 只允许 CF 回源：把同一份网段写成 `geo $realip_remote_addr`，
#      `deploy/nginx.conf` 里各 server 块据此拒掉绕过 CF 的直连。
#
# 两件事**必须同源**：CF 偶尔新增网段，判定和 realip 若各存一份，就一定会有一份
# 先过期 —— 而过期的表现分别是"直连被放进来"和"站点从新边缘节点访问不了"。
#
# 列表会变，所以它必须是**可重复跑**的脚本，而不是抄进仓库的一份死名单。建议每天
# 跑一次（就一次 HTTP 请求 + 一次 reload）。
#
# 用法（要写 /etc/nginx 与 reload，所以是 root）：
#     sudo sh deploy/refresh-cloudflare-ips.sh
set -euo pipefail

out=${1:-/etc/nginx/conf.d/cloudflare-realip.conf}

# 先把列表取全再动文件：取一半就落盘会写出一份"谁都不许进"的配置
v4=$(curl -fsS https://www.cloudflare.com/ips-v4)
v6=$(curl -fsS https://www.cloudflare.com/ips-v6)
[ -n "$v4" ] && [ -n "$v6" ] || {
    echo "取不到 Cloudflare 的网段列表 —— 什么都不改" >&2
    exit 1
}

tmp=$(mktemp)
{
    echo "# 由 deploy/refresh-cloudflare-ips.sh 生成 —— 不要手工编辑"
    echo
    echo "# ① 只信任这些网段送来的 CF-Connecting-IP"
    echo "$v4" | sed 's|^|set_real_ip_from |; s|$|;|'
    echo "$v6" | sed 's|^|set_real_ip_from |; s|$|;|'
    echo "real_ip_header CF-Connecting-IP;"
    echo
    echo "# ② 只允许这些网段与本机回源（决策 95）。判定用 \$realip_remote_addr ——"
    echo "#    那是**没被 realip 改写**的对端地址；写成 \$remote_addr 的话它已经变成"
    echo "#    访客 IP（于是这道门等于不存在）。"
    echo "geo \$realip_remote_addr \$from_cloudflare {"
    echo "    default 0;"
    echo "    127.0.0.1/32 1;"
    echo "    ::1/128 1;"
    echo "$v4" | sed 's|^|    |; s|$| 1;|'
    echo "$v6" | sed 's|^|    |; s|$| 1;|'
    echo "}"
} > "$tmp"

# 落盘前留一手：跑不过 `nginx -t` 就恢复上一份，否则一次失败的刷新会变成
# "下次重启 nginx 直接起不来"
if [ -f "$out" ]; then cp "$out" "$out.bak"; fi
mv "$tmp" "$out"

if ! nginx -t; then
    echo "nginx -t 失败 —— 恢复上一份" >&2
    if [ -f "$out.bak" ]; then mv "$out.bak" "$out"; else rm -f "$out"; fi
    nginx -t
    exit 1
fi

nginx -s reload
echo "已更新 $out（$(grep -c 'set_real_ip_from' "$out") 条网段）"
