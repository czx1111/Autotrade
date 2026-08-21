"""DeepSeek 风格深色主题：设计规范色板 + 全局 CSS 注入。

色彩体系（与 .streamlit/config.toml 保持一致）：
对齐 DeepSeek Platform 暗色设计语言：
- 主背景 #151517 (neutral-bluish-950) / 侧边栏 #1B1B1C / 卡片 #353638
- 强调色 #4D93F8 (DeepSeek Blue 450)
- 涨 #F25A5A / 跌 #4ED17E（A 股惯例：红涨绿跌）
- 主文字 #F9FAFB / 辅助 #979DA6
品牌色：平安 #F26B1F / 银河 #C8102E
"""

from __future__ import annotations

import streamlit as st

# ── 色板（图表与 HTML 内联样式共用） ──
# 对齐 DeepSeek neutral-bluish 暗色色阶
BG = "#151517"          # neutral-bluish-950 — 主背景
SIDEBAR_BG = "#1B1B1C"  # neutral-bluish-900 — 侧边栏
CARD = "#353638"        # neutral-bluish-800 — 卡片
CARD_HOVER = "#43454A"  # neutral-bluish-750 — 悬浮
BORDER = "rgba(255,255,255,.08)"   # 边框（DeepSeek border-inverted2）
BORDER_STRONG = "rgba(255,255,255,.12)"  # 加粗边框

ACCENT = "#4D93F8"      # DeepSeek blue-450 — 强调/链接
ACCENT_LIGHT = "#679EFE"  # DeepSeek blue-400
ACCENT_DARK = "#3964FE"   # DeepSeek blue-500

UP = "#F25A5A"          # A 股：红涨 (DeepSeek red-400)
DOWN = "#4ED17E"        # 绿跌 (DeepSeek green-400)
TEXT = "#F9FAFB"        # neutral-bluish-50 — 主文字
MUTED = "#979DA6"       # neutral-bluish-500 — 辅助文字
MUTED_LIGHT = "#ADB2B8" # neutral-bluish-400

PINGAN = "#F26B1F"
GALAXY = "#C8102E"

PLOTLY_DARK = {
    "paper_bgcolor": BG,
    "plot_bgcolor": BG,
    "font": {"color": TEXT, "family": "Inter, sans-serif"},
    "hoverlabel": {"bgcolor": CARD},
    "xaxis": {"gridcolor": "rgba(255,255,255,.06)"},
    "yaxis": {"gridcolor": "rgba(255,255,255,.06)"},
}

