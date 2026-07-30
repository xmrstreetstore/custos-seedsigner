from dataclasses import dataclass

from seedsigner.gui.keyboard import Keyboard, TextEntryDisplay
from seedsigner.gui.screens.screen import KeyboardScreen


#: How each entered digit is drawn instead of itself.
PIN_MASK_CHAR = "*"


class MaskedTextEntryDisplay(TextEntryDisplay):
    """
    A TextEntryDisplay that draws a mask character per character it is given.

    The PIN is the one thing on this device that unlocks the card's secrets, and
    a hardware signer gets used in rooms with other people and cameras in them.
    Masking keeps a shoulder-glance or a photo from being enough.

    Only the drawing is masked. The screen keeps the real digits in its own
    user_input, so the cursor, backspace and the returned value all still work on
    the actual PIN.
    """

    def render(self, cur_text=None, cursor_position=None):
        if cur_text is not None:
            self.cur_text = cur_text
        return super().render(PIN_MASK_CHAR * len(self.cur_text), cursor_position)


@dataclass
class SeedKeeperPinEntryScreen(KeyboardScreen):
    """
    Numeric keypad for the SeedKeeper card PIN.

    A SeedKeeper PIN is 4-16 digits, so there is no fixed length to return after -
    the user presses the save button when done. Digits only: the card's PIN is
    numeric and offering letters would just invite a wrong entry, and wrong entries
    on this card are expensive (the PIN counter is finite and running out bricks
    access to the secrets).
    """

    #: What to put in the title bar. A field rather than a hardcoded string so the
    #: same keypad can ask for a PIN and then ask for it again to confirm, without
    #: a second screen class that would only differ by five characters.
    prompt: str = "SeedKeeper PIN"

    def __post_init__(self):
        self.title = self.prompt

        # Every KeyboardScreen has to name its own additional keys. The field
        # defaults to Keyboard.ADDITIONAL_KEYS, which is a dict keyed by code,
        # so leaving it alone makes Keyboard iterate the *codes* and index a
        # string with "size" - a TypeError before anything reaches the card.
        # The size has to make the grid add up exactly: 10 digits in 4x3 leaves
        # 2 cells, so the 2-slot backspace is the one that fits. KEY_BACKSPACE
        # is 3 slots and would fail the layout check instead.
        self.custom_additional_keys = [Keyboard.KEY_BACKSPACE_2]

        self.rows = 4
        self.cols = 3
        self.keys_charset = "1234567890"
        self.show_save_button = True
        super().__post_init__()

        # Swap the display stock just built for a masking one. Promoting the
        # instance keeps stock's layout maths - the rect is worked out from
        # local variables inside KeyboardScreen.__post_init__ and is not
        # reachable from here - and leaves screen.py untouched, so this survives
        # a rebase onto upstream. MaskedTextEntryDisplay adds no fields, so the
        # instance is already the right shape.
        self.text_entry_display.__class__ = MaskedTextEntryDisplay
