"""pytest 共享配置：把 backend/ 加入 sys.path，让测试可以裸导入 session_ops。"""
import sys
from pathlib import Path

_BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))
