import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord_bot

NOW = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[1]


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return NOW.replace(tzinfo=None)
        return NOW.astimezone(tz)


class ParseTimeTests(unittest.TestCase):
    def test_parses_utc_z_suffix_and_fractional_seconds(self):
        result = discord_bot.parse_time("2025-01-01T12:00:00.123456789Z")
        self.assertEqual(result, datetime(2025, 1, 1, 12, 0, 0, 123456, UTC))

    def test_assigns_utc_to_naive_datetime(self):
        result = discord_bot.parse_time("2025-01-01T12:00:00")
        self.assertEqual(result, datetime(2025, 1, 1, 12, 0, tzinfo=UTC))

    def test_returns_none_for_missing_or_invalid_value(self):
        self.assertIsNone(discord_bot.parse_time(None))
        self.assertIsNone(discord_bot.parse_time("not a date"))


class ConfigurationValidationTests(unittest.TestCase):
    def test_float_setting_uses_default_for_missing_or_blank_values(self):
        self.assertEqual(discord_bot.parse_float_setting("SETTING", None, 5), 5)
        self.assertEqual(discord_bot.parse_float_setting("SETTING", "  ", 5), 5)

    def test_float_setting_parses_valid_number_and_boundaries(self):
        self.assertEqual(
            discord_bot.parse_float_setting(
                "ALERT_MAX_PRICE", "5000", 30, minimum=1, maximum=5000
            ),
            5000,
        )
        self.assertEqual(
            discord_bot.parse_float_setting(
                "ALERT_INTERVAL_MIN", "0.5", 5, minimum=0, minimum_exclusive=True
            ),
            0.5,
        )

    def test_float_setting_rejects_non_numeric_non_finite_and_out_of_range_values(self):
        invalid_values = (
            ("SETTING", "abc", 5, None, None, False, "must be a number"),
            ("SETTING", "NaN", 5, None, None, False, "finite number"),
            ("SETTING", "Infinity", 5, None, None, False, "finite number"),
            ("ALERT_INTERVAL_MIN", "0", 5, 0, None, True, "greater than 0"),
            ("ALERT_MAX_PRICE", "0", 30, 1, 5000, False, "at least 1"),
            ("ALERT_MAX_PRICE", "5001", 30, 1, 5000, False, "at most 5000"),
        )
        for name, value, default, minimum, maximum, exclusive, message in invalid_values:
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, message),
            ):
                discord_bot.parse_float_setting(
                    name,
                    value,
                    default,
                    minimum=minimum,
                    maximum=maximum,
                    minimum_exclusive=exclusive,
                )

    def test_optional_discord_id_accepts_empty_or_numeric_value(self):
        self.assertIsNone(discord_bot.parse_optional_discord_id("GUILD_ID", " "))
        self.assertEqual(
            discord_bot.parse_optional_discord_id("GUILD_ID", "123456789"),
            "123456789",
        )
        self.assertEqual(
            discord_bot.parse_optional_discord_id("GUILD_ID", " 123456789 "),
            "123456789",
        )

    def test_optional_discord_id_rejects_non_numeric_value(self):
        for value in ("abc123", "²", "١٢٣"):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(ValueError, "GUILD_ID.*only digits"),
            ):
                discord_bot.parse_optional_discord_id("GUILD_ID", value)


class ApiPayloadTests(unittest.TestCase):
    def test_unpack_accepts_list_and_paginated_payloads(self):
        rows = [{"id": "one"}]
        self.assertEqual(discord_bot.unpack(rows), (rows, None))
        self.assertEqual(
            discord_bot.unpack({"data": rows, "cursor": "next"}),
            (rows, "next"),
        )

    def test_ref_price_prefers_csfloat_and_falls_back_to_steam(self):
        self.assertEqual(
            discord_bot.ref_price({
                "reference": {"predicted_price": 1200, "base_price": 1000},
                "item": {"scm": {"price": 900}},
            }),
            (1200, "ref"),
        )
        self.assertEqual(
            discord_bot.ref_price({"item": {"scm": {"price": 900}}}),
            (900, "steam"),
        )
        self.assertEqual(discord_bot.ref_price({}), (None, None))


