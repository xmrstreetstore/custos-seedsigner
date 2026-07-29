"""
Talking to a Satochip SeedKeeper card.

Deliberately written against pysatochip directly rather than ported from
3rdIteration's fork. Their SeedKeeper flow lives inside a `seed_views.py` that is
+4,071/-1,057 lines against stock and pulls in ndeflib, keycard-py and a web of
new helpers; this file is the part we actually want, against stock's APIs.

Nothing here draws to the screen. Views own the UI; this owns the card.
"""

import logging

logger = logging.getLogger(__name__)

#: Secret types we can turn into a SeedSigner seed. From pysatochip's
#: SEEDKEEPER_DIC_TYPE. Masterseed subtype 0x00 is a raw masterseed (an xprv, not a
#: mnemonic) and is deliberately not supported - SeedSigner is mnemonic-based, and
#: silently importing something we cannot round-trip would be worse than refusing.
TYPE_MASTERSEED = 0x10
TYPE_BIP39 = 0x30
TYPE_ELECTRUM = 0x40

#: Only plaintext export can be read without a second card to decrypt with.
EXPORT_PLAINTEXT_ALLOWED = 0x01


class SeedKeeperError(Exception):
    """Anything the user needs to be told about, already phrased for the screen."""


def connect():
    """
    Open a connection to a SeedKeeper card.

    Raises SeedKeeperError with a message worth showing if there is no reader, no
    card, or the card is not a SeedKeeper.
    """
    try:
        from pysatochip.CardConnector import CardConnector
    except ImportError as exc:
        raise SeedKeeperError(f"pysatochip missing: {exc}") from exc

    try:
        from smartcard.System import readers
        if not readers():
            raise SeedKeeperError(
                "No card reader found.\n\nCheck the SEC1210 hat is seated, and that "
                "nothing else is holding the serial port."
            )
    except SeedKeeperError:
        raise
    except Exception as exc:                       # noqa: BLE001 - surfaced to screen
        raise SeedKeeperError(f"Reader check failed:\n{type(exc).__name__}: {exc}") from exc

    try:
        connector = CardConnector(client=None, loglevel=logging.WARNING)
    except Exception as exc:                       # noqa: BLE001
        raise SeedKeeperError(f"Could not open the reader:\n{exc}") from exc

    try:
        connector.card_select_seedkeeper()
    except Exception as exc:                       # noqa: BLE001
        _safe_disconnect(connector)
        raise SeedKeeperError(
            "No SeedKeeper applet on this card.\n\nInsert a SeedKeeper card, or "
            "install the applet from Smartcard Tools."
        ) from exc

    return connector


def verify_pin(connector, pin: str) -> None:
    """Present the PIN. Raises SeedKeeperError describing what the card said."""
    try:
        connector.card_verify_PIN(pin)
    except Exception as exc:                       # noqa: BLE001
        left = getattr(exc, "pin_left", None)
        if left is not None:
            raise SeedKeeperError(
                f"Wrong PIN.\n\n{left} attempt(s) left before the card is blocked."
            ) from exc
        raise SeedKeeperError(f"PIN rejected:\n{exc}") from exc


def list_importable(connector):
    """
    Secret headers on the card that we could turn into a seed.

    Returns a list of (sid, label, description) tuples. Filtered rather than shown
    whole: a card can hold passwords, descriptors and 2FA secrets, and offering
    those as "load a seed" would only produce a failure two screens later.
    """
    try:
        headers = connector.seedkeeper_list_secret_headers()
    except Exception as exc:                       # noqa: BLE001
        raise SeedKeeperError(f"Could not list secrets:\n{exc}") from exc

    out = []
    for header in headers:
        stype = header.get("type")
        subtype = header.get("subtype", 0)
        rights = header.get("export_rights")
        if rights != EXPORT_PLAINTEXT_ALLOWED:
            continue
        if stype in (TYPE_BIP39, TYPE_ELECTRUM) or (
                stype == TYPE_MASTERSEED and subtype == 0x01):
            label = (header.get("label") or "").strip() or f"secret {header.get('id')}"
            kind = "BIP39" if stype != TYPE_ELECTRUM else "Electrum"
            out.append((header["id"], label, kind, stype, subtype))
    return out


def export_mnemonic(connector, sid, stype, subtype):
    """
    Export one secret and decode it to (mnemonic, passphrase).

    The two on-card layouts, both length-prefixed:

      BIP39 / Electrum   [len][mnemonic ascii][len][passphrase ascii]
      Masterseed v2      [len][masterseed][wordlist byte][len][entropy][len][passphrase]

    The v2 layout stores entropy rather than words, so the mnemonic is regenerated
    from it - which is also why only the english wordlist is accepted here: embit
    generates english, and quietly returning english words for a card that says
    japanese would be a wrong seed, not a cosmetic issue.
    """
    try:
        secret = connector.seedkeeper_export_secret(sid, None)
    except Exception as exc:                       # noqa: BLE001
        raise SeedKeeperError(
            f"Export refused by the card:\n{exc}\n\nA secret must be marked "
            "'Plaintext export allowed' to be read this way."
        ) from exc

    raw = bytes.fromhex(secret["secret"])
    if not raw:
        raise SeedKeeperError("The card returned an empty secret.")

    if stype in (TYPE_BIP39, TYPE_ELECTRUM):
        size = raw[0]
        mnemonic = raw[1:1 + size].decode("utf-8", errors="replace").strip()
        passphrase = ""
        rest = raw[1 + size:]
        if rest:
            plen = rest[0]
            passphrase = rest[1:1 + plen].decode("utf-8", errors="replace")
        return mnemonic, passphrase

    if stype == TYPE_MASTERSEED and subtype == 0x01:
        offset = 0
        ms_size = raw[offset]; offset += 1
        offset += ms_size                              # masterseed itself unused
        wordlist_byte = raw[offset]; offset += 1
        entropy_size = raw[offset]; offset += 1
        entropy = raw[offset:offset + entropy_size]; offset += entropy_size
        passphrase = ""
        if offset < len(raw):
            plen = raw[offset]; offset += 1
            passphrase = raw[offset:offset + plen].decode("utf-8", errors="replace")

        if wordlist_byte != 0x00:
            raise SeedKeeperError(
                "This seed is stored with a non-english wordlist, which this build "
                "cannot regenerate.\n\nRefusing rather than returning the wrong words."
            )
        try:
            from embit import bip39
            mnemonic = bip39.mnemonic_from_bytes(entropy)
        except Exception as exc:                   # noqa: BLE001
            raise SeedKeeperError(f"Could not rebuild the mnemonic:\n{exc}") from exc
        return mnemonic, passphrase

    raise SeedKeeperError(f"Unsupported secret type {hex(stype)}.")


def _safe_disconnect(connector) -> None:
    try:
        connector.card_disconnect()
    except Exception:                              # noqa: BLE001 - best effort
        pass


def disconnect(connector) -> None:
    _safe_disconnect(connector)
