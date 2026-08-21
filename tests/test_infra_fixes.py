"""Tests for the three infra fixes: quote routing, A-share identity, shared snapshot.

覆盖：
1. fetch_quotes_ashare 新路由 —— 小列表不碰东财、大列表 EM 熔断冷却
2. resolve_instrument_identity A 股分支（国产源解析名称/行业/交易所）
3. 全部工具型分析师共享验证快照（事实层信息共享）
"""

from __future__ import annotations

from unittest import mock

import pytest

import tradingagents.auto_trader as at
from tradingagents.agents.utils import agent_utils
from tradingagents.auto_trader import Quote, fetch_quotes_ashare


def _quote(price=100.0, name="测试股"):
    return Quote(price=price, prev_close=99.0, name=name)


class TestQuoteRouting:
    """小列表直连多源；大列表 EM 批量 + 失败熔断。"""

    def _reset(self):
        at._em_failed_at = None

    def test_small_list_never_touches_em(self):
        """≤10 只：直接逐票多源，完全不请求东财快照（EM 挂时不再等 6s 超时）。"""
        self._reset()
        with mock.patch.object(at, "_quotes_from_em_snapshot") as em, \
             mock.patch.object(at, "_quote_from_multisource", side_effect=lambda c: _quote()) as ms:
            quotes = fetch_quotes_ashare(["600519", "300750", "601318"])
        em.assert_not_called()
        assert len(quotes) == 3
        assert ms.call_count == 3

    def test_large_list_uses_em_bulk_then_fills_gaps(self):
        """>10 只：EM 批量为主，缺码逐票补齐。"""
        self._reset()
        big = [f"6000{i:02d}" for i in range(12)]

        def em_snapshot(wanted):
            # EM 只拿到前 6 只
            return {c: _quote() for c in sorted(wanted)[:6]}

        with mock.patch.object(at, "_quotes_from_em_snapshot", side_effect=em_snapshot), \
             mock.patch.object(at, "_quote_from_multisource", side_effect=lambda c: _quote()) as ms:
            quotes = fetch_quotes_ashare(big)
        assert len(quotes) == 12
        assert ms.call_count == 6          # 只补 EM 没拿到的 6 只

    def test_em_failure_enters_cooldown(self):
        """EM 失败 → 记录熔断时间；冷却期内大列表也直接走逐票。"""
        import sys
        import types

        self._reset()
        big = [f"6000{i:02d}" for i in range(12)]

        # 让真实 _quotes_from_em_snapshot 里的 akshare 调用抛错
        broken_ak = types.ModuleType("akshare")

        def _boom(*a, **k):
            raise ConnectionError("EM down")

        broken_ak.stock_zh_a_spot_em = _boom

        with mock.patch.dict(sys.modules, {"akshare": broken_ak}), \
             mock.patch.object(at, "_quote_from_multisource", side_effect=lambda c: _quote()):
            quotes = fetch_quotes_ashare(big)
        assert len(quotes) == 12
        assert at._em_failed_at is not None   # 熔断已记录

        # 第二次调用：冷却期内不再碰 EM（akshare 抛错也不该被调用）
        with mock.patch.dict(sys.modules, {"akshare": broken_ak}), \
             mock.patch.object(at, "_quotes_from_em_snapshot") as em2, \
             mock.patch.object(at, "_quote_from_multisource", side_effect=lambda c: _quote()):
            fetch_quotes_ashare(big)
        em2.assert_not_called()
        self._reset()

    def test_cooldown_expiry_retries_em(self):
        """冷却到期后大列表恢复尝试 EM。"""
        import time as _time
        at._em_failed_at = _time.time() - at._EM_COOLDOWN_SECONDS - 1
        big = [f"6000{i:02d}" for i in range(11)]
        with mock.patch.object(
            at, "_quotes_from_em_snapshot", return_value={c: _quote() for c in big},
        ) as em, mock.patch.object(at, "_quote_from_multisource") as ms:
            fetch_quotes_ashare(big)
        em.assert_called_once()
        ms.assert_not_called()               # EM 全量命中，无需逐票
        self._reset()

    def test_all_sources_failed_notifies(self):
        self._reset()
        with mock.patch.object(at, "_quote_from_multisource", return_value=None), \
             mock.patch.object(at, "_notify_quote_failure") as notify:
            quotes = fetch_quotes_ashare(["600519"])
        assert quotes == {}
        notify.assert_called_once_with(["600519"])

    def test_empty_input(self):
        assert fetch_quotes_ashare([]) == {}