class ApiRequestTests(unittest.TestCase):
    def test_api_get_retries_rate_limit_then_returns_response(self):
        rate_limited = Mock(status_code=429, headers={"Retry-After": "3"})
        success = Mock(status_code=200)
        success.json.return_value = {"data": []}
        session = Mock()
        session.get.side_effect = [rate_limited, success]

        with patch.object(discord_bot.time, "sleep") as sleep:
            result = discord_bot.api_get(session, {"limit": 5})

        self.assertEqual(result, {"data": []})
        self.assertEqual(session.get.call_count, 2)
        sleep.assert_called_once_with(3)

    def test_api_get_raises_for_unauthorized_without_retry(self):
        session = Mock()
        session.get.return_value = Mock(status_code=401)

        with self.assertRaisesRegex(discord_bot.CSFloatError, "401.*CSFLOAT_API_KEY"):
            discord_bot.api_get(session, {})

        session.get.assert_called_once()

    def test_api_get_raises_after_exhausting_rate_limit_retries(self):
        session = Mock()
        session.get.return_value = Mock(
            status_code=429,
            headers={"Retry-After": "120"},
        )

        with (
            patch.object(discord_bot.time, "sleep") as sleep,
            self.assertRaisesRegex(discord_bot.CSFloatError, "429"),
        ):
            discord_bot.api_get(session, {})

        self.assertEqual(session.get.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [60, 60, 60])


