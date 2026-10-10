import time
from dataclasses import dataclass
from gettext import gettext as _

from seedsigner.gui.components import CustosIconConstants, GUIConstants, Icon, TextArea
from seedsigner.gui.keyboard import TextEntryDisplay
from seedsigner.gui.screens.screen import RET_CODE__BACK_BUTTON, BaseTopNavScreen
from seedsigner.hardware.buttons import HardwareButtonsConstants
from seedsigner.gui.screens.seed_screens import SeedAddPassphraseScreen


#: How each entered character is drawn instead of itself.
PIN_MASK_CHAR = "*"


class MaskedTextEntryDisplay(TextEntryDisplay):
    """
    A TextEntryDisplay that draws a mask character per character it is given.

    The PIN is the one thing on this device that unlocks the card's secrets, and
    a hardware signer gets used in rooms with other people and cameras in them.
    Masking keeps a shoulder-glance or a photo from being enough.

    Only the drawing is masked. The screen keeps the real characters in its own
    state, so the cursor, backspace and the returned value all still work on
    the actual PIN.
    """

    def render(self, cur_text=None, cursor_position=None):
        if cur_text is not None:
            self.cur_text = cur_text
        return super().render(PIN_MASK_CHAR * len(self.cur_text), cursor_position)


@dataclass
class SeedKeeperPinEntryScreen(SeedAddPassphraseScreen):
    """
    The SeedKeeper card PIN, typed on the passphrase keyboard.

    A SeedKeeper PIN is 4-16 bytes of anything - Satochip's own tools accept
    letters and symbols, and pysatochip sends the string's UTF-8 bytes - so a
    digits-only keypad locked out every card whose PIN was set elsewhere with a
    letter in it. The passphrase keyboard (abc / ABC / 123 / two symbol pages)
    is the same one used for the BIP-39 passphrase, so there is nothing new to
    learn, and everything it can type is ASCII: one character, one byte.

    Returns the PIN as a str, or RET_CODE__BACK_BUTTON.
    """

    #: What to put in the title bar, so the same keyboard can ask for a PIN and
    #: then ask for it again to confirm.
    prompt: str = "SeedKeeper PIN"

    def __post_init__(self):
        self.title = self.prompt
        super().__post_init__()

        # Swap the display the passphrase screen just built for a masking one.
        # Promoting the instance keeps stock's layout maths and leaves
        # seed_screens.py untouched. MaskedTextEntryDisplay adds no fields, so
        # the instance is already the right shape.
        self.text_entry_display.__class__ = MaskedTextEntryDisplay

    def _run(self):
        # The passphrase screen answers with a dict, because its caller offers
        # to keep a half-typed passphrase on Back. A half-typed PIN is worth
        # nothing, so translate to the plain answers the card views expect.
        ret = super()._run()
        if ret.get("is_back_button"):
            return RET_CODE__BACK_BUTTON
        return ret["passphrase"]


