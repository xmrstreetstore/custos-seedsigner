"""
SeedKeeper card support: import a seed, and install the applet.

The two features this fork exists to add. Everything that talks to the card is in
helpers/seedkeeper.py; this file is only screens and flow.

Failures are shown, never swallowed. This runs on a device with no console, no
network and no serial port free (the SEC1210 has the UART), so the display is the
only diagnostic channel there is - an exception that does not reach it is an
exception nobody will ever see.
"""

import logging

from gettext import gettext as _

from seedsigner.gui.components import SeedSignerIconConstants
from seedsigner.gui.screens import RET_CODE__BACK_BUTTON
from seedsigner.gui.screens.screen import (
    ButtonListScreen, ButtonOption, LoadingScreenThread, WarningScreen,
)
from seedsigner.helpers import seedkeeper
from seedsigner.models.seed import Seed
from seedsigner.views.view import BackStackView, Destination, MainMenuView, View

logger = logging.getLogger(__name__)


class SmartcardMenuView(View):
    """Top of the smartcard tree, reached from the Tools menu."""

    LOAD_SEED = ButtonOption("Load seed from card", SeedSignerIconConstants.MICROSD)
    INSTALL = ButtonOption("Install applet")

    def run(self):
        button_data = [self.LOAD_SEED, self.INSTALL]
        selected = self.run_screen(
            ButtonListScreen,
            title=_("Smartcard"),
            is_button_text_centered=False,
            button_data=button_data,
        )
        if selected == RET_CODE__BACK_BUTTON:
            return Destination(BackStackView)
        if button_data[selected] == self.LOAD_SEED:
            return Destination(SeedKeeperSelectView)
        return Destination(InstallAppletView)


class SeedKeeperSelectView(View):
    """
    Connect to the card, take the PIN, and list what can be loaded.

    Also the destination of "From SeedKeeper" in the load-seed menu.
    """

    def run(self):
        from seedsigner.gui.screens.seedkeeper_screens import SeedKeeperPinEntryScreen

        loading = LoadingScreenThread(text=_("Reading card..."))
        connector = None
        try:
            loading.start()
            try:
                connector = seedkeeper.connect()
            finally:
                loading.stop()

            pin = self.run_screen(SeedKeeperPinEntryScreen)
            if pin == RET_CODE__BACK_BUTTON:
                return Destination(BackStackView)
            if not pin:
                return self._warn(_("No PIN entered."))

            loading = LoadingScreenThread(text=_("Unlocking..."))
            loading.start()
            try:
                seedkeeper.verify_pin(connector, pin)
                secrets = seedkeeper.list_importable(connector)
            finally:
                loading.stop()

            if not secrets:
                return self._warn(
                    _("No loadable seeds on this card.\n\nOnly secrets marked "
                      "'Plaintext export allowed' can be read.")
                )

            button_data = [ButtonOption(f"{label} ({kind})") for _sid, label, kind, _t, _s in secrets]
            selected = self.run_screen(
                ButtonListScreen,
                title=_("Card secrets"),
                is_button_text_centered=False,
                button_data=button_data,
            )
            if selected == RET_CODE__BACK_BUTTON:
                return Destination(BackStackView)

            sid, label, _kind, stype, subtype = secrets[selected]
            loading = LoadingScreenThread(text=_("Exporting..."))
            loading.start()
            try:
                mnemonic, passphrase = seedkeeper.export_mnemonic(
                    connector, sid, stype, subtype)
            finally:
                loading.stop()

        except seedkeeper.SeedKeeperError as exc:
            return self._warn(str(exc))
        except Exception as exc:                       # noqa: BLE001
            # Deliberately broad: an unhandled traceback here would leave a blank
            # screen on a device with no other way to report anything.
            logger.exception("SeedKeeper import failed")
            return self._warn(f"{type(exc).__name__}:\n{exc}")
        finally:
            if connector is not None:
                seedkeeper.disconnect(connector)

        words = mnemonic.split()
        try:
            seed = Seed(mnemonic=words, passphrase=passphrase)
        except Exception as exc:                       # noqa: BLE001
            return self._warn(
                _("The card returned an invalid mnemonic ({n} words):\n{err}").format(
                    n=len(words), err=exc)
            )

        self.controller.storage.set_pending_seed(seed)
        from seedsigner.views.seed_views import SeedFinalizeView
        return Destination(SeedFinalizeView)

    def _warn(self, text):
        self.run_screen(
            WarningScreen,
            title=_("SeedKeeper"),
            status_headline=None,
            text=text,
            show_back_button=False,
        )
        return Destination(MainMenuView)


