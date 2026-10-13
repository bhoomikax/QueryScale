import unittest
from unittest.mock import Mock, patch

from backend.services.index_manager import (
    build_create_index_sql,
    build_index_name,
    normalize_columns,
    remove_candidate_index,
    validate_candidate,
)


class IndexManagerTests(unittest.TestCase):
    def test_valid_composite_candidate_generates_quoted_sql(self):
        columns = validate_candidate(
            "IDX_TRANSACTIONS_ACCOUNT_ID_TXN_DATE",
            "Transactions",
            "account_id, txn_date",
        )

        self.assertEqual(columns, ("account_id", "txn_date"))
        self.assertEqual(build_index_name("Transactions", columns), "idx_transactions_account_id_txn_date")
        self.assertEqual(
            build_create_index_sql("idx_transactions_account_id_txn_date", "Transactions", columns),
            "CREATE INDEX `idx_transactions_account_id_txn_date` ON `Transactions` (`account_id`, `txn_date`)",
        )

    def test_invalid_candidate_identifiers_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_candidate("IDX_USERS_ID", "Users", "id")
        with self.assertRaises(ValueError):
            normalize_columns("account_id, account_id")
        with self.assertRaises(ValueError):
            normalize_columns("account_id; DROP TABLE Accounts")

    @patch("backend.services.index_manager._connect_to_test_database")
    def test_recovery_cleanup_drops_only_the_validated_candidate_index(self, connect_test_db):
        connection = Mock()
        cursor = connection.cursor.return_value
        cursor.fetchone.return_value = (1,)
        connect_test_db.return_value = connection

        removed = remove_candidate_index(
            "IDX_TRANSACTIONS_ACCOUNT_ID", "Transactions", ["account_id"]
        )

        self.assertTrue(removed)
        self.assertEqual(
            cursor.execute.call_args_list[-1].args[0],
            "DROP INDEX `idx_transactions_account_id` ON `Transactions`",
        )
        connection.commit.assert_called_once_with()
        connection.close.assert_called_once_with()

    @patch("backend.services.index_manager._connect_to_test_database")
    def test_recovery_cleanup_rejects_unsafe_candidate_before_database_access(self, connect_test_db):
        with self.assertRaises(ValueError):
            remove_candidate_index("IDX_USERS_ID", "Users", ["id"])
        connect_test_db.assert_not_called()


if __name__ == "__main__":
    unittest.main()
