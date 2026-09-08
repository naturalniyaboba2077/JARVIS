"""Small, non-activating voice capsule above the primary monitor's taskbar.

stdin JSON lines: {"amp": 0..1, "show": true/false}, {"quit": true}.
Only the UI thread touches Tk. Reader stores latest state (no event backlog).
EOF stops the overlay even when the parent exits without a quit message.
"""
import ctypes
from ctypes import wintypes
import json
import math
import sys
import threading
import tkinter as tk

CHROMA = "#010203"
WIDTH, HEIGHT = 280, 64


def amplitude(value):
    try:
        value = float(value)
        return max(0.0, min(1.0, value)) if math.isfinite(value) else 0.0
    except (TypeError, ValueError):
        return 0.0


def capsule_position(rect):
    left, top, right, bottom = rect
    return max(left, left + (right - left - WIDTH) // 2), max(top, bottom - HEIGHT - 16)


class Overlay:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", CHROMA)
        self.root.attributes("-alpha", 0.96)
        self.canvas = tk.Canvas(self.root, width=WIDTH, height=HEIGHT,
                                bg=CHROMA, highlightthickness=0, bd=0)
        self.canvas.pack()
        self._position()
        self.root.update_idletasks()
        self._make_click_through()
        for box in ((2, 4, 58, 60), (222, 4, 278, 60)):
            self.canvas.create_oval(*box, fill="#192229", outline="")
        self.canvas.create_rectangle(30, 4, 250, 60, fill="#192229", outline="")
        self.canvas.create_text(24, 23, text="JARVIS", anchor="w", fill="#e8edf0",
                                font=("Segoe UI", 10, "bold"))
        self.canvas.create_text(24, 43, text="Ответ — в окне приложения", anchor="w",
                                fill="#a2aeb7", font=("Segoe UI", 8))
        self.bars = [self.canvas.create_line(212+i*7, 21, 212+i*7, 25,
                     fill="#8cc8e8", width=3, capstyle=tk.ROUND) for i in range(7)]
        self.lock = threading.Lock()
        self.want_show, self.shown, self.closed = False, False, False
        self.amp, self.level = 0.0, 0.0
        threading.Thread(target=self._read_stdin, name="overlay-input", daemon=True).start()
        self._tick()

    def _position(self):
        rect = (0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight())
        try:
            area = wintypes.RECT()
            if ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0):
                rect = (area.left, area.top, area.right, area.bottom)
        except (AttributeError, OSError):
            pass
        x, y = capsule_position(rect)
        self.root.geometry(f"{WIDTH}x{HEIGHT}{x:+d}{y:+d}")

    def _make_click_through(self):
        try:
            user32 = ctypes.windll.user32
            user32.GetParent.restype = wintypes.HWND
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
            hwnd = user32.GetParent(self.root.winfo_id())
            style = user32.GetWindowLongW(hwnd, -20)
            user32.SetWindowLongW(hwnd, -20, style | 0x80000 | 0x20 | 0x80 | 0x08000000)
        except (AttributeError, OSError):
            pass

    def _read_stdin(self):
        try:
            for line in sys.stdin:
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                with self.lock:
                    if msg.get("quit"):
                        return
                    if "amp" in msg:
                        self.amp = amplitude(msg["amp"])
                    if isinstance(msg.get("show"), bool):
                        self.want_show = msg["show"]
        finally:
            with self.lock:
                self.closed = True

    def _tick(self):
        with self.lock:
            closed, visible, amp = self.closed, self.want_show, self.amp
        if closed:
            self.root.destroy()
            return
        if visible != self.shown:
            if visible:
                self._position()
                self.root.deiconify()
            else:
                self.root.withdraw()
            self.shown = visible
        if visible:
            self.level += (amp - self.level) * 0.5
            for i, weight in enumerate((0.5, 0.7, 0.9, 1.0, 0.9, 0.7, 0.5)):
                half = 2 + 9 * self.level * weight
                self.canvas.coords(self.bars[i], 212+i*7, 23-half, 212+i*7, 23+half)
        else:
            self.level = 0.0
        self.root.after(33 if visible else 120, self._tick)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Overlay().run()
