"""政策分析师：宏观政策 / 监管动态 / 产业政策对标的与大盘的影响。

数据源：全球/宏观新闻（get_global_news）+ 宏观指标（get_macro_indicators）
+ A 股市场快照（get_ashare_market_snapshot）。
"""

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    get_global_news,
    get_indicators,
    get_instrument_context_from_state,
    get_language_instruction,
    get_macro_indicators,
    get_verified_market_snapshot,
)
from tradingagents.agents.utils.ashare_enrichment_tools import get_ashare_market_snapshot


def create_policy_analyst(llm):

    def policy_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state)

        tools = [
            get_global_news,
            get_macro_indicators,
            get_ashare_market_snapshot,
            get_indicators,
            get_verified_market_snapshot,
        ]

        system_message = (
            """You are a policy analyst covering China A-shares. Your role is to assess how
policy, regulation, and macro developments affect the ticker under analysis:

- 货币政策: rate cuts/LOMO/RRR moves, liquidity conditions
- 财政与产业政策: sector subsidies, consumption stimulus, infrastructure programs
- 监管动态: crackdowns, IPO/refinancing rules, trading rules changes
- 地缘与外部: trade tensions, tariffs, sanctions affecting supply chains
- 宏观指标: PMI, CPI/PPI, credit growth, from get_macro_indicators

Working method:
1. Call get_global_news for recent macro/policy headlines (lookback ~1 week).
2. Call get_macro_indicators for the key macro series.
3. Call get_ashare_market_snapshot for how the broad market is pricing policy today.
4. Optionally call get_indicators if you need to check how the ticker reacted to past policy windows.
5. Call get_verified_market_snapshot(symbol, curr_date) for the deterministic price
   snapshot — treat it as the source of truth when relating policy to price levels.

Weigh each item by: likelihood, magnitude, and time horizon (immediate vs structural).
Distinguish clearly between confirmed policy and market speculation/rumor.
Conclude with a policy bias: supportive / neutral / restrictive, and which
price-relevant catalysts to watch next."""
            + """ Make sure to append a Markdown table at the end summarizing: 政策事件 | 方向影响 | 力度 | 时间窗口."""
            + get_language_instruction()
        )

        prompt = ChatPromptTemplate.from_messages(
            [
                (
                    "system",
                    "You are a helpful AI assistant, collaborating with other assistants."
                    " Use the provided tools to progress towards answering the question."
                    " If you are unable to fully answer, that's OK; another assistant with different tools"
                    " will help where you left off. Execute what you can to make progress."
                    " If you or any other assistant has the FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** or deliverable,"
                    " prefix your response with FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL** so the team knows to stop."
                    " You have access to the following tools: {tool_names}."
                    " Today's date is {current_date}; treat it as 'now' for all analysis and tool-call date ranges. {instrument_context}\n"
                    "{system_message}",
                ),
                MessagesPlaceholder(variable_name="messages"),
            ]
        )

        prompt = prompt.partial(system_message=system_message)
        prompt = prompt.partial(tool_names=", ".join([tool.name for tool in tools]))
        prompt = prompt.partial(current_date=current_date)
        prompt = prompt.partial(instrument_context=instrument_context)

        chain = prompt | llm.bind_tools(tools)

        result = chain.invoke(state["messages"])

        report = ""
        if len(result.tool_calls) == 0:
            report = result.content

        return {
            "messages": [result],
            "policy_report": report,
        }

    return policy_analyst_node
