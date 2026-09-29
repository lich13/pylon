import tempfile
import unittest
from pathlib import Path
from urllib import error

from pylon.tasks.itunes import telegram_document


class TelegramDocumentTests(unittest.TestCase):
    def test_load_env_reads_required_values_and_optional_override(self):
        with tempfile.TemporaryDirectory() as td:
            env = Path(td) / ".env"
            env.write_text(
                "\n".join(
                    [
                        "TELEGRAM_BOT_TOKEN=token-value",
                        "TELEGRAM_HOME_CHANNEL=-1001234567890",
                        "CHARTOO_TELEGRAM_CHAT_ID=override-chat",
                    ]
                ),
                encoding="utf-8",
            )

            config = telegram_document.load_telegram_config(env)

        self.assertEqual(config.bot_token, "token-value")
        self.assertEqual(config.chat_id, "override-chat")

    def test_send_document_uses_multipart_request_without_leaking_token(self):
        captured = {}

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["body"] = request.data
            captured["content_type"] = request.headers["Content-type"]

            class Response:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return None

                def read(self):
                    return b'{"ok":true,"result":{"document":{"file_id":"abc"}}}'

            return Response()

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "daily.html"
            path.write_text("<!doctype html><title>x</title>", encoding="utf-8")

            result = telegram_document.send_document(
                telegram_document.TelegramConfig(bot_token="secret-token", chat_id="-1001234567890"),
                path,
                caption="Chartoo report",
                urlopen=fake_urlopen,
            )

        self.assertTrue(result["ok"])
        self.assertIn("/sendDocument", captured["url"])
        self.assertNotIn("secret-token", captured["body"].decode("utf-8", errors="ignore"))
        self.assertIn(b"-1001234567890", captured["body"])
        self.assertIn(b"daily.html", captured["body"])
        self.assertIn("multipart/form-data", captured["content_type"])

    def test_send_document_error_masks_bot_token(self):
        def fake_urlopen(request, timeout):
            raise error.URLError("failed with secret-token")

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "daily.html"
            path.write_text("x", encoding="utf-8")

            with self.assertRaisesRegex(telegram_document.TelegramDocumentError, r"\*\*\*"):
                telegram_document.send_document(
                    telegram_document.TelegramConfig(bot_token="secret-token", chat_id="-1001234567890"),
                    path,
                    urlopen=fake_urlopen,
                )


if __name__ == "__main__":
    unittest.main()
