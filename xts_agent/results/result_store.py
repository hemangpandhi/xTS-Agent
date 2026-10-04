from __future__ import annotations
"""
from __future__ import annotations
Result persistence module using SQLite.
"""
import sqlite3
import json
from typing import List, Dict, Any
from pathlib import Path
from .result_parser import TestResults

class ResultStore:
    def __init__(self, db_path: str | Path = 'results.db'):
        self.db_path = Path(db_path)
        self.init_db()
        
    def init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("""
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_name TEXT,
                    suite_name TEXT,
                    start_time TEXT,
                    end_time TEXT,
                    pass_count INTEGER,
                    fail_count INTEGER,
                    metadata TEXT,
                    results_json TEXT
                )
            """)
            conn.commit()
            
    def save_run(self, plan_name: str, suite_name: str, results: TestResults, metadata: Dict[str, Any]):
        with sqlite3.connect(self.db_path) as conn:
            c = conn.cursor()
            c.execute("""
                INSERT INTO runs (plan_name, suite_name, start_time, end_time, pass_count, fail_count, metadata, results_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (plan_name, suite_name, results.start_time, results.end_time, 
                  results.summary.get('pass', 0), results.summary.get('fail', 0), 
                  json.dumps(metadata), json.dumps(results.__dict__, default=str)))
            conn.commit()
            
    def get_history(self, suite_name: str, limit: int = 10) -> List[Any]:
        # Return simplified history
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM runs WHERE suite_name = ? ORDER BY id DESC LIMIT ?", (suite_name, limit))
            return [dict(r) for r in c.fetchall()]

    def get_flaky_tests(self, suite_name: str, window_runs: int = 5) -> List[str]:
        # Placeholder for flaky test detection logic
        return []
        
    def get_trends(self, suite_name: str) -> Dict[str, List[float]]:
        history = self.get_history(suite_name, 50)
        rates = []
        for h in reversed(history):
            t = h['pass_count'] + h['fail_count']
            r = (h['pass_count'] / t * 100) if t > 0 else 0
            rates.append(r)
        return {"pass_rate_trend": rates}
