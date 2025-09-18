import discord
from screener_service import Screener
from screener_service import Asset
import asyncio
from execution_service import HyperLiquidExecutionService
from typing import Optional
import json
import math
import logging
import time
import os
from datetime import datetime
from tracker import TradeTracker

logger = logging.getLogger(__name__)


class CommunicationService(discord.Client):
    def __init__(
        self,
        hlbot: Optional[HyperLiquidExecutionService],
        screener: Screener,
        shared_webhook=None,  # Add shared webhook parameter
        tv_service=None,
        candle_sl_manager=None,
        tracker: TradeTracker = None,  # Add tracker parameter
        error_logger=None,  # Add error logger parameter
        *args,
        **kwargs,
    ):
        i = discord.Intents.all()
        super().__init__(intents=i)
        self.tv_service = tv_service
        self.screener: Screener = screener
        self.hlbot: Optional[HyperLiquidExecutionService] = hlbot
        self.candle_sl_manager = candle_sl_manager
        self.tracker = tracker  # Store the shared tracker
        self.error_logger = error_logger  # Store the error logger
        
        # Load config from environment variables
        self.config = {
            'discord_channel_id': int(os.environ.get('DISCORD_CHANNEL_ID', '0'))
        }
        
        self.shared_webhook = shared_webhook  # Store shared webhook
        print("Comms loaded.")

    async def on_ready(self):
        print(f"Logged in as {self.user}")
        await self.send_message(
            "Bot is online."
        )

    def get_assetListMsg(self):
        if len(self.screener.assets) == 0:
            return "No assets in the list"

        msg = ""

        for a in self.screener.assets:
            a: Asset
            trend = "Up" if a.hmaTrend else "down"
            trend = "Initializing" if a.hmaTrend == None else trend
            ordertype = "SL Enabled" if a.setSl else "Market"
            ordertype_heading = "Buy with" if a.setSl == False else "Risk with"
            side = "Long" if a.is_longStrat else "Short"

            msg += (
                f"ID: {a.id} \nAsset: {a.coinpair} ({int(a.leverage)}x {side}) {a.tf} \nTrend: {a.hmalength} {trend} \n{ordertype_heading}: {a.txn_USDTAmount} ({ordertype}) \n"
                + "-" * 40
                + "\n"
            )

        hlbot = self.hlbot

        totalAccValue = round(float(hlbot.get_totalAccValue()), 2)

        msg += f"\nTotal Account Value: {totalAccValue}"
        backticks = "```"
        msg = backticks + msg + backticks
        return msg

    def create_hlbot(self, password):
        try:
            # Pass shared webhook to execution service
            hlbot = HyperLiquidExecutionService(password, self.shared_webhook)
            self.hlbot = hlbot
            self.screener.hyperliquidBot = hlbot
            return hlbot.address
        except Exception as e:
            print(f"Error creating hlbot: {e}")
            return False

    async def on_message(self, message: discord.Message):
        if message.author == self.user:
            return

        # Remove or comment out the $start command handler since we don't need it anymore
        # if message.content.startswith("$start"):
        #     # $start <password>
        #     args = message.content.split(" ")
        #     if len(args) != 2:
        #         await message.channel.send(
        #             "Invalid arguments\nUsage: $start <password>"
        #         )
        #         return

        #     password = args[1]
        #     res = self.create_hlbot(password)
        #     if res:
        #         await message.channel.send(f"running with account {res}")
        #     else:
        #         await message.channel.send("Failure")

        if message.content.startswith("$long"):
            # $long <rawAssetName> <usdt_size> <leverage>
            args = message.content.split(" ")
            print(args)
            if len(args) < 3:
                await message.channel.send(
                    "Invalid arguments\nUsage: $long <rawAssetName> <usdt_size> <leverage>"
                )
                return

            rawAssetName = args[1]
            size = float(args[2])

            if size < 10:
                await message.channel.send("Minimum size is 10 USDT")
                logger.error("Minimum size is 10 USDT")
                return "Minimum size is 10 USDT"

            hlbot = self.hlbot

            if len(args) == 4:
                leverage = int(args[3])
                hlbot.set_leverage(rawAssetName, leverage)

            hlbot.generate_order(rawAssetName, size, True)
            await message.channel.send(f"Longed {rawAssetName} with {size} USDT")

        if message.content.startswith("$short"):
            # $short <rawAssetName> <usdt_size> <leverage>
            args = message.content.split(" ")
            if len(args) < 3:
                await message.channel.send(
                    "Invalid arguments\nUsage: $short <rawAssetName> <usdt_size> <leverage>"
                )
                return

            rawAssetName = args[1]
            size = float(args[2])

            if size < 10:
                await message.channel.send("Minimum size is 10 USDT")
                logger.error("Minimum size is 10 USDT")
                return "Minimum size is 10 USDT"

            hlbot = self.hlbot

            if len(args) == 4:
                leverage = int(args[3])
                hlbot.set_leverage(rawAssetName, leverage)

            hlbot.generate_order(rawAssetName, size, False)
            await message.channel.send(f"Shorted {rawAssetName} with {size} USDT")

        if message.content.startswith("$limit"):
            # $limit <rawAssetName> <is_buy : 1/0> <usdt_Amount> <price> <reduce_only:1/0=0>
            args = message.content.split(" ")

            if len(args) != 6:
                await message.channel.send(
                    "Invalid arguments\nUsage: $limit <rawAssetName> <is_buy : 1/0> <usdt_Amount> <price> <reduce_only:1/0>"
                )
                return

            rawAssetName = args[1]
            is_buy = True if args[2] == "1" else False
            usdt_Amount = float(args[3])
            price = float(args[4])
            reduce_only = True if args[5] == "1" else False

            hlbot = self.hlbot
            exchange = hlbot.ex

            # get current price
            AssetName = hlbot.get_asset_name(rawAssetName)
            price = hlbot.get_correct_price(AssetName, price)

            # get asset amount
            assetAmount = usdt_Amount / price
            info = hlbot.get_info_forAsset(rawAssetName)
            szDecimal = info["szDecimals"]
            assetAmount = round(assetAmount, szDecimal)
            if szDecimal == 0:
                assetAmount = int(assetAmount)

            if assetAmount == 0:
                print("Asset Amount is 0")
                await message.channel.send(
                    f"@everyone Asset Amount is 0 for {AssetName}. Unable to place order"
                )
                return f"@everyone Asset Amount is 0 for {AssetName}. Unable to place order"

            res = hlbot.place_limit_order(
                rawAssetName, is_buy, assetAmount, price, reduce_only
            )
            if res == None:
                await message.channel.send("Error placing order")
                return

            await message.channel.send(
                f"Placed limit order to {'buy' if is_buy else 'sell'} for {assetAmount} {rawAssetName}@{price}"
            )

        if message.content.startswith("$tp"):
            # $tp <rawAssetName> <tp_price>
            openpositions = self.hlbot.get_all_open_positions()
            if len(openpositions) == 0:
                await message.channel.send("No open positions")
                return "No open positions"

            args = message.content.split(" ")
            if len(args) != 3:
                await message.channel.send(
                    "Invalid arguments\nUsage: $tp <rawAssetName> <tp_price>"
                )
                return "Invalid arguments\nUsage: $tp <rawAssetName> <tp_price>"

            rawAssetName = args[1].upper()
            tp_price = float(args[2])

            AssetName = self.hlbot.get_asset_name(rawAssetName)
            tp_price = self.hlbot.get_correct_price(AssetName, tp_price)
            openpositions = self.hlbot.get_all_open_positions()
            for pos in openpositions:
                print(pos)
                if pos["position"]["coin"] == AssetName:
                    assetAmount = float(pos["position"]["szi"])
                    side = assetAmount > 0
                    self.hlbot.set_tp(AssetName, tp_price, abs(assetAmount), not side)

                    break

        if message.content.startswith("$sl"):
            # $sl <rawAssetName> <sl_price>
            # openpositions = self.hlbot.get_all_open_positions()
            # if len(openpositions) == 0:
            #     await message.channel.send("No open positions")
            #     return

            args = message.content.split(" ")
            if len(args) != 3:
                await message.channel.send(
                    "Invalid arguments\nUsage: $sl <rawAssetName> <sl_price>"
                )
                return "Invalid arguments\nUsage: $sl <rawAssetName> <sl_price>"

            rawAssetName = args[1].upper()
            sl_price = float(args[2])

            AssetName = self.hlbot.get_asset_name(rawAssetName)
            sl_price = self.hlbot.get_correct_price(AssetName, sl_price)

            openpositions = self.hlbot.get_all_open_positions()
            for pos in openpositions:
                print(pos)
                if pos["position"]["coin"] == AssetName:
                    assetAmount = float(pos["position"]["szi"])
                    side = assetAmount > 0
                    self.hlbot.set_sl(AssetName, sl_price, abs(assetAmount), not side)

                    break

        if message.content.startswith("$cancel"):
            # $cancel <rawAssetName> <oid : Optional>
            args = message.content.split(" ")
            if len(args) < 2:
                await message.channel.send(
                    "Invalid arguments\nUsage: $cancel <rawAssetName> <oid : Optional>"
                )
                return (
                    "Invalid arguments\nUsage: $cancel <rawAssetName> <oid : Optional>"
                )

            if len(args) == 2:
                asset = args[1]
                hlbot = self.hlbot
                res = hlbot.cancel_all_orders(asset)
                return "Cancelled all orders for " + asset

            if len(args) == 3:
                asset = args[1]
                oid = args[2]
                hlbot = self.hlbot
                res = hlbot.cancel_limit_order(
                    asset, oid
                )  # messages are sent in the function
                return "Cancelled limit order for " + asset + " with oid " + oid

        if message.content.startswith("$add"):
            # $add <rawAssetName> <tf> <sl> <hma> <size> <leverage> <is_long>
            print("inside add")
            args = message.content.split(" ")
            if len(args) != 8:
                await message.channel.send(
                    "Invalid arguments\nUsage: $add <rawAssetName> <tf> <sl : 1/0> <hma> <size> <leverage> <is_long>"
                )
                return "Invalid arguments\nUsage: $add <rawAssetName> <tf> <sl : 1/0> <hma> <size> <leverage> <is_long>"

            asset = args[1].upper()
            tf = args[2].lower()
            sl = True if args[3] == "1" else False
            hma = int(args[4])
            size = float(args[5])
            leverage = int(args[6])
            is_long = True if args[7] == "1" else False

            self.screener.addAsset(asset, tf, sl, hma, size, leverage, is_long)

            await message.channel.send(f"Added {asset} {tf}")
            msg = self.get_assetListMsg()
            await message.channel.send(msg)

        if message.content.startswith("$remove"):
            # $remove <id>
            args = message.content.split(" ")
            if len(args) != 2:
                await message.channel.send("Invalid arguments\nUsage: $remove <id>")
                return

            id = int(args[1])
            self.screener.removeAsset(id)
            await message.channel.send(f"Removed {id}")

            msg = self.get_assetListMsg()
            await message.channel.send(msg)

        if message.content.startswith("$list"):
            msg = self.get_assetListMsg()
            await message.channel.send(msg)

        if message.content.startswith("$open"):
            # $open
            hlbot = self.hlbot

            msg = hlbot.get_all_open_positions()
            pretty_msg = json.dumps(msg, indent=4)
            chunks = [pretty_msg[i : i + 1800] for i in range(0, len(pretty_msg), 1800)]
            for chunk in chunks:
                await asyncio.sleep(0.1)
                await message.channel.send("```json\n Open Positions" + chunk + "```")

            openorders = hlbot.get_all_open_orders()
            pretty_msg = json.dumps(openorders, indent=4)
            chunks = [pretty_msg[i : i + 1800] for i in range(0, len(pretty_msg), 1800)]
            for chunk in chunks:
                await asyncio.sleep(0.1)
                await message.channel.send("```json\n Open Orders" + chunk + "```")

            marginsummary = hlbot.get_margin_summary()
            await asyncio.sleep(0.1)  # to prevent rate limit
            marginsummary = json.dumps(marginsummary, indent=4)
            await message.channel.send("```json\n" + marginsummary + "```")

        if message.content.startswith("$lev"):
            # $lev <rawAssetName> <lev>
            args = message.content.split(" ")
            if len(args) != 3:
                await message.channel.send(
                    "Invalid arguments\nUsage: $lev <rawAssetName> <lev>"
                )
                return

            rawAssetName = None
            lev = None
            try:
                rawAssetName = args[1]
                lev = int(args[2])
            except:
                await message.channel.send("Invalid arguments")
                return

            hlbot = self.hlbot
            hlbot.set_leverage(rawAssetName, lev)

        if message.content.startswith("$hma"):
            # $hma <id> <length>
            args = message.content.split(" ")
            if len(args) != 3:
                await message.channel.send(
                    "Invalid arguments\nUsage: $hma <id> <length>"
                )
                return

            id = None
            length = None
            try:
                id = int(args[1])
                length = int(args[2])
            except:
                await message.channel.send("Invalid arguments")
                return

            for a in self.screener.assets:
                a: Asset
                if a.id == id:
                    a.changehma(length)
                    await message.channel.send(
                        f"Successfully set HMA Length for {a.id} {a.coinpair} to {length}"
                    )
                    return

            await message.channel.send("Invalid ID")

        if message.content.startswith("$amt"):
            # $amt <id> <amount>
            args = message.content.split(" ")
            if len(args) != 3:
                await message.channel.send(
                    "Invalid arguments\nUsage: $amt <id> <amount>"
                )
                return

            id = None
            amount = None
            try:
                id = int(args[1])
                amount = float(args[2])
            except:
                await message.channel.send("Invalid arguments")
                return
            for a in self.screener.assets:
                if a.id == id:
                    a.change_txn_amount(amount)
                    await message.channel.send(
                        f"Set Ape Size for {a.id} {a.coinpair} to {amount}"
                    )
                    return

            await message.channel.send("Invalid ID")

        if message.content.startswith("$dec"):
            # $dec <rawAssetName>
            args = message.content.split(" ")
            if len(args) != 2:
                await message.channel.send(
                    "Invalid arguments\nUsage: $dec <rawAssetName>"
                )
                return

            asset = args[1].upper()
            hlbot = self.hlbot
            res = hlbot.get_decimals_forAsset(asset)
            if res == None:
                await message.channel.send("Invalid asset")
                return
            await message.channel.send(f"Decimals for {asset} is {res}")

        if message.content.startswith("$closeall"):
            try:
                hlbot = self.hlbot
                hlbot.close_all_positions()
            except:
                await message.channel.send("Error closing all positions")
                return

            await message.channel.send(f"Closed all positions {message}")
            return  # prevent sending the message below of $close

        if message.content.startswith("$close"):
            # $close <rawAssetname>
            args = message.content.split(" ")
            if len(args) != 2:
                await message.channel.send(
                    "Invalid arguments\nUsage: $close <rawAssetName>"
                )
                return

            asset = args[1].upper()
            hlbot = self.hlbot
            hlbot.close_position(asset)
            await message.channel.send(f"Closed {asset}")

        # Trade tracking commands
        if message.content.startswith("$trades"):
            await self.handle_trades_command(message)

        if message.content.startswith("$history"):
            await self.handle_history_command(message)

        if message.content.startswith("$stats"):
            await self.handle_stats_command(message)

        if message.content.startswith("$pending"):
            await self.handle_pending_command(message)

        if message.content.startswith("$stoplosses"):
            await self.handle_stoplosses_command(message)

        if message.content.startswith("$trade "):
            await self.handle_trade_detail_command(message)

        if message.content.startswith("$perf"):
            # $perf - Show performance statistics
            try:
                msg = "📊 **PERFORMANCE STATISTICS**\n"
                msg += "=" * 40 + "\n"

                # TV Service stats
                if self.tv_service:
                    tv_stats = self.tv_service.get_performance_stats()
                    msg += f"📈 **TradingView Service:**\n"
                    msg += f"Monitoring Active: {'✅' if tv_stats['monitoring_active'] else '❌'}\n"
                    msg += f"Pending Trades: {tv_stats['pending_trades_count']}\n"
                    msg += f"Check Interval: {tv_stats['monitoring_interval']}s\n\n"

                # Candle SL Manager stats
                if self.tv_service and hasattr(self.tv_service, "candle_sl_manager"):
                    sl_stats = self.tv_service.candle_sl_manager.get_performance_stats()
                    msg += f"🛡️ **Candle SL Manager:**\n"
                    msg += f"Monitoring: {'✅' if sl_stats['monitoring'] else '❌'}\n"
                    msg += f"Active Stops: {sl_stats['active_stops_count']}\n"
                    msg += f"Check Interval: {sl_stats['check_interval']}s\n\n"

                # HL Bot stats
                if self.hlbot:
                    try:
                        acc_value = self.hlbot.get_totalAccValue()
                        msg += f"�� **Account Value:** ${acc_value:,.2f}\n"
                    except:
                        msg += f"�� **Account Value:** Unable to fetch\n"

                await message.channel.send(msg)

            except Exception as e:
                await message.channel.send(
                    f"❌ Error getting performance stats: {str(e)}"
                )

        # Dynamic Trading Commands
        if message.content.startswith("$dlong"):
            # $dlong <rawAssetName> [leverage] - Dynamic long order
            args = message.content.split(" ")
            if len(args) < 2:
                await message.channel.send("❌ Usage: $dlong <rawAssetName> [leverage]")
                return

            rawAssetName = args[1]
            leverage = int(args[2]) if len(args) > 2 else 1  # Default to 1x leverage

            if not self.hlbot:
                await message.channel.send(
                    "❌ Bot not initialized. Use $start <password> first."
                )
                return

            # Calculate dynamic amount
            dynamic_amount = self.hlbot.calculate_dynamic_usd_amount()

            if leverage:
                self.hlbot.set_leverage(rawAssetName, leverage)

            # Pass leverage, candle-close SL will be enabled by default
            self.hlbot.generate_order(
                rawAssetName,
                dynamic_amount,
                True,
                leverage=leverage,
                use_candle_close_sl=True,  # Always enable
            )
            await message.channel.send(
                f" Dynamic Long: {rawAssetName} with ${dynamic_amount:.2f} USDT (leverage: {leverage}x)"
            )

        if message.content.startswith("$dshort"):
            # $dshort <rawAssetName> [leverage] - Dynamic short order
            args = message.content.split(" ")
            if len(args) < 2:
                await message.channel.send(
                    "❌ Usage: $dshort <rawAssetName> [leverage]"
                )
                return

            rawAssetName = args[1]
            leverage = int(args[2]) if len(args) > 2 else None

            if not self.hlbot:
                await message.channel.send(
                    "❌ Bot not initialized. Use $start <password> first."
                )
                return

            # Calculate dynamic amount
            dynamic_amount = self.hlbot.calculate_dynamic_usd_amount()

            if leverage:
                self.hlbot.set_leverage(rawAssetName, leverage)

            self.hlbot.generate_order(rawAssetName, dynamic_amount, False)
            await message.channel.send(
                f"📉 Dynamic Short: {rawAssetName} with ${dynamic_amount:.2f} USDT"
            )

        if message.content.startswith("$dlimit"):
            # $dlimit <rawAssetName> <is_buy : 1/0> <price> <reduce_only:1/0=0> - Dynamic limit order
            args = message.content.split(" ")
            if len(args) != 5:
                await message.channel.send(
                    "❌ Usage: $dlimit <rawAssetName> <is_buy : 1/0> <price> <reduce_only:1/0>"
                )
                return

            rawAssetName = args[1]
            is_buy = True if args[2] == "1" else False
            price = float(args[3])
            reduce_only = True if args[4] == "1" else False

            if not self.hlbot:
                await message.channel.send(
                    "❌ Bot not initialized. Use $start <password> first."
                )
                return

            # Calculate dynamic amount
            dynamic_amount = self.hlbot.calculate_dynamic_usd_amount()

            AssetName = self.hlbot.get_asset_name(rawAssetName)
            price = self.hlbot.get_correct_price(AssetName, price)

            # Calculate asset amount
            assetAmount = dynamic_amount / price
            info = self.hlbot.get_info_forAsset(rawAssetName)
            szDecimal = info["szDecimals"]
            assetAmount = round(assetAmount, szDecimal)
            if szDecimal == 0:
                assetAmount = int(assetAmount)

            if assetAmount == 0:
                await message.channel.send(
                    f"❌ Asset Amount is 0 for {AssetName}. Unable to place order"
                )
                return

            res = self.hlbot.place_limit_order(
                rawAssetName, is_buy, assetAmount, price, reduce_only
            )
            if res == None:
                await message.channel.send("❌ Error placing order")
                return

            await message.channel.send(
                f"📊 Dynamic Limit Order: {'buy' if is_buy else 'sell'} {assetAmount} {rawAssetName} @ ${price} (${dynamic_amount:.2f} USDT)"
            )

        if message.content.startswith("$dcalc"):
            # $dcalc - Show current dynamic amount calculation
            if not self.hlbot:
                await message.channel.send(
                    "❌ Bot not initialized. Use $start <password> first."
                )
                return

            try:
                total_value = self.hlbot.get_totalAccValue()
                calculated_amount = total_value * 0.01
                dynamic_amount = max(10.0, calculated_amount)

                msg = f" **Dynamic Amount Calculation**\n"
                msg += f"Portfolio Value: ${total_value:,.2f}\n"
                msg += f"1% of Portfolio: ${calculated_amount:,.2f}\n"
                msg += f"Minimum Amount: $10.00\n"
                msg += f"**Dynamic Amount: ${dynamic_amount:,.2f}**"

                await message.channel.send(msg)
            except Exception as e:
                await message.channel.send(
                    f"❌ Error calculating dynamic amount: {str(e)}"
                )

        if message.content.startswith("$help"):
            await message.channel.send(
                """```Manual Trading:
$long <rawAssetName> <usdt_size> <leverage>
$short <rawAssetName> <usdt_size> <leverage>
$limit <rawAssetName> <is_buy : 1/0> <assetAsmount> <price> <reduce_only:1/0=0>
$sl <rawAssetName> <sl_price>
$cancel <rawAssetName> <oid>
$lev <rawAssetName> <lev>
$dec <rawAssetName>
$close <rawAssetName>
$closeall

Dynamic Trading (1% of portfolio or $10 minimum):
$dlong <rawAssetName> [leverage] - Dynamic long order
$dshort <rawAssetName> [leverage] - Dynamic short order
$dlimit <rawAssetName> <is_buy : 1/0> <price> <reduce_only:1/0> - Dynamic limit order
$dcalc - Show current dynamic amount calculation

Automated Trading:
$add <rawAssetName> <tf> <sl : 1/0> <hma> <usdt_size> <leverage> <is_long>
$remove <id>
$hma <id> <length>
$amt <id> <usdt_size>

Trade Tracking:
$trades - Show active trades
$history [limit] - Show recent trades (default 10)
$stats - Show performance statistics
$trade <uuid> - Show detailed trade info
$pending - Show pending TradingView trades
$stoplosses - Show active candle-close stop losses

Performance:
$perf - Show performance statistics
$debug [on/off] - Toggle debug performance mode

Others:
$list #for strategy list
$open #for open positions```"""
            )

        if message.content.startswith("$debug"):
            # $debug [on/off] - Toggle debug performance mode
            args = message.content.split(" ")

            if len(args) == 1:
                # Show current debug status
                try:
                    from tv_service import DEBUG_PERFORMANCE as tv_debug
                    from candle_service import DEBUG_PERFORMANCE as candle_debug

                    msg = f"📊 **DEBUG MODE STATUS**\n"
                    msg += f"TV Service: {'✅ ON' if tv_debug else '❌ OFF'}\n"
                    msg += f"Candle Service: {'✅ ON' if candle_debug else '❌ OFF'}\n"
                    msg += f"\nUse `$debug on`