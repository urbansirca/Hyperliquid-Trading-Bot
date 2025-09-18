import asyncio
import os
import json
import threading
import getpass
from datetime import datetime
from flask import Flask, request, jsonify

from comm_service import CommunicationService
from execution_service import HyperLiquidExecutionService
from screener_service import Screener
from tv_service import TradingViewWebhookService
from candle_service import CandleCloseStopLossManager
from tracker import TradeTracker
from discord import SyncWebhook

# Global Flask app
app = Flask(__name__)

# Global services container
services = None


def load_config_from_env():
    """Load configuration from environment variables with fallbacks"""
    config = {}

    # Required environment variables
    required_vars = {
        "HYPERLIQUID_SECRET_KEY": "secret_key",
        "ACCOUNT_ADDRESS": "account_address",
        "EXCHANGE": "exchange",
        "DISCORD_WEBHOOK_URL": "webhook",
        "DISCORD_BOT_TOKEN": "token",
        "TV_KEYWORD": "tv_keyword",
        "DISCORD_CHANNEL_ID": "discord_channel_id",
    }

    # Check for required variables
    missing_vars = []
    for env_var, config_key in required_vars.items():
        value = os.environ.get(env_var)
        if value:
            config[config_key] = value
        else:
            missing_vars.append(env_var)

    if missing_vars:
        raise ValueError(
            f"Missing required environment variables: {', '.join(missing_vars)}"
        )

    # Optional variables with defaults
    config["tv_port"] = int(os.environ.get("TV_PORT", "5000"))

    return config


class ServiceContainer:
    """Central container for all services - follows Dependency Injection pattern"""

    def __init__(self, config: dict, hyperliquid_password: str):
        self.config = config
        self.hyperliquid_password = hyperliquid_password

        # Create shared webhook first
        webhook_url = config["webhook"]
        self.shared_webhook = SyncWebhook.from_url(webhook_url)

        # Create TradeTracker first (singleton)
        print("🔧 Initializing TradeTracker...")
        self.tracker = TradeTracker()
        print("✅ TradeTracker initialized")

        # Initialize HyperLiquid service with shared tracker
        print("🔧 Initializing HyperLiquid service...")
        try:
            self.hl_service = HyperLiquidExecutionService(
                password=hyperliquid_password,
                webhook=self.shared_webhook,
                tracker=self.tracker,
            )
            print(
                f"✅ HyperLiquid service initialized with account {self.hl_service.address}"
            )
        except Exception as e:
            print(f"❌ Error initializing HyperLiquid service: {e}")
            raise e

        # Initialize all other services
        self._initialize_all_services()

    def log_backend_error(self, error_msg: str, context: str = "", service: str = ""):
        """Centralized error logging for all services"""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        error_details = f"[{timestamp}] [{service}] {context}: {error_msg}"

        # Log to console
        print(f"❌ {error_details}")

        # Send to Discord webhook
        try:
            self.shared_webhook.send(
                content=f"🚨 **Backend Error**\n```{error_details}```"
            )
        except Exception as e:
            print(f"Failed to send error to Discord: {e}")

    def _initialize_all_services(self):
        """Initialize all services with proper dependency injection"""
        print("🔧 Initializing all services...")

        # Initialize Communication Service
        print("🔧 Initializing Communication Service...")
        self.comm_service = CommunicationService(
            hlbot=self.hl_service,
            screener=None,  # Will be set after screener is created
            shared_webhook=self.shared_webhook,
            tv_service=None,  # Will be set after TV service is created
            candle_sl_manager=None,  # Will be set after candle service is created
            tracker=self.tracker,
            error_logger=self.log_backend_error,
        )
        print("✅ Communication Service initialized")

        # Initialize Screener Service
        print("🔧 Initializing Screener Service...")
        self.screener = Screener(
            hl_service=self.hl_service,
        )
        print("✅ Screener Service initialized")

        # Initialize TradingView Webhook Service
        print("🔧 Initializing TradingView Webhook Service...")
        self.tv_service = TradingViewWebhookService(
            security_keyword=self.config.get("tv_keyword"),
            hyperliquid_password=self.hyperliquid_password,
            port=self.config.get("tv_port", 5001),
            webhook=self.shared_webhook,
            hl_service=self.hl_service,
            tracker=self.tracker,
            error_logger=self.log_backend_error,
        )
        print("✅ TradingView Webhook Service initialized")

        # Initialize Candle Close Stop Loss Manager
        print("🔧 Initializing Candle Close Stop Loss Manager...")
        self.candle_sl_manager = CandleCloseStopLossManager(
            tv_service=self.tv_service,
            hl_service=self.hl_service,
            tracker=self.tracker,
            error_logger=self.log_backend_error,
        )
        print("✅ Candle Close Stop Loss Manager initialized")

        # Set up cross-references
        self.comm_service.screener = self.screener
        self.comm_service.tv_service = self.tv_service
        self.comm_service.candle_sl_manager = self.candle_sl_manager

        # Set up candle service reference in TV service
        self.tv_service.candle_sl_manager = self.candle_sl_manager

        print("✅ All services initialized and cross-referenced")


# Flask routes
@app.route("/webhook", methods=["POST"])
def webhook():
    if services and services.tv_service:
        return services.tv_service.handle_webhook()
    return jsonify({"error": "Service not available"}), 503


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})


def run_discord_bot():
    try:
        asyncio.run(services.comm_service.start(services.config["token"]))
    except Exception as e:
        print(f"Discord bot error: {e}")


async def main():
    global services
    print("🚀 Starting HyperLiquid Trading Bot...")

    # Load config from environment variables
    try:
        config = load_config_from_env()
        print("✅ Configuration loaded from environment variables")
    except ValueError as e:
        print(f"❌ Configuration error: {e}")
        return

    # Get HyperLiquid password/key - priority order:
    # 1. Environment variable (for Heroku)
    # 2. Prompt as last resort

    hyperliquid_password = os.environ.get("HYPERLIQUID_PASSWORD")

    if not hyperliquid_password:
        # Only prompt if nothing found and not in production
        if os.environ.get("HEROKU") or os.environ.get("DYNO"):
            raise ValueError(
                "HYPERLIQUID_PASSWORD environment variable is required for Heroku deployment"
            )
        else:
            hyperliquid_password = getpass.getpass("Enter your HyperLiquid password: ")

    # Create service container
    services = ServiceContainer(config, hyperliquid_password)

    # Start Discord bot in background thread
    discord_thread = threading.Thread(target=run_discord_bot, daemon=True)
    discord_thread.start()

    print("✅ Bot is ready! All services are running.")
    print("📋 Use $help to see available commands")
    print("🌐 Webhook available at /webhook")
    print("❤️ Health check available at /health")

    # Start Flask app (this blocks)
    # Heroku assigns PORT automatically
    port = int(os.environ.get("PORT", config.get("tv_port", 5000)))
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    asyncio.run(main())
