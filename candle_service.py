from flask import Flask, request, jsonify
import json
import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import os
import threading
import time
from dataclasses import dataclass, asdict
from execution_service import HyperLiquidExecutionService
import ccxt
from tracker import TradeTracker

# Debug flag for performance timing
DEBUG_PERFORMANCE = False  # Set to True to enable performance timing prints


@dataclass
class CandleCloseStopLoss:
    """Data class to represent a candle-close stop loss"""

    order_id: str
    asset: str
    stop_price: float
    timeframe: str  # '1m', '5m', '15m', '1h', '4h', '1d', etc.
    is_long: bool
    position_size: float
    created_at: datetime
    last_checked_candle: datetime = None


class CandleCloseStopLossManager:
    def __init__(
        self,
        tv_service,
        hl_service: HyperLiquidExecutionService,
        tracker: TradeTracker,
        error_logger=None,  # Add error logger parameter
        check_interval: int = 10,
    ):
        """
        Initialize the candle close stop loss manager

        Args:
            tv_service: Instance of TradingViewWebhookService
            hl_service: Instance of HyperLiquidExecutionService
            tracker: Instance of TradeTracker
            error_logger: Function to log errors
            check_interval: How often to check for candle closes (seconds)
        """
        self.tv_service = tv_service
        self.hl_service = hl_service
        self.tracker = tracker
        self.error_logger = error_logger  # Store the error logger
        self.check_interval = check_interval
        self.active_stops: Dict[str, CandleCloseStopLoss] = {}
        self.monitoring = False
        self.monitor_thread = None

    def _log_error(self, error_msg: str, context: str = ""):
        """Log error using the centralized error logger"""
        if self.error_logger:
            self.error_logger(error_msg, context, "CandleSL")
            print(f"ERROR [CandleSL] ({context}): {error_msg}")
        else:
            print(f"ERROR [CandleSL] ({context}): {error_msg}")

    def add_candle_close_stop_loss(
        self,
        order_id: str,
        asset: str,
        stop_price: float,
        timeframe: str,
        is_long: bool,
        position_size: float,
    ) -> bool:
        """Add a new candle-close stop loss"""
        try:
            # Validate inputs
            if not order_id or not asset or not timeframe:
                self._log_error(
                    "Missing required parameters for stop loss",
                    f"order_id={order_id}, asset={asset}, timeframe={timeframe}",
                )
                return False

            if stop_price <= 0 or position_size <= 0:
                self._log_error(
                    "Invalid stop price or position size",
                    f"stop_price={stop_price}, position_size={position_size}",
                )
                return False

            stop_loss = CandleCloseStopLoss(
                order_id=order_id,
                asset=asset,
                stop_price=stop_price,
                timeframe=timeframe,
                is_long=is_long,
                position_size=position_size,
                created_at=datetime.now(),
            )

            self.active_stops[order_id] = stop_loss

            # Start monitoring if not already started
            if not self.monitoring:
                self.start_monitoring()

            # Send success notification
            if (
                self.hl_service
                and hasattr(self.hl_service, "webhook")
                and self.hl_service.webhook
            ):
                self.hl_service.webhook.send(
                    f"🛡️ Candle-close SL added for {asset} at {stop_price} "
                    f"(timeframe: {timeframe}, order: {order_id})"
                )

            return True

        except Exception as e:
            self._log_error(
                f"Failed to add candle-close stop loss: {str(e)}",
                f"asset={asset}, order_id={order_id}",
            )
            return False

    def remove_stop_loss(self, order_id: str):
        """Remove a stop loss from monitoring"""
        try:
            if order_id in self.active_stops:
                asset = self.active_stops[order_id].asset
                del self.active_stops[order_id]

                # Stop monitoring if no active stops
                if not self.active_stops and self.monitoring:
                    self.stop_monitoring()

                print(f"Removed candle-close stop loss for {asset} (order: {order_id})")
            else:
                self._log_error(
                    f"Stop loss not found for removal", f"order_id={order_id}"
                )

        except Exception as e:
            self._log_error(
                f"Failed to remove stop loss: {str(e)}", f"order_id={order_id}"
            )

    def get_latest_candle(self, asset: str, timeframe: str) -> Optional[tuple]:
        """Get the latest completed candle for an asset"""
        try:
            if not asset or not timeframe:
                self._log_error(
                    "Missing asset or timeframe for candle fetch",
                    f"asset={asset}, timeframe={timeframe}",
                )
                return None

            symbol = asset.upper() + "USDT"

            # Use the same method as TV service
            if not self.tv_service:
                self._log_error(
                    "TV service not available for candle fetch", f"asset={asset}"
                )
                return None

            candle_data = self.tv_service.get_reference_candle(asset, timeframe)
            if candle_data:
                return (
                    datetime.fromtimestamp(candle_data["timestamp"] / 1000),
                    candle_data["open"],
                    candle_data["high"],
                    candle_data["low"],
                    candle_data["close"],
                )
            else:
                self._log_error(
                    "No candle data returned", f"asset={asset}, timeframe={timeframe}"
                )

        except Exception as e:
            self._log_error(
                f"Failed to fetch candle data: {str(e)}",
                f"asset={asset}, timeframe={timeframe}",
            )

        return None

    def check_stop_loss_conditions(self):
        """Check all active stop losses against latest candle data"""
        stops_to_remove = []
        total_start = time.time()

        try:
            for order_id, stop_loss in self.active_stops.items():
                stop_start = time.time()
                try:
                    candle_start = time.time()
                    candle_data = self.get_latest_candle(
                        stop_loss.asset, stop_loss.timeframe
                    )
                    candle_duration = (time.time() - candle_start) * 1000
                    if DEBUG_PERFORMANCE:
                        print(
                            f"🛡️  SL Candle fetch for {stop_loss.asset} {stop_loss.timeframe}: {candle_duration:.2f}ms"
                        )

                    if candle_data is None:
                        continue

                    candle_time, open_price, high_price, low_price, close_price = (
                        candle_data
                    )

                    # Skip if we've already checked this candle
                    if (
                        stop_loss.last_checked_candle
                        and candle_time <= stop_loss.last_checked_candle
                    ):
                        continue

                    # Update last checked candle time
                    stop_loss.last_checked_candle = candle_time

                    # Check stop loss condition based on position type
                    stop_triggered = False

                    if stop_loss.is_long:
                        # Long position: stop if close price is below stop price
                        if close_price <= stop_loss.stop_price:
                            stop_triggered = True
                            trigger_reason = f"Long SL triggered: Close {close_price} <= SL {stop_loss.stop_price}"
                    else:
                        # Short position: stop if close price is above stop price
                        if close_price >= stop_loss.stop_price:
                            stop_triggered = True
                            trigger_reason = f"Short SL triggered: Close {close_price} >= SL {stop_loss.stop_price}"

                    if stop_triggered:
                        execute_start = time.time()
                        self.execute_stop_loss(stop_loss, close_price, trigger_reason)
                        execute_duration = (time.time() - execute_start) * 1000
                        if DEBUG_PERFORMANCE:
                            print(
                                f"⚡ SL execution for {order_id}: {execute_duration:.2f}ms"
                            )
                        stops_to_remove.append(order_id)

                except Exception as e:
                    self._log_error(
                        f"Error checking individual stop loss: {str(e)}",
                        f"order_id={order_id}, asset={stop_loss.asset}",
                    )

                stop_duration = (time.time() - stop_start) * 1000
                if DEBUG_PERFORMANCE:
                    print(f"🔍 SL check for {order_id}: {stop_duration:.2f}ms")

            # Remove triggered stops
            remove_start = time.time()
            for order_id in stops_to_remove:
                if order_id in self.active_stops:
                    del self.active_stops[order_id]
            remove_duration = (time.time() - remove_start) * 1000
            if DEBUG_PERFORMANCE:
                print(
                    f"🗑️  SL removal: {remove_duration:.2f}ms for {len(stops_to_remove)} stops"
                )

        except Exception as e:
            self._log_error(
                f"Error in stop loss conditions check: {str(e)}",
                "check_stop_loss_conditions",
            )

        total_duration = (time.time() - total_start) * 1000
        if DEBUG_PERFORMANCE:
            print(f"🛡️  Total SL check cycle: {total_duration:.2f}ms")

    def execute_stop_loss(
        self, stop_loss: CandleCloseStopLoss, trigger_price: float, reason: str
    ):
        """Execute the stop loss by closing the position"""
        execute_start = time.time()

        try:
            print(f"Executing candle-close SL for {stop_loss.asset} at {trigger_price}")

            if not self.hl_service:
                self._log_error(
                    "HyperLiquid service not available for stop loss execution",
                    f"asset={stop_loss.asset}",
                )
                return

            # Calculate position size for closing
            asset_name = self.hl_service.get_asset_name(stop_loss.asset)
            position_size = abs(stop_loss.position_size)

            # Determine if we need to buy or sell to close
            is_buy = not stop_loss.is_long  # Opposite of position direction

            # Place market order to close position
            order_start = time.time()
            result = self.hl_service.place_market_order(
                stop_loss.asset, is_buy, position_size
            )
            order_duration = (time.time() - order_start) * 1000
            print(f"📊 SL market order placement: {order_duration:.2f}ms")

            if result is not None:
                # Cancel traditional stop loss order
                try:
                    if not self.tracker:
                        self._log_error(
                            "Tracker not available for stop loss execution",
                            f"asset={stop_loss.asset}",
                        )
                        return

                    trade = self.tracker.get_trade_by_hyperliquid_id(stop_loss.order_id)
                    if trade and trade.get("absolute_sl_order_id"):
                        self.hl_service.cancel_limit_order(
                            stop_loss.asset, trade["absolute_sl_order_id"]
                        )
                except Exception as e:
                    self._log_error(
                        f"Failed to cancel traditional stop loss: {str(e)}",
                        f"asset={stop_loss.asset}",
                    )

                # Update tracker
                try:
                    self.tracker.update_stop_loss_triggered(
                        trade["uuid"], "candle_close", trigger_price
                    )
                    if self.hl_service.webhook:
                        self.hl_service.webhook.send(
                            f"🛡️ Candle-close SL executed for {stop_loss.asset}: {reason}"
                        )
                except Exception as e:
                    self._log_error(
                        f"Failed to update tracker after stop loss execution: {str(e)}",
                        f"asset={stop_loss.asset}",
                    )
            else:
                self._log_error(
                    "Failed to execute market order for stop loss",
                    f"asset={stop_loss.asset}, position_size={position_size}",
                )

        except Exception as e:
            self._log_error(
                f"Failed to execute stop loss: {str(e)}",
                f"asset={stop_loss.asset}, reason={reason}",
            )

        execute_duration = (time.time() - execute_start) * 1000
        print(f"⚡ Total SL execution: {execute_duration:.2f}ms")

    def start_monitoring(self):
        """Start the monitoring thread"""
        try:
            if not self.monitoring:
                self.monitoring = True
                self.monitor_thread = threading.Thread(
                    target=self._monitor_loop, daemon=True
                )
                self.monitor_thread.start()
                print("Candle-close SL monitoring started")
            else:
                print("Candle-close SL monitoring already active")
        except Exception as e:
            self._log_error(f"Failed to start monitoring: {str(e)}", "start_monitoring")

    def stop_monitoring(self):
        """Stop the monitoring thread"""
        try:
            self.monitoring = False
            if self.monitor_thread:
                self.monitor_thread.join(timeout=5)
            print("Candle-close SL monitoring stopped")
        except Exception as e:
            self._log_error(f"Failed to stop monitoring: {str(e)}", "stop_monitoring")

    def _monitor_loop(self):
        """Main monitoring loop that runs in a separate thread"""
        loop_count = 0
        while self.monitoring:
            loop_start = time.time()
            loop_count += 1

            try:
                if self.active_stops:
                    check_start = time.time()
                    self.check_stop_loss_conditions()
                    check_duration = (time.time() - check_start) * 1000
                    if DEBUG_PERFORMANCE:
                        print(
                            f"🛡️  SL conditions check: {check_duration:.2f}ms for {len(self.active_stops)} stops"
                        )
                else:
                    # No active stops, stop monitoring
                    self.monitoring = False
                    break

                loop_duration = (time.time() - loop_start) * 1000
                if DEBUG_PERFORMANCE:
                    print(
                        f"🔄 SL monitoring loop #{loop_count}: {loop_duration:.2f}ms total"
                    )
                    print(f"🛡️ Active stop losses: {len(self.active_stops)}")
                    print("=" * 30)

                time.sleep(self.check_interval)

            except Exception as e:
                self._log_error(
                    f"Error in monitoring loop: {str(e)}", f"loop_count={loop_count}"
                )
                time.sleep(self.check_interval)

    def get_performance_stats(self):
        """Get performance statistics"""
        try:
            stats = {
                "monitoring": self.monitoring,
                "active_stops_count": len(self.active_stops),
                "check_interval": self.check_interval,
            }
            return stats
        except Exception as e:
            self._log_error(
                f"Failed to get performance stats: {str(e)}", "get_performance_stats"
            )
            return {"error": str(e)}
