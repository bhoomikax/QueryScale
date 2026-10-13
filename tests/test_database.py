import unittest
from unittest.mock import Mock, patch

from mysql.connector.errors import Error as MySQLError

from backend.config import settings
from backend.database import close_connection, open_connection


class DatabaseConnectionTests(unittest.TestCase):
    @patch("backend.database.mysql.connector.connect")
    def test_open_connection_passes_shared_config_and_server_overrides(self, connect):
        connection = Mock()
        connect.return_value = connection

        result = open_connection({"database": "queryscale_test"})

        self.assertIs(result, connection)
        config = connect.call_args.kwargs
        self.assertEqual(config["database"], "queryscale_test")
        self.assertGreater(config["connection_timeout"], 0)
        self.assertGreater(config["read_timeout"], 0)
        self.assertGreater(config["write_timeout"], 0)
        self.assertEqual(config["ssl_verify_cert"], settings.db_tls_verify)
        self.assertEqual(config["ssl_verify_identity"], settings.db_tls_verify)

    @patch("backend.database.mysql.connector.connect", side_effect=MySQLError("offline"))
    def test_open_connection_wraps_connector_errors(self, _connect):
        with self.assertRaisesRegex(RuntimeError, "Unable to connect to MySQL"):
            open_connection()

    def test_close_connection_closes_without_requiring_connected_check(self):
        connection = Mock()
        connection.is_connected.return_value = False

        close_connection(connection)

        connection.close.assert_called_once_with()

    def test_close_connection_does_not_mask_prior_errors(self):
        connection = Mock()
        connection.close.side_effect = MySQLError("already closed")

        close_connection(connection)

        connection.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
