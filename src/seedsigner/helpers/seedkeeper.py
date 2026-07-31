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


#: Where pcsc-lite puts its client socket, and the daemon that creates it.
PCSCD_SOCKET = "/run/pcscd/pcscd.comm"
PCSCD_PID = "/run/pcscd/pcscd.pid"
PCSCD_BIN = "/usr/sbin/pcscd"
TTY_READER = "/dev/ttyAMA0"

#: How long to let pysatochip's monitor thread finish connecting to the card
#: before giving up. Measured at ~3s over the SEC1210's 9600-baud serial link,
#: so this is generous on purpose: waiting a moment longer costs nothing next to
#: telling someone their good card is blank.
CARD_READY_TIMEOUT = 15.0
CARD_READY_POLL = 0.25


class SeedKeeperError(Exception):
    """Anything the user needs to be told about, already phrased for the screen."""


#: What to catch around a call into pygp or pysatochip.
#:
#: pygp reports card errors with a bare `raise BaseException(...)`, which is not a
#: subclass of Exception - so `except Exception` around a pygp call misses exactly
#: the failures worth catching. On this device an escaped exception is a blank
#: screen, there being no console to print it to, so the net is cast this wide
#: deliberately. KeyboardInterrupt and SystemExit are re-raised at every use.
_LIB_FAILURE = BaseException


def _facts() -> str:
    """
    The three things that decide whether the reader can work, in one short line.

    This device has no console, no network and no serial port to spare, so the
    screen is the only place a diagnosis can ever appear. Without this, every
    distinct failure below looks identical to the user.
    """
    import os

    # Existing and openable are different failures with the same symptom: a
    # missing node means the kernel never bound the UART, while an existing
    # node that will not open means something else holds it.
    if not os.path.exists(TTY_READER):
        tty = "MISSING"
    else:
        try:
            os.close(os.open(TTY_READER, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK))
            tty = "ok"
        except OSError as exc:
            tty = f"errno{exc.errno}"

    return "tty:{} pcscd:{} bin:{}".format(
        tty,
        "up" if os.path.exists(PCSCD_SOCKET) else "down",
        "ok" if os.path.exists(PCSCD_BIN) else "MISSING",
    )


