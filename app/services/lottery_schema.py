"""Idempotent additive lottery schema upgrade, for PostgreSQL and local SQLite.

Never synthesize historical winners/entries or enqueue old ended announcements.
Unexpected migration errors fail startup rather than silently disabling claims.
"""
from sqlalchemy import inspect, text


def ensure_lottery_schema(db):
    additions = {
        'points_cost': 'INTEGER NOT NULL DEFAULT 10',
        'draw_token': 'VARCHAR(64)',
        'draw_lease_until': 'TIMESTAMP',
        'draw_candidates': 'TEXT',
        'draw_finished_at': 'TIMESTAMP',
        'prizes': 'TEXT',
        'winner_awards': 'TEXT',
    }
    with db.engine.begin() as connection:
        columns = {column['name'] for column in inspect(connection).get_columns('group_lottery')}
        for name, definition in additions.items():
            if name not in columns:
                guard = 'IF NOT EXISTS ' if connection.dialect.name == 'postgresql' else ''
                connection.execute(text(f'ALTER TABLE group_lottery ADD COLUMN {guard}{name} {definition}'))
        for table_name in ('lottery_entry', 'lottery_delivery'):
            db.metadata.tables[table_name].create(bind=connection, checkfirst=True)
