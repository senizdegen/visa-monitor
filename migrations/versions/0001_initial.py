"""initial

Revision ID: 0001
Revises: 
Create Date: 2026-11-11 00:34:21.462177
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '0001'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('adapters',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('adapter_type', sa.Text(), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('source_country', sa.Text(), nullable=False),
    sa.Column('destination_country', sa.Text(), nullable=False),
    sa.Column('provider', sa.Text(), nullable=False),
    sa.Column('center', sa.Text(), nullable=False),
    sa.Column('visa_category', sa.Text(), nullable=False),
    sa.Column('timezone', sa.Text(), nullable=False),
    sa.Column('config', postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    sa.Column('interval_minutes', sa.Integer(), nullable=False),
    sa.Column('enabled', sa.Boolean(), server_default=sa.text('true'), nullable=False),
    sa.Column('next_run_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.CheckConstraint("source IN ('live','demo')", name='ck_adapters_source'),
    sa.CheckConstraint('interval_minutes > 0', name='ck_adapters_interval_positive'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_adapters_next_run_enabled', 'adapters', ['next_run_at'], unique=False, postgresql_where=sa.text('enabled'))
    op.create_table('workers',
    sa.Column('id', sa.Text(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('check_jobs',
    sa.Column('id', sa.UUID(), server_default=sa.text('gen_random_uuid()'), nullable=False),
    sa.Column('adapter_id', sa.Text(), nullable=False),
    sa.Column('trigger', sa.Text(), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('claim_count', sa.Integer(), server_default=sa.text('0'), nullable=False),
    sa.Column('locked_by', sa.Text(), nullable=True),
    sa.Column('locked_until', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.CheckConstraint("status IN ('pending','running','done','failed')", name='ck_check_jobs_status'),
    sa.CheckConstraint("trigger IN ('scheduled','manual')", name='ck_check_jobs_trigger'),
    sa.ForeignKeyConstraint(['adapter_id'], ['adapters.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_check_jobs_status_created', 'check_jobs', ['status', 'created_at'], unique=False)
    op.create_index('uq_check_jobs_active', 'check_jobs', ['adapter_id'], unique=True, postgresql_where=sa.text("status IN ('pending','running')"))
    op.create_table('checks',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('job_id', sa.UUID(), nullable=False),
    sa.Column('adapter_id', sa.Text(), nullable=False),
    sa.Column('checked_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('status', sa.Text(), nullable=False),
    sa.Column('available_dates', sa.ARRAY(sa.Date()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('slots_count', sa.Integer(), nullable=True),
    sa.Column('has_waitlist', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('response_time_ms', sa.Integer(), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=False),
    sa.Column('attempts', sa.Integer(), server_default=sa.text('1'), nullable=False),
    sa.Column('evidence_path', sa.Text(), nullable=True),
    sa.Column('raw', postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
    sa.CheckConstraint("source IN ('live','demo')", name='ck_checks_source'),
    sa.ForeignKeyConstraint(['adapter_id'], ['adapters.id'], ),
    sa.ForeignKeyConstraint(['job_id'], ['check_jobs.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('job_id')
    )
    op.create_index('ix_checks_adapter_checked', 'checks', ['adapter_id', sa.literal_column('checked_at DESC')], unique=False)
    op.create_index('ix_checks_checked_at', 'checks', ['checked_at'], unique=False)
    op.create_index('ix_checks_status', 'checks', ['status'], unique=False)
    op.create_table('slot_events',
    sa.Column('id', sa.BigInteger(), sa.Identity(always=False), nullable=False),
    sa.Column('adapter_id', sa.Text(), nullable=False),
    sa.Column('check_id', sa.BigInteger(), nullable=False),
    sa.Column('detected_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('event_type', sa.Text(), nullable=False),
    sa.Column('dates_before', sa.ARRAY(sa.Date()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('dates_after', sa.ARRAY(sa.Date()), server_default=sa.text("'{}'"), nullable=False),
    sa.Column('source', sa.Text(), nullable=False),
    sa.CheckConstraint("event_type IN ('APPEARED','CHANGED','DISAPPEARED')", name='ck_slot_events_type'),
    sa.CheckConstraint("source IN ('live','demo')", name='ck_slot_events_source'),
    sa.ForeignKeyConstraint(['adapter_id'], ['adapters.id'], ),
    sa.ForeignKeyConstraint(['check_id'], ['checks.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_slot_events_adapter_detected', 'slot_events', ['adapter_id', sa.literal_column('detected_at DESC')], unique=False)


def downgrade() -> None:
    op.drop_index('ix_slot_events_adapter_detected', table_name='slot_events')
    op.drop_table('slot_events')
    op.drop_index('ix_checks_status', table_name='checks')
    op.drop_index('ix_checks_checked_at', table_name='checks')
    op.drop_index('ix_checks_adapter_checked', table_name='checks')
    op.drop_table('checks')
    op.drop_index('uq_check_jobs_active', table_name='check_jobs', postgresql_where=sa.text("status IN ('pending','running')"))
    op.drop_index('ix_check_jobs_status_created', table_name='check_jobs')
    op.drop_table('check_jobs')
    op.drop_table('workers')
    op.drop_index('ix_adapters_next_run_enabled', table_name='adapters', postgresql_where=sa.text('enabled'))
    op.drop_table('adapters')
