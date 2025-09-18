from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
import base64
import json
import os
import getpass


def encrypt_secret_key(secret_key: str, password: str) -> str:
    # Generate a random salt
    salt = os.urandom(16)

    # Generate a random nonce
    nonce = os.urandom(12)

    # Derive key from password using PBKDF2
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=100000,
        backend=default_backend(),
    )
    key = kdf.derive(password.encode())

    # Encrypt the secret key
    aesgcm = AESGCM(key)
    encrypted_key = aesgcm.encrypt(nonce, secret_key.encode(), None)

    # Encode everything as base64
    encrypted_data = {
        "salt": base64.b64encode(salt).decode(),
        "nonce": base64.b64encode(nonce).decode(),
        "encrypted_key": base64.b64encode(encrypted_key).decode(),
    }

    return json.dumps(encrypted_data)


def main():
    print("HyperLiquid Secret Key Encryption Tool")
    print("=" * 40)

    # Get secret key
    secret_key = getpass.getpass("Enter your HyperLiquid secret key: ")

    # Get password for encryption
    password = getpass.getpass("Enter a password to encrypt the secret key: ")
    password_confirm = getpass.getpass("Confirm password: ")

    if password != password_confirm:
        print("Passwords do not match!")
        return

    # Encrypt the secret key
    encrypted_key = encrypt_secret_key(secret_key, password)

    print("\nEncrypted secret key:")
    print(encrypted_key)
    print("\nSet this as your HYPERLIQUID_SECRET_KEY environment variable")
    print("Make sure to also set your ACCOUNT_ADDRESS environment variable")


if __name__ == "__main__":
    main()
