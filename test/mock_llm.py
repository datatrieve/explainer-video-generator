import json, re
from fastapi import FastAPI, Request
import uvicorn
app = FastAPI()
PLAN = {"title": "Gradient Descent", "scenes": [
 {"narration": "Gradient descent is how machines learn. Imagine standing on a hill in fog, and you want to reach the valley.", "visual": "title then hill curve with a ball"},
 {"narration": "At each step, feel the slope under your feet and take a small step downhill. Repeat, and you reach the bottom.", "visual": "ball rolls down in steps"}]}
GOOD2 = '''```python
from vg import *

class S02(Timed):
    def construct(self):
        ax = Axes(x_range=[-3, 3], y_range=[0, 9], x_length=8, y_length=4.5, axis_config={"color": INK})
        curve = ax.plot(lambda x: x**2, color=BLUE)
        self.play(Create(ax), Create(curve))
        dot = Dot(ax.c2p(2.6, 2.6**2), color=RED, radius=0.12)
        self.play(FadeIn(dot))
        x = 2.6
        for _ in range(5):
            x = x - 0.3 * 2 * x
            self.play(dot.animate.move_to(ax.c2p(x, x**2)), run_time=0.6)
            self.wait(0.4)
        self.play(Write(T("minimum!", 36, GREEN).next_to(dot, UP)))
        self.finish()
```'''
BAD2 = GOOD2.replace('T("minimum!"', 'MathTex("minimum!"')
GOOD1 = '''```python
from vg import *

class S01(Timed):
    def construct(self):
        t = T("Gradient Descent", 64, weight=BOLD)
        self.play(Write(t))
        self.wait(1)
        self.play(t.animate.scale(0.6).to_edge(UP))
        hill = FunctionGraph(lambda x: 0.25 * x**2 - 2, x_range=[-5, 5], color=GREEN)
        ball = Dot([4, 2, 0], color=RED, radius=0.15)
        self.play(Create(hill), FadeIn(ball))
        self.play(Write(T("find the valley", 30, MUTED).to_edge(DOWN)))
        self.finish()
```'''
n = {"fix": 0}
@app.post("/v1/chat/completions")
async def chat(req: Request):
    b = await req.json(); last = b["messages"][-1]["content"]; sys = b["messages"][0]["content"]
    if "Return ONLY a JSON" in sys: out = json.dumps(PLAN)
    elif "failed to render" in last: out = GOOD2
    elif "class named S01" in sys: out = GOOD1
    else: out = BAD2
    return {"choices": [{"message": {"role": "assistant", "content": out}}]}
uvicorn.run(app, host="127.0.0.1", port=9301)
