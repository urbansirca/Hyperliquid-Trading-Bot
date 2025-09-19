from flask import request, jsonify
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import os
import threading
import time
from dataclasses import dataclass, asdict, field
from execution_service import HyperLiquidExecutionService
from candle_service import CandleCloseStopLossManager

import ccxt
from tracker import TradeTracker

# Debug flag for performance timing
DEBUG_PERFORMANCE = False  # Set to True to enable performance timing prints


@dataclass
class PendingTrade:
    """Data class for pending trades waiting for entry signal"""

    id: str
    symbol: str
    timeframe: str
    is_long: bool
    mid_price: float
    negation_price: float
    amount_usd: float
    reference_candle: Dict
    leverage: int = 1  # Default leverage
    use_candle_close_sl: bool = True  # Always enable candle-close SL
    abs_stop_loss_price: float = 0.0
    created_at: datetime = field(default_factory=datetime.now)
    payload: Dict = field(default_factory=dict)  # Original webhook payload

    def to_dict(self):
        return {**asdict(self), "created_at": self.created_at.isoformat()}


class TradingViewWebhookService:
    def __init__(
        self,
        security_keyword: str,
        hyperliquid_password: str,
        log_file: str = "webhook_security.log",
        port: int = 5000,
        monitoring_interval: float = 2.0,  # seconds between price checks
        webhook=None,  # Add shared webhook parameter
        hl_service: HyperLiquidExecutionService = None,  # Required now
        tracker: TradeTracker = None,  # Add tracker parameter
        error_logger=None,  # Add error logger parameter
    ):
        self.security_keyword = security_keyword
        self.log_file = log_file
        self.port = port
        self.monitoring_interval = monitoring_interval
        self.hl_service = hl_service
        self.tracker = tracker  # Store the shared tracker
        self.error_logger = error_logger  # Store the error logger

        # IP allowlist - only these IPs can access the webhook
        self.allowed_ips = {
            "52.89.214.238",
            "34.212.75.30",
            "54.218.53.128",
            "52.32.178.7",
            "127.0.0.1",
            "62.195.119.92",  # personal IP
        }

        self.webhook = webhook

        # Don't create candle_sl_manager here - it will be set externally
        self.candle_sl_manager = None

        # Pending trades storage
        self.pending_trades: Dict[str, PendingTrade] = {}
        self.pending_trades_lock = threading.Lock()

        # Monitoring thread
        self.monitoring_active = False
        self.monitoring_thread = None

        # Setup logging
        logging.basicConfig(
            filename=log_file,
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
        )

        print("TradingView webhook service initialized with candle-close stop loss")
        print(f"IP allowlist enabled: {', '.join(self.allowed_ips)}")

    def _log_error(self, error_msg: str, context: str = ""):
        """Log error using the centralized error logger"""
        if self.error_logger:
            self.error_logger(error_msg, context, "TradingView")
            print(f"ERROR [TradingView] ({context}): {error_msg}")
        else:
            print(f"ERROR [TradingView] ({context}): {error_msg}")

    def is_ip_allowed(self, ip_address: str) -> bool:
        """Check if the IP address is in the allowlist"""
        return ip_address in self.allowed_ips

    def start_monitoring(self):
        """Start the price monitoring thread"""
        if not self.monitoring_active:
            self.monitoring_active = True
            self.monitoring_thread = threading.Thread(
                target=self._monitor_prices, daemon=True
            )
            self.monitoring_thread.start()
            print("Started price monitoring thread")

    def stop_monitoring(self):
        """Stop the price monitoring thread"""
        self.monitoring_active = False
        if self.monitoring_thread and self.monitoring_thread.is_alive():
            self.monitoring_thread.join(timeout=5)
        print("Stopped price monitoring thread")

    def _monitor_prices(self):
        """Background thread that monitors prices for pending trades"""
        loop_count = 0
        while self.monitoring_active:
            loop_start = time.time()
            loop_count += 1

            try:
                with self.pending_trades_lock:
                    trades_to_remove = []
                    trades_to_execute = []

                    # Time the trade checking loop
                    check_start = time.time()
                    for trade_id, pending_trade in self.pending_trades.items():
                        trade_check_start = time.time()
                        try:
                            # get the most recent candle at that timeframe
                            candle_start = time.time()
                            candle_data = self.get_reference_candle(
                                pending_trade.symbol, pending_trade.timeframe
                            )
                            candle_duration = (time.time() - candle_start) * 1000
                            if DEBUG_PERFORMANCE:
                                print(
                                    f"📊️  Candle fetch for {pending_trade.symbol} {pending_trade.timeframe}: {candle_duration:.2f}ms"
                                )

                            if not candle_data:
                                self._log_error(
                                    f"Could not get reference candle",
                                    f"symbol={pending_trade.symbol}, timeframe={pending_trade.timeframe}",
                                )
                                continue

                            last_candle_closing_price = candle_data["close"]

                            # Check for negation signal (price crosses negation level)
                            negation_triggered = False
                            if pending_trade.is_long:
                                # Long: negation if price goes below the original candle's low
                                negation_triggered = (
                                    last_candle_closing_price
                                    <= pending_trade.negation_price
                                )
                            else:
                                # Short: negation if price goes above the original candle's high
                                negation_triggered = (
                                    last_candle_closing_price
                                    >= pending_trade.negation_price
                                )

                            # Get current price
                            price_start = time.time()
                            if not self.hl_service:
                                self._log_error(
                                    "HyperLiquid service not available for price fetch",
                                    f"symbol={pending_trade.symbol}",
                                )
                                continue

                            current_price = self.hl_service.get_last_price(
                                pending_trade.symbol
                            )
                            if current_price is None:
                                self._log_error(
                                    "Failed to get current price",
                                    f"symbol={pending_trade.symbol}",
                                )
                                # remove order from tracker and pending trades
                                # trades_to_remove.append(trade_id) # TODO: check if this is correct because new incoming alerts might still be processed
                                # self.pending_trades.pop(trade_id, None)
                                continue

                            current_price = self.hl_service.get_correct_price(
                                self.hl_service.get_asset_name(pending_trade.symbol),
                                current_price,
                            )
                            price_duration = (time.time() - price_start) * 1000
                            if DEBUG_PERFORMANCE:
                                print(
                                    f" Price fetch for {pending_trade.symbol}: {price_duration:.2f}ms"
                                )

                            if negation_triggered:
                                print(
                                    f"NEGATION triggered for {trade_id}: {pending_trade.symbol} price {current_price} crossed negation level {pending_trade.negation_price}"
                                )
                                trades_to_remove.append(trade_id)
                                continue

                            # Check for entry signal (price hits mid-point)
                            entry_triggered = False
                            if pending_trade.is_long:
                                # Long: enter when price reaches or falls below mid-point
                                entry_triggered = (
                                    current_price <= pending_trade.mid_price
                                )
                            else:
                                # Short: enter when price reaches or exceeds above mid-point
                                entry_triggered = (
                                    current_price >= pending_trade.mid_price
                                )

                            if entry_triggered:
                                print(
                                    f"ENTRY signal triggered for {trade_id}: {pending_trade.symbol} price {current_price} hit target {pending_trade.mid_price}"
                                )
                                trades_to_execute.append(pending_trade)
                                trades_to_remove.append(trade_id)

                        except Exception as e:
                            self._log_error(
                                f"Error monitoring individual trade: {str(e)}",
                                f"trade_id={trade_id}, symbol={pending_trade.symbol}",
                            )

                        trade_check_duration = (time.time() - trade_check_start) * 1000
                        if DEBUG_PERFORMANCE:
                            print(
                                f"🔍 Trade check for {trade_id}: {trade_check_duration:.2f}ms"
                            )

                check_duration = (time.time() - check_start) * 1000
                if DEBUG_PERFORMANCE:
                    print(
                        f"📈 Total trade checking: {check_duration:.2f}ms for {len(self.pending_trades)} trades"
                    )

                # Remove processed trades
                remove_start = time.time()
                for trade_id in trades_to_remove:
                    self.pending_trades.pop(trade_id, None)
                remove_duration = (time.time() - remove_start) * 1000
                if DEBUG_PERFORMANCE:
                    print(
                        f"️  Trade removal: {remove_duration:.2f}ms for {len(trades_to_remove)} trades"
                    )

                # Execute triggered trades (outside the lock to avoid blocking)
                execute_start = time.time()
                for pending_trade in trades_to_execute:
                    threading.Thread(
                        target=self._execute_pending_trade,
                        args=(pending_trade,),
                        daemon=True,
                    ).start()
                execute_duration = (time.time() - execute_start) * 1000
                if DEBUG_PERFORMANCE:
                    print(
                        f"⚡ Trade execution setup: {execute_duration:.2f}ms for {len(trades_to_execute)} trades"
                    )

                loop_duration = (time.time() - loop_start) * 1000
                if DEBUG_PERFORMANCE:
                    print(
                        f"�� Monitoring loop #{loop_count}: {loop_duration:.2f}ms total"
                    )
                    print(f"📈 Active pending trades: {len(self.pending_trades)}")
                    print("=" * 50)

                time.sleep(self.monitoring_interval)

            except Exception as e:
                self._log_error(
                    f"Error in price monitoring loop: {str(e)}",
                    f"loop_count={loop_count}",
                )
                time.sleep(self.monitoring_interval)

    def _execute_pending_trade(self, pending_trade: PendingTrade):
        # TODO: make this use generate_order function from execution_service.py
        """Execute a pending trade that has triggered"""
        try:
            print(
                f"Executing pending trade: {pending_trade.symbol} {pending_trade.timeframe}"
            )

            if not self.hl_service:
                self._log_error(
                    "HyperLiquid service not available for trade execution",
                    f"symbol={pending_trade.symbol}",
                )
                return

            if not pending_trade.abs_stop_loss_price:
                self._log_error(
                    "No stop loss price set",
                    f"symbol={pending_trade.symbol}, timeframe={pending_trade.timeframe}",
                )
                return
            if not pending_trade.amount_usd:
                self._log_error(
                    "No amount USD set",
                    f"symbol={pending_trade.symbol}, timeframe={pending_trade.timeframe}",
                )
                return

            # Set leverage
            try:
                self.hl_service.set_leverage(
                    pending_trade.symbol, pending_trade.leverage
                )
            except Exception as e:
                self._log_error(
                    f"Failed to set leverage: {str(e)}",
                    f"symbol={pending_trade.symbol}, leverage={pending_trade.leverage}",
                )

            # Calculate position details
            AssetName = self.hl_service.get_asset_name(pending_trade.symbol)
            current_price = self.hl_service.get_last_price(pending_trade.symbol)
            if current_price is None:
                self._log_error(
                    "Failed to get current price for trade execution",
                    f"symbol={pending_trade.symbol}",
                )
                return

            current_price = self.hl_service.get_correct_price(AssetName, current_price)

            # Calculate asset amount
            info = self.hl_service.get_info_forAsset(pending_trade.symbol)
            if not info:
                self._log_error(
                    "Failed to get asset info", f"symbol={pending_trade.symbol}"
                )
                return

            szDecimal = info["szDecimals"]
            assetAmount = pending_trade.amount_usd / current_price
            assetAmount = round(assetAmount, szDecimal)

            if szDecimal == 0:
                assetAmount = int(assetAmount)

            # Place market order
            res = self.hl_service.place_market_order(
                pending_trade.symbol, pending_trade.is_long, assetAmount
            )

            if res != None:
                assetAmount = res["asset_amount"]
                order_id = res["order_id"]
                avg_price = res["avg_price"]
                total_usd = res["total_usd"]  # TODO: add these to tracker

                # Add to tracker with original asset amount
                if not self.tracker:
                    self._log_error(
                        "Tracker not available for trade recording",
                        f"symbol={pending_trade.symbol}",
                    )
                    return

                trade_id = self.tracker.add_trade(
                    currency=pending_trade.symbol,
                    timeframe=pending_trade.timeframe,
                    qty_usd=pending_trade.amount_usd,
                    qty_asset=assetAmount,  # Store original asset amount
                    original_qty_asset=assetAmount,  # Also store as original amount
                    entry_price=current_price,
                    side="long" if pending_trade.is_long else "short",
                    stop_loss_price=pending_trade.abs_stop_loss_price,
                    hyperliquid_order_id=str(order_id),
                    leverage=pending_trade.leverage,
                    use_candle_close_sl=True,  # Always enable candle-close SL
                    candle_sl_timeframe=pending_trade.timeframe,  # Use the same timeframe as trade
                )

                # Set up candle-close stop loss if enabled always add it
                if not self.candle_sl_manager:
                    self._log_error(
                        "Candle SL manager not available",
                        f"symbol={pending_trade.symbol}",
                    )
                else:
                    try:
                        self.candle_sl_manager.add_candle_close_stop_loss(
                            order_id=str(res),
                            asset=pending_trade.symbol,
                            stop_price=pending_trade.negation_price,  # TODO: check if this is correct
                            timeframe=pending_trade.timeframe,
                            is_long=pending_trade.is_long,
                            position_size=assetAmount,
                        )
                    except Exception as e:
                        self._log_error(
                            f"Failed to add candle-close stop loss: {str(e)}",
                            f"symbol={pending_trade.symbol}",
                        )

                # Use traditional stop loss
                try:
                    self.hl_service.set_sl(
                        AssetName=AssetName,
                        slPrice=pending_trade.abs_stop_loss_price,
                        assetAmount=assetAmount,
                        is_buy=not pending_trade.is_long,  # stop loss order must be on the opposite side
                    )
                except Exception as e:
                    self._log_error(
                        f"Failed to set traditional stop loss: {str(e)}",
                        f"symbol={pending_trade.symbol}",
                    )

                print(f"Trade executed successfully: {trade_id}")
                logging.info(
                    f"Pending trade executed: {pending_trade.id} -> {trade_id}"
                )
            else:
                self._log_error(
                    "Market order failed",
                    f"symbol={pending_trade.symbol}, amount={assetAmount}",
                )

        except Exception as e:
            self._log_error(
                f"Failed to execute pending trade: {str(e)}",
                f"trade_id={pending_trade.id}, symbol={pending_trade.symbol}",
            )

    def get_real_client_ip(self, request):
        """Get the real client IP accounting for proxies/load balancers"""
        # Check common forwarded IP headers in order of preference
        forwarded_headers = [
            "X-Forwarded-For",  # Most common
            "X-Real-IP",  # Nginx
            "CF-Connecting-IP",  # Cloudflare
            "X-Client-IP",  # Some proxies
        ]

        for header in forwarded_headers:
            forwarded_ip = request.headers.get(header)
            if forwarded_ip:
                # X-Forwarded-For can contain multiple IPs, take the first (original client)
                client_ip = forwarded_ip.split(",")[0].strip()
                print(f"Found client IP in {header}: {client_ip}")
                return client_ip

        # Fallback to remote_addr (direct connection)
        client_ip = request.remote_addr
        print(f"Using remote_addr: {client_ip}")
        return client_ip

    def handle_webhook(self):
        """Main webhook handler"""
        try:
            # Get the real client IP from headers (for load balancers/proxies)
            client_ip = self.get_real_client_ip(request)

            # Prepare comprehensive request data
            full_request_data = {
                "method": request.method,
                "url": request.url,
                "headers": dict(request.headers),
                "args": dict(request.args),
                "form": dict(request.form),
                "data": request.get_data(as_text=True),
                "json": request.get_json(),
                "remote_addr": request.remote_addr,
                "user_agent": request.headers.get("User-Agent"),
                "content_type": request.content_type,
                "content_length": request.content_length,
            }

            # Check IP allowlist first
            if not self.is_ip_allowed(client_ip):
                self.log_security_event(
                    "IP not allowed",
                    {"ip": client_ip, "allowed_ips": list(self.allowed_ips)},
                    client_ip,
                    full_request_data,
                )
                return jsonify({"error": "Access denied"}), 403

            # Get payload
            payload = request.get_json()
            if not payload:
                self.log_security_event(
                    "Empty payload", request.headers, client_ip, full_request_data
                )
                return jsonify({"error": "No JSON payload"}), 400

            # Check security keyword
            keyword = payload.get("keyword")
            if keyword != self.security_keyword:
                self.log_security_event(
                    "Invalid keyword", payload, client_ip, full_request_data
                )
                return jsonify({"error": "Unauthorized"}), 401

            # Parse and execute alert
            result = self.parse_and_execute_alert(payload)

            if result["success"]:
                return jsonify(result), 200
            else:
                self._log_error(
                    f"Webhook execution failed: {result['error']}", f"payload={payload}"
                )
                return jsonify({"error": result["error"]}), 400

        except Exception as e:
            self._log_error(f"Webhook handler error: {str(e)}", "handle_webhook")
            return jsonify({"error": "Internal server error"}), 500

    def log_security_event(
        self,
        event_type: str,
        payload: Any,
        ip_address: str,
        full_request_data: Dict = None,
    ):
        """Log security events with complete request information"""
        log_entry = {
            "timestamp": datetime.now().isoformat(),
            "event_type": event_type,
            "ip_address": ip_address,
            "payload": payload,  # Remove the 500 character limit
            "request_data": full_request_data or {},
        }

        logging.warning(f"SECURITY EVENT: {json.dumps(log_entry, indent=2)}")
        print(f"Security Event: {event_type} from {ip_address}")

    def parse_and_execute_alert(self, payload: Dict) -> Dict:
        """Parse TradingView alert and execute trade"""
        try:
            # Extract required fields
            alert_type = payload.get(
                "action", None
            )  # e.g., "enter_long", "tp1_short", etc.
            symbol = payload.get("symbol", None)  # e.g., "BTC"
            timeframe = payload.get("timeframe", None)  # e.g., "1h", "4h"
            amount_usd = payload.get("amount", None)  # USD amount
            leverage = payload.get("leverage", 1)  # leverage

            tv_ticker = payload.get("ticker", None)  # tv ticker
            tv_time = payload.get("time", None)  # tv time
            tv_timenow = payload.get("timenow", None)  # tv timenow

            tv_candle_open = payload.get("candle_open", None)  # candle open
            tv_candle_close = payload.get("candle_close", None)  # candle close
            tv_candle_high = payload.get("candle_high", None)  # candle high
            tv_candle_low = payload.get("candle_low", None)  # candle low

            # tv_info = {
            #     "ticker": tv_ticker,
            #     "time": tv_time,
            #     "timenow": tv_timenow,
            #     "candle_open": tv_candle_open,
            #     "candle_close": tv_candle_close,
            #     "candle_high": tv_candle_high,
            #     "candle_low": tv_candle_low,
            # }

            # Validate required fields
            if not all([alert_type, symbol, timeframe]):
                return {
                    "success": False,
                    "error": "Missing required fields: action, symbol, timeframe",
                }

            print(f"Processing alert: {alert_type} {symbol} {timeframe}")

            # Route to appropriate handler
            if alert_type in ["enter_long", "enter_short"]:

                return self.handle_entry_alert(
                    alert_type,
                    symbol,
                    timeframe,
                    amount_usd,
                    leverage,
                    payload,
                )

            elif alert_type in ["tp1_long", "tp1_short", "tp2_long", "tp2_short"]:
                return self.handle_tp_alert(alert_type, symbol, timeframe)

            elif alert_type in ["negation_long", "negation_short"]:
                return self.handle_negation_alert(alert_type, symbol, timeframe)

            else:
                return {"success": False, "error": f"Unknown alert type: {alert_type}"}

        except Exception as e:
            return {"success": False, "error": f"Parsing error: {str(e)}"}

    def handle_entry_alert(
        self,
        alert_type: str,
        symbol: str,
        timeframe: str,
        amount_usd: float,
        leverage: int,
        payload: Dict,
    ) -> Dict:
        """Handle entry alerts - create pending trade instead of immediate execution"""
        try:
            is_long = alert_type == "enter_long"

            amount_usd = self.hl_service.calculate_dynamic_usd_amount()
            # amount_usd = 10.0  # hardcoded for now # TODO: make this dynamic
            leverage = 1  # hardcoded for now # TODO: make this dynamic

            # Check if we already have 5 or more active trades
            active_trades = self.tracker.get_active_trades()
            if len(active_trades) >= 5:
                return {
                    "success": False,
                    "error": f"Maximum of 5 active trades reached. Currently have {len(active_trades)} active trades.",
                }

            # Get the reference candle and calculate levels
            candle_data = self.get_reference_candle(symbol, timeframe)
            if not candle_data:
                return {
                    "success": False,
                    "error": f"Could not get reference candle for {symbol} {timeframe}",
                }

            mid_price = (candle_data["high"] + candle_data["low"]) / 2
            # Calculate mid-price (entry trigger) and negation price
            # upper_open_close = max(candle_data["open"], candle_data["close"])
            # lower_open_close = min(candle_data["open"], candle_data["close"])
            # Negation price is the extreme of the candle
            if is_long:
                negation_price = candle_data[
                    "low"
                ]  # TODO: check if negation is based on openclose or highlow
                # Long negated if price goes below candle low
                abs_stop_loss_price = candle_data["low"] * 0.8
            else:
                negation_price = candle_data[
                    "high"
                ]  # TODO: check if negation is based on openclose or highlow
                # Short negated if price goes above candle high
                abs_stop_loss_price = candle_data["high"] * 1.2

            # Adjust prices for special assets
            asset_name = self.hl_service.get_asset_name(symbol)
            mid_price = self.hl_service.get_correct_price(asset_name, mid_price)
            negation_price = self.hl_service.get_correct_price(
                asset_name, negation_price
            )

            # Create pending trade
            trade_id = (
                f"{symbol}_{timeframe}_{alert_type}_{int(datetime.now().timestamp())}"
            )
            pending_trade = PendingTrade(
                id=trade_id,
                symbol=symbol,
                timeframe=timeframe,
                is_long=is_long,
                amount_usd=amount_usd,
                reference_candle=candle_data,
                mid_price=mid_price,
                negation_price=negation_price,
                leverage=leverage,
                abs_stop_loss_price=abs_stop_loss_price,
                created_at=datetime.now(),
                payload=payload,
            )

            # Store pending trade
            with self.pending_trades_lock:
                self.pending_trades[trade_id] = pending_trade

            # Start monitoring if not already active
            if not self.monitoring_active:
                self.start_monitoring()

            print(f"Created pending trade {trade_id}")
            print(f"   Entry trigger: {mid_price}")
            print(f"   Negation level: {negation_price}")

            return {
                "success": True,
                "action": "pending_trade_created",
                "trade_id": trade_id,
                "entry_price": mid_price,
                "negation_price": negation_price,
                "monitoring": "active",
            }

        except Exception as e:
            return {"success": False, "error": f"Entry alert error: {str(e)}"}

    def handle_tp_alert(self, alert_type: str, symbol: str, timeframe: str) -> Dict:
        """Handle TP alerts - close active trades and cancel pending ones"""
        try:
            # Cancel any pending trades for this symbol/timeframe
            cancelled_pending = []
            with self.pending_trades_lock:
                trades_to_remove = []
                for trade_id, pending_trade in self.pending_trades.items():
                    if (
                        pending_trade.symbol == symbol
                        and pending_trade.timeframe == timeframe
                    ):
                        trades_to_remove.append(trade_id)
                        cancelled_pending.append(trade_id)

                for trade_id in trades_to_remove:
                    self.pending_trades.pop(trade_id, None)

            # Handle active trades from tracker - find by symbol and timeframe
            active_trades = self.hl_service.tracker.get_trades_by_symbol_timeframe(
                symbol, timeframe
            )

            # Filter active trades based on alert type
            relevant_trades = []
            if alert_type in ["tp1_long", "tp1_short"]:
                relevant_trades = [
                    trade for trade in active_trades if trade.get("status") == "active"
                ]
            elif alert_type in ["tp2_long", "tp2_short"]:
                relevant_trades = [
                    trade
                    for trade in active_trades
                    if trade.get("status") == "tp1_achieved"
                ]

            # Check if there are any relevant trades to process
            if not relevant_trades:
                error_msg = (
                    f"No active trades found for {alert_type} on {symbol} {timeframe}"
                )
                if alert_type in ["tp1_long", "tp1_short"]:
                    error_msg += " (need active trades)"
                elif alert_type in ["tp2_long", "tp2_short"]:
                    error_msg += " (need trades with TP1 achieved)"

                self._log_error(
                    error_msg,
                    f"symbol={symbol}, timeframe={timeframe}, alert_type={alert_type}",
                )
                return {
                    "success": False,
                    "error": error_msg,
                    "active_trades_found": len(active_trades),
                    "relevant_trades_found": len(relevant_trades),
                    "pending_trades_cancelled": cancelled_pending,
                }

            trades_processed = 0

            if alert_type in ["tp1_long", "tp1_short"]:
                for trade in relevant_trades:
                    try:
                        original_qty = trade["original_qty_asset"]
                        current_price = self.hl_service.get_last_price(symbol)
                        if current_price is None:
                            self._log_error(
                                "Failed to get current price for TP1",
                                f"symbol={symbol}",
                            )
                            # remove order from tracker
                            continue

                        current_price = self.hl_service.get_correct_price(
                            self.hl_service.get_asset_name(symbol), current_price
                        )

                        # Calculate optimal close amount
                        close_amount, skip_tp2 = (
                            self.calculate_optimal_tp1_close_amount(
                                original_qty, current_price, min_usd=20.0
                            )
                        )

                        is_buy = trade["side"] == "short"
                        result = self.hl_service.place_market_order(
                            symbol, is_buy, close_amount
                        )

                        if result:
                            if skip_tp2:
                                # Mark as fully closed
                                self.hl_service.tracker.update_trade_status(
                                    trade["uuid"], "fully_closed", current_price
                                )
                                # Remove stop losses
                                order_id = trade["hyperliquid_order_id"]
                                if order_id in self.candle_sl_manager.active_stops:
                                    self.candle_sl_manager.remove_stop_loss(order_id)
                            else:
                                # Normal TP1
                                self.hl_service.tracker.update_trade_tp1(
                                    trade["uuid"], current_price, close_amount
                                )
                            trades_processed += 1
                        else:
                            self._log_error(
                                "Failed to execute TP1 market order",
                                f"symbol={symbol}, trade_id={trade['uuid']}",
                            )

                    except Exception as e:
                        self._log_error(
                            f"Error processing TP1 for individual trade: {str(e)}",
                            f"symbol={symbol}, trade_id={trade['uuid']}",
                        )

            elif alert_type in ["tp2_long", "tp2_short"]:
                # TP2: Close remaining 50% of original position
                for trade in relevant_trades:
                    try:
                        original_qty = trade["original_qty_asset"]
                        close_amount = original_qty * 0.5  # Close remaining half
                        is_buy = trade["side"] == "short"

                        current_price = self.hl_service.get_last_price(symbol)
                        if current_price is None:
                            self._log_error(
                                "Failed to get current price for TP2",
                                f"symbol={symbol}",
                            )
                            continue

                        current_price = self.hl_service.get_correct_price(
                            self.hl_service.get_asset_name(symbol), current_price
                        )

                        # Place close order
                        result = self.hl_service.place_market_order(
                            symbol, is_buy, close_amount
                        )

                        if result:
                            assetAmount = result["asset_amount"]
                            order_id = result["order_id"]
                            avg_price = result["avg_price"]
                            total_usd = result["total_usd"]

                            # Update tracker - mark as TP2 achieved (fully closed)
                            self.hl_service.tracker.update_trade_tp2(
                                trade["uuid"], current_price, assetAmount
                            )

                            # Remove candle-close SL since position is fully closed
                            order_id = trade["hyperliquid_order_id"]
                            if order_id in self.candle_sl_manager.active_stops:
                                self.candle_sl_manager.remove_stop_loss(order_id)
                            trades_processed += 1
                        else:
                            self._log_error(
                                "Failed to execute TP2 market order",
                                f"symbol={symbol}, trade_id={trade['uuid']}",
                            )

                    except Exception as e:
                        self._log_error(
                            f"Error processing TP2 for individual trade: {str(e)}",
                            f"symbol={symbol}, trade_id={trade['uuid']}",
                        )

            result = {
                "success": True,
                "action": f"{alert_type} executed",
                "active_trades_found": len(active_trades),
                "relevant_trades_processed": trades_processed,
                "pending_trades_cancelled": cancelled_pending,
            }

            if cancelled_pending:
                print(f"Cancelled pending trades due to TP: {cancelled_pending}")

            return result

        except Exception as e:
            self._log_error(
                f"TP execution error: {str(e)}",
                f"symbol={symbol}, timeframe={timeframe}, alert_type={alert_type}",
            )
            return {"success": False, "error": f"TP execution error: {str(e)}"}

    def handle_negation_alert(
        self, alert_type: str, symbol: str, timeframe: str
    ) -> Dict:
        """Handle negation alerts - close active trades and cancel pending ones"""
        try:
            # Cancel any pending trades for this symbol/timeframe
            cancelled_pending = []
            with self.pending_trades_lock:
                trades_to_remove = []
                for trade_id, pending_trade in self.pending_trades.items():
                    if (
                        pending_trade.symbol == symbol
                        and pending_trade.timeframe == timeframe
                    ):
                        trades_to_remove.append(trade_id)
                        cancelled_pending.append(trade_id)

                for trade_id in trades_to_remove:
                    self.pending_trades.pop(trade_id, None)

            # Close active trades
            active_trades = self.hl_service.tracker.get_trades_by_symbol_timeframe(
                symbol, timeframe
            )

            if active_trades:
                self.hl_service.close_position(symbol)

                # Remove any associated candle-close stop losses
                for trade in active_trades:
                    order_id = trade.get("hyperliquid_order_id")
                    if order_id and order_id in self.candle_sl_manager.active_stops:
                        self.candle_sl_manager.remove_stop_loss(order_id)

                    # Update tracker status
                    self.hl_service.tracker.update_trade_status(
                        trade["uuid"], "negated", None
                    )

            result = {
                "success": True,
                "action": f"Negation executed for {symbol} {timeframe}",
                "active_trades_closed": len(active_trades),
                "pending_trades_cancelled": cancelled_pending,
            }

            if cancelled_pending:
                print(f"Cancelled pending trades due to negation: {cancelled_pending}")

            return result

        except Exception as e:
            return {"success": False, "error": f"Negation execution error: {str(e)}"}

    def get_reference_candle(self, symbol: str, timeframe: str) -> Optional[Dict]:
        """Get the last completed candle for reference calculations"""
        try:
            exchange = self.hl_service.ex
            symbol_ccxt = symbol.upper() + "/USDT:USDT"

            timeframe_map = {
                "1m": "1m",
                "5m": "5m",
                "15m": "15m",
                "30m": "30m",
                "1h": "1h",
                "4h": "4h",
                "1d": "1d",
            }

            ccxt_timeframe = timeframe_map.get(timeframe, "1h")
            ohlcv = exchange.fetch_ohlcv(symbol_ccxt, ccxt_timeframe, limit=3)

            if not ohlcv or len(ohlcv) < 2:
                return None

            # Get the most recent completed candle
            recent_candle = ohlcv[-2]  # -1 is current incomplete, -2 is last complete
            ts, open_price, high_price, low_price, close_price, vol = recent_candle

            return {
                "timestamp": ts,
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
                "volume": vol,
            }

        except Exception as e:
            print(f"Error getting reference candle for {symbol}: {e}")
            return None

    def get_performance_stats(self):
        """Get performance statistics"""
        stats = {
            "monitoring_active": self.monitoring_active,
            "pending_trades_count": len(self.pending_trades),
            "monitoring_interval": self.monitoring_interval,
        }
        return stats

    def calculate_optimal_tp1_close_amount(
        self, original_qty: float, current_price: float, min_usd: float = 20.0
    ) -> tuple:
        """Calculate optimal close amount and whether to skip TP2"""
        remaining_after_50pct = (original_qty * 0.5) * current_price

        if remaining_after_50pct < min_usd:
            # Close everything, skip TP2
            return original_qty, True  # (close_amount, skip_tp2)
        else:
            # Normal 50% close, proceed to TP2
            return original_qty * 0.5, False
