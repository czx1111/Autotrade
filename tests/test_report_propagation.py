"""A-share extension reports must flow into the debate and risk-decision prompts.

回归背景：policy/hotmoney/unlock 三个分析师的报告写入了 state，但 Bull/Bear
Researcher 与三个 Risk Debator 的 prompt 只读原版 4 报告——A 股特色分析
（政策面/资金面/解禁面）从未进入决策链。此测试保证 5 个下游 agent 的
prompt 里实际包含这三份报告的内容。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from tradingagents.agents.researchers.bear_researcher import create_bear_researcher
from tradingagents.agents.researchers.bull_researcher import create_bull_researcher
from tradingagents.agents.risk_mgmt.aggressive_debator import create_aggressive_debator
from tradingagents.agents.risk_mgmt.conservative_debator import create_conservative_debator
from tradingagents.agents.risk_mgmt.neutral_debator import create_neutral_debator
from tradingagents.agents.utils.agent_utils import get_extended_reports_block

POLICY_MARK = "POLICY-REGULATION-FINDING-123"
HOTMONEY_MARK = "HOTMONEY-DRAGON-TIGER-FINDING-456"
UNLOCK_MARK = "UNLOCK-LOCKUP-EXPIRY-FINDING-789"


def _capture_llm(captured):
    llm = MagicMock()
    llm.invoke.side_effect = lambda prompt: (
        captured.update(prompt=prompt),
        MagicMock(content="ok"),
    )[1]
    return llm


def _research_state() -> dict:
    return {
        "company_of_interest": "600519",
        "market_report": "market report body",
        "sentiment_report": "sentiment report body",
        "news_report": "news report body",
        "fundamentals_report": "fundamentals report body",
        "policy_report": f"policy analysis with {POLICY_MARK}",
        "hotmoney_report": f"hotmoney analysis with {HOTMONEY_MARK}",
        "unlock_report": f"unlock analysis with {UNLOCK_MARK}",
        "investment_debate_state": {
            "history": "", "bull_history": "", "bear_history": "",
            "current_response": "", "judge_decision": "", "count": 0,
        },
    }


def _risk_state() -> dict:
    state = _research_state()
    state["trader_investment_plan"] = "buy 600519 at market"
    state["risk_debate_state"] = {
        "history": "", "aggressive_history": "", "conservative_history": "",
        "neutral_history": "", "latest_speaker": "",
        "current_aggressive_response": "",
        "current_conservative_response": "",
        "current_neutral_response": "",
        "judge_decision": "", "count": 0,
    }
    return state


class TestGetExtendedReportsBlock:
    def test_renders_all_three_reports(self):
        block = get_extended_reports_block(_research_state())
        assert POLICY_MARK in block
        assert HOTMONEY_MARK in block
        assert UNLOCK_MARK in block
        assert "Policy Analysis Report" in block
        assert "Hot-money Flow Report" in block
        assert "Share Unlock Report" in block

    def test_empty_reports_filtered(self):
        state = _research_state()
        state["policy_report"] = "   \n  "       # 空白不算有报告
        state["unlock_report"] = ""
        block = get_extended_reports_block(state)
        assert POLICY_MARK not in block
        assert UNLOCK_MARK not in block
        assert HOTMONEY_MARK in block             # 仅剩游资报告

    def test_missing_keys_return_empty(self):
        # US/crypto 管线：state 里根本没有这三个 key
        state = {"market_report": "x"}
        assert get_extended_reports_block(state) == ""


class TestResearchersReceiveReports:
    def test_bull_prompt_contains_all_extension_reports(self):
        captured = {}
        create_bull_researcher(_capture_llm(captured))(_research_state())
        prompt = captured["prompt"]
        for mark in (POLICY_MARK, HOTMONEY_MARK, UNLOCK_MARK):
            assert mark in prompt, f"bull researcher prompt missing {mark}"

    def test_bear_prompt_contains_all_extension_reports(self):
        captured = {}
        create_bear_researcher(_capture_llm(captured))(_research_state())
        prompt = captured["prompt"]
        for mark in (POLICY_MARK, HOTMONEY_MARK, UNLOCK_MARK):
            assert mark in prompt, f"bear researcher prompt missing {mark}"

    def test_empty_extension_reports_leave_no_noise(self):
        # 空报告时 prompt 仍应工作且不出现空标签行
        captured = {}
        state = _research_state()
        state["policy_report"] = ""
        state["hotmoney_report"] = ""
        state["unlock_report"] = ""
        create_bull_researcher(_capture_llm(captured))(state)
        assert "Policy Analysis Report" not in captured["prompt"]
        assert "Hot-money Flow Report" not in captured["prompt"]


class TestRiskDebatorsReceiveReports:
    def test_aggressive_prompt_contains_all_extension_reports(self):
        captured = {}
        create_aggressive_debator(_capture_llm(captured))(_risk_state())
        prompt = captured["prompt"]
        for mark in (POLICY_MARK, HOTMONEY_MARK, UNLOCK_MARK):
            assert mark in prompt, f"aggressive debator prompt missing {mark}"

    def test_conservative_prompt_contains_all_extension_reports(self):
        captured = {}
        create_conservative_debator(_capture_llm(captured))(_risk_state())
        prompt = captured["prompt"]
        for mark in (POLICY_MARK, HOTMONEY_MARK, UNLOCK_MARK):
            assert mark in prompt, f"conservative debator prompt missing {mark}"

    def test_neutral_prompt_contains_all_extension_reports(self):
        captured = {}
        create_neutral_debator(_capture_llm(captured))(_risk_state())
        prompt = captured["prompt"]
        for mark in (POLICY_MARK, HOTMONEY_MARK, UNLOCK_MARK):
            assert mark in prompt, f"neutral debator prompt missing {mark}"

    def test_debators_keep_core_reports_and_trader_plan(self):
        # 注入扩展报告的同时，原有的 4 报告与交易员决策不丢
        captured = {}
        create_neutral_debator(_capture_llm(captured))(_risk_state())
        prompt = captured["prompt"]
        assert "market report body" in prompt
        assert "fundamentals report body" in prompt
        assert "buy 600519 at market" in prompt
