"""Helper library available to every generated scene: `from vg import *`."""
import os
from manim import *

config.background_color = os.getenv("VG_BG", "#FBF8F1")
INK = "#2B2B2B"; MUTED = "#666666"
BLUE = "#4C9BE8"; GREEN = "#5DBB63"; ORANGE = "#F2A541"; PURPLE = "#9B59B6"; RED = "#E74C3C"; TEAL = "#1ABC9C"
FONT = os.getenv("VG_FONT", "DejaVu Sans")
MONO = "DejaVu Sans Mono"
SCENE_DURATION = float(os.getenv("VG_DURATION", "10"))


def T(s, size=30, color=INK, **k):
    """Text in the house font. Use this instead of Text/Tex/MathTex."""
    return Text(str(s), font=FONT, font_size=size, color=color, **k)


def fit(m, max_w=13.0, max_h=7.0):
    """Scale a mobject down so it stays on screen."""
    if m.width > max_w:
        m.scale_to_fit_width(max_w)
    if m.height > max_h:
        m.scale_to_fit_height(max_h)
    return m


def box(label, color=BLUE, w=2.4, h=1.0, size=24):
    r = RoundedRectangle(width=w, height=h, corner_radius=0.12, fill_color=color, fill_opacity=0.85, stroke_color=INK, stroke_width=2)
    t = T(label, size, INK)
    if t.width > w - 0.2:
        t.scale_to_fit_width(w - 0.2)
    return VGroup(r, t.move_to(r))


def bullets(items, size=28, color=INK, buff=0.3):
    g = VGroup(*[T("\u2022 " + s, size, color) for s in items]).arrange(DOWN, aligned_edge=LEFT, buff=buff)
    return fit(g)


def code_block(src, size=20, language="python"):
    c = Code(code_string=src, language=language, background="rectangle", formatter_style="friendly",
             paragraph_config={"font": MONO, "font_size": size}, add_line_numbers=True)
    return fit(c, 13.6, 7.4)


class Timed(Scene):
    """Base class: call self.finish() at the end of construct() to hold the last frame until the narration ends."""
    def finish(self):
        left = SCENE_DURATION - self.renderer.time
        if left > 0:
            self.wait(left)
