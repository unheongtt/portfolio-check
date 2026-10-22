"""SQLite 存储层。

三类表：
- 种子数据表（sample_holding / industry_concept）：随 seed_data/*.json 装载，
  提供"无凭证也能跑通"的静态持仓示例与行业/概念归属；
- 市场缓存表（quote / valuation / kline）：数据源拉取结果 + fetched_at + data_status，
  新鲜度判断在读取时进行；
- 产品状态表（report / ai_output）：体检报告与 AI 产出，全部可追溯。
"""
import json
import sqlite3
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS sample_holding (
    portfolio_id TEXT, thscode TEXT, name TEXT, market TEXT,
    weight_pct REAL, ord INTEGER,
    PRIMARY KEY (portfolio_id, thscode)
);
CREATE TABLE IF NOT EXISTS industry_concept (
    thscode TEXT PRIMARY KEY, name TEXT, industry TEXT, industry_source TEXT,
    concepts TEXT
);
CREATE TABLE IF NOT EXISTS quote (
    thscode TEXT PRIMARY KEY, name TEXT, last_price REAL,
    price_change_ratio_pct REAL, turnover REAL, as_of_ms INTEGER,
    source TEXT, data_status TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS valuation (
    thscode TEXT PRIMARY KEY, name TEXT, pe_ttm REAL, pb_mrq REAL,
    ps_ttm REAL, as_of_ms INTEGER, source TEXT, data_status TEXT, fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS kline (
    thscode TEXT, date_ms INTEGER, close_price REAL,
    source TEXT, fetched_at TEXT,
    PRIMARY KEY (thscode, date_ms)
);
CREATE TABLE IF NOT EXISTS report (
    id TEXT PRIMARY KEY, portfolio_name TEXT, payload TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS ai_output (
    id TEXT PRIMARY KEY, kind TEXT, model TEXT, prompt_hash TEXT,
    input_refs TEXT, output TEXT, review_status TEXT, created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_kline_code ON kline(thscode);
CREATE INDEX IF NOT EXISTS idx_quote_fetched ON quote(fetched_at);
"""


class Database:
    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- 基础查询 ---------------------------------------------------------
    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        cur = self._conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def query_one(self, sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
        cur = self._conn.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None

    def execute(self, sql: str, params: tuple = ()) -> None:
        self._conn.execute(sql, params)
        self._conn.commit()

    def upsert(self, table: str, row: dict[str, Any], keys: list[str]) -> None:
        cols = list(row.keys())
        placeholders = ",".join("?" for _ in cols)
        updates = ",".join(f"{c}=excluded.{c}" for c in cols if c not in keys)
        sql = (
            f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) "
            f"ON CONFLICT({','.join(keys)}) DO UPDATE SET {updates}"
        )
        self._conn.execute(sql, tuple(row[c] for c in cols))
        self._conn.commit()

    # -- 种子数据装载（seed_data/*.json） ----------------------------------
    def load_seed(self, seed_dir: str | Path) -> dict[str, int]:
        """从 seed_data 装载示例组合与行业/概念归属。幂等。返回各表装载条数。"""
        seed_dir = Path(seed_dir)
        counts: dict[str, int] = {}

        sp = json.loads((seed_dir / "sample_portfolio.json").read_text(encoding="utf-8"))
        for i, h in enumerate(sp.get("holdings", [])):
            self.upsert(
                "sample_holding",
                {"portfolio_id": sp["id"], "thscode": h["thscode"], "name": h.get("name", ""),
                 "market": h.get("market", "A股"), "weight_pct": h.get("weight_pct", 0),
                 "ord": h.get("ord", i)},
                ["portfolio_id", "thscode"],
            )
        counts["sample_holding"] = len(sp.get("holdings", []))

        rows = json.loads((seed_dir / "industry_concept.json").read_text(encoding="utf-8"))
        for r in rows:
            self.upsert(
                "industry_concept",
                {"thscode": r["thscode"], "name": r.get("name", ""),
                 "industry": r.get("industry", ""),
                 "industry_source": r.get("industry_source", ""),
                 "concepts": json.dumps(r.get("concepts", []), ensure_ascii=False)},
                ["thscode"],
            )
        counts["industry_concept"] = len(rows)
        return counts


_db: Optional[Database] = None


def get_db() -> Database:
    global _db
    if _db is None:
        from .config import get_settings
        _db = Database(get_settings().db_path)
    return _db
