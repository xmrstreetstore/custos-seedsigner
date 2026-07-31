"""
SeedKeeper card support: import a seed, and manage the applet on the card.

The features this fork exists to add. Everything that talks to the card is in
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
    ButtonListScreen, ButtonOption, DireWarningScreen, LoadingScreenThread,
    WarningScreen,
)
from seedsigner.helpers import seedkeeper
from seedsigner.models.seed import Seed
from seedsigner.views.view import BackStackView, Destination, MainMenuView, View

logger = logging.getLogger(__name__)


#: Roughly how many characters of body text fit on one line of a 240px screen.
#: Measured, not guessed: at the 17px body font the usable width between the
#: warning screens' padding is 208px, and the widest lines that fit come out
#: around 22-25 characters.
_CHARS_PER_LINE = 22


def _show_success(view, title: str, text: str):
    """
    Report something that worked, in green.

    The warning screens were doing double duty: an applet installing correctly
    and an applet failing to install both arrived under the same yellow
    exclamation mark and the same pulsing amber border. On a signing device that
    is worth separating - the icon should tell you the outcome before the words
    do.

    LargeIconStatusScreen is stock's own way of saying this, and deliberately not
    a WarningScreen: it carries the green SUCCESS colour and, because it does not
    mix in WarningEdgesMixin, none of the animated border either.
    """
    from seedsigner.gui.screens.screen import LargeIconStatusScreen

    view.run_screen(
        LargeIconStatusScreen,
        title=title,
        status_headline=_("Success!"),
        text=text,
        show_back_button=False,
        button_data=[ButtonOption("OK")],
    )
    return Destination(MainMenuView)


def _wrap_capname(name: str) -> str:
    """
    Break a .cap filename at its hyphens so it fits the screen, losing nothing.

    TextArea reflows on spaces only, so a filename like
    SeedKeeper-Ndef-v0.2-0.1.cap is one unbreakable 246px token on a screen with
    208px to give: it renders straight off the right edge. Truncating it with an
    ellipsis fits but reads like a bug, and on a screen that is about to
    overwrite someone's secrets the exact name is worth keeping. So the breaks go
    in at the hyphens, where they look deliberate.

    The .cap suffix is dropped - every candidate has it, so it distinguishes
    nothing and costs a third of a line.
    """
    if name.lower().endswith(".cap"):
        name = name[:-4]

    lines, current = [], ""
    for part in name.split("-"):
        piece = part if not current else "-" + part
        if current and len(current) + len(piece) > _CHARS_PER_LINE:
            lines.append(current + "-")
            current = part
        else:
            current += piece
    if current:
        lines.append(current)
    return "\n".join(lines)


class SmartcardMenuView(View):
    """Top of the smartcard tree, reached from the Tools menu."""

    LOAD_SEED = ButtonOption("Load seed from card", SeedSignerIconConstants.MICROSD)
    INITIALIZE = ButtonOption("Set up new card")
    INSTALL = ButtonOption("Install applet")
    UNINSTALL = ButtonOption("Uninstall applet")

    def run(self):
        button_data = [self.LOAD_SEED, self.INITIALIZE, self.INSTALL, self.UNINSTALL]
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
        if button_data[selected] == self.INITIALIZE:
            return Destination(InitializeCardView)
        if button_data[selected] == self.INSTALL:
            return Destination(InstallAppletView)
        return Destination(UninstallAppletView)


class InitializeCardView(View):
    """
    First-time setup for a freshly installed applet: give it a PIN.

    A new applet has no PIN, and until it does every command that matters refuses
    to run - which is the wall you hit after installing one. pysatochip reports
    that as "not initialized" and offers nothing further, so this is the missing
    step rather than a nicety.

    Asks twice and compares, because there is no way back from a typo: the PUK
    that would normally rescue a card with a forgotten PIN is set to random bytes
    and discarded, which is what the reference tool does deliberately.
    """

    PROCEED = ButtonOption("Set this PIN")

    def run(self):
        from seedsigner.gui.screens.seedkeeper_screens import SeedKeeperPinEntryScreen

        connector = None
        try:
            loading = LoadingScreenThread(text=_("Reading card..."))
            loading.start()
            try:
                connector = seedkeeper.connect()
            finally:
                loading.stop()

            if seedkeeper.is_initialized(connector):
                return self._warn(
                    _("Already set up.\n\nA new PIN needs the applet reinstalled, "
                      "which erases the card.")
                )

            pin = self.run_screen(SeedKeeperPinEntryScreen, prompt=_("New PIN"))
            if pin == RET_CODE__BACK_BUTTON:
                return Destination(BackStackView)
            if not pin:
                return self._warn(_("No PIN entered."))
            if not seedkeeper.PIN_MIN_LENGTH <= len(pin) <= seedkeeper.PIN_MAX_LENGTH:
                return self._warn(
                    _("A PIN must be {lo} to {hi} digits.\n\nNothing was "
                      "changed.").format(lo=seedkeeper.PIN_MIN_LENGTH,
                                         hi=seedkeeper.PIN_MAX_LENGTH)
                )

            again = self.run_screen(SeedKeeperPinEntryScreen, prompt=_("Confirm PIN"))
            if again == RET_CODE__BACK_BUTTON:
                return Destination(BackStackView)
            if again != pin:
                return self._warn(
                    _("The two PINs did not match.\n\nNothing was changed.")
                )

            ret = self.run_screen(
                DireWarningScreen,
                title=_("Set up new card"),
                status_headline=_("No recovery!"),
                # Kept to four lines because DireWarningScreen's headline leaves
                # only 93px of body: at the 17px font that is four lines, and a
                # fifth renders underneath the button where it cannot be read.
                text=_("{tries} wrong PIN entries locks the card for good.\nThe "
                       "only way back erases it.").format(tries=seedkeeper.PIN_TRIES),
                show_back_button=True,
                button_data=[self.PROCEED],
            )
            if ret == RET_CODE__BACK_BUTTON:
                return Destination(BackStackView)

            loading = LoadingScreenThread(text=_("Setting up..."))
            loading.start()
            try:
                seedkeeper.initialize(connector, pin)
            finally:
                loading.stop()

        except seedkeeper.SeedKeeperError as exc:
            return self._warn(str(exc))
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:                   # noqa: BLE001
            # BaseException on purpose - see the note in UninstallAppletView.
            logger.exception("card setup failed")
            return self._warn(f"{type(exc).__name__}:\n{exc}")
        finally:
            if connector is not None:
                seedkeeper.disconnect(connector)

        # Deliberately does not promise that a seed can now be saved to the card.
        # This fork only reads secrets off a SeedKeeper; putting one on still needs
        # the Satochip tools. Saying otherwise would send someone looking for a
        # menu item that does not exist.
        return _show_success(
            self, _("Set up new card"),
            _("Card set up.\n\nUnlock it with this PIN to load a seed stored on it."),
        )

    def _warn(self, text):
        self.run_screen(
            WarningScreen,
            title=_("Set up new card"),
            status_headline=None,
            text=text,
            show_back_button=False,
        )
        return Destination(MainMenuView)


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

            # A new applet has no PIN yet, so asking for one here would only
            # produce a refusal the user cannot act on. Say what is wrong and name
            # the menu item that fixes it.
            if not seedkeeper.is_initialized(connector):
                return self._warn(
                    _("Card not set up yet.\n\nUse Smartcard > Set up new card "
                      "first.")
                )

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
                    _("No loadable seeds.\n\nOnly secrets marked 'Plaintext "
                      "export allowed' can be read.")
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
            text=_("Flash this applet?\n{name}\nOverwrites it and any secrets on "
                   "it.").format(name=_wrap_capname(name)),
            show_back_button=True,
        )
        if ret == RET_CODE__BACK_BUTTON:
            return Destination(BackStackView)

        pygp_module = None
        install_error = None
        state = seedkeeper.UNCONFIRMED
        loading = LoadingScreenThread(text=_("Installing..."))
        try:
            loading.start()
            try:
                # Go through the helper rather than driving pygp here. This used to
                # do its own terminal/card/auth, which is how it kept failing with
                # "Transaction failed": the first session after an uninstall lands
                # on a card that pygp's close() just cold-reset, and there was no
                # retry. gp_open also proves the session works before returning it.
                pygp_module = seedkeeper.gp_open()

                # Refuse rather than let GlobalPlatform refuse. A package already
                # on the card makes the load fail somewhere in the middle, with a
                # status word that says nothing about what to do next.
                if seedkeeper.gp_package_loaded_for_cap(pygp_module, path):
                    return self._warn(
                        _("That applet is already on this card.\n\nUninstall it "
                          "first, which erases what it holds.")
                    )

                try:
                    if storage:
                        pygp_module.install_capfile(
                            path, application_specific_parameters=storage)
                    else:
                        pygp_module.install_capfile(path)
                except (KeyboardInterrupt, SystemExit):
                    raise
                except BaseException as exc:           # noqa: BLE001
                    # Hold the error rather than reporting it. install_capfile has
                    # been seen to raise after the applet was fully installed, so
                    # the card decides whether this was a failure, not pygp.
                    install_error = exc

                state = seedkeeper.gp_install_state(pygp_module, path)
            finally:
                loading.stop()
        except seedkeeper.SeedKeeperError as exc:
            return self._warn(str(exc))
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:                   # noqa: BLE001
            # pygp raises bare BaseException for card errors, so Exception is not
            # enough - it would leave the failure to blank the screen instead.
            logger.exception("applet install failed")
            return self._warn(
                _("Install failed:\n{e}").format(e=str(exc)[:70])
            )
        finally:
            if pygp_module is not None:
                seedkeeper.gp_close(pygp_module)

        if state == seedkeeper.NOT_INSTALLED:
            logger.error("applet install failed", exc_info=install_error)
            return self._warn(
                _("Install failed:\n{e}").format(
                    e=str(install_error or _("the applet is not on the card"))[:70])
            )

        if state == seedkeeper.UNCONFIRMED:
            logger.error("applet install unconfirmed", exc_info=install_error)
            return self._warn(
                _("Could not confirm the install.\n\nCheck Uninstall applet to see "
                  "what is on the card.")
            )

        if install_error is not None:
            # Installed anyway. Worth a log line, not a warning screen: the applet
            # is on the card and selectable, which is all the user asked for.
            logger.warning("install reported %s but the applet is present: %s",
                           type(install_error).__name__, install_error)

        # Wrapped for the same reason as the confirm screen: the bare filename is
        # one token wider than the screen and would run off the right edge.
        return _show_success(
            self, _("Install applet"),
            _("Installed:\n{name}").format(name=_wrap_capname(name)),
        )

    def _warn(self, text):
        self.run_screen(
            WarningScreen,
            title=_("Smartcard"),
            status_headline=None,
            text=text,
            show_back_button=False,
        )
        return Destination(MainMenuView)


class UninstallAppletView(View):
    """
    Remove an applet package, and everything it holds, from the card.

    The counterpart to InstallAppletView, and the only irreversible thing in this
    fork. A Javacard applet's storage belongs to the applet: delete a SeedKeeper
    and the seed inside it is gone, with no backup anywhere on the device. So the
    flow names what will be destroyed, asks once with a dire warning, and then
    checks the card afterwards rather than assuming it worked.
    """

    def run(self):
        pygp_module = None

        try:
            loading = LoadingScreenThread(text=_("Reading card..."))
            loading.start()
            try:
                pygp_module = seedkeeper.gp_open()
                packages = seedkeeper.gp_list_packages(pygp_module)
                # The whole card, not just our applets. The post-delete check
                # asks whether the card plausibly went empty, and the JavaCard
                # framework's own packages count towards that whether or not we
                # are willing to offer them for deletion.
                total_packages = seedkeeper.gp_count_packages(pygp_module)
            finally:
                loading.stop()

            if not packages:
                return self._warn(
                    _("No removable applets.\n\nThe card's own packages are "
                      "hidden; erasing them would break it.")
                )

            button_data = []
            for _aid, label, instances in packages:
                suffix = _(" ({n} applets)").format(n=len(instances)) if len(instances) > 1 else ""
                button_data.append(ButtonOption(f"{label}{suffix}"))

            selected = self.run_screen(
                ButtonListScreen,
                title=_("Uninstall applet"),
                is_button_text_centered=False,
                button_data=button_data,
            )
            if selected == RET_CODE__BACK_BUTTON:
                return Destination(BackStackView)

            package_aid, label, instances = packages[selected]

            # Spell out the AID as well as the friendly name. The name is decoded
            # from whatever ASCII happens to be in the AID, so on a card with two
            # similar applets it is the hex that tells them apart.
            #
            # Four lines total, and no more: the dire headline leaves 93px of body,
            # which is four lines of the 17px font. The name and the AID take one
            # each, so the warning itself gets two - the instance count goes on the
            # name's line rather than costing a line of its own, and the word
            # "Permanent!" in the headline carries what a longer sentence would.
            detail = label
            if len(instances) > 1:
                detail += _(" - {n} applets").format(n=len(instances))

            ret = self.run_screen(
                DireWarningScreen,
                title=_("Uninstall applet"),
                status_headline=_("Permanent!"),
                text=_("{detail}\n{aid}\nErases the applet and any seed on "
                       "it.").format(detail=detail, aid=package_aid),
                show_back_button=True,
                button_data=[ButtonOption(_("Erase it"))],
            )
            if ret == RET_CODE__BACK_BUTTON:
                return Destination(BackStackView)

            loading = LoadingScreenThread(text=_("Erasing..."))
            loading.start()
            try:
                seedkeeper.gp_delete_package(pygp_module, package_aid)
                # The delete raised nothing, which is not the same as the applet
                # being gone. Ask the card, and let it say "I don't know".
                state = seedkeeper.gp_deletion_state(
                    pygp_module, package_aid, packages_before=total_packages)
            finally:
                loading.stop()

            if state == seedkeeper.NOT_DELETED:
                return self._warn(
                    _("The card reported no error, but {label} is still installed.\n\n"
                      "Nothing was removed.").format(label=label)
                )
            if state == seedkeeper.UNCONFIRMED:
                return self._warn(
                    _("{label} deleted, but not confirmed.\n\nCheck the card "
                      "before relying on it.").format(label=label)
                )

        except seedkeeper.SeedKeeperError as exc:
            return self._warn(str(exc))
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as exc:                   # noqa: BLE001
            # BaseException, not Exception: pygp reports card errors with a bare
            # `raise BaseException(...)`, so catching Exception here would let the
            # most likely failure through. Same reasoning as the import flow -
            # an unhandled traceback is a blank screen on a device with no other
            # way to report anything.
            logger.exception("applet uninstall failed")
            return self._warn(f"{type(exc).__name__}:\n{exc}")
        finally:
            if pygp_module is not None:
                seedkeeper.gp_close(pygp_module)

        return _show_success(
            self, _("Uninstall applet"),
            _("{label} removed.").format(label=label),
        )

    def _warn(self, text):
        self.run_screen(
            WarningScreen,
            title=_("Smartcard"),
            status_headline=None,
            text=text,
            show_back_button=False,
        )
        return Destination(MainMenuView)


def _short_label(label: str) -> str:
    """
    A label cut down to one line, for screens that have other things to say.

    The card takes a label of up to 127 bytes and the keyboard will happily
    supply one. On the confirmation screen the label sits above the line warning
    that the passphrase is being written too - so an over-long label does not
    merely look untidy, it pushes the one sentence that matters off the bottom,
    where TextArea draws it under the button. Truncate here; the card still gets
    the whole thing.
    """
    if len(label) <= _CHARS_PER_LINE:
        return label
    return label[:_CHARS_PER_LINE - 1] + "…"


class ExportSeedToCardView(View):
    """
    Write a loaded seed onto a SeedKeeper card.

    The other direction of SeedKeeperSelectView, and reached from Backup Seed
    rather than from the Smartcard menu: the seed is the subject here, the card
    is only where it goes.

    Stored with 'Plaintext export allowed' deliberately - that is the one export
    right this device can read back, and list_importable() filters on it. A seed
    saved any other way would be a seed this signer could not load again.
    """

    SAVE_ANYWAY = ButtonOption("Save another copy")

    def __init__(self, seed: Seed):
        super().__init__()
        self.seed = seed


    def run(self):
        from seedsigner.gui.screens import seed_screens
        from seedsigner.gui.screens.seedkeeper_screens import SeedKeeperPinEntryScreen
        from seedsigner.models.settings_definition import SettingsConstants

        fingerprint = self.seed.get_fingerprint(
            network=self.settings.get_value(SettingsConstants.SETTING__NETWORK))

        # Prefilled with the fingerprint because that is what identifies a seed
        # unambiguously; a card holding three seeds called "wallet" helps nobody.
        # Editable because the card's own list is the only place this is ever
        # read, and a person choosing from it may want their own word for it.
        ret = self.run_screen(
            seed_screens.SeedAddPassphraseScreen,
            title=_("Card label"),
            passphrase=fingerprint,
        )
        if "is_back_button" in ret:
            return Destination(BackStackView)
        label = (ret.get("passphrase") or "").strip() or fingerprint

        connector = None
        loading = LoadingScreenThread(text=_("Reading card..."))
        try:
            loading.start()
            try:
                connector = seedkeeper.connect()
            finally:
                loading.stop()

            if not seedkeeper.is_initialized(connector):
                return self._warn(
                    _("Card not set up yet.\n\nUse Smartcard > Set up new card "
                      "first.")
                )

            pin = self.run_screen(SeedKeeperPinEntryScreen)
            if pin == RET_CODE__BACK_BUTTON:
                return Destination(BackStackView)
            if not pin:
                return self._warn(_("No PIN entered."))

            loading = LoadingScreenThread(text=_("Unlocking..."))
            loading.start()
            try:
                seedkeeper.verify_pin(connector, pin)
                secret_dic, fingerprint_expected, described = \
                    seedkeeper.build_seed_secret(
                        connector,
                        mnemonic=self.seed.mnemonic_str,
                        passphrase=self.seed.passphrase,
                        label=label,
                        seed_bytes=self.seed.seed_bytes,
                    )
                needed = seedkeeper.secret_bytes_needed(secret_dic)
                free = seedkeeper.card_free_memory(connector)
                duplicate = seedkeeper.find_by_fingerprint(
                    connector, fingerprint_expected)
            finally:
                loading.stop()

            # Ask before writing, not after failing: a full card refuses the
            # secret partway through OP_PROCESS, which is a worse thing to
            # explain than a number compared beforehand.
            if needed > free:
                return self._warn(
                    _("Not enough room on the card.\n\nneeds {needed}, free "
                      "{free} bytes").format(needed=needed, free=free)
                )

            if duplicate is not None:
                ret = self.run_screen(
                    WarningScreen,
                    title=_("Save to card"),
                    status_headline=_("Already saved"),
                    text=_("This seed is on the card as '{label}'.").format(
                        label=_short_label(duplicate)),
                    show_back_button=True,
                    button_data=[self.SAVE_ANYWAY],
                )
                if ret == RET_CODE__BACK_BUTTON:
                    return Destination(BackStackView)

            # The passphrase travels with the seed, because the card's format
            # carries it and export_mnemonic() reads it back. Say so: keeping a
            # passphrase apart from its seed is the whole point of having one,
            # and this is the moment that separation ends.
            warning = _("Stored so this device can read it back.")
            if self.seed.passphrase:
                warning = _("The passphrase is saved with it.")

            ret = self.run_screen(
                DireWarningScreen,
                title=_("Save to card"),
                status_headline=_("Plaintext"),
                text=_("{label}\n{warning}").format(label=_short_label(label), warning=warning),
                show_back_button=True,
                button_data=[ButtonOption(_("Save it"))],
            )
            if ret == RET_CODE__BACK_BUTTON:
                return Destination(BackStackView)

            loading = LoadingScreenThread(text=_("Saving..."))
            loading.start()
            try:
                sid, _fp = seedkeeper.import_secret(
                    connector, secret_dic, fingerprint_expected)
            finally:
                loading.stop()

        except seedkeeper.SeedKeeperError as exc:
            return self._warn(str(exc))
        except Exception as exc:                       # noqa: BLE001
            # Broad on purpose: an escaped exception here is a blank screen on a
            # device with nowhere else to report it.
            logger.exception("SeedKeeper export failed")
            return self._warn(f"{type(exc).__name__}:\n{exc}")
        finally:
            if connector is not None:
                seedkeeper.disconnect(connector)

        return _show_success(
            self, _("Save to card"),
            _("Saved as '{label}'\nsecret {sid}, {described}").format(
                label=_short_label(label), sid=sid, described=described),
        )


    def _warn(self, text):
        self.run_screen(
            WarningScreen,
            title=_("Save to card"),
            status_headline=None,
            text=text,
            show_back_button=False,
        )
        return Destination(MainMenuView)
