from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from tests.conftest import ROOT

EXPECTED_TABLES = {
    "events",
    "processed_events",
    "deliveries",
    "delivery_attempts",
    "replays",
    "signature_rejections",
    "destination_states",
    "source_secrets",
    "signature_nonces",
}


def test_migrations_downgrade_to_base_and_back_to_head(engine, database_url):
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "launchbridge" / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)

    command.downgrade(config, "base")
    assert set(inspect(engine).get_table_names()) & EXPECTED_TABLES == set()

    command.upgrade(config, "head")
    assert set(inspect(engine).get_table_names()) >= EXPECTED_TABLES
    columns = {c["name"] for c in inspect(engine).get_columns("destination_states")}
    assert columns == {
        "destination",
        "breaker_state",
        "consecutive_failures",
        "opened_at",
        "updated_at",
    }