class InstallAppletView(View):
    """
    Flash a Javacard applet from the .cap files bundled in the image.

    Also reads <microsd>/javacard-cap/, so an applet can be supplied on the card
    without rebuilding an image.
    """

    INTERNAL_DIR = "/opt/javacard-cap"

    def run(self):
        import os
        from seedsigner.hardware.microsd import MicroSD

        candidates = []
        for directory in (self.INTERNAL_DIR,
                          os.path.join(str(MicroSD.MOUNT_POINT), "javacard-cap")):
            try:
                for name in sorted(os.listdir(directory)):
                    if name.lower().endswith(".cap"):
                        candidates.append((name, os.path.join(directory, name)))
            except OSError:
                continue

        if not candidates:
            return self._warn(_("No .cap files found in {d}.").format(d=self.INTERNAL_DIR))

        button_data = [ButtonOption(name) for name, _p in candidates]
        selected = self.run_screen(
            ButtonListScreen,
            title=_("Install applet"),
            is_button_text_centered=False,
            button_data=button_data,
        )
        if selected == RET_CODE__BACK_BUTTON:
            return Destination(BackStackView)

        name, path = candidates[selected]

        # SeedKeeper's applet takes its secret-storage size as an install
        # parameter, and it cannot be changed afterwards without wiping the card.
        storage = None
        if "seedkeeper" in name.lower():
            sizes = [
                ButtonOption("4 KB", return_data="0FFF"),
                ButtonOption("8 KB (default)", return_data="1FFF"),
                ButtonOption("16 KB", return_data="3FFF"),
                ButtonOption("32 KB", return_data="7FFF"),
                ButtonOption("64 KB", return_data="FFFF"),
            ]
            picked = self.run_screen(
                ButtonListScreen,
                title=_("Storage size"),
                is_button_text_centered=False,
                button_data=sizes,
                selected_button=1,
            )
            if picked == RET_CODE__BACK_BUTTON:
                return Destination(BackStackView)
            storage = sizes[picked].return_data or "1FFF"

        ret = self.run_screen(
            WarningScreen,
            title=_("Install applet"),
            status_headline=None,
            text=_("Flash {name} onto the inserted card?\n\nThis overwrites the "
                   "applet and any secrets it holds.").format(name=name),
            show_back_button=True,
        )
        if ret == RET_CODE__BACK_BUTTON:
            return Destination(BackStackView)

        # Default GlobalPlatform test key. Cards shipped locked with their own keys
        # will refuse this, which is the correct outcome - we report it rather than
        # trying anything clever.
        default_key = "404142434445464748494A4B4C4D4E4F"

        loading = LoadingScreenThread(text=_("Installing..."))
        try:
            loading.start()
            try:
                import pygp
                pygp.terminal()
                pygp.card()
                pygp.auth(
                    enc_key=default_key, mac_key=default_key, dek_key=default_key,
                    keysetversion="00", securitylevel=pygp.SECURITY_LEVEL_C_MAC,
                )
                if storage:
                    pygp.install_capfile(path, application_specific_parameters=storage)
                else:
                    pygp.install_capfile(path)
            finally:
                loading.stop()
        except Exception as exc:                       # noqa: BLE001
            logger.exception("applet install failed")
            return self._warn(_("Install failed:\n{t}: {e}").format(
                t=type(exc).__name__, e=str(exc)[:160]))

        return self._warn(_("{name} installed.").format(name=name))

    def _warn(self, text):
        self.run_screen(
            WarningScreen,
            title=_("Smartcard"),
            status_headline=None,
            text=text,
            show_back_button=False,
        )
        return Destination(MainMenuView)
