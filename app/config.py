"""持仓体检 PortfolioCheck 全局配置。

所有凭证只从环境变量 / .env 读取，任何路径都不允许硬编码密钥。
"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # 数据源：扶摇（同花顺金融数据 API）
    fuyao_api_key: str = ""
    fuyao_base_url: str = "https://fuyao.aicubes.cn"

    # 数据源：iFinD MCP（streamable HTTP）
    ifind_mcp_url: str = ""
    ifind_mcp_token: str = ""

    # AI 分析层（Anthropic Messages API 兼容；缺省走规则引擎）
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = ""

    # 服务
    host: str = "0.0.0.0"
    port: int = 8000
    db_path: str = "data/portfolio.db"
    seed_dir: str = "seed_data"

    # 数据新鲜度阈值（秒）：行情 5 分钟、财务 7 天、历史日线 1 天
    stale_quote_seconds: int = 300
    stale_financial_seconds: int = 604800
    stale_klines_seconds: int = 86400

    @property
    def fuyao_enabled(self) -> bool:
        return bool(self.fuyao_api_key)

    @property
    def ifind_enabled(self) -> bool:
        return bool(self.ifind_mcp_url) and bool(self.ifind_mcp_token)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key) and bool(self.llm_base_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()
