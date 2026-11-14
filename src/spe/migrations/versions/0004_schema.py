"""schema: users, their connections, the Sure write queue/ledger and source evidence

Revision ID: 0004
Revises: none (the first published schema; numbered 0004 because unreleased databases already reached it)
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('kv',
    sa.Column('key', sa.String(length=100), nullable=False),
    sa.Column('value', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('sure_user_id', sa.String(length=64), nullable=False),
    sa.Column('family_id', sa.String(length=64), nullable=True),
    sa.Column('family_name', sa.String(length=200), nullable=True),
    sa.Column('locale', sa.String(length=10), nullable=True),
    sa.Column('token', sa.LargeBinary(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('status_since', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('sure_user_id')
    )
    op.create_table('connections',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('provider', sa.String(length=50), nullable=False),
    sa.Column('label', sa.String(length=200), nullable=True),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('interval_hours', sa.Integer(), nullable=False),
    sa.Column('config', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('message', sa.String(length=500), nullable=True),
    sa.Column('cursor', sa.JSON(), nullable=False),
    sa.Column('session', sa.LargeBinary(), nullable=True),
    sa.Column('failures', sa.Integer(), nullable=False),
    sa.Column('sync_requested', sa.Boolean(), nullable=False),
    sa.Column('last_attempt_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_success_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('next_run_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('connections', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_connections_user_id'), ['user_id'], unique=False)

    op.create_table('sure_ops',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('key', sa.String(length=300), nullable=False),
    sa.Column('group_key', sa.String(length=300), nullable=False),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('provider', sa.String(length=50), nullable=False),
    sa.Column('account_id', sa.String(length=36), nullable=True),
    sa.Column('transaction_id', sa.String(length=36), nullable=True),
    sa.Column('summary', sa.String(length=500), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('before', sa.JSON(), nullable=True),
    sa.Column('response', sa.JSON(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('error', sa.String(length=500), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('key')
    )
    with op.batch_alter_table('sure_ops', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sure_ops_group_key'), ['group_key'], unique=False)
        batch_op.create_index(batch_op.f('ix_sure_ops_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_sure_ops_user_id'), ['user_id'], unique=False)

    op.create_table('evidence',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('connection_id', sa.Integer(), nullable=True),
    sa.Column('provider', sa.String(length=50), nullable=False),
    sa.Column('key', sa.String(length=200), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('payload', sa.JSON(), nullable=False),
    sa.Column('issue', sa.String(length=300), nullable=True),
    sa.Column('first_seen', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['connection_id'], ['connections.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('connection_id', 'key')
    )
    with op.batch_alter_table('evidence', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_evidence_user_id'), ['user_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('evidence', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_evidence_user_id'))

    op.drop_table('evidence')
    with op.batch_alter_table('sure_ops', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sure_ops_user_id'))
        batch_op.drop_index(batch_op.f('ix_sure_ops_status'))
        batch_op.drop_index(batch_op.f('ix_sure_ops_group_key'))

    op.drop_table('sure_ops')
    with op.batch_alter_table('connections', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_connections_user_id'))

    op.drop_table('connections')
    op.drop_table('users')
    op.drop_table('kv')
