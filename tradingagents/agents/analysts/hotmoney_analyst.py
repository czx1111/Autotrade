"""游资追踪分析师：龙虎榜席位 / 北向资金 / 融资融券的“聪明钱”动向。

数据源：龙虎榜（get_dragon_tiger_list）、北向资金（get_northbound_flow）、
融资融券（get_margin_trading）、个股舆情（get_social_sentiment）。
"""

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    get_indicators,
    get_instrument_context_from_state,
    get_language_instruction,
    get_stock_data,
    get_verified_market_snapshot,
)
from tradingagents.agents.utils.ashare_enrichment_tools import (
    get_dragon_tiger_list,
    get_margin_trading,
    get_northbound_flow,
    get_social_sentiment,
)


def create_hotmoney_analyst(llm):

    def hotmoney_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state)

        tools = [
            get_dragon_tiger_list,
            get_northbound_flow,
            get_margin_trading,
            get_social_sentiment,
            get_stock_data,
            get_indicators,
            get_verified_market_snapshot,
        ]

        system_message = (
            """You are a "smart money" flow analyst tracking hot capital in China A-shares (游资追踪).
Your role is to detect institutional and speculative positioning for the ticker under analysis:

- 龙虎榜 (dragon-tiger list): whether the ticker appeared on recent lists, which seats
  (famous 游资 seats vs institutional专用) bought/sold, net amounts, and the typical
  holding period of those seats. Call get_dragon_tiger_list with recent dates
  (yyyy-mm-dd strings) around the trade date.
- 北向资金: whether overseas capital is accumulating or distributing, via
  get_northbound_flow.
- 杠杆资金: margin balance trend for the stock via get_margin_trading.
- 散户情绪: retail heat via get_social_sentiment — extreme retail euphoria
  often marks short-term tops for 游资 stocks.
- Use get_stock_data / get_indicators to relate flows to price/volume action
  (e.g. 涨停 continuity, volume spikes, turnover rate).
- Call get_verified_market_snapshot(symbol, curr_date) for the deterministic
  price/indicator snapshot — treat it as the source of truth for exact price
  levels and indicator values in your report.

Judge the style of capital involved: 机构趋势 vs 游资题材 vs 散户接力, because
each implies a different risk profile and holding period. Flag follow-the-money
risks: one-day 游资 in-and-out, seat divergence, or northbound selling into strength.
Conclude with a money-flow verdict: 净流入强 / 温和流入 / 中性 / 流出."""
            + """ Make sure to append a Markdown table at the end summarizing: 资金类型 | 方向 | 强度 | 依据."""
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
            "hotmoney_report": report,
        }

    return hotmoney_analyst_node
