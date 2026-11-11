"""Kaomianbao 项目入口。

用法（在项目根目录执行）：
    python main.py          # 启动 Web 后端
    python main.py --cli    # 进入售后模块命令行对话
"""
import sys

from 后端.app import run_server
from 售后.cli import run_cli


def main():
    if "--cli" in sys.argv:
        run_cli()
    else:
        run_server()


if __name__ == "__main__":
    main()
