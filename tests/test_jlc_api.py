from __future__ import annotations

import unittest

from lcsc_altium_loader.jlc_api import (
    ACCESS_KEY_ENV,
    APP_ID_ENV,
    SECRET_KEY_ENV,
    JLCOpenApiSettings,
    authorization_header,
    compact_json,
    signature,
    signing_self_test,
    signing_text,
)


class JLCOpenApiTests(unittest.TestCase):
    def test_official_signing_vector(self) -> None:
        body = compact_json(
            {"goodsId": 100, "quantity": 52, "createdTime": "2024-03-21 10:03:20"}
        )

        actual = signature(
            "z0BWlikshimuyiwBsH1i2qwnzMb3j3kA",
            "/order/v1/createOrder",
            body,
            timestamp=1625208260,
            nonce="IZHEJYNIHYZIE8S0LLC0VWTPJVRRTO50",
        )

        self.assertEqual(actual, "sygwKhKBkLwHVv0c7D+a/A7JTEJjGH/kLugFKh16918=")
        self.assertTrue(signing_self_test())

    def test_authorization_header_contains_no_secret(self) -> None:
        settings = JLCOpenApiSettings("293992070061998081", "access", "very-secret")

        value = authorization_header(
            settings,
            "/demo/v1/query",
            "{}",
            timestamp=123,
            nonce="A" * 32,
        )

        self.assertTrue(value.startswith('JOP appid="293992070061998081"'))
        self.assertIn('accesskey="access"', value)
        self.assertNotIn("very-secret", value)

    def test_settings_report_missing_environment_without_guessing_app_id(self) -> None:
        settings = JLCOpenApiSettings.from_env({})

        self.assertFalse(settings.configured)
        self.assertEqual(
            settings.missing_variables,
            [APP_ID_ENV, ACCESS_KEY_ENV, SECRET_KEY_ENV],
        )

    def test_signing_rejects_full_urls_and_malformed_nonce(self) -> None:
        with self.assertRaises(ValueError):
            signing_text(
                "https://open-api.jlc.com/demo",
                "{}",
                timestamp=1,
                nonce="A" * 32,
            )
        with self.assertRaises(ValueError):
            signing_text("/demo", "{}", timestamp=1, nonce="too-short")


if __name__ == "__main__":
    unittest.main()