class TestAshareIdentity:
    def test_ashare_code_uses_domestic_sources(self, monkeypatch):
        """A 股代码：腾讯/新浪取名称 + akshare 取行业，不走 yfinance。"""
        monkeypatch.setattr(
            agent_utils, "yf", mock.MagicMock(),
        )
        monkeypatch.setattr(
            "tradingagents.dataflows.quote_sources.get_quote",
            lambda code: {"name": "贵州茅台", "price": 1300.0},
        )

        info_df = _info_df({"行业": "白酒", "股票简称": "贵州茅台"})
        fake_ak = mock.MagicMock()
        fake_ak.stock_individual_info_em.return_value = info_df
        monkeypatch.setitem(__import__("sys").modules, "akshare", fake_ak)

        agent_utils.resolve_instrument_identity.cache_clear()
        identity = agent_utils.resolve_instrument_identity("600519")
        assert identity["company_name"] == "贵州茅台"
        assert identity["industry"] == "白酒"
        assert identity["exchange"] == "SH"
        agent_utils.resolve_instrument_identity.cache_clear()

    def test_identity_survives_source_failures(self, monkeypatch):
        """全部国产源失败：仍返回交易所（来自代码解析），不抛异常。"""
        monkeypatch.setattr(
            "tradingagents.dataflows.quote_sources.get_quote",
            lambda code: (_ for _ in ()).throw(ConnectionError("down")),
        )
        # akshare 导入失败：注入一个 import 时抛错的假模块
        import sys
        import types

        class _BrokenAk(types.ModuleType):
            def __getattr__(self, item):
                raise ImportError("akshare broken")

        monkeypatch.setitem(sys.modules, "akshare", _BrokenAk("akshare"))
        agent_utils.resolve_instrument_identity.cache_clear()
        identity = agent_utils.resolve_instrument_identity("600519")
        assert identity.get("exchange") == "SH"
        assert "company_name" not in identity
        agent_utils.resolve_instrument_identity.cache_clear()

    def test_non_ashare_still_uses_yfinance(self, monkeypatch):
        """非 A 股代码走 yfinance 原路径。"""
        fake_yf = mock.MagicMock()
        fake_yf.Ticker.return_value.info = {
            "longName": "NVIDIA", "sector": "Technology",
        }
        monkeypatch.setattr(agent_utils, "yf", fake_yf)
        agent_utils.resolve_instrument_identity.cache_clear()
        identity = agent_utils.resolve_instrument_identity("NVDA")
        assert identity["company_name"] == "NVIDIA"
        fake_yf.Ticker.assert_called_once_with("NVDA")
        agent_utils.resolve_instrument_identity.cache_clear()


def _info_df(table: dict):
    import pandas as pd

    return pd.DataFrame({"item": list(table), "value": list(table.values())})


class TestSharedVerifiedSnapshot:
    """全部工具型分析师的工具箱必须包含验证快照（共享事实层）。"""

    def _tool_names(self, create_fn, state) -> set[str]:
        from langchain_core.runnables import RunnableLambda

        captured = {}

        def fake_bind_tools(tools):
            captured["tools"] = tools
            # 返回合法 Runnable：chain.invoke 得到一个无工具调用的消息对象
            return RunnableLambda(
                lambda _msgs: mock.MagicMock(content="", tool_calls=[])
            )

        fake_llm = mock.MagicMock()
        fake_llm.bind_tools.side_effect = fake_bind_tools
        try:
            create_fn(fake_llm)(state)
        except Exception:
            pass  # 节点后半段（report 渲染）可能失败，toolkit 捕获不受影响
        return {t.name for t in captured.get("tools", [])}

    def test_all_tool_analysts_have_verified_snapshot(self):
        from tradingagents.agents.analysts.fundamentals_analyst import create_fundamentals_analyst
        from tradingagents.agents.analysts.hotmoney_analyst import create_hotmoney_analyst
        from tradingagents.agents.analysts.news_analyst import create_news_analyst
        from tradingagents.agents.analysts.policy_analyst import create_policy_analyst
        from tradingagents.agents.analysts.unlock_analyst import create_unlock_analyst

        state = {
            "trade_date": "2026-08-19",
            "company_of_interest": "600519",
            "messages": [],
        }
        for create_fn in (
            create_fundamentals_analyst,
            create_news_analyst,
            create_policy_analyst,
            create_hotmoney_analyst,
            create_unlock_analyst,
        ):
            names = self._tool_names(create_fn, state)
            assert "get_verified_market_snapshot" in names, (
                f"{create_fn.__module__} 缺少验证快照工具; 实际 tools={names}"
            )
