from dataclasses import dataclass

from seedsigner.gui.screens.screen import KeyboardScreen


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

    def __post_init__(self):
        self.title = "SeedKeeper PIN"
        self.rows = 4
        self.cols = 3
        self.keys_charset = "123456789 0 "
        self.show_save_button = True
        super().__post_init__()
