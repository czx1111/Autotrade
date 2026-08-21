# AutoTrade：多智能体 LLM A 股交易终端

基于多代理 LLM 框架深度定制的 **A 股自动化交易系统**。7 位 A 股专属分析师、自动化交易闭环（盘前/盘中/盘后）、WebUI 控制台、多券商对接、下单守卫、挂单看护、持仓复查、状态持久化、风控体系与钉钉告警，覆盖从 LLM 分析到下单执行的完整链路。

支持三种运行方式：**WebUI 控制台** · **自动交易守护进程** · **Python API**，并可接入 **DSH（DeepSeek Harness）** 实现 AI 对话式交易。

> ⚠️ **风险提示**：本框架仅用于研究与技术验证，不构成任何投资建议。

> 📌 **基于 TradingAgents**：本项目基于开源多智能体 LLM 交易框架 [TradingAgents](https://github.com/TauricResearch/TradingAgents) 进行深度定制与二次开发，针对 A 股市场特性重新设计了分析师体系、行情数据源、风控规则与交易执行链路。在此向 TradingAgents 原始团队表示感谢。

---

## 快速开始

### 安装

```bash
git clone https://github.com/czx1111/autotrade.git
cd autotrade

conda create -n autotrade python=3.12
conda activate autotrade

pip install .                    # 核心依赖
pip install ".[ui]"              # WebUI（Streamlit + Plotly）
pip install ".[easytrader]"      # 同花顺通用客户端实盘
pip install ".[qmt]"             # miniQMT 实盘
pip install ".[bedrock]"         # AWS Bedrock（可选）
pip install fastmcp              # MCP Server（接入 DSH 用，可选）
```

### 配置

1. 复制环境变量模板并填写 LLM 密钥：

```bash
cp .env.example .env
# 编辑 .env，至少设置一个 LLM provider 的 API key
```

2. 复制账号配置模板：

```bash
cp accounts.example.json accounts.json
# 编辑 accounts.json，配置券商通道与股票池（不配则默认 paper 模拟盘）
```

3. （可选）配置钉钉告警机器人：

```bash
# .env 中添加
TRADINGAGENTS_DINGTALK_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=你的token
TRADINGAGENTS_DINGTALK_SECRET=SEC你的加签密钥
```

### 启动

```bash
streamlit run webui.py               # WebUI 控制台
python run_auto.py                    # 自动交易守护进程（常驻）
python run_auto.py --once             # 立即跑一轮盘中流程（试跑）
python -m cli.main analyze            # CLI 交互分析
```

---

## 核心特性

### 🤖 7 位 A 股专属分析师

| 分析师 | 职责 | 数据源 |
|--------|------|--------|
| 📈 市场分析师 | 技术指标（MACD/RSI/均线）+ 价格走势 | 东财/腾讯/新浪/通达信 K 线 |
| 💬 舆情分析师 | 社交媒体情绪聚合（东方财富股吧/雪球） | akshare 社交接口 |
| 📰 新闻分析师 | 全球宏观新闻 + 个股新闻 | akshare 新闻接口 |
| 📊 基本面分析师 | 财务报表、估值指标（PE/PB/ROE） | akshare 基本面 |
| 🏛️ 政策分析师 | 货币/财政/产业政策与监管动态影响 | 宏观新闻 + 经济指标 |
| 🐉 游资追踪 | 龙虎榜席位 / 北向资金 / 融资融券动向 | akshare 资金流数据 |
| 🔓 解禁监控 | 限售解禁批次 + 减持公告的供给冲击 | akshare 解禁数据 |

### 🔄 自动化交易闭环

三阶段无人值守守护进程，匹配 A 股交易时段：

```
盘前 09:00    重置风控基线 → 过期旧审批 → LLM 从股票池筛选当日重点
盘中 09:30~   逐只运行多代理分析图 → 解析 Rating → 仓位映射 → 下单
              大额订单自动进入审批队列，批准后下一轮执行
盯盘 5min     分钟级持仓巡检 → 止损/止盈/移动止损/均线死叉 → 自动卖出
              异动检测（急涨急跌/逼近涨跌停/逼近止损）→ 重置分析 → LLM 重跑
盘后 15:30    T+1 滚动 → 当日汇总报告 → 净值点记录 → 周五/月末自动出周报/月报
```

### 🖥️ WebUI 控制台（9 页面）

DeepSeek 风格深色主题，Streamlit 实现：

| 页面 | 功能 |
|------|------|
| 📊 总览仪表盘 | 状态栏 / 四列 KPI / 双账号卡片 / 持仓饼图 / 收益曲线 / 通知流 |
| 🤖 智能体工作室 | 7 位分析师实时思考过程（流式活动轨）+ 决策面板 + 一键执行 |
| 📈 持仓与交易 | 多账号持仓 / 加减清仓 / 交易记录 / 待执行审批 / 盯盘信号 / AI 持仓分析 |
| 🔍 发现选股 | 指数板块 / 全市场筛选 / 一键多因子选股 + AI 复核 / 自选股管理 |
| 📐 K 线看盘 | 多源 K 线（东财→腾讯→新浪→通达信）+ 均线成交量 |
| 🩺 系统健康 | 数据源探活 / 守护进程心跳 / 交易日历 / LLM 连通测试 |
| 🔔 告警中心 | 告警事件流（熔断/审批/止损/故障）/ 盯盘信号历史 / 待审批订单 |
| ⚙️ 策略配置 | 分析师组合 / 风控阈值 / 盯盘策略 / 券商连接测试 / LLM 查看 |
| 📋 历史报告 | 分析报告与交易日报归档、导出 |

### 🛡️ 多层风控体系

| 层级 | 规则 | 说明 |
|------|------|------|
| 场地规则 | ST/\*ST 黑名单 | 自动过滤风险标的 |
| | 涨跌停带检查 | 拒绝超出涨停/跌停价的订单（可自动裁剪到合法区间） |
| | 整手校验 | A 股 100 股一手，不足整手自动向下取整 |
| 组合风控 | 单票仓位上限 | 默认 20%，可按账号配置 |
| | 日最大亏损 | 默认 3%，触发即熔断暂停开新仓 + 钉钉 critical 告警 |
| | 每日下单数 | 默认 10 笔/日 |
| | 行业集中度 | 单行业仓位 ≤ 40%（可选） |
| | 资金储备 | 买入前检查可用资金 |
| T+1 约束 | 可卖数量检查 | 当日买入不可卖，次日自动可卖 |
| 大额审批 | 金额门控 | 实盘订单 ≥ 阈值（默认 5 万）自动进审批队列 |

### 📡 多源行情（故障转移）

| 数据 | 供应商链 | 说明 |
|------|----------|------|
| 实时报价 | 腾讯 → 新浪 | 逐票多源，45s TTL 缓存 |
| 日 K 线 | 东财(akshare) → 腾讯 → 通达信 → 新浪 | 30min TTL 缓存，前复权优先 |
| 全市场快照 | 东财(akshare) | 大列表批量，失败 10min 熔断冷却 |
| 交易日历 | 新浪 trade-date 表 | 本地缓存，每周刷新，节假日自动跳过 |

### 📋 盯盘策略引擎

每账号可独立配置退出策略：

```json
{
  "stop_loss_pct": 0.07,
  "take_profit_pct": 0.15,
  "trailing_stop_pct": 0.08,
  "ma_cross_exit": false,
  "max_hold_days": null
}
```

- `stop_loss_pct` — 止损比例（默认 7%）
- `take_profit_pct` — 止盈比例（默认 15%）
- `trailing_stop_pct` — 移动止损：持有期高点回撤比例（默认 8%）
- `ma_cross_exit` — MA5 下穿 MA10 卖出（默认关闭）
- `max_hold_days` — 最大持有天数（`null` 不限）

### 🔔 钉钉告警

风控事件 → 机器人 webhook，事件分级 + 去重防刷屏：

- **info**：止损/止盈执行、周报/月报生成等常规动作
- **warning**：数据源故障、大额订单待审批、持仓异动、挂单超时撤单、客户端掉线已拉起
- **critical**：日亏熔断、进程异常、客户端拉起失败、撤单失败

---

## 运行方式

### WebUI 控制台

```bash
streamlit run webui.py
```

### 自动交易守护进程

```bash
python run_auto.py                      # 常驻运行：盘前筛选 / 盘中分析下单 / 盘后复盘
python run_auto.py --once               # 立即跑一轮盘中流程（忽略交易时段，试跑用）
python run_auto.py --monitor-once       # 立即盯盘巡检一次（止损/止盈检查）
python run_auto.py --pre-market         # 立即跑盘前筛选
python run_auto.py --post-market        # 立即跑盘后复盘
python run_auto.py --account pingan     # 只跑指定账号
python run_auto.py --list-pending       # 查看待审批的大额订单
python run_auto.py --approve <ID>       # 批准一笔待审批订单
python run_auto.py --reject <ID>        # 拒绝一笔待审批订单
python run_auto.py --review weekly      # 手动生成周报
python run_auto.py --review monthly     # 手动生成月报
```

### CLI 交互模式

```bash
python -m cli.main analyze              # 分析单个标的
python -m cli.main analyze --checkpoint  # 启用检查点恢复
```

### Python API

```python
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.default_config import DEFAULT_CONFIG

config = DEFAULT_CONFIG.copy()
config["llm_provider"] = "deepseek"
config["deep_think_llm"] = "deepseek-v4-pro"
config["quick_think_llm"] = "deepseek-v4-flash"
config["output_language"] = "Chinese"

ta = TradingAgentsGraph(debug=True, config=config)
_, decision = ta.propagate("600519", "2026-01-15")
print(decision)
```

自动交易 API：

```python
from tradingagents.auto_trader import AutoTrader
from tradingagents.default_config import DEFAULT_CONFIG

trader = AutoTrader(
    {"name": "paper-test", "broker_settings": {"broker": "paper"},
     "watchlist": ["600519", "000858"]},
    DEFAULT_CONFIG.copy(),
)

trader.run_pre_market()         # 盘前筛选
trader.run_intraday(force=True) # 盘中分析下单
trader.run_monitor()            # 盯盘巡检
trader.run_post_market()        # 盘后复盘
```

### Windows 一键启动

```powershell
.\start.ps1 webui    # WebUI 控制台
.\start.ps1 auto     # 自动交易守护进程
.\start.ps1 cli      # CLI 交互模式
.\start.ps1 once     # 立即跑一轮盘中流程
```

---

## 接入 DSH（DeepSeek Harness）

本项目内置 MCP Server（`mcp_server.py`），通过 MCP stdio 协议将交易系统的 13 个能力暴露为工具，供 DSH 等 MCP 客户端作为主 Agent 调用。接入后，用户可以在 DSH 中用自然语言直接查询行情、深度分析、下单交易。

```
用户 ←→ DSH (主 Agent) ←MCP→ autotrade MCP Server → 多智能体分析 / 风控 / 券商
```

### 接入步骤

#### 1. 安装 MCP 依赖

```bash
pip install fastmcp
```

#### 2. 配置 DSH MCP 客户端

将 `dsh_mcp_config.json` 的内容加入 DSH 的 MCP 配置目录（或手动创建配置文件）：

```json
{
  "mcpServers": {
    "autotrade": {
      "command": "python",
      "args": ["mcp_server.py"],
      "cwd": "/path/to/autotrade",
      "env": {
        "AUTOTRADE_MCP_ALLOW_TRADE": "1",
        "AUTOTRADE_ACCOUNTS": "accounts.json",
        "DEEPSEEK_API_KEY": "你的API Key",
        "TRADINGAGENTS_LLM_PROVIDER": "deepseek",
        "TRADINGAGENTS_DEEP_THINK_LLM": "deepseek-v4-pro",
        "TRADINGAGENTS_QUICK_THINK_LLM": "deepseek-v4-flash",
        "TRADINGAGENTS_OUTPUT_LANGUAGE": "Chinese"
      }
    }
  }
}
```

> **一键生成配置**：`python dsh_connect.py --print-config` 会读取 `.env` 自动填充 API Key 和模型参数。

#### 3. 配置 DSH 系统提示词

将 `dsh_system_prompt.md` 的内容加入 DSH 的系统提示词，使 DSH 了解可用的工具及使用规范。

#### 4. 启动

启动 DSH 后，它会自动拉起 autotrade MCP Server 并注册全部工具。无需手动启动 MCP Server。

> **自检**：`python dsh_connect.py --check` 验证依赖、账号配置、工具注册是否正常。

> **只读模式**：设置 `AUTOTRADE_MCP_ALLOW_TRADE=0` 可禁用所有交易类工具，先跑通分析再放开交易。

### 接入后的对话示例

```
用户：茅台现在多少钱？
DSH：[调用 get_quote("600519")] 贵州茅台最新价 1685.20，涨 +0.83%...

用户：帮我深度分析一下宁德时代
DSH：[调用 analyze_symbol("300750")] 正在运行 7 位分析师多智能体分析，约需 3-5 分钟...
     分析完成。Rating: Overweight（增持），目标仓位 6%...

用户：买入 100 股贵州茅台，限价 1680
DSH：[调用 place_order("600519", "buy", 100, 1680.0)] 订单已提交，风控通过，已执行...

用户：看看我今天交易了什么
DSH：[调用 get_daily_summary()] 今日成交 2 笔，盈亏 +3,250.00 元...
```

### MCP 工具集（13 个工具）

| 分组 | 工具 | 参数 | 说明 |
|------|------|------|------|
| 只读数据 | `list_accounts` | — | 账号清单（名称/券商/股票池） |
| | `get_quote` | `symbols` | 实时行情（逗号分隔代码） |
| | `get_market_snapshot` | — | 大盘快照（指数/涨跌家数/情绪） |
| 深度分析 | `analyze_symbol` | `symbol`, `account?` | 7 位分析师全管线（耗时数分钟） |
| 账户查询 | `get_account` | `account?` | 总资产/可用/市值/冻结 |
| | `get_positions` | `account?` | 持仓（数量/可用/成本/现价） |
| | `get_pending_orders` | `account?` | 待审批的大额订单 |
| | `get_daily_summary` | `account?` | 当日成交/盈亏/风控状态 |
| 交易动作 | `place_order` | `symbol`, `action`, `quantity`, `price`, `account?` | 走全量风控，大额自动进审批 |
| | `approve_order` | `order_id`, `account?` | 批准待审批订单 |
| | `reject_order` | `order_id`, `account?` | 拒绝待审批订单 |
| 流程触发 | `run_intraday_once` | `account?` | 跑一轮盘中分析下单 |
| | `run_monitor_once` | `account?` | 跑一轮盯盘巡检（止损/止盈） |

> `account` 参数为空时默认使用第一个账号。多账号场景先 `list_accounts` 查看。

### 安全设计（DSH 无法绕过）

- **全量风控**：`place_order` 走 executor 管道——涨跌停带检查 / 整手校验 / T+1 约束 / 仓位上限 / 日亏熔断 / 资金储备
- **大额审批**：实盘订单 ≥ 阈值（默认 5 万）自动进审批队列，返回 `PENDING_APPROVAL` + `approval_id`，需 `approve_order` 批准后下一轮执行
- **只读开关**：`AUTOTRADE_MCP_ALLOW_TRADE=0` 整体禁用交易类工具
- **状态共享**：paper 账号状态文件与 `run_auto` 守护进程共享，避免双进程同时下单

### DSH 与守护进程并行

DSH 和 `run_auto.py` 守护进程可以同时运行：DSH 负责 AI 对话式按需交易，守护进程负责无人值守闭环。两者通过 `~/.tradingagents/` 下的状态文件共享数据，不会冲突。

### dsh_connect.py 辅助脚本

```bash
python dsh_connect.py                    # 启动 MCP Server（stdio 模式，手动调试用）
python dsh_connect.py --check            # 自检：依赖 + 配置 + 工具注册
python dsh_connect.py --readonly         # 只读模式启动（禁用交易工具）
python dsh_connect.py --print-config     # 打印 DSH 配置 JSON（自动读取 .env）
```

---

## 架构概览

```
autotrade/
├── webui.py                            # Streamlit WebUI 入口
├── run_auto.py                         # 自动交易守护进程入口
├── mcp_server.py                       # MCP Server（13 个工具，stdio 传输）
├── dsh_connect.py                     # DSH 接入辅助脚本（配置/自检/启动）
├── dsh_mcp_config.json                 # DSH MCP 客户端配置模板
├── dsh_system_prompt.md                # DSH 系统提示词
├── accounts.json                       # 多账号配置（券商/股票池/风控）
├── .env                                # LLM 密钥 + 配置
├── tradingagents/
│   ├── agents/
│   │   ├── analysts/                   # 7 位 A 股分析师
│   │   │   ├── market_analyst.py          # 市场分析师（技术指标）
│   │   │   ├── sentiment_analyst.py       # 舆情分析师（社交情绪）
│   │   │   ├── news_analyst.py            # 新闻分析师
│   │   │   ├── fundamentals_analyst.py     # 基本面分析师
│   │   │   ├── policy_analyst.py          # 政策分析师
│   │   │   ├── hotmoney_analyst.py        # 游资追踪（龙虎榜/北向/融资融券）
│   │   │   └── unlock_analyst.py          # 解禁监控
│   │   ├── researchers/                # 多空研究员（辩论）
│   │   ├── risk_mgmt/                  # 风控辩论（激进/保守/中立）
│   │   ├── managers/                   # 研究经理 + 组合经理
│   │   └── trader/                     # 交易员
│   ├── graph/                          # LangGraph 多代理编排
│   ├── auto_trader.py                  # 自动交易闭环（盘前/盘中/盘后）
│   ├── execution.py                    # 下单执行管道（规则→风控→券商）
│   ├── strategy.py                     # 盯盘策略引擎（止损/止盈/移动止损）
│   ├── monitor.py                      # 盯盘监控（分钟级巡检 + 异动检测）
│   ├── open_orders.py                  # 挂单看护（成交确认 / 超时撤单）
│   ├── review.py                       # 持仓复查（周报/月报自动复盘）
│   ├── scheduler.py                    # A 股交易日历调度器
│   ├── notifier.py                     # 钉钉告警通知
│   ├── process_lock.py                 # 进程锁（防同账号双开）
│   ├── broker/
│   │   ├── paper.py                    # 模拟盘
│   │   ├── qmt.py                      # miniQMT 实盘
│   │   ├── easytrader_broker.py        # 同花顺通用客户端实盘
│   │   ├── xiadan_guard.py             # 下单客户端进程守护
│   │   ├── base.py                     # 券商抽象接口
│   │   ├── models.py                   # 数据模型（Order/Position/Trade）
│   │   ├── fees.py                     # A 股手续费计算
│   │   └── path_helper.py              # 客户端路径辅助
│   ├── dataflows/
│   │   ├── quote_sources.py            # 多源行情（腾讯/新浪/东财/通达信）
│   │   ├── trading_calendar.py         # A 股交易日历
│   │   ├── akshare_data.py             # akshare 数据层
│   │   ├── pytdx_source.py             # 通达信数据源
│   │   ├── ashare_symbol_utils.py      # A 股代码归一化
│   │   ├── interface.py                # 供应商路由
│   │   └── market_data_validator.py     # 行情数据校验
│   ├── rules/                          # 交易规则 + 风控控制器
│   ├── llm_clients/                    # 多 LLM provider 客户端
│   └── ui/
│       ├── pages/                      # 9 个 WebUI 页面
│       ├── theme.py                    # DeepSeek 风格深色主题
│       ├── health_check.py             # 数据源健康探活
│       ├── screener.py                 # 选股引擎
│       ├── charts.py                   # 图表组件
│       ├── store.py                    # 净值/状态存储
│       └── data.py                     # UI 数据层
├── cli/                                # CLI 交互界面
├── scripts/                            # 辅助脚本
│   ├── check_vendors.py               # 多源行情可用性验证
│   ├── diag_pages.py                   # WebUI 页面诊断
│   └── smoke_structured_output.py      # 结构化输出冒烟测试
└── tests/                              # 测试套件
```

---

## 账号配置

`accounts.json` 支持多账号，每账号独立配置券商通道、股票池、风控阈值与盯盘策略：

```json
{
  "accounts": [
    {
      "name": "pingan",
      "comment": "平安证券（同花顺通用客户端通道）",
      "broker_settings": {
        "broker": "easytrader",
        "easytrader_client": "universal",
        "easytrader_client_path": "C:\\同花顺软件\\同花顺"
      },
      "watchlist": ["600519", "000858", "601318", "300750", "600036"],
      "focus_max": 3,
      "screening_enabled": true,
      "min_order_value": 5000,
      "large_order_confirm_value": 50000,
      "order_fill_timeout_min": 15,
      "strategy": {
        "stop_loss_pct": 0.07,
        "take_profit_pct": 0.15,
        "trailing_stop_pct": 0.08,
        "ma_cross_exit": false,
        "max_hold_days": null
      },
      "risk": {
        "max_position_pct": 0.20,
        "daily_loss_limit_pct": 0.03,
        "max_daily_orders": 10
      }
    }
  ]
}
```

| 字段 | 说明 | 默认值 |
|------|------|--------|
| `name` | 账号标识（唯一） | 必填 |
| `broker_settings.broker` | 券商类型：`paper` / `qmt` / `easytrader` | `paper` |
| `watchlist` | 基础股票池（6 位 A 股代码） | `[]` |
| `focus_max` | 盘前 LLM 筛选后的当日重点数量 | `3` |
| `screening_enabled` | 是否启用盘前 LLM 筛选 | `true` |
| `min_order_value` | 低于该金额的差额不下单（防折腾） | `5000` |
| `large_order_confirm_value` | ≥ 该金额需人工批准 | `50000` |
| `order_fill_timeout_min` | 挂单超时撤单（分钟） | `15` |
| `strategy` | 盯盘退出策略（见上文） | — |
| `risk` | RiskController 参数 | — |

### Rating → 仓位映射

多代理分析图输出的五级评级自动映射为目标仓位：

| Rating | 动作 | 默认目标仓位 |
|--------|------|-------------|
| Buy | 买入 | 12% |
| Overweight | 增持 | 6% |
| Hold | 持有 | 不动 |
| Underweight | 减持 | 当前持仓减半 |
| Sell | 清仓 | 0%（T+1 可用部分） |

---

## LLM 配置

通过 `.env` 配置，支持多种 provider：

```bash
# .env 示例（DeepSeek 部署）
TRADINGAGENTS_LLM_PROVIDER=deepseek
TRADINGAGENTS_DEEP_THINK_LLM=deepseek-v4-pro
TRADINGAGENTS_QUICK_THINK_LLM=deepseek-v4-flash
TRADINGAGENTS_OUTPUT_LANGUAGE=Chinese
```

支持的 provider：OpenAI / Google / Anthropic / xAI / DeepSeek / Qwen / GLM / MiniMax / OpenRouter / Ollama / Azure OpenAI / AWS Bedrock / 任何 OpenAI 兼容端点。

---

## 状态持久化

守护进程和 MCP Server 共享 `~/.tradingagents/` 下的状态文件，盘中进程重启不丢当日状态：

| 路径 | 说明 |
|------|------|
| `state/<account>_analyzed.json` | 当日已分析标的（防重复分析，省 LLM 成本） |
| `state/<account>_day_start.json` | 当日开盘基线（日亏熔断不因重启失效） |
| `state/open_orders/<account>.json` | 挂单看护列表（跨重启对账） |
| `approvals/<account>.json` | 大额审批队列（跨进程复用） |
| `auto/<account>_<YYYYMMDD>.json` | 每日盘后明细（周/月报复盘数据源） |
| `auto/review/<account>_<kind>_<date>.md` | 周/月报复盘报告 |
| `reports/<symbol>_*/complete_report.md` | 单标的分析报告 |
| `daemon_heartbeat.json` | 守护进程心跳（UI 健康页读取） |
| `memory/trading_memory.md` | 决策日志与反思 |

---

## 测试

```bash
pytest                              # 全量测试
pytest -m unit                      # 仅快速单元测试
pytest tests/test_mcp_server.py     # MCP Server 测试
pytest tests/test_xiadan_guard.py   # 下单守卫测试
pytest tests/test_open_orders.py     # 挂单看护测试
pytest tests/test_review.py         # 持仓复查测试
pytest tests/test_state_persistence.py  # 状态持久化测试
```

---

## 可复现性

本系统由 LLM 驱动，同一标的和日期的两次运行结果可能不同。这是研究工具的固有特性，非缺陷。设置 `TRADINGAGENTS_TEMPERATURE=0.0` 可降低变异（对遵守 temperature 的模型有效）。
