import json
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Any
import os


class TradeTracker:
    def __init__(self, json_file_path: str = "trades.json"):
        self.json_file_path = json_file_path
        self.trades: Dict[str, Dict] = {}
        self.load_from_json()

    def add_trade(
        self,
        currency: str,
        timeframe: str,
        qty_usd: float,
        qty_asset: float,
        entry_price: float,
        side: str,  # "long" or "short"
        stop_loss_price: float,
        hyperliquid_order_id: Optional[str] = None,
        original_qty_asset: Optional[float] = None,  # NEW: Track original position size
        take_profit_1: Optional[float] = None,
        take_profit_2: Optional[float] = None,
        leverage: Optional[int] = None,
        use_candle_close_sl: bool = False,
        candle_sl_timeframe: Optional[str] = None,
    ) -> str:
        """
        Add a new trade to tracking
        Returns: UUID of the created trade
        """
        trade_uuid = str(uuid.uuid4())
        timestamp = datetime.now().isoformat()

        trade_data = {
            "uuid": trade_uuid,
            "currency": currency.upper(),
            "timeframe": timeframe,
            "qty_usd": qty_usd,
            "qty_asset": qty_asset,
            "original_qty_asset": original_qty_asset
            or qty_asset,  # Store original amount
            "current_qty_asset": qty_asset,  # Track current remaining position
            "entry_price": entry_price,
            "exit_price": None,
            "avg_exit_price": None,  # For partial exits
            "trade_type": "futures",  # HyperLiquid is futures
            "side": side.lower(),
            "stop_loss_price": stop_loss_price,
            "take_profit_1": take_profit_1,
            "take_profit_2": take_profit_2,
            "leverage": leverage,
            "status": "active",  # Changed from "open" to "active"
            "hyperliquid_order_id": hyperliquid_order_id,
            # Stop Loss tracking - Always both types
            "absolute_sl_price": stop_loss_price,  # Traditional/immediate SL
            "absolute_sl_active": True,  # Always enable absolute SL
            "absolute_sl_order_id": None,  # Track SL order ID
            "candle_close_sl_price": stop_loss_price,  # Candle-close SL (same level initially)
            "candle_close_sl_active": True,  # Always enable candle-close SL
            "candle_sl_timeframe": candle_sl_timeframe
            or timeframe,  # Use trade timeframe if not specified
            # TP tracking
            "tp1_achieved": False,
            "tp1_price": None,
            "tp1_qty_closed": 0.0,
            "tp1_timestamp": None,
            "tp2_achieved": False,
            "tp2_price": None,
            "tp2_qty_closed": 0.0,
            "tp2_timestamp": None,
            # P&L tracking
            "realized_pnl_usd": 0.0,
            "unrealized_pnl_usd": 0.0,
            # Stop loss execution tracking
            "sl_triggered_by": None,  # "absolute" or "candle_close"
            "sl_trigger_price": None,  # Price at which SL was triggered
            # Timestamps
            "created_at": timestamp,
            "updated_at": timestamp,
            "closed_at": None,
            # Additional metadata
            "execution_details": [],  # Track all partial executions
            "notes": "",
        }

        self.trades[trade_uuid] = trade_data
        self.save_to_json()
        return trade_uuid

    def update_trade_status(
        self, trade_uuid: str, new_status: str, exit_price: Optional[float] = None
    ) -> bool:
        """
        Update trade status
        Valid statuses: "active", "tp1_achieved", "tp2_achieved", "fully_closed",
                       "stopped_out", "negated", "manual_close", "cancelled"
        """
        valid_statuses = [
            "active",
            "tp1_achieved",
            "tp2_achieved",
            "fully_closed",
            "stopped_out",
            "negated",
            "manual_close",
            "cancelled",
        ]

        if new_status not in valid_statuses:
            raise ValueError(f"Invalid status. Must be one of: {valid_statuses}")

        if trade_uuid not in self.trades:
            return False

        trade = self.trades[trade_uuid]
        trade["status"] = new_status
        trade["updated_at"] = datetime.now().isoformat()

        if exit_price is not None:
            trade["exit_price"] = exit_price

        # Set closed timestamp for final statuses
        if new_status in ["fully_closed", "stopped_out", "negated", "manual_close"]:
            trade["closed_at"] = datetime.now().isoformat()

        self.save_to_json()
        return True

    def update_trade_status_by_order_id(
        self, order_id: str, new_status: str, exit_price: Optional[float] = None
    ) -> bool:
        """Update trade status by HyperLiquid order ID"""
        trade = self.get_trade_by_hyperliquid_id(order_id)
        if trade:
            return self.update_trade_status(trade["uuid"], new_status, exit_price)
        return False

    def update_trade_tp1(
        self, trade_uuid: str, tp1_price: float, qty_closed: float
    ) -> bool:
        """Mark TP1 as achieved and record details"""
        if trade_uuid not in self.trades:
            return False

        trade = self.trades[trade_uuid]
        trade["tp1_achieved"] = True
        trade["tp1_price"] = tp1_price
        trade["take_profit_1"] = tp1_price  # BUG FIX: Update take_profit_1 field
        trade["tp1_qty_closed"] = qty_closed
        trade["tp1_timestamp"] = datetime.now().isoformat()
        trade["current_qty_asset"] = trade["current_qty_asset"] - qty_closed
        trade["status"] = "tp1_achieved"
        trade["updated_at"] = datetime.now().isoformat()

        # Add execution detail
        execution_detail = {
            "type": "tp1",
            "price": tp1_price,
            "qty": qty_closed,
            "timestamp": datetime.now().isoformat(),
            "remaining_qty": trade["current_qty_asset"],
        }
        trade["execution_details"].append(execution_detail)

        # Calculate realized P&L for this portion
        self._update_realized_pnl(trade_uuid, tp1_price, qty_closed)

        self.save_to_json()
        return True

    def update_stop_loss_triggered(
        self, trade_uuid: str, triggered_by: str, trigger_price: float
    ) -> bool:
        """Record which stop loss type was triggered"""
        if trade_uuid not in self.trades:
            return False

        trade = self.trades[trade_uuid]
        trade["sl_triggered_by"] = triggered_by  # "absolute" or "candle_close"
        trade["sl_trigger_price"] = trigger_price
        trade["updated_at"] = datetime.now().isoformat()

        # Deactivate both stop losses since one was triggered
        trade["absolute_sl_active"] = False
        trade["candle_close_sl_active"] = False

        self.save_to_json()
        return True

    def update_absolute_sl_order_id(self, trade_uuid: str, sl_order_id: str) -> bool:
        """Store the absolute stop loss order ID"""
        if trade_uuid not in self.trades:
            return False

        trade = self.trades[trade_uuid]
        trade["absolute_sl_order_id"] = sl_order_id
        trade["updated_at"] = datetime.now().isoformat()
        self.save_to_json()
        return True

    def deactivate_stop_losses(self, trade_uuid: str) -> bool:
        """Deactivate both stop loss types (when position is closed)"""
        if trade_uuid not in self.trades:
            return False

        trade = self.trades[trade_uuid]
        trade["absolute_sl_active"] = False
        trade["candle_close_sl_active"] = False
        trade["updated_at"] = datetime.now().isoformat()
        self.save_to_json()
        return True

    def update_trade_tp2(
        self, trade_uuid: str, tp2_price: float, qty_closed: float
    ) -> bool:
        """Mark TP2 as achieved (full close) and record details"""
        if trade_uuid not in self.trades:
            return False

        trade = self.trades[trade_uuid]
        remaining_qty = trade["current_qty_asset"]

        trade["tp2_achieved"] = True
        trade["tp2_price"] = tp2_price
        trade["take_profit_2"] = tp2_price  # BUG FIX: Update take_profit_2 field
        trade["tp2_qty_closed"] = remaining_qty
        trade["tp2_timestamp"] = datetime.now().isoformat()
        trade["current_qty_asset"] = 0.0
        trade["status"] = "fully_closed"
        trade["closed_at"] = datetime.now().isoformat()
        trade["updated_at"] = datetime.now().isoformat()
        trade["exit_price"] = tp2_price  # Final exit price

        # Add execution detail
        execution_detail = {
            "type": "tp2",
            "price": tp2_price,
            "qty": remaining_qty,
            "timestamp": datetime.now().isoformat(),
            "remaining_qty": 0.0,
        }
        trade["execution_details"].append(execution_detail)

        # Calculate realized P&L for remaining portion
        self._update_realized_pnl(trade_uuid, tp2_price, remaining_qty)

        self.save_to_json()
        return True

    def _update_realized_pnl(
        self, trade_uuid: str, exit_price: float, qty_closed: float
    ):
        """Update realized P&L for partial closes"""
        trade = self.trades[trade_uuid]
        entry_price = trade["entry_price"]
        side = trade["side"]

        # Calculate P&L for this portion
        if side == "long":
            pnl_per_unit = exit_price - entry_price
        else:  # short
            pnl_per_unit = entry_price - exit_price

        portion_pnl_usd = (pnl_per_unit / entry_price) * (qty_closed * entry_price)
        trade["realized_pnl_usd"] += portion_pnl_usd

    def get_trade(self, trade_uuid: str) -> Optional[Dict]:
        """Get a specific trade by UUID"""
        return self.trades.get(trade_uuid)

    def get_trades_by_currency(self, currency: str) -> List[Dict]:
        """Get all trades for a specific currency"""
        return [
            trade
            for trade in self.trades.values()
            if trade["currency"] == currency.upper()
        ]

    def get_trades_by_timeframe(self, timeframe: str) -> List[Dict]:
        """Get all trades for a specific timeframe"""
        return [
            trade for trade in self.trades.values() if trade["timeframe"] == timeframe
        ]

    def get_trades_by_symbol_timeframe(self, symbol: str, timeframe: str) -> List[Dict]:
        """Get trades by symbol and timeframe combination"""
        return [
            trade
            for trade in self.trades.values()
            if trade["currency"] == symbol.upper() and trade["timeframe"] == timeframe
        ]

    def get_active_trades(self) -> List[Dict]:
        """Get all currently active trades (not fully closed)"""
        active_statuses = ["active", "tp1_achieved"]
        return [
            trade
            for trade in self.trades.values()
            if trade["status"] in active_statuses
        ]

    def get_active_trades_by_currency(self, currency: str) -> List[Dict]:
        """Get active trades for a specific currency"""
        active_statuses = ["active", "tp1_achieved"]
        return [
            trade
            for trade in self.trades.values()
            if trade["currency"] == currency.upper()
            and trade["status"] in active_statuses
        ]

    def get_trade_by_hyperliquid_id(self, hl_order_id: str) -> Optional[Dict]:
        """Find trade by HyperLiquid order ID"""
        for trade in self.trades.values():
            if trade["hyperliquid_order_id"] == hl_order_id:
                return trade
        return None

    def close_trade_fully(
        self, trade_uuid: str, exit_price: float, reason: str = "manual"
    ) -> bool:
        """
        Fully close a trade with exit price
        reason: "tp1", "tp2", "negated", "stopped_out", "manual"
        """
        if trade_uuid not in self.trades:
            return False

        trade = self.trades[trade_uuid]
        remaining_qty = trade["current_qty_asset"]

        status_map = {
            "tp1": "tp1_achieved",
            "tp2": "fully_closed",
            "negated": "negated",
            "stopped_out": "stopped_out",
            "manual": "manual_close",
        }

        new_status = status_map.get(reason, "manual_close")

        # Update trade details
        trade["exit_price"] = exit_price
        trade["current_qty_asset"] = 0.0
        trade["closed_at"] = datetime.now().isoformat()

        # Add execution detail
        execution_detail = {
            "type": f"full_close_{reason}",
            "price": exit_price,
            "qty": remaining_qty,
            "timestamp": datetime.now().isoformat(),
            "remaining_qty": 0.0,
        }
        trade["execution_details"].append(execution_detail)

        # Calculate final realized P&L
        self._update_realized_pnl(trade_uuid, exit_price, remaining_qty)

        return self.update_trade_status(trade_uuid, new_status, exit_price)

    def close_trade_partial(
        self,
        trade_uuid: str,
        exit_price: float,
        qty_to_close: float,
        reason: str = "partial",
    ) -> bool:
        """Partially close a trade"""
        if trade_uuid not in self.trades:
            return False

        trade = self.trades[trade_uuid]
        if qty_to_close > trade["current_qty_asset"]:
            return False  # Can't close more than current position

        trade["current_qty_asset"] -= qty_to_close
        trade["updated_at"] = datetime.now().isoformat()

        # Add execution detail
        execution_detail = {
            "type": f"partial_close_{reason}",
            "price": exit_price,
            "qty": qty_to_close,
            "timestamp": datetime.now().isoformat(),
            "remaining_qty": trade["current_qty_asset"],
        }
        trade["execution_details"].append(execution_detail)

        # Calculate realized P&L for closed portion
        self._update_realized_pnl(trade_uuid, exit_price, qty_to_close)

        # Update status if fully closed
        if trade["current_qty_asset"] <= 0:
            trade["status"] = "fully_closed"
            trade["closed_at"] = datetime.now().isoformat()
            trade["exit_price"] = exit_price

        self.save_to_json()
        return True

    def calculate_current_pnl(
        self, trade_uuid: str, current_price: Optional[float] = None
    ) -> Optional[Dict]:
        """Calculate current P&L for a trade (realized + unrealized)"""
        trade = self.get_trade(trade_uuid)
        if not trade:
            return None

        entry_price = trade["entry_price"]
        side = trade["side"]
        realized_pnl = trade["realized_pnl_usd"]

        # Calculate unrealized P&L for remaining position
        unrealized_pnl = 0.0
        if trade["current_qty_asset"] > 0 and current_price:
            remaining_value = trade["current_qty_asset"] * entry_price

            if side == "long":
                pnl_pct = (current_price - entry_price) / entry_price
            else:  # short
                pnl_pct = (entry_price - current_price) / entry_price

            unrealized_pnl = remaining_value * pnl_pct

        total_pnl = realized_pnl + unrealized_pnl
        total_invested = trade["qty_usd"]
        total_pnl_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0

        return {
            "realized_pnl_usd": round(realized_pnl, 2),
            "unrealized_pnl_usd": round(unrealized_pnl, 2),
            "total_pnl_usd": round(total_pnl, 2),
            "total_pnl_percentage": round(total_pnl_pct, 2),
            "current_price": current_price,
            "entry_price": entry_price,
            "remaining_qty": trade["current_qty_asset"],
        }

    def calculate_final_pnl(self, trade_uuid: str) -> Optional[Dict]:
        """Calculate final P&L for a closed trade"""
        trade = self.get_trade(trade_uuid)
        if not trade or trade["status"] in ["active", "tp1_achieved"]:
            return None

        # For fully closed trades, use realized P&L
        total_pnl = trade["realized_pnl_usd"]
        total_invested = trade["qty_usd"]
        pnl_pct = (total_pnl / total_invested * 100) if total_invested > 0 else 0

        return {
            "final_pnl_usd": round(total_pnl, 2),
            "final_pnl_percentage": round(pnl_pct, 2),
            "entry_price": trade["entry_price"],
            "exit_price": trade["exit_price"],
            "tp1_achieved": trade["tp1_achieved"],
            "tp2_achieved": trade["tp2_achieved"],
        }

    def get_trade_summary(self) -> Dict:
        """Get comprehensive summary statistics of all trades"""
        if not self.trades:
            return {
                "total_trades": 0,
                "active_trades": 0,
                "closed_trades": 0,
                "total_realized_pnl_usd": 0,
                "win_rate_percent": 0,
                "profitable_trades": 0,
                "tp1_hit_rate": 0,
                "tp2_hit_rate": 0,
            }

        total_trades = len(self.trades)
        active_trades = len(self.get_active_trades())
        closed_trades = total_trades - active_trades

        # Calculate statistics for closed trades
        total_realized_pnl = 0
        profitable_trades = 0
        tp1_hits = 0
        tp2_hits = 0

        for trade in self.trades.values():
            total_realized_pnl += trade["realized_pnl_usd"]

            if trade["realized_pnl_usd"] > 0:
                profitable_trades += 1

            if trade["tp1_achieved"]:
                tp1_hits += 1

            if trade["tp2_achieved"]:
                tp2_hits += 1

        win_rate = (profitable_trades / total_trades * 100) if total_trades > 0 else 0
        tp1_rate = (tp1_hits / total_trades * 100) if total_trades > 0 else 0
        tp2_rate = (tp2_hits / total_trades * 100) if total_trades > 0 else 0

        # Get status breakdown
        status_counts = {}
        for trade in self.trades.values():
            status = trade["status"]
            status_counts[status] = status_counts.get(status, 0) + 1

        return {
            "total_trades": total_trades,
            "active_trades": active_trades,
            "closed_trades": closed_trades,
            "total_realized_pnl_usd": round(total_realized_pnl, 2),
            "win_rate_percent": round(win_rate, 2),
            "profitable_trades": profitable_trades,
            "tp1_hit_rate_percent": round(tp1_rate, 2),
            "tp2_hit_rate_percent": round(tp2_rate, 2),
            "status_breakdown": status_counts,
        }

    def get_performance_by_timeframe(self) -> Dict:
        """Get performance statistics broken down by timeframe"""
        timeframe_stats = {}

        for trade in self.trades.values():
            tf = trade["timeframe"]
            if tf not in timeframe_stats:
                timeframe_stats[tf] = {
                    "total_trades": 0,
                    "realized_pnl": 0,
                    "profitable_trades": 0,
                    "tp1_hits": 0,
                    "tp2_hits": 0,
                }

            stats = timeframe_stats[tf]
            stats["total_trades"] += 1
            stats["realized_pnl"] += trade["realized_pnl_usd"]

            if trade["realized_pnl_usd"] > 0:
                stats["profitable_trades"] += 1
            if trade["tp1_achieved"]:
                stats["tp1_hits"] += 1
            if trade["tp2_achieved"]:
                stats["tp2_hits"] += 1

        # Calculate percentages
        for tf, stats in timeframe_stats.items():
            total = stats["total_trades"]
            if total > 0:
                stats["win_rate_percent"] = round(
                    stats["profitable_trades"] / total * 100, 2
                )
                stats["tp1_rate_percent"] = round(stats["tp1_hits"] / total * 100, 2)
                stats["tp2_rate_percent"] = round(stats["tp2_hits"] / total * 100, 2)

        return timeframe_stats

    def save_to_json(self):
        """Save trades to JSON file with error handling"""
        try:
            # Create backup before saving
            if os.path.exists(self.json_file_path):
                backup_path = f"{self.json_file_path}.backup"
                with open(self.json_file_path, "r") as source:
                    with open(backup_path, "w") as backup:
                        backup.write(source.read())

            # Save current data
            with open(self.json_file_path, "w") as f:
                json.dump(self.trades, f, indent=2, default=str)

        except Exception as e:
            print(f"Error saving trades to JSON: {e}")

    def load_from_json(self):
        """Load trades from JSON file with error handling"""
        if os.path.exists(self.json_file_path):
            try:
                with open(self.json_file_path, "r") as f:
                    loaded_trades = json.load(f)

                # Migrate old trade format if needed
                for trade_id, trade in loaded_trades.items():
                    # Add missing fields with defaults
                    if "original_qty_asset" not in trade:
                        trade["original_qty_asset"] = trade.get("qty_asset", 0)
                    if "current_qty_asset" not in trade:
                        trade["current_qty_asset"] = trade.get("qty_asset", 0)
                    if "tp1_achieved" not in trade:
                        trade["tp1_achieved"] = False
                    if "tp2_achieved" not in trade:
                        trade["tp2_achieved"] = False
                    if "realized_pnl_usd" not in trade:
                        trade["realized_pnl_usd"] = 0.0
                    if "execution_details" not in trade:
                        trade["execution_details"] = []
                    # Always both stop loss types
                    if "absolute_sl_price" not in trade:
                        trade["absolute_sl_price"] = trade.get("stop_loss_price", 0)
                    if "absolute_sl_active" not in trade:
                        trade["absolute_sl_active"] = True
                    if "absolute_sl_order_id" not in trade:
                        trade["absolute_sl_order_id"] = None
                    if "candle_close_sl_price" not in trade:
                        trade["candle_close_sl_price"] = trade.get("stop_loss_price", 0)
                    if "candle_close_sl_active" not in trade:
                        trade["candle_close_sl_active"] = True
                    if "candle_sl_timeframe" not in trade:
                        trade["candle_sl_timeframe"] = trade.get("timeframe", "1h")
                    if "sl_triggered_by" not in trade:
                        trade["sl_triggered_by"] = None
                    if "sl_trigger_price" not in trade:
                        trade["sl_trigger_price"] = None
                    # Migrate status
                    if trade.get("status") == "open":
                        trade["status"] = "active"

                self.trades = loaded_trades
                self.save_to_json()  # Save migrated format

            except Exception as e:
                print(f"Error loading trades from JSON: {e}")
                # Try backup if available
                backup_path = f"{self.json_file_path}.backup"
                if os.path.exists(backup_path):
                    try:
                        with open(backup_path, "r") as f:
                            self.trades = json.load(f)
                        print("Loaded from backup file")
                    except:
                        self.trades = {}
                else:
                    self.trades = {}
        else:
            self.trades = {}

    def export_to_csv(self, filename: str = "trades_export.csv"):
        """Export trades to CSV for analysis"""
        import csv

        if not self.trades:
            print("No trades to export")
            return

        # Flatten the trade data for CSV export
        flattened_trades = []
        for trade in self.trades.values():
            # Create a flattened version excluding nested lists/dicts for CSV
            flat_trade = {
                k: v for k, v in trade.items() if not isinstance(v, (list, dict))
            }
            flattened_trades.append(flat_trade)

        if flattened_trades:
            with open(filename, "w", newline="") as csvfile:
                fieldnames = list(flattened_trades[0].keys())
                writer = csv.DictWriter(csvfile, fieldnames=fieldnames)

                writer.writeheader()
                for trade in flattened_trades:
                    writer.writerow(trade)

            print(f"Trades exported to {filename}")
