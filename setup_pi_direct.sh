#!/bin/bash
# 纯直连安装：不走任何国内镜像/代理（海外网络，或你自己已挂全局代理时用）。
# 等价于：BMO_MIRROR=direct ./setup_pi.sh
cd "$(dirname "$0")"
exec env BMO_MIRROR=direct ./setup_pi.sh "$@"