def _start_pcscd(timeout: float = 12.0) -> str:
    """
    Make sure something is serving PC/SC, starting the daemon if not.

    S01pcscd starts it at boot, but if that did not happen - or the daemon died
    - nothing else on this device will notice. Starting it here is also the
    safer moment: a *serial* reader is probed once, when pcscd starts, and
    never again, so a daemon that came up before /dev was ready would never
    find the SEC1210 no matter how long you waited.

    Returns a short string describing what happened, for the error screen.
    """
    import os
    import subprocess
    import time

    # Every caller renders this as "pcscd: <state>", so none of these say the
    # word again - on a 240px-wide screen "pcscd: pcscd exited 1" spent a line
    # and a half repeating itself.
    if os.path.exists(PCSCD_SOCKET):
        return "already up"
    if not os.path.exists(PCSCD_BIN):
        return "not installed"

    try:
        proc = subprocess.run(
            [PCSCD_BIN], capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "did not return"
    except Exception as exc:                       # noqa: BLE001 - surfaced to screen
        return f"could not run: {exc}"

    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(PCSCD_SOCKET):
            return "started"
        time.sleep(0.25)

    # It ran but never created the socket. Whatever it printed is the reason -
    # except that in daemon mode it prints nothing at all, so ask again.
    said = " ".join((proc.stdout or "").split() + (proc.stderr or "").split())
    if said:
        return f"exit {proc.returncode}: {said[:120]}"

    why = _why_pcscd_failed()
    if why:
        return f"exit {proc.returncode}: {why}"
    return f"exit {proc.returncode}, no socket"


#: Log lines pcscd emits on its way up that name no fault. Reporting one of
#: these as "the reason" would be worse than saying nothing.
_PCSCD_NOISE = (
    "daemon ready",
    "Using drivers directory",
    "pcsc-lite ",
    "debug level=",
    "Received signal",
    "cleaning ",
    "Parsing conf",
)

#: What a line has to say to be the reason pcscd stopped. Matched case-folded:
#: pcscd's --debug output carries no severity labels at all, only
#: "file.c:line:func() message", and its own wording is inconsistent -
#: "Error initializing pcscd." next to "SEC1210 init failed." So match on the
#: words, not on a level, and never on capitalisation.
_PCSCD_FAULT = ("error", "failed", "cannot", "invalid", "no such", "critical")


def _why_pcscd_failed(timeout: float = 8.0) -> str:
    """
    Get pcscd to say *why* it will not start, in words we can put on screen.

    Started normally, pcscd forks and calls DebugLogSetLogType(SYSLOG) before
    anything can go wrong, so every reason it might give goes to syslog - and
    this image runs no syslog daemon, so the reason is discarded. Worse, the
    daemon's own exit code is only ever the byte its child wrote to the startup
    pipe: 1 for "at_exit() was reached", which covers reader-config parsing,
    socket creation and context setup alike. "exited 1" therefore names four
    different faults and distinguishes none of them.

    --foreground switches logging to stdout, which we can capture. Nothing is
    left behind: if it comes up it is killed again, and the caller retries the
    normal path.
    """
    import subprocess

    survived = False
    try:
        proc = subprocess.run(
            [PCSCD_BIN, "--foreground", "--debug"],
            capture_output=True, text=True, timeout=timeout,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as exc:
        # Still alive at the deadline, so in the foreground it works.
        survived = True
        out = _as_text(exc.stdout) + _as_text(exc.stderr)
    except Exception as exc:                       # noqa: BLE001 - surfaced to screen
        logger.info("could not run pcscd --foreground: %s", exc)
        return ""
    finally:
        # A foreground pcscd that got far enough creates the client socket, and
        # the timeout above kills it outright, so at_exit() never runs to remove
        # it. Left behind, that socket makes the next _start_pcscd() report
        # "already up" and hand the caller a dead daemon - a worse fault than
        # the one being diagnosed, and one that outlives the diagnosis.
        _clear_stale_pcscd_files()

    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]

    if survived:
        if any("daemon ready" in ln for ln in lines):
            return "runs in foreground but not as a daemon"
        return "pcscd hung starting up"

    # Its last complaint is the one that stopped it.
    for line in reversed(lines):
        if any(noise in line for noise in _PCSCD_NOISE):
            continue
        low = line.lower()
        if any(word in low for word in _PCSCD_FAULT):
            return _trim_pcscd_line(line)
    return ""


def _clear_stale_pcscd_files() -> None:
    """
    Remove a socket/pid file left by a pcscd that is no longer running.

    Only if it really is gone. S01pcscd may have started a daemon at boot that
    was still initialising when we looked - opening the SEC1210 over a 9600-baud
    link is not instant - and deleting a live daemon's socket would break a
    working reader in the name of tidying up. pcscd settles the same question
    the same way: read the pid, signal 0, believe the answer.
    """
    import os

    try:
        with open(PCSCD_PID) as fh:
            pid = int(fh.read().strip("\x00 \n\r\t") or 0)
    except (OSError, ValueError):
        pid = 0

    if pid > 0:
        try:
            os.kill(pid, 0)
            return                                 # alive - leave it alone
        except OSError:
            pass                                   # gone, or not ours to signal

    for path in (PCSCD_SOCKET, PCSCD_PID):
        try:
            if os.path.exists(path):
                os.unlink(path)
        except OSError as exc:
            logger.info("could not remove %s: %s", path, exc)


def _as_text(raw) -> str:
    """TimeoutExpired hands back bytes or str depending on how it was reached."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return raw


def _trim_pcscd_line(line: str) -> str:
    """
    Drop pcscd's timestamp/file/line preamble, keep the sentence.

    --debug lines look like
        00000000 pcscdaemon.c:733:main() Error initializing pcscd.
    and the screen this lands on is 240px wide, so the preamble would push the
    only useful words off the edge.

    Capped short on purpose: this line shares a WarningScreen with the exception
    name and _facts(), and TextArea renders straight past the bottom edge rather
    than complaining where anyone can see it.
    """
    if "()" in line:
        line = line.split("()", 1)[1].lstrip(": ")
    return line[:44]


def connect():
    """
    Open a connection to a SeedKeeper card.

    Raises SeedKeeperError with a message worth showing if there is no reader, no
    card, or the card is not a SeedKeeper.
    """
    import time

    try:
        from pysatochip.CardConnector import CardConnector, CardSelectError
    except ImportError as exc:
        raise SeedKeeperError(f"pysatochip missing: {exc}") from exc

    # Bring the daemon up before asking it anything. Without this, a pcscd that
    # is not running produces SCARD_E_NO_SERVICE, which reads like a reader
    # fault and sends you looking at the hardware.
    pcscd_state = _start_pcscd()

    try:
        from smartcard.System import readers
        if not readers():
            raise SeedKeeperError(
                "No card reader found.\n\n"
                f"pcscd: {pcscd_state}\n{_facts()}\n\n"
                "Check the SEC1210 hat is seated."
            )
    except SeedKeeperError:
        raise
    except Exception as exc:                       # noqa: BLE001 - surfaced to screen
        raise SeedKeeperError(
            f"Reader check failed:\n{type(exc).__name__}\n\n"
            f"pcscd: {pcscd_state}\n{_facts()}"
        ) from exc

    try:
        connector = CardConnector(client=None, loglevel=logging.WARNING)
    except Exception as exc:                       # noqa: BLE001
        raise SeedKeeperError(f"Could not open the reader:\n{exc}") from exc

    # Getting to a usable card takes three steps, and all three can fail for
    # reasons that are only about timing, so they retry together against one
    # deadline. Splitting them was a bug: the select retried while the secure
    # channel got a single attempt, and the first attempt after any applet
    # install or removal reliably lost - pygp's close() ends in a *cold reset*,
    # which powers the card down, so the next transmit has to re-negotiate T1 on
    # a card that has just come back up. That surfaced as
    # "SCARD_E_NOT_TRANSACTED (0x80100016)" on screen, followed by everything
    # working on the second try.
    #
    #   1. select the applet - CardConnector connects on its own monitor thread,
    #      so straight after the constructor cardservice is a bare
    #      PassThruCardService with no protocol negotiated and transmitting
    #      raises "Invalid protocol in transmit". Measured at ~3s here.
    #   2. read the status - this is the only thing that sets
    #      needs_secure_channel.
    #   3. open the secure channel if the card wants one. Without step 2 the flag
    #      stays false, every command goes out in plain text, the card answers
    #      9C21, pysatochip re-initiates, loops, sends plain text again, and
    #      spins inside `while self.card_present` forever - the UI thread simply
    #      stops with nothing on screen.
    #
    # Retrying is safe: all three are idempotent. Re-selecting is a no-op, the
    # status read changes nothing, and card_initiate_secure_channel just builds a
    # fresh SecureChannel.
    deadline = time.monotonic() + CARD_READY_TIMEOUT
    attempts = 0
    while True:
        attempts += 1
        try:
            connector.card_select_seedkeeper()
            connector.card_get_status()
            if connector.needs_secure_channel:
                connector.card_initiate_secure_channel()
            break
        except CardSelectError as exc:
            # The card answered and does not have the applet. That is an answer,
            # not a timing problem, so do not sit here retrying it.
            _safe_disconnect(connector)
            raise SeedKeeperError(
                "No SeedKeeper applet on this card.\n\nInsert a SeedKeeper card, or "
                "install the applet from Smartcard Tools."
            ) from exc
        except (KeyboardInterrupt, SystemExit):
            raise
        except _LIB_FAILURE as exc:                # card not ready yet
            if time.monotonic() >= deadline:
                _safe_disconnect(connector)
                raise SeedKeeperError(
                    "The card never became ready.\n\n"
                    f"{type(exc).__name__}: {str(exc)[:100]}\n\n"
                    f"after {attempts} attempts\n"
                    f"pcscd: {pcscd_state}\n{_facts()}"
                ) from exc
            time.sleep(CARD_READY_POLL)

    if attempts > 1:
        logger.debug("card became usable on attempt %d", attempts)

    return connector


#: A SeedKeeper PIN is 4-16 characters. The card enforces the upper bound; the
#: lower one is ours, and matches what the Satochip tools ask for.
PIN_MIN_LENGTH = 4
PIN_MAX_LENGTH = 16

#: Wrong-PIN attempts the card allows before it locks itself.
PIN_TRIES = 0x05


def is_initialized(connector) -> bool:
    """
    Whether the applet has been set up, i.e. has a PIN.

    A freshly installed applet has none, and every command that matters refuses
    to run until it does. connect() already asks for the card's status, so this is
    just reading what it found.
    """
    return bool(getattr(connector, "setup_done", False))


def initialize(connector, pin: str) -> None:
    """
    Perform first-time setup on a new applet: set its PIN.

    Parameters follow satochip_cli.py's common_initial_setup, which ships in this
    same image and is the reference the Satochip tools use. Of note, and passed on
    to the user rather than hidden:

      - The PUK, which is what would normally unlock a card whose PIN is
        forgotten, is set to 16 random bytes that are then discarded. That is
        upstream's deliberate choice - their comment is that the PUK "is not used
        currently and is not user friendly". The consequence is real: after
        PIN_TRIES wrong attempts the card locks for good and the only way back is
        to reinstall the applet, taking any secret with it.
      - The second PIN slot is unused, so it also gets random bytes.
    """
    if not pin or not pin.isdigit():
        raise SeedKeeperError("The PIN must be digits only.")
    if not PIN_MIN_LENGTH <= len(pin) <= PIN_MAX_LENGTH:
        raise SeedKeeperError(
            f"The PIN must be {PIN_MIN_LENGTH} to {PIN_MAX_LENGTH} digits."
        )

    from os import urandom

    pin_0 = list(pin.encode("utf-8"))
    try:
        response, sw1, sw2 = connector.card_setup(
            PIN_TRIES, 0x01, pin_0, list(urandom(16)),
            0x01, 0x01, list(urandom(16)), list(urandom(16)),
            32,          # secmemsize, as the reference tool passes it
            0x0000,      # memsize, reserved for future use
            0x01, 0x01, 0x01,          # the three ACLs, also reserved
            option_flags=0, hmacsha160_key=None, amount_limit=0,
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except _LIB_FAILURE as exc:
        raise SeedKeeperError(f"Setup failed:\n{type(exc).__name__}: {exc}") from exc

    if (sw1, sw2) != (0x90, 0x00):
        raise SeedKeeperError(
            f"The card refused setup (status {sw1:02X}{sw2:02X})."
        )

    # card_setup sets this itself on success; check rather than assume, so a
    # silent partial setup cannot be reported as done.
    if not is_initialized(connector):
        raise SeedKeeperError(
            "The card accepted setup but still reports itself uninitialized."
        )


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


# --------------------------------------------------------------------------- #
# Card management (GlobalPlatform), for installing and removing applets
# --------------------------------------------------------------------------- #

#: The default GlobalPlatform test key, which is what an unlocked development
#: Javacard ships with. A card locked to its owner's own keys will refuse it, and
#: that refusal is the correct outcome - we report it rather than trying anything
#: clever to get around it.
GP_DEFAULT_KEY = "404142434445464748494A4B4C4D4E4F"

#: How many times to build a card-manager session before giving up.
#:
#: pygp reports card trouble by raising bare `BaseException` - see _LIB_FAILURE -
#: and a session can come up authenticated but dead, answering every GET STATUS
#: with an empty list until it is torn down and rebuilt. One retry turns that from
#: a wrong answer on screen into a pause.
GP_OPEN_ATTEMPTS = 2

def _gp_session(pygp, pcscd_state: str = ""):
    """One attempt at terminal + card + auth. Raises SeedKeeperError."""
    try:
        pygp.terminal()
        pygp.card()
    except _LIB_FAILURE as exc:
        # pygp says only "Service not available" here - SCARD_E_NO_SERVICE
        # spelled out, i.e. the symptom we already know - and nothing about why.
        # connect() has always carried pcscd's own account onto its screen; this
        # path dropped it, which is why the card manager failure was the less
        # informative of the two. When there is a reason, print that instead of
        # pygp's prose rather than as well as it: the lines are not free.
        detail = f"{str(exc)[:120]}\n" if not pcscd_state else ""
        raise SeedKeeperError(
            f"Could not reach the card manager:\n{type(exc).__name__}\n{detail}\n"
            + (f"pcscd: {pcscd_state}\n" if pcscd_state else "")
            + _facts()
        ) from exc

    try:
        pygp.auth(
            enc_key=GP_DEFAULT_KEY, mac_key=GP_DEFAULT_KEY, dek_key=GP_DEFAULT_KEY,
            keysetversion="00", securitylevel=pygp.SECURITY_LEVEL_C_MAC,
        )
    except _LIB_FAILURE as exc:
        raise SeedKeeperError(
            "The card refused the default GlobalPlatform key.\n\n"
            "It is locked to its own keys, so applets cannot be installed or "
            "removed from here."
        ) from exc


def gp_open():
    """
    Open an authenticated GlobalPlatform session with the card manager.

    Separate from connect(): that one selects the SeedKeeper applet and talks to
    it, this one talks to the domain that installs and deletes applets. They
    cannot be combined - selecting an applet deselects the card manager.

    The session is proved working before being handed back, because an
    authenticated-but-dead one is worse than a failure: it reports a card full of
    applets as empty, and "empty" is the answer that would let a delete look
    successful.

    Raises SeedKeeperError phrased for the screen.
    """
    try:
        import pygp
    except ImportError as exc:
        raise SeedKeeperError(f"pygp missing: {exc}") from exc

    pcscd_state = _start_pcscd()

    import time

    last_error = None
    for attempt in range(GP_OPEN_ATTEMPTS):
        if attempt:
            # Let the reader settle. A previous session's close() ends in a cold
            # reset, and coming straight back at a card that has just been
            # unpowered is what produces SCARD_E_NOT_TRANSACTED.
            time.sleep(CARD_READY_POLL)
        try:
            _gp_session(pygp, pcscd_state)
        except SeedKeeperError as exc:
            last_error = exc
            gp_close(pygp)
            continue

        try:
            # Liveness probe. A working session answers this; a dead one raises
            # or returns nothing at all.
            if list(pygp.get_loaded_package_aids() or []):
                return pygp
            # An empty answer is either a blank card or a dead session, and they
            # are indistinguishable from here. Rebuild once; a blank card gives
            # the same answer next time and is then believed.
            if attempt + 1 >= GP_OPEN_ATTEMPTS:
                return pygp
            last_error = SeedKeeperError("The card manager session came up empty.")
        except _LIB_FAILURE as exc:
            last_error = SeedKeeperError(
                f"The card manager session did not answer:\n{type(exc).__name__}: "
                f"{str(exc)[:120]}"
            )
        gp_close(pygp)

    raise last_error or SeedKeeperError("Could not open a card manager session.")


def aid_label(aid: str) -> str:
    """
    A human hint for an AID, from the printable ASCII inside it.

    Applet AIDs are arbitrary bytes, but the Satochip family spells its names in
    them - 'SeedKeeper' is literally 53656564 4B6565706572 - so this is usually
    the difference between a list of hex strings and a list a person can choose
    from. Falls back to the hex when there is nothing readable, and never lets a
    decode failure stop the listing.
    """
    try:
        raw = bytes.fromhex(aid)
    except ValueError:
        return aid
    text = "".join(chr(b) if 32 <= b < 127 else "" for b in raw)
    return text if len(text) >= 4 else aid


#: GET STATUS answered normally.
_SW_OK = "9000"
#: GET STATUS matched nothing - an answer, not a failure.
_SW_NOT_FOUND = "6A88"


def _gp_read_aids(pygp_module, fetch, what: str, attempts: int = 3):
    """
    Read a list of AIDs from the card, telling "empty" apart from "failed".

    pygp's listing functions return [] both when the card holds nothing and when
    the command did not work - their own docstrings say "or on error". That
    ambiguity is dangerous here: an empty list is what we would otherwise read as
    proof that a delete succeeded. Observed for real on this hardware, a whole
    session returned [] five times running while the card demonstrably held five
    packages.

    So the status word decides. An empty list is only believed when the card
    actually said 9000 or 6A88; anything else is a failure and raises. The retry
    is for the flakiness itself - a listing cannot invent packages, so the first
    non-empty answer is the truth.
    """
    import time

    last_sw = None
    for attempt in range(attempts):
        try:
            aids = list(fetch() or [])
        except (KeyboardInterrupt, SystemExit):
            raise
        except _LIB_FAILURE as exc:
            raise SeedKeeperError(
                f"Could not read {what}:\n{type(exc).__name__}: {str(exc)[:120]}"
            ) from exc

        try:
            last_sw = str(pygp_module.last_status()).strip().upper()
        except (KeyboardInterrupt, SystemExit):
            raise
        except _LIB_FAILURE:
            last_sw = None

        if aids:
            return aids
        if last_sw in (_SW_OK, _SW_NOT_FOUND):
            return []
        if attempt + 1 < attempts:
            time.sleep(0.2)

    raise SeedKeeperError(
        f"Could not read {what}.\n\nThe card answered {last_sw or 'nothing'}."
    )


def managed_aids() -> tuple:
    """
    The AIDs this tool is willing to remove, taken from pysatochip.

    Everything else on a Javacard is the card's own: the JavaCard framework
    packages (A0000000 62xxxx), GlobalPlatform's own (A000000151...), and whatever
    the manufacturer preloaded. Deleting those is not a feature - at best it
    fails, at worst it takes the card with it. So removal is opt-in by AID rather
    than opt-out, and the list comes from the library instead of being typed out
    here, so a Satochip family member added upstream is picked up for free.
    """
    try:
        from pysatochip.CardConnector import CardConnector
    except ImportError:                            # pragma: no cover - checked at import
        return ()

    aids = []
    for name in ("SEEDKEEPER_AID", "SATOCHIP_AID", "SATODIME_AID", "SATOCASH_AID"):
        value = getattr(CardConnector, name, None)
        if value:
            aids.append(bytes(value).hex().upper())
    return tuple(aids)


def _is_managed(package_aid: str, managed: tuple) -> bool:
    """
    Whether a package AID belongs to an applet this tool installs.

    Compared as a prefix in both directions: a package AID and the AID its applet
    is selected by are usually the same bytes, but nothing in GlobalPlatform
    requires it - an instance AID is often the package AID plus a suffix.
    """
    aid = package_aid.upper()
    return any(aid.startswith(m) or m.startswith(aid) for m in managed)


def gp_list_packages(pygp_module, managed_only: bool = True):
    """
    The packages on the card, each with the applet instances it owns.

    Returns a list of (package_aid, label, [instance_aids]).

    Packages are the unit offered for removal rather than instances, because
    deleting an instance on its own leaves its package loaded, and a loaded
    package blocks reinstalling the same applet later - a confusing state to be
    left in by a menu item called uninstall.

    By default only applets this tool manages are returned - see managed_aids().
    Pass managed_only=False to see everything the card reports, which is what the
    delete-confirmation arithmetic needs: whether the card still holds other
    packages is a question about all of them, not just ours.
    """
    packages = _gp_read_aids(
        pygp_module, pygp_module.get_loaded_package_aids, "the card's packages")
    instances = _gp_read_aids(
        pygp_module, pygp_module.get_installed_application_aids, "the card's applets")

    if managed_only:
        managed = managed_aids()
        packages = [p for p in packages if _is_managed(p, managed)]

    result = []
    for package in packages:
        # An instance's AID normally begins with its package's AID. That is a
        # convention rather than a rule, so it is only used to enrich the
        # listing; a package with nothing matched still gets offered.
        owned = [i for i in instances if i.startswith(package)]
        result.append((package, aid_label(package), owned))
    return result


def gp_count_packages(pygp_module) -> int:
    """
    How many packages the card reports in total, ours and the card's own.

    Kept separate from the filtered listing because the deletion check reasons
    about the whole card: an empty listing after a delete is only implausible if
    the card really was holding several packages, and it holds the framework's
    whether we offer them or not.
    """
    return len(_gp_read_aids(
        pygp_module, pygp_module.get_loaded_package_aids, "the card's packages"))


def gp_delete_package(pygp_module, package_aid: str) -> None:
    """
    Delete a package and every applet instance belonging to it.

    This is destructive and there is no undo: a Javacard applet's storage goes
    with the applet, so any seed held by a SeedKeeper being removed here is gone.
    Callers must have confirmed with the user first.
    """
    try:
        pygp_module.delete_package(package_aid)
    except (KeyboardInterrupt, SystemExit):
        raise
    except _LIB_FAILURE as exc:
        raise SeedKeeperError(
            f"Delete failed:\n{type(exc).__name__}: {str(exc)[:140]}"
        ) from exc


#: Nothing on the card matched the AID that was selected.
_SW_NO_SUCH_AID = "6A82"

#: What gp_deletion_state can conclude.
DELETED = "removed"
NOT_DELETED = "present"
UNCONFIRMED = "unknown"


def _gp_select_sw(pygp_module, aid: str):
    """
    SELECT an AID and return the status word, or None if it could not be asked.

    Unlike the GET STATUS listing, this status word is trustworthy: it comes
    straight back from the APDU we just sent, so there is no way for a silent
    failure to leave a stale success behind.

    Note SELECT matches on a prefix, so selecting a package's AID also finds any
    applet instance installed underneath it. That is what makes one probe enough.

    Call this last. A SELECT that finds nothing leaves no applet selected, which
    takes the secure channel with it - measured here, a GET STATUS issued after a
    6A82 fails. So this is a terminal move, not something to probe with first.
    """
    apdu = "00A40400%02X%s" % (len(aid) // 2, aid)
    try:
        # Every outcome we care about is "expected", so pygp reports the status
        # word rather than raising on the interesting one.
        pygp_module.send(apdu, raw_mode=True, exsw="9000, 6A82, 6999, 6A80, 6A81")
        return str(pygp_module.last_status()).strip().upper()
    except (KeyboardInterrupt, SystemExit):
        raise
    except _LIB_FAILURE:
        return None


def gp_deletion_state(pygp_module, package_aid: str, packages_before: int) -> str:
    """
    Decide whether a package really went away. Returns DELETED, NOT_DELETED or
    UNCONFIRMED.

    Worth this much care because the alternative is lying about someone's seed. A
    false "removed" says a secret was destroyed when it was not; a false "still
    there" sends them to erase it again. So both answers need evidence, and when
    there is none the honest result is UNCONFIRMED.

    Two signals, because neither alone is enough:

      - The GET STATUS listing covers packages, which is what was deleted, but it
        returns an empty list on failure as readily as on an empty card and its
        status word can be stale. An empty listing is only credible when the card
        was not holding several packages a moment ago: deleting one of five
        cannot leave none.
      - Selecting the AID is reliable but incomplete. 9000 means an applet is
        still there, full stop. 6A82 only means nothing *selectable* remains, and
        a loaded package with no instance is invisible to SELECT.

    The listing goes first because the SELECT is destructive to the session: a
    SELECT that matches nothing leaves the secure channel gone, so a GET STATUS
    after it fails. In the ordinary case the listing settles it and no SELECT is
    sent at all.
    """
    try:
        packages = _gp_read_aids(
            pygp_module, pygp_module.get_loaded_package_aids, "the card's packages")
    except SeedKeeperError:
        packages = None

    if packages:
        return NOT_DELETED if package_aid in packages else DELETED

    if packages == [] and packages_before <= 1:
        # The card credibly holds nothing, and it only held this one thing.
        return DELETED

    # Either the listing could not be read, or it came back empty in a way that
    # does not add up. Fall back to the probe, and accept losing the session.
    sw = _gp_select_sw(pygp_module, package_aid)
    if sw == _SW_OK:
        return NOT_DELETED
    if sw == _SW_NO_SUCH_AID:
        return DELETED
    return UNCONFIRMED


def gp_package_loaded_for_cap(pygp_module, cap_path: str) -> bool:
    """
    Whether the card already holds the package this .cap would load.

    GlobalPlatform will refuse a second copy, but it refuses partway through the
    load with a status word pygp surfaces as a bare "Transaction failed." - which
    tells the user nothing and looks identical to the transient reader errors this
    device throws anyway. Better to ask first and say something useful.

    The cap's applet AIDs are read from the file itself. An applet AID is its
    package's AID plus a suffix, so the loaded package is matched as a prefix.
    Returns False if the cap cannot be read: refusing to install because we could
    not parse the file would be worse than trying and reporting what the card says.
    """
    try:
        applet_aids = [a.upper() for a in (pygp_module.get_applet_aids(cap_path) or [])]
    except (KeyboardInterrupt, SystemExit):
        raise
    except _LIB_FAILURE:
        return False

    if not applet_aids:
        return False

    loaded = _gp_read_aids(
        pygp_module, pygp_module.get_loaded_package_aids, "the card's packages")
    return any(aid.startswith(package.upper())
               for package in loaded for aid in applet_aids)


#: What gp_install_state can conclude.
INSTALLED = "installed"
NOT_INSTALLED = "absent"


def gp_install_state(pygp_module, cap_path: str) -> str:
    """
    After an install attempt, did the applet actually land? INSTALLED,
    NOT_INSTALLED or UNCONFIRMED.

    Needed because install_capfile can raise having already done the job. Seen on
    this hardware: the load and the instance creation both completed, a trailing
    command hit the reader's transient "Transaction failed", and pygp turned that
    into a bare BaseException - so a card with a perfectly good applet on it
    reported "install failed" and invited the user to try again. A second attempt
    then finds the package loaded and refuses.

    The applet *instance* is what gets checked rather than the package, because an
    instance is what makes the applet selectable, and a package loaded without one
    is not a usable install.
    """
    try:
        applet_aids = [a.upper() for a in (pygp_module.get_applet_aids(cap_path) or [])]
    except (KeyboardInterrupt, SystemExit):
        raise
    except _LIB_FAILURE:
        return UNCONFIRMED

    if not applet_aids:
        return UNCONFIRMED

    try:
        instances = [i.upper() for i in _gp_read_aids(
            pygp_module, pygp_module.get_installed_application_aids,
            "the card's applets")]
    except SeedKeeperError:
        return UNCONFIRMED

    # any(), not all(): a cap can carry several applets while only one instance is
    # asked for, and one selectable instance of what we loaded is a working install.
    if any(aid in instances for aid in applet_aids):
        return INSTALLED
    if instances:
        return NOT_INSTALLED
    # Nothing at all came back. The card may simply have no instances, but after an
    # install that just claimed to work it is not evidence either way.
    return UNCONFIRMED


def gp_close(pygp_module) -> None:
    """
    Drop the card manager session.

    Worth doing rather than leaving to process exit: sessions left open appear to
    be what made GET STATUS flaky here, with a fresh script sometimes finding an
    empty card until the reader settled.
    """
    try:
        pygp_module.close()
    except (KeyboardInterrupt, SystemExit):
        raise
    except _LIB_FAILURE:                          # best effort
        pass
