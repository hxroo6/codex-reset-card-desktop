"""Create README screenshots from isolated fictional data only (requires Pillow)."""
from pathlib import Path
import sys
import tkinter as tk
import os
import time
import ctypes
from ctypes import wintypes

from PIL import ImageGrab

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop"))
from reset_card_panel import Panel


def main():
    output = ROOT / "docs" / "images"
    output.mkdir(parents=True, exist_ok=True)
    root = tk.Tk()
    panel = Panel(root, demo=True)
    panel.sound.set(False)
    panel.auto_live.set(False)
    panel.floating_enabled.set(True)
    panel.toggle_floating()
    root.geometry("1160x900+30+30")

    def grab(widget, name):
        # Capture a fully mapped, opaque demo window only. Check that every sampled
        # client point belongs to this process before and after the capture.
        widget.attributes("-topmost", True)
        widget.lift()
        root.update()
        time.sleep(0.6)
        root.update()
        user = ctypes.windll.user32
        user.WindowFromPoint.argtypes = [wintypes.POINT]
        user.WindowFromPoint.restype = ctypes.c_void_p
        user.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
        box = (widget.winfo_rootx(), widget.winfo_rooty(),
               widget.winfo_rootx() + widget.winfo_width(), widget.winfo_rooty() + widget.winfo_height())
        def owned():
            for x in range(box[0] + 2, box[2] - 1, 16):
                for y in range(box[1] + 2, box[3] - 1, 16):
                    handle = user.WindowFromPoint(wintypes.POINT(x, y))
                    process = wintypes.DWORD()
                    user.GetWindowThreadProcessId(handle, ctypes.byref(process))
                    if process.value != os.getpid():
                        return False
            return True
        if not owned():
            raise RuntimeError("A different app covers the demo; refusing the screenshot")
        picture = ImageGrab.grab(bbox=box)
        if not owned():
            raise RuntimeError("A different app moved over the demo; refusing the screenshot")
        picture.save(output / name)

    def capture():
        grab(root, "dashboard.png")
        grab(panel.floating_window.window, "floating-countdown.png")
        if panel.last_notice:
            panel.dismiss_notice()
        panel.show_notice("重置卡使用失败", "演示卡 DEMO-RESULT\n服务端提示卡片不可用。此为虚构提醒，不涉及真实账号。", persistent=True)
        root.after(450, capture_notice)

    def capture_notice():
        grab(panel.last_notice, "result-notification.png")
        panel.dismiss_notice()
        panel.close()

    root.after(1400, capture)
    root.mainloop()


if __name__ == "__main__":
    main()
