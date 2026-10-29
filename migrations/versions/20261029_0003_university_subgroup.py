"""Сохранил личную подгруппу университетской подписки."""

from alembic import op

revision = "20261029_0003"
down_revision = "20261029_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = op.get_bind().exec_driver_sql("PRAGMA table_info(users)").fetchall()
    if not any(row[1] == "university_subgroup" for row in columns):
        op.execute("ALTER TABLE users ADD COLUMN university_subgroup INTEGER")


def downgrade() -> None:
    op.drop_column("users", "university_subgroup")
