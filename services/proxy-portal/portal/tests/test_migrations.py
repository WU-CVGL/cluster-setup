"""A rollback to the previous release: its code writes into a database the current migrations made."""

from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

PREVIOUS = ("portal", "0003_accountrequest_applied_seq")  # the last migration of the previous release
SELF_SERVICE_ADDED = ("portal", "0004_self_service_and_cluster_users")  # as released, without a database default


def previous_model(name):
    """The model as the previous release's code knows it: its fields as of its last migration."""
    return MigrationLoader(connection).project_state(PREVIOUS).apps.get_model("portal", name)


def create_request(model, request_id, **fields):
    return model.objects.create(id=request_id, op="rotate", username="alice", requested_by="carol",
                                requested_at=timezone.now(), **fields)


def self_service_column():
    """(notnull, default) of portal_accountrequest.self_service in the database itself."""
    with connection.cursor() as cursor:
        cursor.execute("PRAGMA table_info(portal_accountrequest)")
        return next((notnull, default) for _, name, _, notnull, default, _ in cursor.fetchall()
                    if name == "self_service")


def self_service_of(request_id):
    with connection.cursor() as cursor:
        cursor.execute("SELECT self_service FROM portal_accountrequest WHERE id = %s", [request_id])
        return cursor.fetchone()


class RollbackTest(TestCase):
    def test_previous_code_creates_account_requests(self):
        row = create_request(previous_model("AccountRequest"), "00000000-0000-4000-8000-000000000001")
        self.assertEqual(self_service_of(row.id), (0,))

    def test_new_columns_have_database_defaults(self):
        """Every NOT NULL column the previous release does not know has a default in the database itself."""
        old_columns = {f.column for f in previous_model("AccountRequest")._meta.local_fields}
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA table_info(portal_accountrequest)")
            missing = [(name, default) for _, name, _, notnull, default, _ in cursor.fetchall()
                       if name not in old_columns and notnull and default is None]
        self.assertEqual(missing, [])


class UpgradeTest(TransactionTestCase):
    """A database that applied 0004 as released, then the current migrations; and their way back."""

    def migrate(self, *targets):
        MigrationExecutor(connection).migrate(list(targets))

    def setUp(self):
        self.addCleanup(lambda: self.migrate(*MigrationExecutor(connection).loader.graph.leaf_nodes()))
        self.migrate(PREVIOUS)
        self.migrate(SELF_SERVICE_ADDED)

    def model_at(self, target):
        return MigrationExecutor(connection).loader.project_state(target).apps.get_model("portal", "AccountRequest")

    def test_previous_code_creates_account_requests_after_the_upgrade(self):
        # 0004 as released: no database default, so the previous release's code cannot create requests
        self.assertEqual(self_service_column(), (1, None))
        with self.assertRaises(IntegrityError), transaction.atomic():
            create_request(previous_model("AccountRequest"), "00000000-0000-4000-8000-000000000002")
        kept = create_request(self.model_at(SELF_SERVICE_ADDED), "00000000-0000-4000-8000-000000000003",
                              self_service=True)

        self.migrate(*MigrationExecutor(connection).loader.graph.leaf_nodes())
        self.assertEqual(self_service_column(), (1, "0"))
        self.assertEqual(self_service_of(kept.id), (1,))
        row = create_request(previous_model("AccountRequest"), "00000000-0000-4000-8000-000000000004")
        self.assertEqual(self_service_of(row.id), (0,))

    def test_database_default_migration_reverses(self):
        self.migrate(*MigrationExecutor(connection).loader.graph.leaf_nodes())
        kept = create_request(previous_model("AccountRequest"), "00000000-0000-4000-8000-000000000005")

        self.migrate(SELF_SERVICE_ADDED)
        self.assertEqual(self_service_column(), (1, None))
        self.assertEqual(self_service_of(kept.id), (0,))

        self.migrate(*MigrationExecutor(connection).loader.graph.leaf_nodes())
        self.assertEqual(self_service_column(), (1, "0"))
        self.assertEqual(self_service_of(kept.id), (0,))


BEFORE_GRANTS = ("portal", "0006_incident_component_labels")  # the last migration before private proxy grants


def proxy_of(request_id):
    with connection.cursor() as cursor:
        cursor.execute("SELECT proxy FROM portal_accountrequest WHERE id = %s", [request_id])
        return cursor.fetchone()


class GrantsRollbackTest(TestCase):
    def test_code_before_grants_creates_account_requests(self):
        model = MigrationLoader(connection).project_state(BEFORE_GRANTS).apps.get_model("portal", "AccountRequest")
        row = create_request(model, "00000000-0000-4000-8000-000000000011", self_service=False)
        self.assertEqual(proxy_of(row.id), (None,))


class GrantsMigrationTest(TransactionTestCase):
    def migrate(self, *targets):
        MigrationExecutor(connection).migrate(list(targets))

    def test_reverses(self):
        self.addCleanup(lambda: self.migrate(*MigrationExecutor(connection).loader.graph.leaf_nodes()))
        model = MigrationExecutor(connection).loader.project_state(
            MigrationExecutor(connection).loader.graph.leaf_nodes()).apps.get_model("portal", "AccountRequest")
        model.objects.create(id="00000000-0000-4000-8000-000000000012", op="grant", username="alice",
                             proxy="jpty1-bwh-2tb", requested_by="carol", requested_at=timezone.now())
        self.migrate(BEFORE_GRANTS)
        with connection.cursor() as cursor:
            cursor.execute("PRAGMA table_info(portal_accountrequest)")
            self.assertNotIn("proxy", [row[1] for row in cursor.fetchall()])
        self.migrate(*MigrationExecutor(connection).loader.graph.leaf_nodes())
        self.assertEqual(proxy_of("00000000-0000-4000-8000-000000000012"), (None,))
