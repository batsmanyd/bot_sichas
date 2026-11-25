"""Initialize the current model schema in a new PostgreSQL database."""
import os

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

os.environ["ALEMBIC_RUNNING"] = "1"
from main import Model, engine  # noqa: E402


def main():
    if engine.dialect.name != "postgresql":
        raise SystemExit("This initializer requires an empty PostgreSQL database.")
    with engine.begin() as connection:
        connection.execute(text("SELECT pg_advisory_xact_lock(92569021)"))
        if inspect(connection).get_table_names():
            raise SystemExit("Database is populated. Use the documented Alembic upgrade.")
        Model.metadata.create_all(connection)
        config = Config("alembic.ini")
        config.attributes["connection"] = connection
        command.stamp(config, "head")


if __name__ == "__main__":
    main()
