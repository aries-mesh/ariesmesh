"""Ed25519 key generation, signing/verification, Shamir 2-of-3 secret sharing, key storage.

Spec reference: §4. Standalone module — depends only on PyNaCl.
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from nacl.exceptions import BadSignatureError
from nacl.pwhash import argon2id
from nacl.secret import SecretBox
from nacl.signing import SigningKey, VerifyKey
from nacl.utils import random as nacl_random

from ._wordlist import BIP39_256


# ---------------------------------------------------------------------------
# KeyPair
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class KeyPair:
    signing_key: SigningKey
    verify_key: VerifyKey = field(init=False)

    def __post_init__(self) -> None:
        # frozen dataclass requires object.__setattr__ to set derived fields
        object.__setattr__(self, "verify_key", self.signing_key.verify_key)

    @classmethod
    def generate(cls) -> "KeyPair":
        return cls(signing_key=SigningKey.generate())

    @classmethod
    def from_seed(cls, seed: bytes) -> "KeyPair":
        if len(seed) != 32:
            raise ValueError(f"Ed25519 seed must be 32 bytes, got {len(seed)}")
        return cls(signing_key=SigningKey(seed))

    @classmethod
    def from_bytes(cls, raw: bytes) -> "KeyPair":
        return cls(signing_key=SigningKey(raw))

    @property
    def public_bytes(self) -> bytes:
        return bytes(self.verify_key)

    @property
    def secret_bytes(self) -> bytes:
        return bytes(self.signing_key)

    def sign(self, message: bytes) -> bytes:
        return self.signing_key.sign(message).signature

    def verify(self, message: bytes, signature: bytes) -> bool:
        try:
            self.verify_key.verify(message, signature)
            return True
        except BadSignatureError:
            return False

    def to_x25519_private(self):
        return self.signing_key.to_curve25519_private_key()

    def to_x25519_public(self):
        return self.verify_key.to_curve25519_public_key()


def verify_detached(public_bytes: bytes, message: bytes, signature: bytes) -> bool:
    """Standalone signature verification from a raw 32-byte public key."""
    try:
        VerifyKey(public_bytes).verify(message, signature)
        return True
    except BadSignatureError:
        return False
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Shamir Secret Sharing over GF(256)
# ---------------------------------------------------------------------------

_GF256_EXP: list[int] = [0] * 512
_GF256_LOG: list[int] = [0] * 256


def _init_gf256() -> None:
    """Initialize GF(2^8) exp/log tables using the AES irreducible polynomial 0x11B.

    Uses 0x03 (= x+1) as the primitive generator. NOTE: 0x02 has order 51 under
    this polynomial, not 255, so it is *not* a primitive element — a common pitfall.
    """
    x = 1
    for i in range(255):
        _GF256_EXP[i] = x
        _GF256_LOG[x] = i
        # multiply x by 3 in GF(2^8): (x*2) XOR x, with mod reduction on the double
        b = x << 1
        if b & 0x100:
            b ^= 0x11B
        x = b ^ x
    # extend exp table to avoid modulo in multiplication
    for i in range(255, 512):
        _GF256_EXP[i] = _GF256_EXP[i - 255]


_init_gf256()


def _gf256_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    return _GF256_EXP[_GF256_LOG[a] + _GF256_LOG[b]]


def _gf256_inv(a: int) -> int:
    if a == 0:
        raise ZeroDivisionError("GF(256) inverse of 0 is undefined")
    return _GF256_EXP[255 - _GF256_LOG[a]]


def shamir_split(secret: bytes, n: int = 3, k: int = 2) -> list[bytes]:
    """Split a secret into n shares such that any k can reconstruct it.

    Each share is `[x_coordinate_byte] + [y_values...]` with len = len(secret) + 1.
    """
    if k < 2 or n < k:
        raise ValueError(f"Require 2 <= k <= n, got k={k}, n={n}")
    if n > 255:
        raise ValueError("Maximum 255 shares")

    shares: list[bytearray] = [bytearray([i + 1]) for i in range(n)]

    for byte in secret:
        # k coefficients; coeffs[0] = secret byte, the rest random
        coeffs = [byte] + [secrets.randbelow(256) for _ in range(k - 1)]
        for i in range(n):
            x = i + 1
            # Horner's method: evaluate polynomial at x in GF(256)
            y = 0
            for c in reversed(coeffs):
                y = _gf256_mul(y, x) ^ c
            shares[i].append(y)

    return [bytes(s) for s in shares]


def shamir_reconstruct(shares: list[bytes]) -> bytes:
    """Lagrange interpolation at x=0 in GF(256). Any k or more shares suffice."""
    if len(shares) < 2:
        raise ValueError("Need at least 2 shares to reconstruct")
    if len({s[0] for s in shares}) != len(shares):
        raise ValueError("Duplicate x-coordinates in shares")

    xs = [s[0] for s in shares]
    secret_len = len(shares[0]) - 1
    for s in shares[1:]:
        if len(s) - 1 != secret_len:
            raise ValueError("Shares have inconsistent length")

    out = bytearray()
    for pos in range(secret_len):
        val = 0
        for i, share in enumerate(shares):
            yi = share[pos + 1]
            # basis polynomial L_i(0)
            basis = 1
            xi = xs[i]
            for j, xj in enumerate(xs):
                if i == j:
                    continue
                # L_i(0) = product( -xj / (xi - xj) ); in GF(256) negation is identity, sub is XOR
                num = xj
                denom = xi ^ xj
                basis = _gf256_mul(basis, _gf256_mul(num, _gf256_inv(denom)))
            val ^= _gf256_mul(yi, basis)
        out.append(val)
    return bytes(out)


# ---------------------------------------------------------------------------
# Key storage
# ---------------------------------------------------------------------------

def save_keypair(key: KeyPair, path: str | os.PathLike, passphrase: Optional[str] = None) -> None:
    """Persist an Ed25519 secret key to JSON at the given path with 0600 perms.

    - **With passphrase:** Argon2id-derive a 32-byte key (16-byte random salt,
      `MODERATE` ops/mem limits) and seal the Ed25519 secret with NaCl SecretBox
      (XSalsa20-Poly1305). File format version 2.

    - **Without passphrase:** plaintext hex (file format version 1). The file
      includes a `_warning: "plaintext"` field so it self-documents. Acceptable
      for v0.1 first-device init since the Shamir shares co-located in the
      same directory are also plaintext.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if passphrase is not None and passphrase != "":
        salt = nacl_random(argon2id.SALTBYTES)  # 16 bytes
        derived = argon2id.kdf(
            SecretBox.KEY_SIZE,
            passphrase.encode("utf-8"),
            salt,
            opslimit=argon2id.OPSLIMIT_MODERATE,
            memlimit=argon2id.MEMLIMIT_MODERATE,
        )
        box = SecretBox(derived)
        nonce = nacl_random(SecretBox.NONCE_SIZE)
        ciphertext = box.encrypt(key.secret_bytes, nonce).ciphertext
        payload = {
            "version": 2,
            "algorithm": "Ed25519",
            "kdf": "argon2id",
            "kdf_params": {"opslimit": "moderate", "memlimit": "moderate"},
            "salt": salt.hex(),
            "nonce": nonce.hex(),
            "ciphertext": ciphertext.hex(),
        }
    else:
        payload = {
            "version": 1,
            "algorithm": "Ed25519",
            "secret_key_hex": key.secret_bytes.hex(),
            "_warning": "plaintext; protect parent dir with OS ACLs (0600 on POSIX)",
        }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)
    except (PermissionError, OSError):
        # On Windows, chmod is a no-op or restricted; rely on user profile dir ACLs.
        pass


