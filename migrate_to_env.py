#!/usr/bin/env python3
"""
Migration script to convert config.json to environment variables
"""
import json
import os
import sys


def load_config():
    """Load config from config.json"""
    try:
        with open("config.json", "r") as f:
            return json.load(f)
    except FileNotFoundError:
        print("❌ config.json not found!")
        return None
    except json.JSONDecodeError as e:
        print(f"❌ Error parsing config.json: {e}")
        return None


def generate_env_file(config):
    """Generate .env file from config"""
    env_content = """# HyperLiquid Configuration
HYPERLIQUID_SECRET_KEY={secret_key}
ACCOUNT_ADDRESS={account_address}

# Trading Exchange
EXCHANGE={exchange}

# Discord Configuration
DISCORD_WEBHOOK_URL={webhook}
DISCORD_BOT_TOKEN={token}
DISCORD_CHANNEL_ID={discord_channel_id}

# TradingView Configuration
TV_KEYWORD={tv_keyword}
TV_PORT={tv_port}
""".format(
        secret_key=config.get("secret_key", ""),
        account_address=config.get("account_address", ""),
        exchange=config.get("exchange", "bybit"),
        webhook=config.get("webhook", ""),
        token=config.get("token", ""),
        discord_channel_id=config.get("discord_channel_id", ""),
        tv_keyword=config.get("tv_keyword", ""),
        tv_port=config.get("tv_port", 5000),
    )

    return env_content


def main():
    print("�� Migrating config.json to environment variables...")

    # Load config
    config = load_config()
    if not config:
        sys.exit(1)

    # Generate .env file
    env_content = generate_env_file(config)

    # Write .env file
    with open(".env", "w") as f:
        f.write(env_content)

    print("✅ Generated .env file")
    print("\n📋 Next steps:")
    print("1. Review the .env file and update any values as needed")
    print("2. Set your HYPERLIQUID_PASSWORD environment variable")
    print("3. Test the application with: python main.py")
    print("4. Once confirmed working, you can delete config.json")

    print(f"\n🔐 Don't forget to set HYPERLIQUID_PASSWORD environment variable!")
    print("You can do this by adding it to your .env file or setting it in your shell")


if __name__ == "__main__":
    main()
