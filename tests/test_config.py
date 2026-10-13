import os
import unittest
from unittest.mock import patch

from backend.config import Settings


class ConfigTests(unittest.TestCase):
    def test_database_and_api_settings_are_loaded_from_environment(self):
        with patch.dict(os.environ, {
            "DB_HOST": "db.example.internal",
            "DB_PORT": "3307",
            "DB_NAME": "banking",
            "DB_TEST_NAME": "banking_test",
            "DB_USER": "source_reader",
            "DB_PASSWORD": "source-secret",
            "DB_TEST_USER": "benchmark_user",
            "DB_TEST_PASSWORD": "benchmark-secret",
            "DB_CONNECTION_TIMEOUT": "4",
            "DB_READ_TIMEOUT": "12",
            "DB_WRITE_TIMEOUT": "13",
            "DB_TLS_VERIFY": "true",
            "DB_TLS_CA": "/run/secrets/rds-ca.pem",
            "API_HOST": "0.0.0.0",
            "API_PORT": "9000",
            "QUERY_LOG_PATH": "/mnt/query_logs.csv",
        }, clear=True):
            settings = Settings()

        config = settings.mysql_config()
        test_config = settings.test_mysql_config()
        self.assertEqual((settings.db_host, settings.db_port, settings.db_name),
                         ("db.example.internal", 3307, "banking"))
        self.assertEqual((settings.api_host, settings.api_port), ("0.0.0.0", 9000))
        self.assertEqual(settings.query_log_path, "/mnt/query_logs.csv")
        self.assertEqual(config["connection_timeout"], 4)
        self.assertEqual(config["read_timeout"], 12)
        self.assertEqual(config["write_timeout"], 13)
        self.assertTrue(config["ssl_verify_cert"])
        self.assertTrue(config["ssl_verify_identity"])
        self.assertEqual(config["ssl_ca"], "/run/secrets/rds-ca.pem")
        self.assertEqual(test_config["database"], "banking_test")
        self.assertEqual(test_config["user"], "benchmark_user")
        self.assertEqual(test_config["password"], "benchmark-secret")

    def test_tls_verification_defaults_to_enabled(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(Settings().mysql_config()["ssl_verify_cert"])

    def test_invalid_tls_boolean_is_rejected(self):
        with patch.dict(os.environ, {"DB_TLS_VERIFY": "sometimes"}, clear=True):
            with self.assertRaises(ValueError):
                Settings()


if __name__ == "__main__":
    unittest.main()
