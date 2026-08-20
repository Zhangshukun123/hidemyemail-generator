import itertools
import unittest
from types import SimpleNamespace
from unittest import mock

from hidemyemail_generator.card_link_proxy_resolver import CardLinkProxyResolver
from hidemyemail_generator.payment_proxy_pool import (
    PaymentProxyPoolError,
    PaymentProxyPoolPresenter,
    PaymentProxySource,
)


class PaymentProxyPoolPresenterTests(unittest.TestCase):
    def test_builds_primary_and_two_backups_with_distinct_measured_exits(self):
        urls = (f"http://candidate-{index}" for index in itertools.count(1))
        exits = iter(
            [
                "192.0.2.10",
                "192.0.2.20",
                "192.0.2.20",
                "192.0.2.30",
                "192.0.2.40",
            ]
        )
        resolver = CardLinkProxyResolver(
            health_detector=lambda *_args, **_kwargs: SimpleNamespace(
                success=True,
                country="GB",
                ip=next(exits),
            ),
            max_candidates=3,
        )
        presenter = PaymentProxyPoolPresenter(resolver)

        selection = presenter.build(
            [PaymentProxySource("card_link", "dynamic", lambda: next(urls))],
            "GB",
            excluded_exit_ips=["192.0.2.10"],
        )

        self.assertEqual(len(selection.candidates), 3)
        self.assertEqual(selection.backup_count, 2)
        self.assertEqual(
            [candidate.exit_ip for candidate in selection.candidates],
            ["192.0.2.20", "192.0.2.30", "192.0.2.40"],
        )
        self.assertEqual(len(set(selection.proxy_urls)), 3)
        self.assertEqual(len(set(selection.exit_fingerprints)), 3)

    def test_uses_second_source_when_first_cannot_make_a_fresh_exit(self):
        first_counter = itertools.count(1)
        second_counter = itertools.count(1)

        def detector(proxy_url, **_kwargs):
            if "first" in proxy_url:
                exit_ip = "192.0.2.10"
            else:
                exit_ip = f"192.0.2.{20 + next(second_counter)}"
            return SimpleNamespace(success=True, country="US", ip=exit_ip)

        presenter = PaymentProxyPoolPresenter(
            CardLinkProxyResolver(health_detector=detector, max_candidates=2)
        )
        selection = presenter.build(
            [
                PaymentProxySource(
                    "card_link",
                    "dynamic",
                    lambda: f"http://first-{next(first_counter)}",
                ),
                PaymentProxySource(
                    "registration",
                    "dynamic",
                    lambda: f"http://second-{next(second_counter)}",
                ),
            ],
            "US",
            excluded_exit_ips=["192.0.2.10"],
        )

        self.assertEqual(len(selection.candidates), 3)
        self.assertTrue(
            all(candidate.source == "registration" for candidate in selection.candidates)
        )
        self.assertIn("card_link", selection.exhausted_sources)

    def test_refuses_to_start_without_a_distinct_backup_exit(self):
        counter = itertools.count(1)
        presenter = PaymentProxyPoolPresenter(
            CardLinkProxyResolver(
                health_detector=lambda *_args, **_kwargs: SimpleNamespace(
                    success=True,
                    country="GB",
                    ip="192.0.2.10",
                ),
                max_candidates=2,
            )
        )

        with self.assertRaisesRegex(PaymentProxyPoolError, "至少需要 2 个"):
            presenter.build(
                [
                    PaymentProxySource(
                        "card_link",
                        "dynamic",
                        lambda: f"http://candidate-{next(counter)}",
                    )
                ],
                "GB",
                excluded_exit_ips=["192.0.2.1"],
            )

    def test_skips_clash_source_when_endpoints_are_not_pinned(self):
        unstable_factory = mock.Mock(return_value="http://127.0.0.1:7897")
        stable_counter = itertools.count(1)

        def detector(proxy_url, **_kwargs):
            suffix = int(proxy_url.rsplit("-", 1)[-1])
            return SimpleNamespace(
                success=True,
                country="JP",
                ip=f"192.0.2.{suffix + 20}",
            )

        presenter = PaymentProxyPoolPresenter(
            CardLinkProxyResolver(health_detector=detector, max_candidates=3)
        )
        selection = presenter.build(
            [
                PaymentProxySource(
                    "clash",
                    "clash",
                    unstable_factory,
                    stable_endpoints=False,
                ),
                PaymentProxySource(
                    "dynamic",
                    "dynamic",
                    lambda: f"http://stable-{next(stable_counter)}",
                ),
            ],
            "JP",
        )

        unstable_factory.assert_not_called()
        self.assertEqual(len(selection.candidates), 3)
        self.assertTrue(
            all(candidate.source == "dynamic" for candidate in selection.candidates)
        )
        self.assertIn("clash(端点不固定)", selection.exhausted_sources)

    def test_discards_candidate_when_same_url_changes_exit(self):
        moving_exits = iter(["192.0.2.10", "192.0.2.20"])
        stable_counter = itertools.count(30)

        def detector(proxy_url, **_kwargs):
            exit_ip = (
                next(moving_exits)
                if proxy_url == "http://shared-selector"
                else f"192.0.2.{int(proxy_url.rsplit('-', 1)[-1])}"
            )
            return SimpleNamespace(success=True, country="JP", ip=exit_ip)

        presenter = PaymentProxyPoolPresenter(
            CardLinkProxyResolver(health_detector=detector, max_candidates=3)
        )
        selection = presenter.build(
            [
                PaymentProxySource(
                    "moving",
                    "dynamic",
                    lambda: "http://shared-selector",
                ),
                PaymentProxySource(
                    "stable",
                    "dynamic",
                    lambda: f"http://stable-{next(stable_counter)}",
                ),
            ],
            "JP",
        )

        self.assertEqual(len(selection.candidates), 3)
        self.assertTrue(
            all(candidate.source == "stable" for candidate in selection.candidates)
        )
        self.assertNotIn("http://shared-selector", selection.proxy_urls)
        self.assertIn("moving(端点重复)", selection.exhausted_sources)

    def test_keeps_recent_exit_history_across_multiple_payments(self):
        counter = itertools.count(1)

        def detector(proxy_url, **_kwargs):
            suffix = int(proxy_url.rsplit("-", 1)[-1])
            return SimpleNamespace(
                success=True,
                country="GB",
                ip=f"192.0.2.{suffix}",
            )

        presenter = PaymentProxyPoolPresenter(
            CardLinkProxyResolver(health_detector=detector, max_candidates=3),
            recent_exit_limit=9,
        )
        source = PaymentProxySource(
            "dynamic",
            "dynamic",
            lambda: f"http://candidate-{next(counter)}",
        )

        first = presenter.build([source], "GB")
        second = presenter.build([source], "GB")

        first_exits = {candidate.exit_ip for candidate in first.candidates}
        second_exits = {candidate.exit_ip for candidate in second.candidates}
        self.assertTrue(first_exits.isdisjoint(second_exits))


if __name__ == "__main__":
    unittest.main()
