"""多源行情真实可用性验证（腾讯/新浪/东财 K 线）。

标记为 integration：依赖外部行情接口，CI 中默认跳过。
"""
import pytest

from tradingagents.dataflows import quote_sources as qs


@pytest.mark.integration
class TestVendorQuotes:
    """验证各行情源能正常返回数据。"""

    def test_tencent_quote(self):
        q = qs.get_quote("600519", vendors=("tencent",))
        assert q["name"]
        assert isinstance(q["price"], (int, float))

    def test_sina_quote(self):
        q = qs.get_quote("000001", vendors=("sina",))
        assert q["name"]
        assert isinstance(q["price"], (int, float))

    def test_tencent_kline(self):
        k = qs.get_kline("600519", 30, vendors=("tencent",))
        assert len(k) > 0
        assert "close" in k.columns

    def test_sina_kline(self):
        k = qs.get_kline("000001", 30, vendors=("sina",))
        assert len(k) > 0
        assert "close" in k.columns

    def test_default_vendor_chain(self):
        """默认链路 东财→腾讯→新浪（东财挂了自动切换）。"""
        k = qs.get_kline("300750", 30)
        assert len(k) > 0
        assert "close" in k.columns
