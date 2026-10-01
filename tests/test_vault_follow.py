import pytest
import requests

import VaultFollow

VAULTS_URL = VaultFollow.Signals.vaults_url


def make_vault(vault_id, owner, collateral_value, symbols, loan_value=10):
    return {
        "vaultId": vault_id,
        "ownerAddress": owner,
        "collateralValue": str(collateral_value),
        "loanValue": str(loan_value),
        "collateralAmounts": [{"symbol": symbol, "amount": "1.00000000"} for symbol in symbols],
    }


class FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self.text = str(body)
        self._body = body

    def json(self):
        return self._body


class FakeOceanApi:
    """Answers like the DeFiChain Ocean API: paginated vault list plus per-vault details."""

    def __init__(self):
        self.pages = [[]]
        self.details = {}  # vault ID -> current details; any other vault gets a 404
        self.list_failure = None  # HTTP status code or exception for the vault list

    def get(self, url, params=None, timeout=None):
        assert timeout is not None, "every request needs a timeout"
        if url == VAULTS_URL:
            if isinstance(self.list_failure, Exception):
                raise self.list_failure
            if self.list_failure is not None:
                return FakeResponse(self.list_failure, {"error": "unavailable"})
            page = int(params.get("next", 0))
            body = {"data": self.pages[page]}
            if page + 1 < len(self.pages):
                body["page"] = {"next": str(page + 1)}
            return FakeResponse(200, body)

        vault_id = url.rsplit("/", 1)[-1]
        if vault_id not in self.details:
            return FakeResponse(404, {"error": {"code": 404, "message": "Unable to find vault"}})
        return FakeResponse(200, {"data": self.details[vault_id]})


@pytest.fixture
def api(monkeypatch):
    fake = FakeOceanApi()
    monkeypatch.setattr(VaultFollow.requests, "get", fake.get)
    return fake


@pytest.fixture
def bot():
    return VaultFollow.Signals()


@pytest.mark.parametrize("ratio, size", [(100, -1.0), (150, -1.0), (175, 0.0), (200, 1.0), (300, 1.0)])
def test_signal_size_follows_collateral_ratio(bot, ratio, size):
    bot.decide_signal("BTC", ratio)

    [(src, sym, kwargs)] = bot.signals
    assert (src, sym) == ("woo", "PERP_BTC_USDT")
    assert kwargs["size"] == pytest.approx(size)


def test_fetch_follows_every_page_and_keeps_vaults_with_loans_and_collateral(bot, api):
    api.pages = [
        [make_vault("a", "owner1", 100, ["BTC"])],
        [make_vault("no-loan", "owner2", 100, ["ETH"], loan_value=0)],
        [make_vault("no-collateral", "owner3", 0, ["BTC"])],
        [make_vault("d", "owner4", 100, ["DFI"])],
    ]

    assert [vault["vaultId"] for vault in bot.get_active_vaults()] == ["a", "d"]


def test_owners_are_ranked_by_total_collateral_across_their_vaults(bot, api):
    bot.top_owner_number = 1
    api.pages = [[
        make_vault("single", "whale", 100, ["BTC"]),
        make_vault("part-1", "spread", 60, ["DFI", "BTC", "ETH"]),
        make_vault("part-2", "spread", 60, ["ETH"]),
    ]]

    bot.refresh_vault_data()

    owner, info = bot.vaults_by_owner_collateral[0]
    assert owner == "spread"
    assert info["totalCollateralValue"] == 120  # part-1 holds two listed assets but counts once
    assert bot.top_vaults_by_asset == {"BTC": {"part-1"}, "ETH": {"part-1", "part-2"}}


@pytest.mark.parametrize("failure", [503, requests.Timeout("timed out")])
def test_failed_refresh_keeps_the_previous_vaults(bot, api, failure):
    api.pages = [[make_vault("a", "owner1", 100, ["BTC"])]]
    bot.refresh_vault_data()

    api.list_failure = failure
    bot.refresh_vault_data()

    assert bot.top_vaults_by_asset == {"BTC": {"a"}}


def test_vaults_stay_available_while_a_refresh_is_fetching(bot, api, monkeypatch):
    api.pages = [[make_vault("old", "owner1", 100, ["BTC"])]]
    bot.refresh_vault_data()

    api.pages = [[make_vault("new", "owner2", 100, ["BTC"])]]
    seen_while_fetching = []

    def get(url, params=None, timeout=None):
        seen_while_fetching.append(bot.top_vaults_by_asset)
        return api.get(url, params=params, timeout=timeout)

    monkeypatch.setattr(VaultFollow.requests, "get", get)
    bot.refresh_vault_data()

    assert seen_while_fetching == [{"BTC": {"old"}}]
    assert bot.top_vaults_by_asset == {"BTC": {"new"}}


def test_signal_is_collateral_weighted_and_skips_unusable_vaults(bot, api):
    api.pages = [[
        make_vault("low", "owner1", 100, ["BTC"]),
        make_vault("high", "owner2", 100, ["BTC"]),
        make_vault("closed", "owner3", 100, ["BTC"]),
        make_vault("dust-loan", "owner4", 100, ["BTC"]),
    ]]
    api.details = {
        "low": {"collateralRatio": "160", "collateralValue": "300"},
        "high": {"collateralRatio": "200", "collateralValue": "100"},
        "dust-loan": {"collateralRatio": "-1", "collateralValue": "1000"},
    }

    bot.refresh_vault_data()
    bot.fetch_vault_data()

    # (160 * 300 + 200 * 100) / (300 + 100) = 170
    assert bot.weighted_collateral_for_asset == {"BTC": pytest.approx(170)}
    [(src, sym, kwargs)] = bot.signals
    assert (src, sym) == ("woo", "PERP_BTC_USDT")
    assert kwargs["size"] == pytest.approx(-0.2)


def test_asset_without_usable_vaults_emits_no_signal(bot, api):
    api.pages = [[
        make_vault("closed", "owner1", 100, ["ETH"]),
        make_vault("emptied", "owner2", 100, ["ETH"]),
    ]]
    api.details = {"emptied": {"collateralRatio": "-1", "collateralValue": "0"}}

    bot.refresh_vault_data()
    bot.fetch_vault_data()

    assert bot.weighted_collateral_for_asset == {}
    assert bot.signals == []