def load_keypair(path: str | os.PathLike, passphrase: Optional[str] = None) -> KeyPair:
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    if data.get("algorithm") != "Ed25519":
        raise ValueError(f"Unsupported key algorithm: {data.get('algorithm')!r}")
    version = int(data.get("version", 1))
    if version == 1:
        raw = bytes.fromhex(data["secret_key_hex"])
        return KeyPair.from_bytes(raw)
    if version == 2:
        if passphrase is None or passphrase == "":
            raise ValueError("Encrypted key file (version 2) requires a passphrase")
        salt = bytes.fromhex(data["salt"])
        nonce = bytes.fromhex(data["nonce"])
        ciphertext = bytes.fromhex(data["ciphertext"])
        derived = argon2id.kdf(
            SecretBox.KEY_SIZE,
            passphrase.encode("utf-8"),
            salt,
            opslimit=argon2id.OPSLIMIT_MODERATE,
            memlimit=argon2id.MEMLIMIT_MODERATE,
        )
        box = SecretBox(derived)
        raw = box.decrypt(ciphertext, nonce)  # raises CryptoError on bad passphrase
        return KeyPair.from_bytes(raw)
    raise ValueError(f"Unsupported key file version: {version}")


# ---- back-compat aliases (deprecated, will be removed in v0.2) ----
save_key_encrypted = save_keypair
load_key = load_keypair


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------

def fingerprint(public_bytes: bytes, words: int = 6) -> str:
    """Render a public key as a space-separated string of BIP39 words.

    Deterministic: same key → same fingerprint. Used for human comparison.
    """
    digest = hashlib.sha256(public_bytes).digest()
    n = int.from_bytes(digest, "big")
    picks: list[str] = []
    for _ in range(words):
        picks.append(BIP39_256[n & 0xFF])
        n >>= 8
    return " ".join(picks)
