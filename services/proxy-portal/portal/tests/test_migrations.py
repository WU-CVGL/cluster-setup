"""A rollback to the previous release: its code writes into a database the current migrations made."""

from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase
from django.utils import timezone

PREVIOUS = ("portal", "0003_accountrequest_applied_seq")  # the last migration of the previous release


class RollbackTest(TestCase):
    def previous_model(self, name):
        """The model as the previous release's code knows it: its fields as of its last migration."""
        return MigrationLoader(connection).project_state(PREVIOUS).apps.get_model("portal", name)

    def test_previous_code_creates_account_requests(self):
        old = self.previous_model("AccountRequest")
        row = old.objects.create(id="00000000-0000-4000-8000-000000000001", op="rotate", username="alice",
                                 requested_by="carol", requested_at=timezone.now())
        with connection.cursor() as cursor:
            cursor.execute("SELECT self_service FROM portal_accountrequest WHERE id = %s", [row.id])
            self.assertEqual(cursor.fetchone(), (0,))

    def test_new_columns_have_database_defaults(self):
        """Every NOT NULL column the previous release does not know has a default in the database itself."""
        old_columns = {f.column for f in self.previous_model("AccountRequest")._meta.local_fields}
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA table_info(portal_accountrequest)")
            missing = [(name, default) for _, name, _, notnull, default, _ in cursor.fetchall()
                       if name not in old_columns and notnull and default is None]
        self.assertEqual(missing, [])
