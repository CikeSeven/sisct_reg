import sqlite3
import tempfile
from pathlib import Path

import app.db as app_db


class IsolatedCodexTeamDbTestCase:
    def setUp(self):
        self._db_tmpdir = tempfile.TemporaryDirectory()
        self._original_db_path = app_db.DB_PATH
        app_db.DB_PATH = Path(self._db_tmpdir.name) / "test_register_machine.db"
        app_db.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        app_db.init_db()
        conn = sqlite3.connect(app_db.DB_PATH)
        try:
            conn.execute('DELETE FROM codex_team_web_sessions')
            conn.execute('DELETE FROM codex_team_job_events')
            conn.execute('DELETE FROM codex_team_jobs')
            conn.execute('DELETE FROM codex_team_parent_accounts')
            conn.commit()
        finally:
            conn.close()
        super().setUp()

    def tearDown(self):
        try:
            super().tearDown()
        finally:
            app_db.DB_PATH = self._original_db_path
            self._db_tmpdir.cleanup()
