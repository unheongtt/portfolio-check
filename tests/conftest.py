"""测试夹具：隔离的临时数据库 + 无凭证数据源环境。

所有测试默认在「未配置 API Key」状态下运行，验证显式失败与不静默兜底；
需要模拟数据源成功时，通过 monkeypatch 替换 market 服务方法。
"""
import os
import sys
from pathlib import Path

import pytest

# 必须在导入 app 之前设置环境（Settings 使用 lru_cache）
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# 注意：pydantic-settings 按字段名大写匹配环境变量（DB_PATH/SEED_DIR），不是 CHAINLENS_* 前缀
os.environ["FUYAO_API_KEY"] = ""
os.environ["FUYAO_BASE_URL"] = "https://fuyao.aicubes.cn"
os.environ["IFIND_MCP_TOKEN"] = ""
os.environ["IFIND_MCP_URL"] = ""
os.environ["LLM_API_KEY"] = ""
os.environ["LLM_BASE_URL"] = ""
os.environ["DB_PATH"] = str(PROJECT_ROOT / "data" / "test_portfolio.db")
os.environ["SEED_DIR"] = str(PROJECT_ROOT / "seed_data")


@pytest.fixture(scope="session")
def db_path() -> Path:
    return Path(os.environ["DB_PATH"])


@pytest.fixture(scope="session")
def client(db_path):
    """带 lifespan 的 TestClient（启动时装载种子数据）。"""
    from fastapi.testclient import TestClient

    if db_path.exists():
        db_path.unlink()

    from app.main import app
    with TestClient(app) as c:
        yield c

    if db_path.exists():
        db_path.unlink()


@pytest.fixture()
def market_service(client):
    from app.datasources.market import get_market
    return get_market()


@pytest.fixture()
def sample_holdings():
    return [
        {"thscode": "600519.SH", "name": "贵州茅台", "weight_pct": 33},
        {"thscode": "300750.SZ", "name": "宁德时代", "weight_pct": 22},
        {"thscode": "002594.SZ", "name": "比亚迪", "weight_pct": 16},
        {"thscode": "601012.SH", "name": "隆基绿能", "weight_pct": 10},
        {"thscode": "002475.SZ", "name": "立讯精密", "weight_pct": 12},
        {"thscode": "688981.SH", "name": "中芯国际", "weight_pct": 7},
    ]


@pytest.fixture()
def sample_membership():
    return {
        "600519.SH": {"industry": "白酒", "concepts": ["白酒", "大消费", "核心资产"]},
        "300750.SZ": {"industry": "电池", "concepts": ["动力电池", "新能源", "储能"]},
        "002594.SZ": {"industry": "汽车整车", "concepts": ["新能源车", "锂电", "新能源"]},
        "601012.SH": {"industry": "光伏设备", "concepts": ["光伏", "新能源"]},
        "002475.SZ": {"industry": "消费电子", "concepts": ["消费电子", "苹果产业链", "果链"]},
        "688981.SH": {"industry": "半导体", "concepts": ["半导体", "国产替代", "芯片"]},
    }
