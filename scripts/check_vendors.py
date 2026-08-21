"""临时脚本：验证多源行情真实可用（腾讯/新浪/东财 K 线）。"""
from tradingagents.dataflows import quote_sources as qs

q = qs.get_quote("600519", vendors=("tencent",))
print("腾讯实时:", q["name"], q["price"], f"{q['pct']:+.2f}%")

q2 = qs.get_quote("000001", vendors=("sina",))
print("新浪实时:", q2["name"], q2["price"], f"{q2['pct']:+.2f}%")

k = qs.get_kline("600519", 30, vendors=("tencent",))
print("腾讯K线:", len(k), "根, 最新收盘", k.iloc[-1]["close"])

k2 = qs.get_kline("000001", 30, vendors=("sina",))
print("新浪K线:", len(k2), "根, 最新收盘", k2.iloc[-1]["close"])

k3 = qs.get_kline("300750", 30)   # 默认链路 东财→腾讯→新浪（东财挂了自动切换）
print("默认链路K线(宁德时代):", len(k3), "根, 来源", k3.attrs.get("source", "?"), "最新收盘", k3.iloc[-1]["close"] if len(k3) else "N/A")
