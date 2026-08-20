import unittest
from unittest.mock import AsyncMock

from hidemyemail_generator.hidemyemail import HideMyEmail


class HideMyEmailDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def test_deactivate_and_delete_use_anonymous_id(self):
        hme = HideMyEmail(cookies="test")
        hme._request_json = AsyncMock(return_value={"success": True})

        await hme.deactivate_email("anonymous-1")
        await hme.delete_email("anonymous-1")

        first = hme._request_json.await_args_list[0]
        second = hme._request_json.await_args_list[1]
        self.assertEqual(first.args[:2], ("POST", f"{hme.base_url_v1}/deactivate"))
        self.assertEqual(first.kwargs["json"], {"anonymousId": "anonymous-1"})
        self.assertEqual(second.args[:2], ("POST", f"{hme.base_url_v1}/delete"))
        self.assertEqual(second.kwargs["json"], {"anonymousId": "anonymous-1"})


if __name__ == "__main__":
    unittest.main()
