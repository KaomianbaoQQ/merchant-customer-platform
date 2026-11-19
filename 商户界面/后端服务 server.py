#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试商城商户管理后台启动器
=====================================================
商户后台的页面与接口已统一到项目主后端（Flask）中，好处是：
  · 商户账号、商品、会话与客户前端共用同一套数据库 / 数据文件
  · 只需要一个后端进程，避免两个服务各写一份数据
本脚本只是为了兼容原来的启动方式：直接运行即可在指定端口启动整套后端，
商户后台入口为 http://127.0.0.1:<端口>/merchant

运行方式（建议使用项目虚拟环境，需要 Flask / PyMySQL）：
  .venv/bin/python "商户界面/后端服务 server.py" [端口]   # 默认 8001
或直接：
  .venv/bin/python main.py                              # 默认 8000

商户功能：
  · 账号注册 / 登录（用户名 + 密码，写入 cs.merchant）
  · 每个商户只能管理自己上架的商品（多商户隔离）
  · 顾客在客户前端「联系商家」留言，商户在「顾客消息」中回复
  · 知识库同步接口已预留：POST /api/merchant/knowledge/update（暂未接入）
=====================================================
"""
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(BASE_DIR)
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from 后端.app import run_server  # noqa: E402


def main():
    port = 8001
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            port = 8001
    print("商户后台入口：http://127.0.0.1:%d/merchant" % port)
    run_server(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()