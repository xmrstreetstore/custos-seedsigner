"""
New seed from dice: type the rolls, or photograph the dice and check them.

A scan ends where manual entry ends: the rolls become the same string of
digits the keypad would have built, in the same order, and go through the same
generate_mnemonic_from_dice() call. Same dice, same seed.
"""
import logging
import time

from gettext import gettext as _

from seedsigner.gui.components import FontAwesomeIconConstants
from seedsigner.gui.screens import RET_CODE__BACK_BUTTON, ButtonListScreen
from seedsigner.gui.screens.screen import ButtonOption, LoadingScreenThread, WarningScreen
from seedsigner.helpers import mnemonic_generation
from seedsigner.models.seed import Seed
from seedsigner.models.settings_definition import SettingsConstants
from seedsigner.views.seed_views import SeedWordsWarningView

from .view import View, Destination, BackStackView

logger = logging.getLogger(__name__)

#: 4:3 at the camera's 2x2-binned size: the whole field of view, square pixels.
PHOTO_RESOLUTION = (1296, 972)


class _Scan:
    """
    The dice read so far, between views.

    Kept here rather than in Destination view_args, which get logged: these
    digits are the seed. Cleared when the flow starts and when it ends.
    """
    values: list = []
    kept: int = 0           # how many of values came from earlier scans

    @classmethod
    def clear(cls):
        cls.values, cls.kept = [], 0


class ToolsDiceEntropyMethodView(View):
    MANUAL = ButtonOption("Enter manually", FontAwesomeIconConstants.KEYBOARD)
    SCAN = ButtonOption("Scan dice", FontAwesomeIconConstants.CAMERA)

    def run(self):
        from .tools_views import ToolsDiceEntropyMnemonicLengthView

        button_data = [self.MANUAL, self.SCAN]
        selected = self.run_screen(
            ButtonListScreen,
            title=_("Dice rolls"),
            is_button_text_centered=False,
            button_data=button_data,
        )
        if selected == RET_CODE__BACK_BUTTON:
            return Destination(BackStackView)
        if button_data[selected] == self.MANUAL:
            return Destination(ToolsDiceEntropyMnemonicLengthView)
        return Destination(DiceScanLengthView)


class DiceScanLengthView(View):
    def run(self):
        # Same labels as the manual flow's length screen, so they share
        # translations.
        twelve = _("12 words ({} rolls)").format(mnemonic_generation.DICE__NUM_ROLLS__12WORD)
        twenty_four = _("24 words ({} rolls)").format(mnemonic_generation.DICE__NUM_ROLLS__24WORD)
        button_data = [
            ButtonOption(twelve, return_data=mnemonic_generation.DICE__NUM_ROLLS__12WORD),
            ButtonOption(twenty_four, return_data=mnemonic_generation.DICE__NUM_ROLLS__24WORD),
        ]
        selected = self.run_screen(
            ButtonListScreen,
            title=_("Mnemonic Length"),
            is_bottom_list=True,
            is_button_text_centered=True,
            button_data=button_data,
        )
        if selected == RET_CODE__BACK_BUTTON:
            return Destination(BackStackView)
        _Scan.clear()
        return Destination(DiceScanCameraView,
                           view_args=dict(total_rolls=button_data[selected].return_data))


def take_photo():
    """One full-frame still, rotated the way the preview shows it."""
    from seedsigner.hardware.camera import Camera
    camera = Camera.get_instance()
    camera.start_single_frame_mode(resolution=PHOTO_RESOLUTION)
    try:
        time.sleep(0.5)                 # let exposure settle, as image entropy does
        return camera.capture_frame(expand=True)
    finally:
        camera.stop_single_frame_mode()