@dataclass
class CardWaitScreen(BaseTopNavScreen):
    """
    "Insert a card or hold it to the reader", until one of them happens.

    The device cannot know in advance which reader the card will come through,
    so it asks for either and watches both. A card in the slot is acknowledged
    and the flow carries on. A card held to the NFC reader gets a connection
    quality bar - the share of recent probes it answered (see CardWatch) - and
    the flow carries on once that is steady, so a card at the edge of the field
    is visibly weak rather than failing half-way through an operation.

    Returns CARD_IN_SLOT, CARD_ON_NFC or RET_CODE__BACK_BUTTON.
    """

    title: str = "Smartcard"
    watch: object = None                 # helpers.seedkeeper.CardWatch
    slot_enabled: bool = True
    nfc_enabled: bool = True

    #: How long the "detected" confirmation stays up before the flow continues.
    CONFIRM_SECS = 0.6
    POLL_SECS = 0.1

    def __post_init__(self):
        super().__post_init__()
        # The back arrow is the only thing to select, so start on it: one press
        # of the joystick (or LEFT) backs out.
        self.top_nav.is_selected = True

    def _prompt(self) -> str:
        if self.slot_enabled and self.nfc_enabled:
            return _("Insert a card\nor hold it to the reader")
        if self.nfc_enabled:
            return _("Hold a card to the reader")
        return _("Insert a card")

    def _draw(self, state, quality=None):
        """state: None (waiting), CARD_IN_SLOT, or CARD_ON_NFC with a quality."""
        from seedsigner.helpers.seedkeeper import CARD_IN_SLOT, CardWatch

        self.clear_screen()
        self.top_nav.render()

        if state is None:
            color, headline = GUIConstants.BODY_FONT_COLOR, self._prompt()
        elif state == CARD_IN_SLOT:
            color, headline = GUIConstants.SUCCESS_COLOR, _("Card inserted")
        else:
            color, headline = GUIConstants.ACCENT_COLOR, _("NFC card detected")

        icon = Icon(icon_name=CustosIconConstants.SMARTCARD, icon_size=80, icon_color=color)
        icon.screen_x = int((self.canvas_width - icon.width) / 2)
        icon.screen_y = self.top_nav.height + GUIConstants.COMPONENT_PADDING
        icon.render()

        text = TextArea(
            text=headline,
            width=self.canvas_width,
            screen_y=icon.screen_y + icon.height + GUIConstants.COMPONENT_PADDING,
            font_color=color,
        )
        text.render()

        if state is None or state == CARD_IN_SLOT or quality is None:
            return

        # Connection quality: a bar and its number, coloured like the rest of
        # the UI's good / caution / bad.
        if quality >= CardWatch.GOOD:
            bar_color = GUIConstants.SUCCESS_COLOR
        elif quality >= 40:
            bar_color = GUIConstants.WARNING_COLOR
        else:
            bar_color = GUIConstants.ERROR_COLOR

        pad = 2 * GUIConstants.EDGE_PADDING
        y = text.screen_y + text.height + GUIConstants.COMPONENT_PADDING
        label = TextArea(
            text=_("Connection {q}%").format(q=quality),
            width=self.canvas_width,
            screen_y=y,
            font_color=GUIConstants.LABEL_FONT_COLOR,
        )
        label.render()
        y = label.screen_y + label.height + int(GUIConstants.COMPONENT_PADDING / 2)
        x0, x1, h = pad, self.canvas_width - pad, 12
        self.image_draw.rounded_rectangle((x0, y, x1, y + h), radius=h // 2,
                                          outline=GUIConstants.INACTIVE_COLOR, width=2)
        fill = x0 + int((x1 - x0) * quality / 100)
        if fill > x0 + h:
            self.image_draw.rounded_rectangle((x0, y, fill, y + h), radius=h // 2,
                                              fill=bar_color)

        if quality < CardWatch.GOOD:
            TextArea(
                text=_("Hold the card flat and still"),
                width=self.canvas_width,
                screen_y=y + h + GUIConstants.COMPONENT_PADDING,
                font_color=GUIConstants.LABEL_FONT_COLOR,
            ).render()

    def _render(self):
        # Initial frame, for display() and the screenshot generator.
        self._draw(None)

    def _show(self, state, quality=None):
        with self.renderer.lock:
            self._draw(state, quality)
            self.renderer.show_image()

    def _run(self):
        from seedsigner.helpers.seedkeeper import CARD_IN_SLOT, CARD_ON_NFC

        # Whatever click brought us here may still be held; ignore input until
        # every key has been let go, or it would back straight out again.
        armed = not self.hw_inputs.has_any_input()
        shown = (None, None)

        while True:
            if not armed:
                armed = not self.hw_inputs.has_any_input()
            elif self.hw_inputs.check_for_low(keys=[
                    HardwareButtonsConstants.KEY_LEFT,
                    HardwareButtonsConstants.KEY_PRESS]):
                return RET_CODE__BACK_BUTTON

            kind = self.watch.present() if self.watch else None

            if kind == CARD_IN_SLOT:
                self._show(CARD_IN_SLOT)
                time.sleep(self.CONFIRM_SECS)
                return CARD_IN_SLOT

            if kind == CARD_ON_NFC:
                self.watch.probe()
                current = (CARD_ON_NFC, self.watch.quality)
                if current != shown:
                    self._show(*current)
                    shown = current
                if self.watch.is_good:
                    time.sleep(self.CONFIRM_SECS)
                    return CARD_ON_NFC
            else:
                if self.watch and self.watch.results:
                    self.watch.reset()         # card taken away: start over
                if shown != (None, None):
                    self._show(None)
                    shown = (None, None)

            time.sleep(self.POLL_SECS)