CSS = f"""
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

/* ═══════════════════════════════════════════════════════════════
   全局基础
   ═══════════════════════════════════════════════════════════════ */
html, body, .stApp, .stMarkdown, p, span, label, div {{
    font-family: 'Inter', 'Microsoft YaHei', system-ui, -apple-system, sans-serif;
}}
code, pre, .stCodeBlock, .stCodeBlock pre {{
    font-family: 'SF Mono', 'JetBrains Mono', Consolas, monospace !important;
}}

/* 主背景：纯色（DeepSeek 风格不使用渐变背景） */
.stApp {{
    background: {BG};
}}

/* ═══════════════════════════════════════════════════════════════
   侧边栏 — 对齐 DeepSeek 左侧导航
   ═══════════════════════════════════════════════════════════════ */
section[data-testid="stSidebar"] {{
    background: {SIDEBAR_BG};
    border-right: 1px solid {BORDER};
}}
/* 导航项：圆角矩形菜单项，激活态高亮背景（对齐 ds-menu-option） */
section[data-testid="stSidebar"] .stRadio > div {{
    gap: 2px;
}}
section[data-testid="stSidebar"] .stRadio label {{
    border-radius: 14px;
    padding: 8px 16px !important;
    margin-bottom: 2px;
    transition: background .15s ease;
    font-size: 14px;
    color: {TEXT};
}}
section[data-testid="stSidebar"] .stRadio label:hover {{
    background: rgba(255,255,255,.04);
}}
section[data-testid="stSidebar"] .stRadio label[data-checked="true"],
section[data-testid="stSidebar"] .stRadio input:checked + * {{
    background: {CARD_HOVER};
    font-weight: 600;
}}

/* ═══════════════════════════════════════════════════════════════
   卡片 / Metric — 对齐 DeepSeek 卡片（16px 圆角，纯色背景）
   ═══════════════════════════════════════════════════════════════ */
div[data-testid="stMetric"], .ta-card {{
    background: {CARD};
    border: none;
    border-radius: 16px;
    padding: 16px 18px !important;
    box-shadow: none;
    transition: background .15s ease;
}}
div[data-testid="stMetric"]:hover {{
    background: {CARD_HOVER};
}}
div[data-testid="stMetricLabel"] p {{
    color: {MUTED} !important;
    font-size: 13px;
    font-weight: 500;
    letter-spacing: 0;
}}
div[data-testid="stMetricValue"] {{
    color: {TEXT} !important;
    font-weight: 600;
    font-size: 24px;
}}
div[data-testid="stMetricDelta"] > div {{
    justify-content: flex-start;
    font-size: 12px;
}}

/* ═══════════════════════════════════════════════════════════════
   标签页 Tabs — 对齐 DeepSeek 顶部 tab
   ═══════════════════════════════════════════════════════════════ */
.stTabs [data-baseweb="tab-list"] {{
    gap: 4px;
    border-bottom: 1px solid {BORDER};
    background: transparent;
}}
.stTabs [data-baseweb="tab"] {{
    background: transparent;
    border-radius: 8px 8px 0 0;
    padding: 8px 16px;
    font-size: 14px;
    font-weight: 500;
    color: {MUTED};
    transition: all .15s ease;
}}
.stTabs [data-baseweb="tab"]:hover {{
    color: {TEXT};
    background: rgba(255,255,255,.03);
}}
.stTabs [aria-selected="true"] {{
    background: transparent !important;
    color: {TEXT} !important;
    font-weight: 600;
    box-shadow: inset 0 -2px 0 {ACCENT};
}}

/* ═══════════════════════════════════════════════════════════════
   按钮 — 对齐 DeepSeek 按钮（圆角矩形，低调边框）
   ═══════════════════════════════════════════════════════════════ */
.stButton > button {{
    border-radius: 10px;
    border: 1px solid {BORDER_STRONG};
    color: {TEXT};
    background: {CARD};
    font-size: 14px;
    font-weight: 500;
    transition: all .15s ease;
    padding: 8px 16px;
}}
.stButton > button:hover {{
    background: {CARD_HOVER};
    border-color: rgba(255,255,255,.20);
    color: {TEXT};
}}
.stButton > button[kind="primary"] {{
    background: {ACCENT};
    color: #FFFFFF;
    font-weight: 600;
    border: none;
}}
.stButton > button[kind="primary"]:hover {{
    background: {ACCENT_LIGHT};
}}

/* ═══════════════════════════════════════════════════════════════
   表格
   ═══════════════════════════════════════════════════════════════ */
.stDataFrame [data-testid="stDataFrameGrid"] {{
    border: 1px solid {BORDER};
    border-radius: 12px;
    overflow: hidden;
}}
.stDataFrame th {{
    background: {SIDEBAR_BG} !important;
    color: {MUTED} !important;
    font-size: 13px;
    font-weight: 600;
}}
.stDataFrame td {{
    font-size: 13px;
    color: {TEXT};
}}
.stDataFrame tr:hover td {{
    background: rgba(255,255,255,.03) !important;
}}

/* ═══════════════════════════════════════════════════════════════
   输入框 / 选择器
   ═══════════════════════════════════════════════════════════════ */
.stTextInput > div > div > input,
.stNumberInput > div > div > input,
.stTextArea > div > textarea {{
    background: {SIDEBAR_BG} !important;
    border: 1px solid {BORDER} !important;
    border-radius: 10px !important;
    color: {TEXT} !important;
    font-size: 14px;
}}
.stTextInput > div > div > input:focus,
.stNumberInput > div > div > input:focus,
.stTextArea > div > textarea:focus {{
    border-color: {ACCENT} !important;
    box-shadow: 0 0 0 2px rgba(77,147,248,.15) !important;
}}
.stSelectbox > div > div {{
    background: {SIDEBAR_BG} !important;
    border: 1px solid {BORDER} !important;
    border-radius: 10px !important;
}}

/* ═══════════════════════════════════════════════════════════════
   状态灯
   ═══════════════════════════════════════════════════════════════ */
.ta-dot {{
    display:inline-block; width:8px; height:8px; border-radius:50%; margin-right:8px;
}}
.ta-dot-green {{ background:{DOWN}; }}
.ta-dot-amber {{ background:#F7AD31; }}
.ta-dot-red   {{ background:{UP}; }}

/* ═══════════════════════════════════════════════════════════════
   状态栏
   ═══════════════════════════════════════════════════════════════ */
.ta-statusbar {{
    display:flex; align-items:center; gap:20px; flex-wrap:wrap;
    background: {CARD};
    border: none;
    border-radius: 12px;
    padding: 12px 18px;
    margin-bottom: 16px;
    font-size: 13px;
    color: {TEXT};
}}
.ta-statusbar .muted {{ color: {MUTED}; }}

/* ═══════════════════════════════════════════════════════════════
   品牌账号卡 — 对齐 DeepSeek 卡片风格
   ═══════════════════════════════════════════════════════════════ */
.ta-acct {{
    border-radius: 16px;
    padding: 18px 20px;
    position: relative;
    overflow: hidden;
    background: {CARD};
}}
.ta-acct::before {{
    content:''; position:absolute; left:0; top:0; bottom:0; width:3px;
    border-radius: 3px;
}}
.ta-acct.pingan::before {{ background: {PINGAN}; }}
.ta-acct.galaxy::before {{ background: {GALAXY}; }}
.ta-acct h4 {{
    margin:0 0 8px;
    color:{TEXT};
    font-size: 16px;
    font-weight: 600;
}}
.ta-acct .row {{
    display:flex; justify-content:space-between;
    padding:3px 0;
    font-size: 13px;
}}
.ta-acct .row .k {{ color:{MUTED}; }}

/* ═══════════════════════════════════════════════════════════════
   Agent 活动轨
   ═══════════════════════════════════════════════════════════════ */
.ta-agent {{
    display:flex; align-items:flex-start; gap:12px;
    padding:10px 14px;
    border-left: 2px solid {BORDER_STRONG};
    margin-bottom: 4px;
    background: {SIDEBAR_BG};
    border-radius: 0 10px 10px 0;
    transition: all .15s ease;
}}
.ta-agent:hover {{
    background: {CARD};
}}
.ta-agent.done {{
    border-left-color: {DOWN};
}}
.ta-agent.running {{
    border-left-color: {ACCENT};
    animation: ta-pulse 1.6s infinite;
}}
.ta-agent .who {{
    font-weight: 600;
    color:{TEXT};
    font-size: 14px;
}}
.ta-agent .sub {{
    color:{MUTED};
    font-size: 12px;
}}
@keyframes ta-pulse {{
    0%,100% {{ box-shadow: 0 0 0 rgba(77,147,248,0); }}
    50% {{ box-shadow: 0 0 12px rgba(77,147,248,.20); }}
}}

/* ═══════════════════════════════════════════════════════════════
   决策徽章
   ═══════════════════════════════════════════════════════════════ */
.ta-badge {{
    display:inline-block;
    padding:6px 16px;
    border-radius: 8px;
    font-weight: 600;
    font-size: 14px;
}}

/* ═══════════════════════════════════════════════════════════════
   风险提示
   ═══════════════════════════════════════════════════════════════ */
.ta-risk {{
    border: none;
    background: rgba(242,90,90,.08);
    color: rgba(242,90,90,.85);
    border-radius: 10px;
    padding: 10px 14px;
    font-size: 13px;
}}

/* ═══════════════════════════════════════════════════════════════
   Expander
   ═══════════════════════════════════════════════════════════════ */
.streamlit-expanderHeader {{
    background: {CARD};
    border-radius: 12px;
    font-size: 14px;
    font-weight: 500;
}}
details[data-testid="stExpander"] {{
    border: 1px solid {BORDER} !important;
    border-radius: 12px !important;
    overflow: hidden;
}}

/* ═══════════════════════════════════════════════════════════════
   标题层级 — 对齐 DeepSeek 排版
   ═══════════════════════════════════════════════════════════════ */
h1 {{
    font-size: 24px !important;
    font-weight: 700 !important;
    color: {TEXT} !important;
}}
h2 {{
    font-size: 20px !important;
    font-weight: 600 !important;
    color: {TEXT} !important;
}}
h3 {{
    font-size: 16px !important;
    font-weight: 600 !important;
    color: {TEXT} !important;
}}

/* ═══════════════════════════════════════════════════════════════
   Divider / Caption
   ═══════════════════════════════════════════════════════════════ */
hr {{
    border-color: {BORDER} !important;
    margin: 16px 0 !important;
}}
.stCaption, .stMarkdown p {{
    color: {MUTED};
    font-size: 13px;
}}

/* ═══════════════════════════════════════════════════════════════
   Alert / Info / Warning
   ═══════════════════════════════════════════════════════════════ */
.stAlert {{
    border-radius: 10px !important;
    border: none !important;
}}
[data-testid="stAlertContainerError"] {{
    background: rgba(242,90,90,.10) !important;
}}
[data-testid="stAlertContainerWarning"] {{
    background: rgba(247,173,49,.10) !important;
}}
[data-testid="stAlertContainerInfo"] {{
    background: rgba(77,147,248,.10) !important;
}}
[data-testid="stAlertContainerSuccess"] {{
    background: rgba(78,209,126,.10) !important;
}}

/* ═══════════════════════════════════════════════════════════════
   隐藏 Streamlit 默认元素
   ═══════════════════════════════════════════════════════════════ */
#MainMenu {{ visibility: hidden; }}
footer {{ visibility: hidden; }}
[data-testid="stHeader"] {{
    background: {BG};
    border-bottom: 1px solid {BORDER};
}}

/* 滚动条 */
::-webkit-scrollbar {{ width: 6px; height: 6px; }}
::-webkit-scrollbar-track {{ background: {BG}; }}
::-webkit-scrollbar-thumb {{ background: {CARD_HOVER}; border-radius: 3px; }}
::-webkit-scrollbar-thumb:hover {{ background: {MUTED}; }}
</style>
"""


def inject_theme() -> None:
    """注入全局 CSS（每个 Streamlit 脚本运行时调用一次）。"""
    st.markdown(CSS, unsafe_allow_html=True)


def rating_badge(rating: str) -> str:
    """评级 → 徽章 HTML。"""
    styles = {
        "buy": (UP, "买入 BUY"),
        "overweight": (ACCENT, "增持 OVERWEIGHT"),
        "hold": (MUTED, "持有 HOLD"),
        "underweight": ("#F7AD31", "减持 UNDERWEIGHT"),
        "sell": (DOWN, "卖出 SELL"),
    }
    color, label = styles.get(rating, (MUTED, rating.upper()))
    return (
        f"<span class='ta-badge' style='color:{color};"
        f"background:{color}1A;'>{label}</span>"
    )


def status_dot(kind: str) -> str:
    """状态灯 HTML：green/amber/red。"""
    return f"<span class='ta-dot ta-dot-{kind}'></span>"


def pct_color(value: float) -> str:
    """按 A 股惯例返回涨红跌绿。"""
    return UP if value >= 0 else DOWN
