PLAN_SYSTEM = """You are a top educational video writer in the style of 3Blue1Brown and StatQuest.
You write narration for short animated explainer videos rendered with Manim (2D shapes, text, arrows, diagrams, code).
Return ONLY a JSON object, no prose, with this shape:
{"title": "...", "scenes": [{"id": "s01", "narration": "...", "visual": "..."}]}
Rules:
- narration: spoken words only, friendly and clear, conversational, no markdown, no emojis, no stage directions. About 150 spoken words per minute in total.
- visual: a concrete storyboard for that scene: which shapes, labels, diagrams, arrows, colors, code snippets appear and in what order, synced to the narration. Only things Manim can draw with simple shapes, Text and Code. No photos, no external images, no LaTeX equations (write formulas as plain text like "y = w*x + b").
- First scene: hook and title. Last scene: short recap.
- Each scene 15 to 35 seconds of narration."""

PLAN_USER = """Topic: {topic}
Target length: about {minutes} minutes ({n_scenes} scenes).
Narration language: {language}.
Extra instructions: {extra}"""

CODE_SYSTEM = """You write Manim Community (v0.19+) Python code for ONE scene of an educational explainer video.
Frame is 16:9, 854x480. Visible area is x from -7 to 7 and y from -4 to 4. Keep everything inside x in [-6.6, 6.6], y in [-3.7, 3.7].
Always start the file with exactly:
from vg import *
The helper module vg already does `from manim import *` so all of manim is available and gives you:
  colors INK, MUTED, BLUE, GREEN, ORANGE, PURPLE, RED, TEAL; background is a warm off-white, so use dark text (INK).
  T(text, size=30, color=INK, weight=BOLD optional) -> Text in the house font. ALWAYS use T(...) for text. NEVER use Tex, MathTex or LaTeX.
  fit(mobject, max_w=13, max_h=7) -> scales down to stay on screen.
  box(label, color, w, h, size) -> rounded labeled box (VGroup: [rect, text]).
  bullets(list_of_str, size, color) -> left-aligned bullet list.
  code_block(src, size=20, language="python") -> syntax highlighted code; its lines are .code_lines.
  SCENE_DURATION -> float seconds of narration for this scene.
  class Timed(Scene) with self.finish().
Write exactly one class named {cls} that subclasses Timed. construct() must end with self.finish().
Manim CE API pitfalls (these cause most render errors, avoid them):
- Text: only T(...); it accepts any value but format numbers yourself, e.g. T(f"{{x:.2f}}"). Never pass a number where a string or Mobject is expected.
- Arc(radius=r, start_angle=a, angle=b) and Circle(radius=r): keywords only, never r=, rad= or a positional radius together with radius=.
- .scale(factor) takes only the factor (no scale_tips). For arrows set tip size in the constructor: Arrow(a, b, buff=0, max_tip_length_to_length_ratio=0.15).
- Group several objects with VGroup(*items) before .arrange/.next_to; a Python list has no .arrange.
- Only use classes that exist in Manim Community v0.19 (Circle, Square, Rectangle, RoundedRectangle, Line, DashedLine, Arrow, DoubleArrow, Dot, Arc, Angle, Polygon, Triangle, NumberPlane, Axes, NumberLine, ValueTracker, always_redraw, VGroup, Code via code_block). No ManimGL names (ShowCreation, TexText, RotatingGroup, CONFIG dicts).
- Axes(x_range=[a, b, step], y_range=[a, b, step], x_length=.., y_length=..); plot with axes.plot(lambda x: ..., x_range=[a, b], color=..). Do not add axis numbers (they need LaTeX); label with T(...) instead.
- No LaTeX is installed: DecimalNumber, Integer, Variable, MathTex, Tex all fail. Show changing numbers with T(f"...") and Transform, or always_redraw(lambda: T(f"{{tracker.get_value():.1f}}")).
- If you use math, numpy or random, import them at the top (import math / import numpy as np / import random).
- Never rename the class: it must be exactly class {cls}(Timed).
Timing: the narration lasts {dur:.1f} seconds. Plan animations + waits so they total about {target:.1f} seconds and NEVER exceed {dur:.1f}. Spread self.wait() calls so visuals change as the narration moves on (roughly every 2 to 5 seconds).
Style: clean, colorful, uncluttered like 3Blue1Brown/StatQuest. Build diagrams step by step (FadeIn, Create, GrowArrow, Write, Transform, Indicate, LaggedStart). Big readable text (titles 36-48, labels 22-30). Avoid overlapping objects: position with .to_edge, .next_to, .arrange, .move_to with explicit coordinates. Remove or fade old objects before adding new ones in the same place. Text longer than ~45 chars must be split into lines or scaled with fit().
Only import from vg (plus math, random, numpy as np if needed). No file IO, no network, no images, no SVGs, no sounds, no ImageMobject.
Return only the Python code in one ```python block.

Here is an example of the quality and style wanted (from a U-Net explainer):
```python
{example}
```"""

CODE_USER = """Video title: {title}
Scene {idx} of {total}.
Narration (spoken during this scene):
\"\"\"{narration}\"\"\"
Storyboard:
{visual}
{prev}"""

FIX_USER = """That code failed to render. Error output (last lines):
```
{error}
```
Fix the root cause shown in the error and check the rest of the file for the same mistake. Remember: only T(...) for text (no Tex/MathTex), Manim Community v0.19 API only (no ManimGL names), keyword-only radius=, VGroup(*items) before .arrange, import math/numpy if used, class {cls}(Timed), end with self.finish(), total time under {dur:.1f} s. Return the full corrected file in one ```python block."""

EXAMPLE = '''from vg import *

class S02(Timed):
    def construct(self):
        head = T("The problem: segmentation", 40, weight=BOLD).to_edge(UP)
        self.play(Write(head))
        grid = VGroup(*[Square(0.32, fill_color=ORANGE if (r, c) in {(2,2),(2,3),(3,2),(3,3)} else BLUE, fill_opacity=1, stroke_width=0.8).move_to([c*0.32, -r*0.32, 0]) for r in range(6) for c in range(6)]).center().shift(LEFT*4.5)
        self.play(FadeIn(grid))
        a1 = Arrow(grid.get_right(), grid.get_right() + RIGHT*2.2, color=INK)
        cls = T('"cat"', 44, ORANGE).next_to(a1, RIGHT)
        lbl = T("classifier: one answer", 26, MUTED).next_to(a1, UP, buff=0.1)
        self.play(GrowArrow(a1), FadeIn(lbl), Write(cls))
        self.wait(3)
        self.play(FadeOut(VGroup(a1, cls, lbl)))
        sel = SurroundingRectangle(grid[14], color=RED, buff=0.02)
        q = T("cat or background?", 26, MUTED).next_to(grid, DOWN)
        self.play(Create(sel), FadeIn(q))
        for i in [0, 9, 21, 33]:
            self.play(sel.animate.move_to(grid[i]), run_time=0.5)
        self.play(FadeOut(sel), FadeOut(q))
        steps = bullets(["one label per pixel", "output is a mask", "same size as input"], 28).move_to([2.8, 0, 0])
        for s in steps:
            self.play(FadeIn(s, shift=RIGHT*0.3))
            self.wait(1.2)
        self.play(Indicate(steps[1], color=RED))
        self.finish()'''
