import unittest
from unittest.mock import AsyncMock, patch

from hidemyemail_generator.main import RichHideMyEmail


class HideMyEmailGenerationRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reserve_is_retried_after_unconfirmed_response(self):
        hme = RichHideMyEmail(cookie_file="missing-cookie", no_output_file=True)
        hme.generate_email = AsyncMock(
            return_value={
                "success": True,
                "result": {"hme": "new_alias@icloud.com"},
            }
        )
        hme.reserve_email = AsyncMock(
            side_effect=[
                {"success": False, "error": {"errorMessage": "temporary"}},
                {"success": True},
            ]
        )

        with patch(
            "hidemyemail_generator.main.asyncio.sleep", new=AsyncMock()
        ) as sleep:
            email = await hme._generate_one("OpenAI 一键注册")

        self.assertEqual(email, "new_alias@icloud.com")
        self.assertEqual(hme.reserve_email.await_count, 2)
        sleep.assert_awaited_once()

    async def test_list_confirms_reserve_that_returned_an_error(self):
        hme = RichHideMyEmail(cookie_file="missing-cookie", no_output_file=True)
        hme.generate_email = AsyncMock(
            return_value={
                "success": True,
                "result": {"hme": "new_alias@icloud.com"},
            }
        )
        hme.reserve_email = AsyncMock(
            return_value={
                "success": False,
                "error": {"errorMessage": "response lost"},
            }
        )
        hme.list_email = AsyncMock(
            return_value={
                "success": True,
                "result": {
                    "hmeEmails": [
                        {"hme": "NEW_ALIAS@icloud.com", "isActive": True}
                    ]
                },
            }
        )

        with patch(
            "hidemyemail_generator.main.asyncio.sleep", new=AsyncMock()
        ):
            email = await hme._generate_one("OpenAI 一键注册")

        self.assertEqual(email, "new_alias@icloud.com")
        self.assertEqual(hme.reserve_email.await_count, 3)
        hme.list_email.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
