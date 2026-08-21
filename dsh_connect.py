"""DSH（DeepSeek Harness）接入启动脚本。

一键启动 autotrade MCP server 并打印 DSH 接入指引。

用法::

    python dsh_connect.py               # 启动 MCP server（stdio 模式）
    python dsh_connect.py --check       # 自检：列出工具 + 验证配置
    python dsh_connect.py --readonly    # 只读模式启动（禁用交易工具）
    python dsh_connect.py --print-config  # 打印 DSH MCP 客户端配置 JSON

DSH 接入步骤::

    1. pip install fastmcp
    2. 将 dsh_mcp_config.json 的内容加入 DSH 的 MCP 配置
    3. 在 DSH 的系统提示词中加入 dsh_system_prompt.md 的内容
    4. 启动 DSH，它会自动拉起 autotrade MCP server
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
_CONFIG_TEMPLATE = {
    "mcpServers": {
        "autotrade": {
            "command": sys.executable,
            "args": ["mcp_server.py"],
            "cwd": str(_REPO_ROOT),
            "env": {
                "AUTOTRADE_MCP_ALLOW_TRADE": "1",
                "AUTOTRADE_ACCOUNTS": "accounts.json",
            },
        },
    },
}


def _print_config(readonly: bool = False) -> str:
    """生成 DSH MCP 客户端配置 JSON。"""
    config = json.loads(json.dumps(_CONFIG_TEMPLATE))  # deep copy
    if readonly:
        config["mcpServers"]["autotrade"]["env"]["AUTOTRADE_MCP_ALLOW_TRADE"] = "0"

    # 注入 .env 中的 LLM 配置
    env_overrides = {
        "DEEPSEEK_API_KEY": os.environ.get("DEEPSEEK_API_KEY", ""),
        "TRADINGAGENTS_LLM_PROVIDER": os.environ.get(
            "TRADINGAGENTS_LLM_PROVIDER", "deepseek",
        ),
        "TRADINGAGENTS_DEEP_THINK_LLM": os.environ.get(
            "TRADINGAGENTS_DEEP_THINK_LLM", "deepseek-v4-pro",
        ),
        "TRADINGAGENTS_QUICK_THINK_LLM": os.environ.get(
            "TRADINGAGENTS_QUICK_THINK_LLM", "deepseek-v4-flash",
        ),
        "TRADINGAGENTS_OUTPUT_LANGUAGE": os.environ.get(
            "TRADINGAGENTS_OUTPUT_LANGUAGE", "Chinese",
        ),
    }
    config["mcpServers"]["autotrade"]["env"].update(env_overrides)
    return json.dumps(config, ensure_ascii=False, indent=2)


def _check() -> int:
    """自检：验证依赖 + 列出工具 + 检查配置。"""
    print("=" * 60)
    print("AutoTrade MCP Server 自检")
    print("=" * 60)

    # 1. 检查 fastmcp
    print("\n1. 检查 fastmcp 依赖...")
    try:
        import fastmcp  # noqa: F401
        print(f"   [OK] fastmcp 已安装 (v{fastmcp.__version__})")
    except ImportError:
        print("   [FAIL] fastmcp 未安装")
        print("   请运行: pip install fastmcp")
        return 1

    # 2. 检查 accounts.json
    print("\n2. 检查账号配置...")
    accounts_path = Path(_REPO_ROOT) / "accounts.json"
    if accounts_path.exists():
        try:
            data = json.loads(accounts_path.read_text(encoding="utf-8"))
            accounts = data if isinstance(data, list) else data.get("accounts", [])
            print(f"   [OK] accounts.json 存在，{len(accounts)} 个账号")
            for a in accounts:
                name = a.get("name", "?")
                broker = (a.get("broker_settings") or {}).get("broker", "paper")
                wl = a.get("watchlist", [])
                print(f"     - {name}: broker={broker}, watchlist={len(wl)} 只")
        except Exception as exc:
            print(f"   [FAIL] accounts.json 解析失败: {exc}")
    else:
        print("   [WARN] accounts.json 不存在（将使用 paper-default 模拟盘）")

    # 3. 检查 .env
    print("\n3. 检查 LLM 配置...")
    env_path = Path(_REPO_ROOT) / ".env"
    if env_path.exists():
        try:
            from dotenv import load_dotenv  # noqa: F401
            load_dotenv(str(env_path))
        except ImportError:
            pass
    provider = os.environ.get("TRADINGAGENTS_LLM_PROVIDER", "")
    if provider:
        print(f"   [OK] LLM provider: {provider}")
        deep = os.environ.get("TRADINGAGENTS_DEEP_THINK_LLM", "")
        quick = os.environ.get("TRADINGAGENTS_QUICK_THINK_LLM", "")
        print(f"   [OK] 深度模型: {deep}")
        print(f"   [OK] 快速模型: {quick}")
    else:
        print("   [WARN] 未配置 LLM provider（.env 中设置 TRADINGAGENTS_LLM_PROVIDER）")

    # 4. 列出注册工具
    print("\n4. 列出 MCP 工具...")
    try:
        import mcp_server as ms
        rc = ms.main(["--list-tools"])
        if rc == 0:
            print("   [OK] 工具注册成功")
        else:
            print(f"   [FAIL] 工具注册失败 (exit code {rc})")
            return 1
    except Exception as exc:
        print(f"   [FAIL] 工具注册失败: {exc}")
        return 1

    # 5. 打印 DSH 配置
    print("\n5. DSH MCP 配置（加入 DSH 的 mcp_servers 配置目录）:")
    print("-" * 60)
    print(_print_config())
    print("-" * 60)

    print("\n自检完成。")
    print("接入 DSH: 将上方 JSON 配置加入 DSH 的 MCP 配置目录，")
    print("系统提示词见 dsh_system_prompt.md")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="DSH（DeepSeek Harness）接入启动脚本",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="自检模式：验证依赖、列出工具、打印配置",
    )
    parser.add_argument(
        "--readonly", action="store_true",
        help="只读模式启动 MCP server（禁用交易工具）",
    )
    parser.add_argument(
        "--print-config", action="store_true",
        help="打印 DSH MCP 客户端配置 JSON 后退出",
    )
    parser.add_argument(
        "--accounts", default=None,
        help="指定账号配置 JSON 路径",
    )
    args = parser.parse_args(argv)

    if args.print_config:
        print(_print_config(readonly=args.readonly))
        return 0

    if args.check:
        return _check()

    # 启动 MCP server
    server_args = []
    if args.accounts:
        server_args.extend(["--accounts", args.accounts])

    # readonly 模式通过环境变量传递
    if args.readonly:
        os.environ["AUTOTRADE_MCP_ALLOW_TRADE"] = "0"

    # 加载 .env
    try:
        from dotenv import load_dotenv
        load_dotenv(str(Path(_REPO_ROOT) / ".env"))
    except ImportError:
        pass

    print("启动 autotrade MCP server...", file=sys.stderr)
    if args.readonly:
        print("[WARN] 只读模式：交易工具已禁用", file=sys.stderr)

    import mcp_server as ms
    return ms.main(server_args)


if __name__ == "__main__":
    raise SystemExit(main())