class SeenAuctionsStateTests(unittest.TestCase):
    def test_load_seen_returns_empty_when_state_file_is_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            self.assertEqual(discord_bot.load_seen(path, now=NOW), {})

    def test_save_and_load_seen_preserves_active_auctions_and_drops_expired(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            discord_bot.save_seen(
                {
                    "active": NOW + timedelta(hours=1),
                    "expired": NOW - timedelta(minutes=1),
                },
                path,
            )
            loaded = discord_bot.load_seen(path, now=NOW)
            saved = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(loaded, {"active": NOW + timedelta(hours=1)})
        self.assertEqual(saved, {
            "active": "2025-01-01T13:00:00+00:00",
            "expired": "2025-01-01T11:59:00+00:00",
        })

    def test_load_seen_skips_invalid_entries_and_keeps_valid_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "seen.json"
            path.write_text(
                json.dumps({
                    "valid": (NOW + timedelta(hours=1)).isoformat(),
                    "invalid-date": "not a date",
                    "invalid-type": 123,
                }),
                encoding="utf-8",
            )

            with self.assertLogs(discord_bot.logger, level="WARNING") as logs:
                loaded = discord_bot.load_seen(path, now=NOW)

        self.assertEqual(loaded, {"valid": NOW + timedelta(hours=1)})
        self.assertEqual(len(logs.records), 2)

    def test_manual_seen_state_is_saved_and_loaded_per_user(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manual_seen.json"
            state = {
                "user-1": {"active": NOW + timedelta(hours=1)},
                "user-2": {"expired": NOW - timedelta(minutes=1)},
            }
            discord_bot.save_manual_seen(state, path)
            loaded = discord_bot.load_manual_seen(path, now=NOW)

        self.assertEqual(loaded, {"user-1": {"active": NOW + timedelta(hours=1)}})


class SetupHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_setup_hook_starts_alerts_if_state_file_cannot_be_loaded(self):
        client = discord_bot.RadarBot()
        with (
            patch.object(discord_bot, "GUILD_ID", ""),
            patch.object(discord_bot, "ALERT_CHANNEL_ID", "123456"),
            patch.object(discord_bot, "ALERT_INTERVAL_MIN", 5),
            patch.object(client.tree, "sync", new=AsyncMock()),
            patch.object(discord_bot, "load_seen", side_effect=RuntimeError("invalid JSON")),
            patch.object(discord_bot.alert_loop, "change_interval") as change_interval,
            patch.object(discord_bot.alert_loop, "start") as start,
            self.assertLogs(discord_bot.logger, level="WARNING") as logs,
        ):
            await client.setup_hook()

        self.assertEqual(client.seen, {})
        change_interval.assert_called_once_with(minutes=5)
        start.assert_called_once_with()
        self.assertIn("starting with an empty state", logs.output[0])


class ListingSearchTests(unittest.TestCase):
    def test_find_auctions_filters_invalid_rows_and_sorts_by_discount(self):
        expires_soon = (NOW + timedelta(hours=1)).isoformat()
        expires_later = (NOW + timedelta(hours=2)).isoformat()
        payload = [
            {
                "id": "smaller-discount",
                "state": "listed",
                "price": 800,
                "auction_details": {"expires_at": expires_soon, "min_next_bid": 800},
                "reference": {"predicted_price": 1000},
            },
            {
                "id": "larger-discount",
                "state": "listed",
                "price": 500,
                "auction_details": {"expires_at": expires_later, "min_next_bid": 500},
                "reference": {"predicted_price": 1000},
            },
            {
                "id": "expired",
                "state": "listed",
                "auction_details": {
                    "expires_at": (NOW - timedelta(minutes=1)).isoformat(),
                },
                "reference": {"predicted_price": 1000},
            },
            {
                "id": "too-late",
                "state": "listed",
                "auction_details": {
                    "expires_at": (NOW + timedelta(hours=5)).isoformat(),
                },
                "reference": {"predicted_price": 1000},
            },
            {
                "id": "not-listed",
                "state": "sold",
                "auction_details": {"expires_at": expires_soon},
                "reference": {"predicted_price": 1000},
            },
        ]
        with (
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(discord_bot, "make_session"),
            patch.object(discord_bot, "api_get", return_value=payload),
        ):
            results = discord_bot.find_auctions(25, 4, pages=1)

        self.assertEqual([row["id"] for row in results], [
            "larger-discount",
            "smaller-discount",
        ])
        self.assertEqual(results[0]["disc"], 50)

    def test_find_deals_filters_unlisted_and_unpriced_rows_and_sorts(self):
        payload = [
            {
                "id": "lower-discount",
                "state": "listed",
                "price": 800,
                "reference": {"predicted_price": 1000},
                "created_at": NOW.isoformat(),
            },
            {
                "id": "higher-discount",
                "state": "listed",
                "price": 500,
                "reference": {"predicted_price": 1000},
                "created_at": NOW.isoformat(),
            },
            {
                "id": "sold",
                "state": "sold",
                "price": 100,
                "reference": {"predicted_price": 1000},
            },
            {
                "id": "no-reference",
                "state": "listed",
                "price": 100,
            },
        ]
        with (
            patch.object(discord_bot, "make_session"),
            patch.object(discord_bot, "api_get", return_value=payload) as api_get,
        ):
            results = discord_bot.find_deals(100, 10, "highest_discount", 20, 1)

        self.assertEqual([row["id"] for row in results], [
            "higher-discount",
            "lower-discount",
        ])
        self.assertEqual(results[0]["disc"], 50)
        self.assertEqual(api_get.call_args.args[1]["max_price"], 10000)
        self.assertEqual(api_get.call_args.args[1]["min_price"], 1000)
        self.assertEqual(api_get.call_args.args[1]["min_ref_qty"], 20)


class ListingFormatTests(unittest.TestCase):
    def test_auction_includes_time_remaining_and_float(self):
        row = {
            "kind": "auction",
            "id": "auction-id",
            "exp": NOW + timedelta(hours=2, minutes=15),
            "bid": 1490,
            "ref": 1820,
            "steam": False,
            "disc": 18.2,
            "float": 0.1234,
            "name": "AK-47 | Neon Revolution",
        }
        with patch.object(discord_bot, "datetime", FrozenDateTime):
            result = discord_bot.fmt_row(row)

        self.assertEqual(
            result,
            "**+18.2%** [AK-47 | Neon Revolution]"
            "(https://csfloat.com/item/auction-id)\n"
            "bid $14.90 · ref $18.20 · 2h 15m · float 0.1234",
        )

    def test_deal_includes_float_and_listing_age(self):
        row = {
            "kind": "deal",
            "id": "deal-id",
            "bid": 950,
            "ref": 1080,
            "steam": False,
            "disc": 12.5,
            "float": 0.0345,
            "name": "USP-S | Royal Blue",
            "offer": None,
            "created": NOW - timedelta(minutes=18),
        }
        with patch.object(discord_bot, "datetime", FrozenDateTime):
            result = discord_bot.fmt_row(row)

        self.assertEqual(
            result,
            "**+12.5%** [USP-S | Royal Blue]"
            "(https://csfloat.com/item/deal-id)\n"
            "price $9.50 · ref $10.80 · float 0.0345 · 18m ago",
        )


class SlashCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_registered_slash_commands_have_expected_names_and_options(self):
        auctions = discord_bot.client.tree.get_command("auctions")
        deals = discord_bot.client.tree.get_command("deals")

        self.assertIsNotNone(auctions)
        self.assertIsNotNone(deals)
        self.assertEqual(
            {parameter.name for parameter in auctions.parameters},
            {"max_price", "hours", "pages", "top", "min_discount"},
        )
        pages = next(parameter for parameter in auctions.parameters if parameter.name == "pages")
        self.assertEqual((pages.min_value, pages.max_value, pages.default), (1, 50, 10))
        self.assertEqual(
            {parameter.name for parameter in deals.parameters},
            {"max_price", "min_price", "min_discount", "top", "sort", "min_sales"},
        )

    async def test_auctions_command_defers_and_reports_csfloat_errors(self):
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        loop = SimpleNamespace(
            run_in_executor=AsyncMock(side_effect=discord_bot.CSFloatError("API error"))
        )

        with patch.object(discord_bot.asyncio, "get_running_loop", return_value=loop):
            await discord_bot.auctions.callback(interaction)

        interaction.response.defer.assert_awaited_once_with(thinking=True)
        interaction.followup.send.assert_awaited_once_with(
            "Could not retrieve CSFloat data: API error"
        )

    async def test_deals_command_uses_sort_choice_and_sends_results(self):
        interaction = SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
        )
        rows = [{
            "kind": "deal",
            "id": "deal",
            "bid": 800,
            "ref": 1000,
            "steam": False,
            "disc": 20,
            "float": None,
            "name": "Example Skin",
            "offer": None,
            "created": None,
        }]
        loop = SimpleNamespace(run_in_executor=AsyncMock(return_value=rows))
        sort = discord_bot.app_commands.Choice(name="Newest", value="most_recent")

        with patch.object(discord_bot.asyncio, "get_running_loop", return_value=loop):
            await discord_bot.deals.callback(interaction, sort=sort)

        loop.run_in_executor.assert_awaited_once_with(
            None, discord_bot.find_deals, 30.0, 1.0, "most_recent", 20, 4
        )
        interaction.response.defer.assert_awaited_once_with(thinking=True)
        interaction.followup.send.assert_awaited_once()
        self.assertIn("embed", interaction.followup.send.await_args.kwargs)


class ConfigurationImportTests(unittest.TestCase):
    """Import the module for real: an invalid .env value must stop the bot at startup."""

    def import_with_env(self, **overrides):
        env = {
            **os.environ,
            "PYTHONIOENCODING": "utf-8",
            "GUILD_ID": "",
            "ALERT_CHANNEL_ID": "",
            "ALERT_INTERVAL_MIN": "5",
            "ALERT_MAX_PRICE": "30",
            "ALERT_HOURS": "6",
            "ALERT_MIN_DISCOUNT": "15",
        }
        env.update(overrides)
        return subprocess.run(
            [sys.executable, "-c", "import discord_bot"],
            cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
            timeout=60, check=False,
        )

    def test_one_minute_alert_interval_is_accepted(self):
        result = self.import_with_env(ALERT_INTERVAL_MIN="1")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_alert_interval_below_one_minute_is_rejected(self):
        for value in ("0.5", "0.0001", "0"):
            with self.subTest(value=value):
                result = self.import_with_env(ALERT_INTERVAL_MIN=value)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ALERT_INTERVAL_MIN", result.stderr)


def make_deal_row(index, steam=False):
    return {
        "kind": "deal", "id": f"deal-{index}", "bid": 950 + index, "ref": 1080, "steam": steam,
        "disc": 12.0 - index * 0.1, "float": 0.0712, "name": "★ Butterfly Knife | Doppler (Factory New)",
        "offer": 900, "created": None,
    }


def make_auction_row(index):
    return {
        "kind": "auction", "id": f"auction-{index}", "bid": 1490 + index, "ref": 1820, "steam": False,
        "disc": 20.0 - index * 0.3, "exp": NOW + timedelta(hours=1, minutes=index), "float": 0.1234,
        "name": "★ Butterfly Knife | Doppler (Factory New)",
    }


class ResultSplittingTests(unittest.TestCase):
    def test_long_results_are_split_without_losing_or_reordering_rows(self):
        rows = [make_deal_row(i) for i in range(30)]
        embeds = discord_bot.build_embeds(rows, "Deals")

        self.assertGreater(len(embeds), 1)
        for number, embed in enumerate(embeds, 1):
            self.assertEqual(embed.title, f"Deals ({number}/{len(embeds)})")
            self.assertLessEqual(len(embed.description), discord_bot.EMBED_TEXT_LIMIT)
        text = "\n".join(embed.description for embed in embeds)
        positions = []
        for i in range(30):
            self.assertEqual(text.count(f"/item/deal-{i})"), 1, f"row {i}")
            positions.append(text.index(f"/item/deal-{i})"))
        self.assertEqual(positions, sorted(positions))

    def test_short_results_stay_in_one_embed_with_the_plain_title(self):
        embeds = discord_bot.build_embeds([make_deal_row(0), make_deal_row(1)], "Deals")
        self.assertEqual([embed.title for embed in embeds], ["Deals"])

    def test_empty_results_produce_no_embeds(self):
        self.assertEqual(discord_bot.build_embeds([], "Deals"), [])

    def test_steam_note_appears_only_on_the_embed_that_needs_it(self):
        rows = [make_deal_row(i, steam=(i == 29)) for i in range(30)]
        embeds = discord_bot.build_embeds(rows, "Deals")
        self.assertIsNone(embeds[0].footer.text)
        self.assertEqual(embeds[-1].footer.text, discord_bot.STEAM_FOOTER)

    def test_note_goes_to_the_last_embed_only_and_joins_the_steam_note(self):
        rows = [make_deal_row(i, steam=(i == 29)) for i in range(30)]
        embeds = discord_bot.build_embeds(rows, "Deals", note="NOTE")
        self.assertIsNone(embeds[0].footer.text)
        self.assertEqual(embeds[-1].footer.text, f"{discord_bot.STEAM_FOOTER}\nNOTE")


class AuctionScanLimitTests(unittest.TestCase):
    def lot(self, hours_ahead):
        return {
            "id": f"lot-{hours_ahead}",
            "state": "listed",
            "price": 800,
            "auction_details": {"expires_at": (NOW + timedelta(hours=hours_ahead)).isoformat()},
            "reference": {"predicted_price": 1000},
        }

    def scan(self, pages, *responses, hours=24):
        with (
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(discord_bot, "make_session"),
            patch.object(discord_bot, "api_get", side_effect=list(responses)),
        ):
            return discord_bot.find_auctions(30, hours, pages=pages)

    def test_flag_is_set_when_the_page_limit_stops_the_scan(self):
        result = self.scan(
            2, {"data": [self.lot(1)], "cursor": "c1"}, {"data": [self.lot(2)], "cursor": "c2"})
        self.assertEqual(len(result), 2)
        self.assertTrue(result.page_limit_hit)

    def test_flag_is_not_set_when_all_results_were_read(self):
        result = self.scan(
            2, {"data": [self.lot(1)], "cursor": "c1"}, {"data": [self.lot(2)], "cursor": None})
        self.assertFalse(result.page_limit_hit)

    def test_flag_is_not_set_when_the_end_of_the_window_was_reached(self):
        result = self.scan(2, {"data": [self.lot(1), self.lot(30)], "cursor": "c1"})
        self.assertEqual([row["id"] for row in result], ["lot-1"])
        self.assertFalse(result.page_limit_hit)

class LongResultCommandTests(unittest.IsolatedAsyncioTestCase):
    def make_interaction(self, user_id=123):
        return SimpleNamespace(
            response=SimpleNamespace(defer=AsyncMock()),
            followup=SimpleNamespace(send=AsyncMock()),
            user=SimpleNamespace(id=user_id),
        )

    async def run_auctions(self, found, *, user_id=123, manual_seen=None, **options):
        interaction = self.make_interaction(user_id)
        loop = SimpleNamespace(run_in_executor=AsyncMock(return_value=found))
        state = manual_seen if manual_seen is not None else {}
        with (
            patch.object(discord_bot.client, "manual_seen", state),
            patch.object(discord_bot, "save_manual_seen"),
            patch.object(discord_bot, "datetime", FrozenDateTime),
            patch.object(discord_bot.asyncio, "get_running_loop", return_value=loop),
        ):
            await discord_bot.auctions.callback(interaction, **options)
        return interaction, loop

    def sent_embeds(self, interaction):
        return [
            call.kwargs["embed"]
            for call in interaction.followup.send.await_args_list
            if "embed" in call.kwargs
        ]

    async def test_auctions_sends_every_requested_listing_in_several_messages(self):
        found = discord_bot.SearchResult([make_auction_row(i) for i in range(30)], page_limit_hit=True)
        interaction, loop = await self.run_auctions(found, hours=48.0, top=30)

        loop.run_in_executor.assert_awaited_once_with(None, discord_bot.find_auctions, 30.0, 48.0, 10)
        embeds = self.sent_embeds(interaction)
        self.assertGreater(len(embeds), 1)
        self.assertEqual(sum(embed.description.count("/item/auction-") for embed in embeds), 30)
        self.assertIsNone(embeds[0].footer.text)
        self.assertIn("500 auctions ending soonest", embeds[-1].footer.text)

    async def test_auctions_scans_500_listings_for_the_default_window(self):
        _, loop = await self.run_auctions(discord_bot.SearchResult([make_auction_row(0)]))
        loop.run_in_executor.assert_awaited_once_with(None, discord_bot.find_auctions, 30.0, 12.0, 10)

    async def test_auctions_uses_the_requested_page_limit(self):
        _, loop = await self.run_auctions(discord_bot.SearchResult([make_auction_row(0)]), pages=3)
        loop.run_in_executor.assert_awaited_once_with(None, discord_bot.find_auctions, 30.0, 12.0, 3)

    async def test_auctions_warns_when_using_50_pages(self):
        interaction, _ = await self.run_auctions([make_auction_row(0)], pages=50)
        (embed,) = self.sent_embeds(interaction)
        self.assertIn("up to 50 requests to CSFloat", embed.footer.text)

    async def test_auctions_does_not_repeat_seen_listings_for_the_same_user(self):
        state = {}
        first, _ = await self.run_auctions([make_auction_row(0)], manual_seen=state)
        self.assertIn("auction-0", state.get("123", {}))
        second, _ = await self.run_auctions([make_auction_row(0)], manual_seen=state)

        self.assertEqual(len(self.sent_embeds(first)), 1)
        self.assertEqual(self.sent_embeds(second), [])
        self.assertIn("already been shown to you", second.followup.send.await_args.args[0])

    async def test_auctions_seen_history_is_isolated_per_user(self):
        state = {"123": {"auction-0": NOW + timedelta(hours=1)}}
        interaction, _ = await self.run_auctions(
            [make_auction_row(0)], user_id=456, manual_seen=state
        )

        self.assertEqual(len(self.sent_embeds(interaction)), 1)
        self.assertIn("456", state)

    async def test_auctions_has_no_scan_note_when_everything_was_scanned(self):
        interaction, _ = await self.run_auctions(discord_bot.SearchResult([make_auction_row(0)]))
        (embed,) = self.sent_embeds(interaction)
        self.assertIsNone(embed.footer.text)

    async def test_auctions_accepts_a_plain_list_from_the_search(self):
        interaction, _ = await self.run_auctions([make_auction_row(0)])
        (embed,) = self.sent_embeds(interaction)
        self.assertIsNone(embed.footer.text)

    async def test_auctions_empty_result_mentions_an_incomplete_scan(self):
        interaction, _ = await self.run_auctions(discord_bot.SearchResult([], page_limit_hit=True))
        message = interaction.followup.send.await_args.args[0]
        self.assertIn("No listings found", message)
        self.assertIn("500 auctions ending soonest", message)

    async def test_deals_splits_long_results_into_several_messages(self):
        interaction = self.make_interaction()
        rows = [make_deal_row(i) for i in range(30)]
        loop = SimpleNamespace(run_in_executor=AsyncMock(return_value=rows))
        with patch.object(discord_bot.asyncio, "get_running_loop", return_value=loop):
            await discord_bot.deals.callback(interaction, top=30, min_discount=0.0)

        embeds = self.sent_embeds(interaction)
        self.assertGreater(len(embeds), 1)
        self.assertEqual(sum(embed.description.count("/item/deal-") for embed in embeds), 30)


if __name__ == "__main__":
    unittest.main()
