import os

from alembic import context
from sqlalchemy import create_engine

from spe.db import Base

url = context.config.get_main_option("sqlalchemy.url") or os.environ["DATABASE_URL"]
with create_engine(url).connect() as conn:
    context.configure(connection=conn, target_metadata=Base.metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
