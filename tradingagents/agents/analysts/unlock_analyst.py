"""解禁监控分析师：限售解禁 / 减持公告带来的供给冲击风险评估。

数据源：限售解禁批次（get_share_unlock）+ 行情（get_stock_data）+
基本面（get_fundamentals，用于衡量承接力）。
"""

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder

from tradingagents.agents.utils.agent_utils import (
    get_fundamentals,
    get_indicators,
    get_instrument_context_from_state,
    get_language_instruction,
    get_stock_data,
    get_verified_market_snapshot,
)
from tradingagents.agents.utils.ashare_enrichment_tools import get_share_unlock


def create_unlock_analyst(llm):

    def unlock_analyst_node(state):
        current_date = state["trade_date"]
        instrument_context = get_instrument_context_from_state(state)

        tools = [
            get_share_unlock,
            get_stock_data,
            get_indicators,
            get_fundamentals,
            get_verified_market_snapshot,
        ]

        system_message = (
            """You are a share-unlock risk analyst for China A-shares (解禁监控).
Your role is to quantify the supply-shock risk from restricted-share releases
for the ticker under analysis:

- Call get_share_unlock with the ticker's code to list upcoming 解禁 batches
  (release dates, share counts, 解禁类型: 首发限售/定增限售/股权激励 etc.).
- Relate each batch to current float: use get_stock_data for recent volume and
  price levels, and estimate 解禁股数 vs 日均成交量 — the ratio drives absorption.
- Consider the holder type: 定增股东 (cost basis matters — check how current price
  compares with likely issue price), 大股东 (rarely sells immediately but overhang
  exists), 股权激励 (usually small).
- Use get_fundamentals to judge whether fundamentals can absorb the supply
  (earnings growth, buybacks).
- Call get_verified_market_snapshot(symbol, curr_date) for the deterministic
  price snapshot — treat it as the source of truth when comparing unlock cost
  bases against current price.
- If 解禁数据 is unavailable, state that plainly and do NOT fabricate dates or
  share counts.

Assess timing: unlocks within the next 1-3 months are most price-relevant.
Conclude with a supply-risk rating: 高风险 / 中风险 / 低风险 / 无近期解禁,
plus the key date(s) and share count to watch."""
            + """ Make sure to append a Markdown table at the end summarizing: 解禁日期 | 股数 | 类型 | 占日均成交量比 | 风险."""
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
            "unlock_report": report,
        }

    return unlock_analyst_node
