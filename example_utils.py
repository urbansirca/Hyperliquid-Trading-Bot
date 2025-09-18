from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import base64
import eth_account
from eth_account.signers.local import LocalAccount
import json
import os
import getpass
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info


def decrypt_secret_key(encrypted_data: str, password: str) -> str:
    data = json.loads(encrypted_data)
    salt = base64.b64decode(data["salt"])
    nonce = base64.b64decode(data["nonce"])
    encrypted_key = base64.b64decode(data["encrypted_key"])

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
        backend=default_backend(),
    )
    key = kdf.derive(password.encode())

    aesgcm = AESGCM(key)
    decrypted_key = aesgcm.decrypt(nonce, encrypted_key, None)

    return decrypted_key.decode()


def setup(base_url=None, skip_ws=False, password=None):
    # Load config from environment variables
    secret_key = os.environ.get("HYPERLIQUID_SECRET_KEY")
    account_address = os.environ.get("ACCOUNT_ADDRESS")

    if not secret_key:
        raise ValueError("HYPERLIQUID_SECRET_KEY environment variable is required")

    secret_key = decrypt_secret_key(secret_key, password)

    account: LocalAccount = eth_account.Account.from_key(secret_key)

    if not account_address:
        account_address = account.address
    print("Running with account address:", account_address)
    if account_address != account.address:
        print("Running with agent address:", account.address)
    info = Info(base_url, skip_ws)
    user_state = info.user_state(account_address)
    margin_summary = user_state["marginSummary"]
    if float(margin_summary["accountValue"]) == 0:
        print("Not running the example because the provided account has no equity.")
        url = info.base_url.split(".", 1)[1]
        error_string = f"No accountValue:\nIf you think this is a mistake, make sure that {account_address} has a balance on {url}.\nIf address shown is your API wallet address, update the config to specify the address of your account, not the address of the API wallet."
        raise Exception(error_string)
    exchange = Exchange(account, base_url, account_address=account_address)
    return account_address, info, exchange
