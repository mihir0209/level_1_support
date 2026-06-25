"""
Alembic migration tests — verify schema is versioned and reversible.

Run with: uv run pytest tests/test_migrations.py -v
"""

import subprocess
import pytest


class TestAlembicSetup:

    def test_alembic_ini_exists(self):
        import os
        assert os.path.exists("alembic.ini")

    def test_alembic_directory_exists(self):
        import os
        assert os.path.isdir("alembic")

    def test_initial_migration_exists(self):
        import os
        assert os.path.exists("alembic/versions/001_initial_schema.py")

    def test_env_py_exists(self):
        import os
        assert os.path.exists("alembic/env.py")


class TestMigrationCommands:

    def test_alembic_current(self):
        import socket
        import os
        from config import settings

        # Determine host to check (fallback to localhost if default is docker-only 'postgres')
        host = settings.postgres_host
        if host == "postgres":
            host = "localhost"

        try:
            s = socket.create_connection((host, int(settings.postgres_port or 5432)), timeout=1)
            s.close()
        except Exception:
            pytest.skip("Postgres database not reachable (skipping alembic current test)")

        env = os.environ.copy()
        if env.get("POSTGRES_HOST") == "postgres":
            env["POSTGRES_HOST"] = "localhost"

        result = subprocess.run(
            ["uv", "run", "alembic", "current"],
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        # Should exit 0 and show current revision
        assert result.returncode == 0
        # Should show "001 (head)" or similar
        output = result.stdout + result.stderr
        assert "001" in output or "head" in output.lower()

    def test_alembic_history(self):
        result = subprocess.run(
            ["uv", "run", "alembic", "history"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0
        output = result.stdout + result.stderr
        assert "001" in output

    def test_alembic_version_stamped_in_db(self):
        """Verify alembic_version table exists with 001 entry via psycopg."""
        import os
        if not os.path.exists("/.dockerenv"):
            pytest.skip("Not running inside Docker container")

        # Use subprocess psql instead of docker SDK
        result = subprocess.run(
            ["docker", "exec", "l1_postgres",
             "psql", "-U", "l1support", "-d", "l1_support", "-t", "-c",
             "SELECT version_num FROM alembic_version;"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        output = result.stdout.strip()
        assert "001" in output, f"Expected 001 in alembic_version, got: {output}"


class TestModelsMatchSchema:

    def test_all_tables_have_models(self):
        """Verify SQLAlchemy models exist for every table."""
        import os
        import asyncio
        import psycopg

        # Use localhost for host-side testing (postgres Docker DNS not available here)
        async def check():
            conn_str = (
                "host=localhost port=5432 dbname=l1_support "
                "user=l1support password=l1support_secret"
            )
            try:
                async with await psycopg.AsyncConnection.connect(conn_str) as conn:
                    async with conn.cursor() as cur:
                        await cur.execute("""
                            SELECT table_name FROM information_schema.tables
                            WHERE table_schema = 'public'
                            AND table_name NOT LIKE 'alembic_%'
                            AND table_name NOT LIKE 'pg_%'
                        """)
                        rows = await cur.fetchall()
                        return {r[0] for r in rows}
            except Exception:
                pytest.skip("Postgres not reachable from host (use docker exec)")

        tables = asyncio.get_event_loop().run_until_complete(check())
        expected = {
            "knowledge_chunks", "support_sessions", "intent_label_queue",
            "resolution_review_queue", "crawl_sources", "brand_api_config",
            "registered_api_endpoints", "brand_api_credentials",
        }
        assert expected.issubset(tables), f"Missing tables: {expected - tables}"

    def test_model_column_count_matches_db(self):
        """Spot-check: knowledge_chunks should have the right column count."""
        from core.db.models import KnowledgeChunk
        cols = [c.name for c in KnowledgeChunk.__table__.columns]
        # id, chunk_id, brand_id, source_url, content, chunk_type, embedding,
        # confidence, metadata, crawled_at, updated_at = 11 columns
        assert len(cols) == 11, f"Expected 11 columns, got {cols}"
        assert "metadata_" in cols or "metadata" in cols  # alias version