"""
Screens for scanning dice with the camera: the live preview to aim with, and
the 10-wide grid of what was read, where any die can be corrected.
"""
import time
from dataclasses import dataclass, field
from gettext import gettext as _

from seedsigner.gui.components import Fonts, GUIConstants, TopNav
from seedsigner.gui.screens.screen import RET_CODE__BACK_BUTTON, BaseScreen, BaseTopNavScreen
from seedsigner.hardware.buttons import HardwareButtonsConstants


GRID_COLS = 10
GRID_MAX_CELLS = 100

# What DiceGridScreen returns, besides RET_CODE__BACK_BUTTON.
USE, RESCAN, SCAN_MORE = "use", "rescan", "more"


@dataclass
class DiceScanPreviewScreen(BaseScreen):
    """
    Live camera view to line the dice up in. Returns True when a button is
    clicked (take the photo), or RET_CODE__BACK_BUTTON.
    """
    hint: str = "Dice in rows of 10"

    def __post_init__(self):
        super().__post_init__()
        from seedsigner.hardware.camera import Camera
        self.camera = Camera.get_instance()
        # Square, as the image-entropy preview does: a 320x240 stream comes back
        # pillarboxed, a square one edge to edge.
        size = max(self.canvas_width, self.canvas_height)
        self.camera.start_video_stream_mode(resolution=(size, size), framerate=24, format="rgb")

    def _caption(self, text, y, color=GUIConstants.BODY_FONT_COLOR):
        font = Fonts.get_font(GUIConstants.get_body_font_name(), GUIConstants.get_button_font_size())
        self.renderer.draw.text(
            xy=(int(self.canvas_width / 2), y), text=text, fill=color, font=font,
            stroke_width=4, stroke_fill=GUIConstants.BACKGROUND_COLOR, anchor="ms",
        )

    def _run(self):
        hint = _(self.hint)
        controls = "< " + _("back") + "  |  " + _("click to scan")
        while True:
            if self.hw_inputs.check_for_low(HardwareButtonsConstants.KEY_LEFT):
                self.hw_inputs.update_last_input_time()
                self.camera.stop_video_stream_mode()
                return RET_CODE__BACK_BUTTON

            if self.hw_inputs.check_for_low(keys=HardwareButtonsConstants.KEYS__ANYCLICK):
                self.hw_inputs.update_last_input_time()
                self.camera.stop_video_stream_mode()
                with self.renderer.lock:
                    self._caption(_("Taking photo..."),
                                  self.canvas_height - GUIConstants.EDGE_PADDING,
                                  GUIConstants.ACCENT_COLOR)
                    self.renderer.show_image()
                return True

            frame = self.camera.read_video_stream(as_image=True)
            if frame is None:
                time.sleep(0.01)
                continue

            with self.renderer.lock:
                # Crop the square frame to the screen, keeping its centre.
                dx = max(0, (frame.width - self.canvas_width) // 2)
                dy = max(0, (frame.height - self.canvas_height) // 2)
                self.renderer.canvas.paste(
                    frame.crop((dx, dy, dx + self.canvas_width, dy + self.canvas_height)))
                font_size = GUIConstants.get_button_font_size()
                self._caption(hint, GUIConstants.EDGE_PADDING + font_size)
                self._caption(controls, self.canvas_height - GUIConstants.EDGE_PADDING)
                self.renderer.show_image()


class DiceGrid:
    """
    The state behind DiceGridScreen, kept apart from drawing so it can be tested.

    values: what has been read, in order (1-6, or None for unreadable).
    needed: rolls the seed takes (50 or 99).
    Cells run row by row, 10 to a row. The cursor is a cell index, or one of
    the two buttons below the grid, or the back arrow above it.
    """
    LEFT_BUTTON, RIGHT_BUTTON, BACK = "left", "right", "back"

    def __init__(self, values, needed):
        self.values = list(values)
        self.needed = needed
        self.edited = set()
        self.cells = min(GRID_MAX_CELLS, max(len(self.values), needed))
        # Start on the first die that needs fixing; otherwise on "Use dice" or
        # "Scan more", whichever the right-hand button is.
        unknown = self.first_unknown()
        self.cursor = unknown if unknown is not None else self.RIGHT_BUTTON

    # --- what the grid says ---------------------------------------------------

    def first_unknown(self):
        for i, v in enumerate(self.values[:self.needed]):
            if v is None:
                return i
        return None

    @property
    def known(self) -> int:
        return sum(v is not None for v in self.values[:self.needed])

    @property
    def complete(self) -> bool:
        return len(self.values) >= self.needed and self.first_unknown() is None

    @property
    def right_action(self):
        return USE if len(self.values) >= self.needed else SCAN_MORE

    @property
    def rolls(self) -> str:
        """The rolls as the seed takes them: the same string manual entry builds."""
        return "".join(str(v) for v in self.values[:self.needed])

    def cell_state(self, i):
        """'value', 'unknown', 'edited', 'extra' (beyond needed), or 'empty'."""
        if i >= len(self.values):
            return "empty"
        if i >= self.needed:
            return "extra"
        if self.values[i] is None:
            return "unknown"
        return "edited" if i in self.edited else "value"

    # --- input -----------------------------------------------------------------

    def move(self, key):
        c, rows = self.cursor, (self.cells + GRID_COLS - 1) // GRID_COLS
        K = HardwareButtonsConstants
        if c == self.BACK:
            if key == K.KEY_DOWN:
                self.cursor = 0
        elif c in (self.LEFT_BUTTON, self.RIGHT_BUTTON):
            if key == K.KEY_UP:
                col = 0 if c == self.LEFT_BUTTON else GRID_COLS - 1
                self.cursor = min(self.cells - 1, (rows - 1) * GRID_COLS + col)
            elif key == K.KEY_LEFT:
                self.cursor = self.LEFT_BUTTON
            elif key == K.KEY_RIGHT:
                self.cursor = self.RIGHT_BUTTON
        else:
            row, col = divmod(c, GRID_COLS)
            if key == K.KEY_UP:
                self.cursor = self.BACK if row == 0 else c - GRID_COLS
            elif key == K.KEY_DOWN:
                if c + GRID_COLS < self.cells:
                    self.cursor = c + GRID_COLS
                else:
                    self.cursor = self.LEFT_BUTTON if col < GRID_COLS // 2 else self.RIGHT_BUTTON
            elif key == K.KEY_LEFT and col > 0:
                self.cursor = c - 1
            elif key == K.KEY_RIGHT and col < GRID_COLS - 1 and c + 1 < self.cells:
                self.cursor = c + 1

    def press(self):
        """Click on the cursor. Returns an action to leave with, or None."""
        c = self.cursor
        if c == self.BACK:
            return RET_CODE__BACK_BUTTON
        if c == self.LEFT_BUTTON:
            return RESCAN
        if c == self.RIGHT_BUTTON:
            if self.right_action == SCAN_MORE:
                return SCAN_MORE
            return USE if self.complete else None
        if c < len(self.values) and c < self.needed:
            v = self.values[c]
            self.values[c] = 1 if v is None else v % 6 + 1
            self.edited.add(c)
        return None


@dataclass
class DiceGridScreen(BaseTopNavScreen):
    """
    The dice that were read, 10 to a row, in the order they become the seed.

    Unreadable dice show as "?". Move to any die and click to step it 1-6;
    corrected dice are drawn in the accent colour. "Use dice" works once every
    die the seed needs is known.

    Returns (action, values): action is USE, RESCAN or SCAN_MORE; or
    RET_CODE__BACK_BUTTON.
    """
    values: list = field(default_factory=list)
    needed: int = 99

    def __post_init__(self):
        self.grid = DiceGrid(self.values, self.needed)
        self.title = self._title()
        super().__post_init__()

    def _title(self):
        return _("Dice {}/{}").format(self.grid.known, self.needed)

    def _draw(self):
        g = self.grid
        title = self._title()
        if title != self.top_nav.text:
            # TopNav lays its title out once, so a changed count needs a new one.
            self.top_nav = TopNav(text=title, font_size=self.title_font_size,
                                  width=self.canvas_width, height=GUIConstants.TOP_NAV_HEIGHT,
                                  show_back_button=True)
        self.top_nav.is_selected = g.cursor == g.BACK
        self.clear_screen()
        self.top_nav.render()

        pad = GUIConstants.EDGE_PADDING
        bottom = self.canvas_height - pad - GUIConstants.BUTTON_HEIGHT
        top = self.top_nav.height
        rows = (g.cells + GRID_COLS - 1) // GRID_COLS
        cell_w = (self.canvas_width - 2 * pad) // GRID_COLS
        cell_h = min(cell_w, (bottom - top - pad) // max(1, rows))
        x0 = (self.canvas_width - cell_w * GRID_COLS) // 2
        y0 = top + max(0, (bottom - top - pad - cell_h * rows) // 2)
        # As large as fits: a digit about 70% of the row, so rows stay apart.
        size = int(cell_h * 1.4)
        while True:
            font = Fonts.get_font(GUIConstants.FIXED_WIDTH_EMPHASIS_FONT_NAME, size)
            top_, bottom_ = font.getbbox("8")[1::2]
            if bottom_ - top_ <= 0.7 * cell_h or size <= 10:
                break
            size -= 1

        colors = {
            "value": GUIConstants.BODY_FONT_COLOR,
            "edited": GUIConstants.ACCENT_COLOR,
            "unknown": GUIConstants.ERROR_COLOR,
            "extra": GUIConstants.INACTIVE_COLOR,
            "empty": GUIConstants.INACTIVE_COLOR,
        }
        for i in range(g.cells):
            r, c = divmod(i, GRID_COLS)
            x, y = x0 + c * cell_w, y0 + r * cell_h
            state = g.cell_state(i)
            text = {"unknown": "?", "empty": "·"}.get(state) or str(g.values[i])
            color = colors[state]
            box = (x + 1, y + 1, x + cell_w - 2, y + cell_h - 2)
            if g.cursor == i:
                self.image_draw.rounded_rectangle(box, radius=3, fill=GUIConstants.ACCENT_COLOR)
                color = GUIConstants.BUTTON_SELECTED_FONT_COLOR
            elif state == "unknown":
                # A lone "?" is easy to miss among 99 digits; box it.
                self.image_draw.rounded_rectangle(box, radius=3, outline=GUIConstants.ERROR_COLOR)
            self.image_draw.text((x + cell_w // 2, y + cell_h // 2), text,
                                 fill=color, font=font, anchor="mm")

        left_label = _("Rescan")
        right_label = _("Use dice") if g.right_action == USE else _("Scan more")
        right_ok = g.right_action == SCAN_MORE or g.complete
        bw = (self.canvas_width - 3 * pad) // 2
        for i, (label, key, ok) in enumerate(((left_label, g.LEFT_BUTTON, True),
                                              (right_label, g.RIGHT_BUTTON, right_ok))):
            bx = pad + i * (bw + pad)
            selected = g.cursor == key
            fill = GUIConstants.ACCENT_COLOR if selected else GUIConstants.BUTTON_BACKGROUND_COLOR
            if selected:
                fg = GUIConstants.BUTTON_SELECTED_FONT_COLOR
            else:
                fg = GUIConstants.BUTTON_FONT_COLOR if ok else GUIConstants.INACTIVE_COLOR
            self.image_draw.rounded_rectangle(
                (bx, bottom, bx + bw, bottom + GUIConstants.BUTTON_HEIGHT), radius=8, fill=fill)
            bfont = Fonts.get_font(GUIConstants.get_button_font_name(), GUIConstants.get_button_font_size())
            self.image_draw.text((bx + bw // 2, bottom + GUIConstants.BUTTON_HEIGHT // 2), label,
                                 fill=fg, font=bfont, anchor="mm")

    def _render(self):
        self._draw()

    def _run(self):
        K = HardwareButtonsConstants
        while True:
            key = self.hw_inputs.wait_for(K.ALL_KEYS)
            if key in K.KEYS__LEFT_RIGHT_UP_DOWN:
                self.grid.move(key)
            elif key in K.KEYS__ANYCLICK:
                action = self.grid.press()
                if action == RET_CODE__BACK_BUTTON:
                    return RET_CODE__BACK_BUTTON
                if action is not None:
                    return action, list(self.grid.values)
            with self.renderer.lock:
                self._draw()
                self.renderer.show_image()
