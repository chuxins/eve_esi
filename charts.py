"""matplotlib 图表公共辅助。

统一处理两件事：
- 把 matplotlib 缓存目录指向可写位置（根文件系统可能只读）
- 中文字体探测与设置（找不到时图表回退英文标签）
"""

import os

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_MPL_CACHE_DIR = os.path.join(_BASE_DIR, ".mplcache")
os.environ.setdefault("MPLCONFIGDIR", _MPL_CACHE_DIR)
os.makedirs(_MPL_CACHE_DIR, exist_ok=True)

import matplotlib

matplotlib.use("Agg")  # 无显示环境（服务器）使用非交互后端
import matplotlib.pyplot as plt
from matplotlib import font_manager

_CJK_FONT_CANDIDATES = (
    "Noto Sans CJK SC", "WenQuanYi Micro Hei", "WenQuanYi Zen Hei",
    "SimHei", "Microsoft YaHei", "PingFang SC",
)


def setup_chinese_font():
    """尝试设置中文字体，返回是否成功（失败则图表使用英文标签）。"""
    available = {f.name for f in font_manager.fontManager.ttflist}
    for name in _CJK_FONT_CANDIDATES:
        if name in available:
            plt.rcParams["font.sans-serif"] = [name, "DejaVu Sans"]
            plt.rcParams["axes.unicode_minus"] = False
            return True
    return False
