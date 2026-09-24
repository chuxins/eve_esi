"""pytest 配置：把项目根目录加入 sys.path，便于直接导入扁平结构的模块。"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
