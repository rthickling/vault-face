from profitview import Link, logger, cron
import requests


class Signals(Link):
    """A ProfitView signal bot that follows the largest DeFiChain vault owners.

    Every hour it rebuilds the list of vaults to follow; every minute it turns
    those vaults' collateral ratios into a position signal for each asset's
    BitMEX perpetual.
    """

    exchange = "bitmex"  # Example exchange
    assets = [  # Liquid perps on BitMEX
        { "asset": "BTC", "perp": "XBTUSD" },
        { "asset": "ETH", "perp": "ETHUSD" },
        { "asset": "XRP", "perp": "XRPUSD" },
        { "asset": "SOL", "perp": "SOLUSD" },
        { "asset": "SUI", "perp": "SUIUSD" },
        { "asset": "DOGE", "perp": "DOGEUSDT" },
        { "asset": "LTC", "perp": "LTCUSD" },
        { "asset": "ADA", "perp": "ADAUSD" },
        { "asset": "LINK", "perp": "LINKUSD" },
        { "asset": "PEPE", "perp": "PEPEUSD" },
        { "asset": "DOT", "perp": "DOTUSD" },
        { "asset": "BNB", "perp": "BNBUSD" },
        { "asset": "BCH", "perp": "BCHUSD" },
        { "asset": "AVAX", "perp": "AVAXUSD" },
        { "asset": "NEAR", "perp": "NEARUSD" },
        { "asset": "WLD", "perp": "WLDUSD" },
        { "asset": "APT", "perp": "APTUSD" },
        { "asset": "FIL", "perp": "FILUSD" },
        { "asset": "ARB", "perp": "ARBUSD" }
    ]
    asset_to_perp = {asset['asset']: asset['perp'] for asset in assets}

    vaults_url = "https://ocean.defichain.com/v0/mainnet/loans/vaults"
    request_timeout = 10  # Seconds per HTTP request

    top_owner_number = 10

    active_vaults = []
    vaults_by_asset = {}
    vaults_by_owner = {}
    vaults_by_owner_collateral = []
    top_vaults_by_asset = {}
    weighted_collateral_for_asset = {}

    @cron.run(every=3600)  # Refresh vaults to follow every hour
    def refresh_vault_data(self):
        logger.info("Re-computing vault data")

        active_vaults = self.get_active_vaults()
        if active_vaults is None:
            logger.warning("Vault refresh failed; keeping the previous vault data")
            return

        self.process_vaults(active_vaults)

    def get_active_vaults(self):
        """Fetch all vaults page by page; return those with loans and collateral, or None if a request fails"""
        params = {"size": 100}  # Requested page size; the API may return fewer
        all_vaults = []

        while True:
            try:
                response = requests.get(self.vaults_url, params=params, timeout=self.request_timeout)
            except requests.RequestException as e:
                logger.warning(f"Failed to fetch vault data: {e}")
                return None

            if response.status_code != 200:
                logger.warning(f"Failed to fetch vault data: {response.status_code}, {response.text}")
                return None

            data = response.json()
            all_vaults.extend(data["data"])

            # Check if there are more vaults to fetch
            if "page" not in data or not data["page"].get("next"):
                break

            params["next"] = data["page"]["next"]

        logger.info(f"Total vaults: {len(all_vaults)}")

        # Filter active vaults with loans and collateral
        return [
            vault for vault in all_vaults
            if float(vault.get("loanValue", 0)) > 0 and float(vault.get("collateralValue", 0)) > 0
        ]

    def process_vaults(self, active_vaults):
        # The per-minute signal task runs in another thread, so build everything
        # locally and only replace the bot's data once it is complete
        vaults_by_asset = {}
        vaults_by_owner = {}

        # Process vaults for all assets in the assets list
        for asset in self.assets:
            asset_symbol = asset['asset']
            asset_vaults = self.get_vaults_by_asset(asset_symbol, active_vaults)

            # Store the results in the dictionary
            vaults_by_asset[asset_symbol] = []

            for vault in asset_vaults:
                # Extract the specific collateral amount for this asset
                collateral_amount = next(
                    (c['amount'] for c in vault['collateralAmounts']
                     if c['symbol'] == asset_symbol),
                    '0.00000000'  # Default value if not found
                )

                # Create vault data structure
                vault_data = {
                    'ownerAddress': vault['ownerAddress'],
                    'vaultId': vault['vaultId'],
                    'collateralValue': float(vault['collateralValue']),
                    'collateralAmount': collateral_amount,
                    'symbols': [c['symbol'] for c in vault['collateralAmounts']]
                }

                # Add to the asset's vault list
                vaults_by_asset[asset_symbol].append(vault_data)

                # Update owner's total collateral. A vault holding several listed
                # assets is seen once per asset, so count each vault only once.
                owner_info = vaults_by_owner.setdefault(
                    vault['ownerAddress'], {'totalCollateralValue': 0.0, 'vaults': []}
                )
                if all(v['vaultId'] != vault['vaultId'] for v in owner_info['vaults']):
                    owner_info['vaults'].append(vault_data)
                    owner_info['totalCollateralValue'] += vault_data['collateralValue']

        # Sort owners by total collateral value in descending order
        vaults_by_owner_collateral = sorted(
            vaults_by_owner.items(),
            key=lambda x: x[1]['totalCollateralValue'],
            reverse=True
        )

        top_owners = vaults_by_owner_collateral[:self.top_owner_number]

        # Organize the top owners' vaults by the tradable assets they hold as collateral
        top_vaults_by_asset = {}
        for owner, vault_info in top_owners:
            for vault in vault_info['vaults']:
                for symbol in vault['symbols']:
                    if symbol in self.asset_to_perp:
                        top_vaults_by_asset.setdefault(symbol, set()).add(vault['vaultId'])

        logger.info(
            f"Following {len(top_owners)} owners: "
            + ", ".join(f"{symbol} {len(vault_ids)} vaults" for symbol, vault_ids in top_vaults_by_asset.items())
        )

        self.active_vaults = active_vaults
        self.vaults_by_asset = vaults_by_asset
        self.vaults_by_owner = vaults_by_owner
        self.vaults_by_owner_collateral = vaults_by_owner_collateral
        self.top_vaults_by_asset = top_vaults_by_asset

    def get_vaults_by_asset(self, asset_symbol, vaults):
        return sorted(
            [vault for vault in vaults if any(c['symbol'] == asset_symbol for c in vault.get('collateralAmounts', []))],
            key=lambda x: float(x['collateralValue']),
            reverse=True
        )

    def get_current_vault_details(self, vault_id):
        """Return the vault's current details, or None if they can't be fetched (for example, the vault was closed)"""
        url = f"{self.vaults_url}/{vault_id}"
        try:
            response = requests.get(url, timeout=self.request_timeout)
        except requests.RequestException as e:
            logger.warning(f"Failed to fetch vault {vault_id}: {e}")
            return None

        if response.status_code != 200:
            logger.warning(f"Failed to fetch vault {vault_id}: {response.status_code}")
            return None

        return response.json()['data']

    def get_weighted_collateral(self):
        weighted_collateral_for_asset = {}
        for asset, vault_ids in self.top_vaults_by_asset.items():
            total_collateral_for_asset = 0
            total_collateral_for_asset_ratio = 0
            for vault_id in vault_ids:
                vault_details = self.get_current_vault_details(vault_id)
                if vault_details is None:
                    continue
                collateral_ratio = float(vault_details.get('collateralRatio', -1))
                if collateral_ratio <= 0:  # The API reports -1 when it has no usable ratio for the vault
                    continue
                total_collateral = float(vault_details['collateralValue'])
                total_collateral_for_asset += total_collateral
                total_collateral_for_asset_ratio += collateral_ratio*total_collateral
            if total_collateral_for_asset > 0:
                weighted_collateral_for_asset[asset] = total_collateral_for_asset_ratio/total_collateral_for_asset
        self.weighted_collateral_for_asset = weighted_collateral_for_asset

    @cron.run(every=60)
    def fetch_vault_data(self):
        """Fetches the followed vaults' current collateral ratios and signals each asset's perp."""
        self.get_weighted_collateral()
        for asset, collateral_ratio in self.weighted_collateral_for_asset.items():
            self.decide_signal(asset, collateral_ratio)

    def decide_signal(self, asset, collateral_ratio):
        """Makes trading decisions based on the collateral ratio."""
        if collateral_ratio < 150:  # Risk of liquidation
            size = -1.0  # Go fully short
        elif collateral_ratio > 200:  # Vault is safe
            size = 1.0  # Go fully long
        else:
            # Smooth transition between -1 and 1 based on collateral ratio
            # Normalize ratio to 0-1 range between 150-200
            normalized_ratio = (collateral_ratio - 150) / 50
            # Map to -1 to 1 range
            size = 2 * normalized_ratio - 1

        logger.info(f"signal: Exchange {self.exchange}, Asset: {asset}, Contract: {self.asset_to_perp[asset]}, size: {size}")
        self.signal(self.exchange, self.asset_to_perp[asset], size=size)
