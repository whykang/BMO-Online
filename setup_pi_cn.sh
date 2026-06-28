#!/bin/bash
# 大陆加速安装：pip 全程走清华镜像，GitHub 全程走代理（ghfast.top 等）。
# 等价于：BMO_MIRROR=cn ./setup_pi.sh
# 想指定自己的 GitHub 代理：GH_PROXY="https://你的代理" ./setup_pi_cn.sh
cd "$(dirname "$0")"
exec env BMO_MIRROR=cn ./setup_pi.sh "$@"