class DiceScanCameraView(View):
    def __init__(self, total_rolls: int):
        super().__init__()
        self.total_rolls = total_rolls

    def run(self):
        from seedsigner.gui.screens.dice_scan_screens import DiceScanPreviewScreen
        from seedsigner.hardware.camera import CameraConnectionError
        from seedsigner.helpers.dice_reader import read_dice

        if self.run_screen(DiceScanPreviewScreen) == RET_CODE__BACK_BUTTON:
            if _Scan.values:
                # Back out of "scan more" or "rescan": return to the dice
                # already read rather than throw them away.
                return Destination(DiceScanResultView,
                                   view_args=dict(total_rolls=self.total_rolls),
                                   skip_current_view=True)
            return Destination(BackStackView)

        reading, error = None, None
        loading = LoadingScreenThread(text=_("Reading dice..."))
        loading.start()
        try:
            reading = read_dice(take_photo())
        except CameraConnectionError:
            error = _("Camera not found.\n\nCheck its cable.")
        except Exception as exc:                       # noqa: BLE001
            # A traceback would leave a blank screen; say what happened.
            logger.exception("dice scan failed")
            error = f"{type(exc).__name__}:\n{exc}"
        finally:
            loading.stop()

        if error:
            return self._retry(error)
        if not reading.values:
            return self._retry(
                _("No dice found. Lay them in rows that fill the picture.")
                + f"\n\n({reading.detail})")

        _Scan.kept = len(_Scan.values)
        _Scan.values = _Scan.values + reading.values
        return Destination(DiceScanResultView, view_args=dict(total_rolls=self.total_rolls),
                           skip_current_view=True)

    def _retry(self, text):
        self.run_screen(WarningScreen, title=_("Scan dice"), status_headline=None,
                        text=text, show_back_button=False, button_data=[ButtonOption("Try again")])
        return Destination(DiceScanCameraView, view_args=dict(total_rolls=self.total_rolls),
                           skip_current_view=True)


class DiceScanResultView(View):
    def __init__(self, total_rolls: int):
        super().__init__()
        self.total_rolls = total_rolls

    def run(self):
        from seedsigner.gui.screens.dice_scan_screens import (
            DiceGridScreen, RESCAN, SCAN_MORE, USE)

        ret = self.run_screen(DiceGridScreen, values=list(_Scan.values), needed=self.total_rolls)
        if ret == RET_CODE__BACK_BUTTON:
            _Scan.clear()
            return Destination(BackStackView)

        action, values = ret
        if action == RESCAN:
            # Drop this scan, keep earlier ones (with any corrections).
            _Scan.values = values[:_Scan.kept]
            return self._camera()
        if action == SCAN_MORE:
            _Scan.values, _Scan.kept = values, len(values)
            return self._camera()

        assert action == USE
        rolls = dice_rolls(values, self.total_rolls)
        _Scan.clear()
        return finish_dice_seed(self, rolls)

    def _camera(self):
        return Destination(DiceScanCameraView, view_args=dict(total_rolls=self.total_rolls),
                           skip_current_view=True)


def dice_rolls(values, total_rolls: int) -> str:
    """
    The rolls as manual entry would have typed them: one digit 1-6 per die, in
    reading order, exactly total_rolls long. Raises rather than guess.
    """
    rolls = "".join(str(v) for v in values[:total_rolls] if v is not None)
    if len(rolls) != total_rolls or set(rolls) - set("123456"):
        raise ValueError("incomplete dice rolls")
    return rolls


def finish_dice_seed(view: View, rolls: str):
    """
    What ToolsDiceEntropyEntryView does with the keypad's string, line for line,
    so a scanned and a typed set of the same rolls give the same seed.
    """
    dice_seed_phrase = mnemonic_generation.generate_mnemonic_from_dice(rolls)
    seed = Seed(dice_seed_phrase, wordlist_language_code=view.settings.get_value(
        SettingsConstants.SETTING__WORDLIST_LANGUAGE))
    view.controller.storage.set_pending_seed(seed)
    return Destination(SeedWordsWarningView, view_args={"seed": None}, clear_history=True)
