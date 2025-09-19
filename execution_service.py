from hyperliquid.utils import constants
import example_utils
import ccxt
import json
import os
from typing import Optional, Dict, Any
import requests
from math import log10, floor
from discord import SyncWebhook
from tracker import TradeTracker


specialAssets = ["PEPE", "SHIB", "FLOKI", "BONK"]


class HyperLiquidExecutionService:
    def __init__(self, password, webhook=None, tracker=None):
        self.address, self.info, self.exchange = example_utils.setup(
            constants.MAINNET_API_URL, skip_ws=True, password=password
        )

        # Load exchange from environment variable
        exchange = os.environ.get("EXCHANGE", "bybit")

        if exchange == "binance":
            self.ex = ccxt.binance()

        elif exchange == "bybit":
            self.ex = ccxt.bybit({"options": {"defaultType": "swap"}})

        else:
            print(f"Invalid exchange '{exchange}' in environment variable EXCHANGE")
            return

        # Use provided webhook or create new one
        if webhook is not None:
            self.webhook = webhook
        else:
            webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
            if not webhook_url:
                raise ValueError("DISCORD_WEBHOOK_URL environment variable is required")
            self.webhook = SyncWebhook.from_url(webhook_url)

        self.infoBook = self.get_infoForAll()
        self.tracker = tracker

        print("hyperliquid loaded")

    def get_last_price(self, rawAssetName: str):
        retry = 5
        while retry > 0:
            try:
                symbol = rawAssetName.upper() + "/USDT:USDT"
                r = self.ex.fetch_ticker(symbol)
                # print(f"Getting last price for {symbol} at price {r['last']}")

                return r["last"]
            except Exception as e:
                print(f"Cannot get last price for {symbol}: {e}")
                retry -= 1
                if retry > 0:
                    self.webhook.send(
                        f"@everyone Cannot get last price for {symbol}. Retrying in 10 secs. ({retry} attempts left)"
                    )
                    import time

                    time.sleep(1)  # Add sleep between retries
                else:
                    self.webhook.send(
                        f"@everyone Failed to get last price for {symbol} after 5 attempts. Error: {e}"
                    )
                    return None  # Return None when all retries fail

    def get_asset_name(self, rawAssetName: str):
        rawAssetName = rawAssetName.upper()
        if rawAssetName in specialAssets:
            rawAssetName = "k" + rawAssetName
        return rawAssetName

    def get_amtofopenpositions(self, rawAssetName: str):
        AssetName = self.get_asset_name(rawAssetName)

        openPositions = self.get_allOpenPositionsTicker()
        count = 0

        for i in openPositions:
            if i == AssetName:
                count += 1

        return count

    def get_leverage(self, rawAssetName: str) -> int:
        AssetName = self.get_asset_name(rawAssetName)

        user_state = self.info.user_state(self.address)

        lev = None
        print(user_state)
        for asset_position in user_state["assetPositions"]:
            if asset_position["position"]["coin"] == AssetName:
                lev = asset_position["position"]["leverage"]

        return lev["value"]

    def set_leverage(self, rawAssetName: str, leverage: int):
        AssetName = self.get_asset_name(rawAssetName)

        res = self.exchange.update_leverage(leverage, AssetName, False)
        # {'status': 'err', 'response': 'Cannot decrease leverage with open position.'}
        # {'status': 'ok', 'response': {'type': 'default'}}
        if res["status"] == "err":
            self.webhook.send(
                f'@everyone Error while setting leverage for {rawAssetName} \nError: {res["response"]}'
            )
        if res["status"] == "ok":
            self.webhook.send(
                f"@everyone Leverage set for {rawAssetName} to {leverage}"
            )

        return res

    def get_margin_summary(self):
        user_state = self.info.user_state(self.address)
        return user_state["marginSummary"]

    def get_all_open_orders(self):
        user_state = self.info.open_orders(self.address)
        return user_state

    def get_all_open_positions(self):
        user_state = self.info.user_state(self.address)
        # print(user_state)
        return user_state["assetPositions"]

    def get_allOpenPositionsTicker(self) -> list:
        res = self.get_all_open_positions()
        tickers = []
        for i in res:
            # print(i['position']['coin'])
            tickers.append(i["position"]["coin"])

        return tickers

    def get_infoForAll(self):
        url = "https://api.hyperliquid.xyz/info"
        headers = {"Content-Type": "application/json"}
        body = {"type": "meta"}

        res = requests.post(url, headers=headers, data=json.dumps(body))
        res = json.loads(res.text)

        return res["universe"]

    def get_info_forAsset(self, rawAssetName: str):
        AssetName = self.get_asset_name(rawAssetName)

        info = self.infoBook

        for asset in info:
            if asset["name"] == AssetName:
                return asset
        return None

    def get_correct_price(self, AssetName: str, price: float) -> float:
        if AssetName.startswith("k"):
            price = price * 1000
        return float(price)

    def round_to_5_sig_digs(self, x: float):
        return round(x, -int(floor(log10(abs(x))) - 4))

    def set_tp(self, AssetName: str, tpPrice: float, assetAmount: float, is_buy: bool):

        tpPrice = self.round_to_5_sig_digs(tpPrice)
        tpPrice = round(tpPrice, 6)

        take_profit_order_type = {
            "trigger": {"triggerPx": tpPrice, "isMarket": True, "tpsl": "tp"}
        }

        result = self.exchange.order(
            AssetName,
            is_buy,
            assetAmount,
            tpPrice,
            take_profit_order_type,
            reduce_only=True,
        )

        print(result)
        if result["status"] == "ok":
            try:
                resting = result["response"]["data"]["statuses"][0]["resting"]["oid"]
                self.webhook.send(f"@everyone TP Set for {AssetName} at {tpPrice}")
                self.webhook.send(f"{resting}")
            except KeyError:
                self.webhook.send(
                    f'@everyone Error while setting TP for {AssetName} \nError: {result["response"]["data"]["statuses"][0]["error"]}'
                )
        else:
            self.webhook.send(f"@everyone Error setting TP for {AssetName}.")
            self.webhook.send(f'Error: {result["response"]["error"]}')

    def set_sl(self, AssetName: str, slPrice: float, assetAmount: float, is_buy: bool):

        slPrice = self.round_to_5_sig_digs(
            slPrice
        )  # from documentation, max 5 sig figs
        slPrice = round(slPrice, 6)  # from documentation, max 6 decimal places

        stop_order_type = {
            "trigger": {"triggerPx": slPrice, "isMarket": True, "tpsl": "sl"}
        }

        result = self.exchange.order(
            AssetName, is_buy, assetAmount, slPrice, stop_order_type, reduce_only=True
        )

        print(result)
        if result["status"] == "ok":
            try:
                resting = result["response"]["data"]["statuses"][0]["resting"]["oid"]
                self.webhook.send(f"@everyone SL Set for {AssetName} at {slPrice}")
                self.webhook.send(f"{resting}")
            except KeyError:
                self.webhook.send(
                    f'@everyone Error while setting SL for {AssetName} \nError: {result["response"]["data"]["statuses"][0]["error"]}'
                )
        else:
            self.webhook.send(f"@everyone Error setting SL for {AssetName}.")
            self.webhook.send(f'Error: {result["response"]["error"]}')

    def get_decimals_forAsset(self, rawAssetName: str):
        AssetName = self.get_asset_name(rawAssetName)
        try:
            info = self.get_info_forAsset(AssetName)
            szDecimal = info["szDecimals"]
            return szDecimal
        except:
            return None

    def cancel_limit_order(self, rawAssetName: str, oid):
        AssetName = self.get_asset_name(rawAssetName)

        # When perpetual endpoints expect an integer for asset, use the index of the coin found in the meta info response. E.g. BTC = 0 on mainnet.
        oid = int(oid)

        order_result = self.exchange.cancel(AssetName, oid)

        if order_result["status"] == "ok":
            status = order_result["response"]["data"]["statuses"][0]
            if status == "success":
                self.webhook.send(
                    f"@everyone Limit Order for {rawAssetName} with oid {oid} has been cancelled successfully."
                )
                return

            try:
                error = status["error"]
                self.webhook.send(
                    f"@everyone Error while cancelling Limit Order for {rawAssetName} with oid {oid}. Error: {error}"
                )
            except KeyError:
                self.webhook.send(
                    f"@everyone UNKNOWN Error while cancelling Limit Order for {rawAssetName} with oid {oid}."
                )

    def cancel_all_orders(self, rawAssetName):
        AssetName = self.get_asset_name(rawAssetName)

        openOrders = self.get_all_open_orders()

        for i in openOrders:
            if i["coin"] == AssetName:
                oid = i["oid"]
                self.cancel_limit_order(rawAssetName, oid)

    def place_limit_order(
        self,
        rawAssetName: str,
        is_buy: bool,
        assetamount: float,
        price: float,
        reduce_only: bool = False,
    ) -> Optional[float]:
        AssetName = self.get_asset_name(rawAssetName)
        # TODO: change this to the same as market order (return dict)

        order_type = {"limit": {"tif": "Gtc"}}
        order_result = self.exchange.order(
            AssetName, is_buy, assetamount, price, order_type, reduce_only
        )

        if order_result["status"] == "ok":
            for status in order_result["response"]["data"]["statuses"]:
                # key resting or filled must exist, only 1 of them will exist
                filled = status.get("filled")
                resting = status.get("resting")

                if filled == None and resting == None:
                    self.webhook.send(f"@everyone Error while filling {rawAssetName}")
                    self.webhook.send(f'Error: {status["error"]}')
                    print(f'Error: {status["error"]}')
                    return None

                emoji = ":green_circle:" if is_buy else ":red_circle:"
                side = "Buy" if is_buy else "Sell"

                if filled != None:
                    self.webhook.send(
                        f'@everyone Limit Order filled to {side} {rawAssetName}; filled {filled["totalSz"]} @{filled["avgPx"]} {emoji}'
                    )
                    return float(filled["totalSz"])

                if resting != None:
                    self.webhook.send(f'{resting["oid"]} {emoji}')
                    return float(resting["oid"])

                return 1
        else:
            self.webhook.send(
                f"@everyone Error opening limit position for {rawAssetName}."
            )
            self.webhook.send(f'Error: {order_result["response"]["error"]}')
            return None

    def place_market_order(
        self, rawAssetName: str, is_buy: bool, assetamount: float
    ) -> Optional[Dict[str, Any]]:
        AssetName = self.get_asset_name(rawAssetName)
        order_result = self.exchange.market_open(AssetName, is_buy, assetamount)

        if order_result["status"] == "ok":
            for status in order_result["response"]["data"]["statuses"]:
                try:
                    filled = status["filled"]
                    emoji = ":green_circle:" if is_buy else ":red_circle:"
                    side = "Bought" if is_buy else "Sold"
                    self.webhook.send(
                        f'@everyone Market {side} {rawAssetName}; filled {filled["totalSz"]} @{filled["avgPx"]} {emoji}'
                    )
                    positionsize = float(filled["totalSz"]) * float(filled["avgPx"])
                    self.webhook.send(
                        f"@everyone Position Size is: {positionsize} USDT."
                    )

                    # Return both asset amount and order ID
                    return {
                        "asset_amount": float(filled["totalSz"]),
                        "order_id": status.get("filled", {}).get("oid")
                        or status.get("oid"),  # Order ID location may vary
                        "avg_price": float(filled["avgPx"]),
                        "total_usd": positionsize,
                    }
                except KeyError:
                    self.webhook.send(f"Error while filling {rawAssetName}")
                    self.webhook.send(f'Error: {status["error"]}')
                    print(f'Error: {status["error"]}')
                    return None
        else:
            self.webhook.send(
                f"@everyone Error opening position for {rawAssetName}. Retrying in 5."
            )
            self.webhook.send(f'Error: {order_result["response"]["error"]}')
            return None

    def generate_order(
        self,
        rawAssetName: str,
        USDTAmount: int,
        is_long: bool,
        setSl: bool = False,
        slPrice: Optional[float] = None,
        timeframe: str = "Unknown",
        leverage: Optional[int] = None,
        use_candle_close_sl: bool = True,  # Default to True for all trades
    ):

        # AssetName = rawAssetName.upper()
        # current_price = self.get_last_price(rawAssetName)

        # if AssetName in specialAssets:
        #     AssetName = "k" + rawAssetName
        #     current_price = current_price * 1000
        #     slPrice = slPrice * 1000 if slPrice != None else None

        AssetName = self.get_asset_name(rawAssetName)
        current_price = self.get_last_price(rawAssetName)
        current_price = self.get_correct_price(AssetName, current_price)

        if slPrice != None:
            slPrice = self.get_correct_price(AssetName, slPrice)

        if setSl:
            if slPrice == None:
                self.webhook.send(
                    f"@everyone SL Price is not set for {rawAssetName}. Unable to place order"
                )
                return

            # if slPrice > current_price:
            #     self.webhook.send(f"@everyone SL Price is higher than current price for {rawAssetName}. Unable to place order")
            #     return
            if slPrice > current_price and is_long:
                self.webhook.send(
                    f"@everyone SL Price is higher than current price for {rawAssetName}. Unable to place order"
                )
                return
            if slPrice < current_price and not is_long:
                self.webhook.send(
                    f"@everyone SL Price is lower than current price for {rawAssetName}. Unable to place order"
                )
                return

            risk_amount = USDTAmount
            pct_current_sl = (current_price - slPrice) / current_price
            USDTAmount = risk_amount / pct_current_sl

        info = self.get_info_forAsset(rawAssetName)
        szDecimal = info["szDecimals"]

        assetAmount = USDTAmount / current_price
        assetAmount = round(assetAmount, szDecimal)

        if szDecimal == 0:
            assetAmount = int(assetAmount)

        if assetAmount == 0:
            print("Asset Amount is 0")
            self.webhook.send(
                f"@everyone Asset Amount is 0 for {AssetName}. Unable to place order"
            )
            return

        print("Asset Amount: " + str(assetAmount))

        res = self.place_market_order(rawAssetName, is_long, assetAmount)

        if res != None:
            assetAmount = res["asset_amount"]
            order_id = res["order_id"]
            avg_price = res["avg_price"]
            total_usd = res["total_usd"]
            trade_id = self.tracker.add_trade(
                currency=rawAssetName,
                timeframe=timeframe,
                qty_usd=USDTAmount,
                qty_asset=assetAmount,
                entry_price=current_price,
                side="long" if is_long else "short",
                stop_loss_price=slPrice if slPrice else 0,
                hyperliquid_order_id=str(res),  # res is the order ID or filled amount
                leverage=leverage,
                use_candle_close_sl=use_candle_close_sl,
                candle_sl_timeframe=timeframe,  # Always use the same timeframe as the trade
            )
            self.webhook.send(
                f"@everyone Position Size is: {current_price * assetAmount} USDT."
            )

            if setSl:
                # negative is_long because we want to set SL for the opposite side
                # TODO: understand why SL is set after the order is placed, not with it.
                self.set_sl(AssetName, slPrice, res, not is_long)

    def close_position(self, rawAssetName: str):
        AssetName = self.get_asset_name(rawAssetName)

        if AssetName not in self.get_allOpenPositionsTicker():
            self.webhook.send(
                f"@everyone Error closing position for {rawAssetName}. Position does not exist."
            )
            return

        order_result = self.exchange.market_close(AssetName)

        if order_result["status"] == "ok":
            for status in order_result["response"]["data"]["statuses"]:
                try:
                    filled = status["filled"]
                    self.webhook.send(
                        f'@everyone Closed position for {rawAssetName}; filled {filled["totalSz"]} @{filled["avgPx"]}'
                    )
                except KeyError:
                    self.webhook.send(
                        f"(close position) Error while filling {rawAssetName}"
                    )
                    print(f'Error: {status["error"]}')

    def close_all_positions(self):
        openPositions = self.get_allOpenPositionsTicker()
        print(openPositions)
        for i in openPositions:
            order_result = self.exchange.market_close(i)

            if order_result["status"] == "ok":
                for status in order_result["response"]["data"]["statuses"]:
                    try:
                        filled = status["filled"]
                        self.webhook.send(
                            f'@everyone Closed position for {i}; filled {filled["totalSz"]} @{filled["avgPx"]}'
                        )
                    except KeyError:
                        self.webhook.send(f"(close position) Error while filling {i}")
                        print(f'Error: {status["error"]}')

    def get_totalAccValue(self) -> float:
        user_state = self.info.user_state(self.address)
        print(user_state)
        account_value = user_state["marginSummary"]["accountValue"]
        return float(account_value)

    def calculate_dynamic_usd_amount(
        self, min_amount: float = 20.0, portfolio_percentage: float = 0.02
    ) -> float:
        """
        Calculate dynamic USD amount for orders.
        Uses either minimum amount or 1% of total portfolio value, whichever is higher.

        Args:
            min_amount: Minimum USD amount (default: $20)
            portfolio_percentage: Percentage of portfolio to use (default: 0.02 = 2%)

        Returns:
            float: Calculated USD amount
        """
        try:
            total_value = self.get_totalAccValue()

            # Ensure total_value is a float
            if isinstance(total_value, str):
                total_value = float(total_value)
            elif not isinstance(total_value, (int, float)):
                total_value = float(total_value)

            calculated_amount = total_value * portfolio_percentage

            # Use the higher of minimum amount or calculated percentage
            dynamic_amount = max(min_amount, calculated_amount)

            print(f"Portfolio Value: ${total_value:.2f}")
            print(f"1% of Portfolio: ${calculated_amount:.2f}")
            print(f"Dynamic Amount: ${dynamic_amount:.2f}")

            return round(dynamic_amount, 2)

        except Exception as e:
            print(f"Error calculating dynamic amount: {e}")
            # Fallback to minimum amount if calculation fails
            return min_amount


# e = HyperLiquidExecutionService()
# e.set_leverage("PEPE", 3)
# e.generate_order("PEPE", 20, True, True, 0.00001)

# e.get_info_forAsset("BTC")

# e.set_sl("BTC", 67301.1, 0.00247)

# e.get_leverage("BTC")

# e.set_sl("kBONK", 0.03150770000000001, 10000)
# e.set_sl("kBONK", 0.031, 10000)


# e.generate_order("BTC", 10, True, True, 68100)
