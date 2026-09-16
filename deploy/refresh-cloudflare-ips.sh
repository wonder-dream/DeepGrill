#!/bin/sh
# 把 Cloudflare 公布的出口网段写成 nginx 的 realip 配置（决策 77）。
#
# 为什么需要它：`allow`/`deny` 和 `$binary_remote_addr` 看的都是 **TCP 对端地址**。
# 走 Cloudflare 时对端永远是 CF 的边缘节点 —— 于是
#   ① `/admin` 的白名单里写谁的 IP 都进不去（管理员自己也是 403），
#   ② 按 IP 的限流把全站访客算成同一个来源。
# realip 模块在**只信任 CF 网段**的前提下把 `$remote_addr` 换成 `CF-Connecting-IP`，
# 两件事一起恢复正常；而"只信任 CF 网段"这条前提让伪造这个头没有意义
# （源站即使还在对全网开放，非 CF 来源送来的头也不会被采信）。
#
# 列表会变（CF 偶尔新增网段），所以它必须是**可重复跑**的脚本，而不是抄进仓库的
# 一份死名单。一周跑一次足够。
#
# 用法（要写 /etc/nginx 与 reload，所以是 root）：
#     sudo sh deploy/refresh-cloudflare-ips.sh
set -eu

out=${1:-/etc/nginx/conf.d/cloudflare-realip.conf}

{
    echo "# 由 deploy/refresh-cloudflare-ips.sh 生成 —— 不要手工编辑"
    echo "# 只信任这些网段送来的 CF-Connecting-IP"
    curl -fsS https://www.cloudflare.com/ips-v4 | sed 's|^|set_real_ip_from |; s|$|;|'
    curl -fsS https://www.cloudflare.com/ips-v6 | sed 's|^|set_real_ip_from |; s|$|;|'
    echo "real_ip_header CF-Connecting-IP;"
} > "$out"

nginx -t
nginx -s reload
echo "已更新 $out（$(grep -c 'set_real_ip_from' "$out") 条网段）"
