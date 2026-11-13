"""Ways to ask the user for a secret without showing it on screen or in shell history."""

from __future__ import annotations

import getpass
from collections.abc import Callable
from typing import Any

# Friendly names for the pop-up window.
LABELS = {
    "twitch_client_id": "Twitch Client ID",
    "twitch_client_secret": "Twitch Client Secret",
    "groq_api_key": "Groq API key",
    "llm_api_key": "LLM API key",
    "uploadpost_api_key": "Upload-Post API key",
}


def clean_secret(value: str | None) -> str:
    """Strip surrounding whitespace; return ``""`` when the value can't be a real key.

    A paste that didn't work can leave control characters behind (for example
    the Ctrl+V keystroke itself); storing that would only cause a confusing
    "rejected" error later.
    """
    value = (value or "").strip()
    if not value or not value.isprintable() or " " in value:
        return ""
    return value


def ask_in_terminal(name: str) -> str:
    """Ask at the terminal with hidden input."""
    return getpass.getpass(f"Paste the value for {name} (it stays hidden), then Enter: ")


def ask_in_window(
    name: str, on_ready: Callable[[Any, Any, Callable[[], None]], None] | None = None
) -> str | None:
    """Ask in a small pop-up window with a masked box; ``None`` if the user cancels.

    Pasting works the normal way (Ctrl+V or right-click). ``on_ready`` receives
    the window, the entry box and the save function; it exists for testing.
    """
    import tkinter as tk

    label = LABELS.get(name, name)
    result: dict[str, str | None] = {"value": None}

    root = tk.Tk()
    root.title("ClipVault")
    root.resizable(False, False)
    root.attributes("-topmost", True)

    frame = tk.Frame(root, padx=18, pady=16)
    frame.pack()
    tk.Label(frame, text=f"Paste the {label}", font=("Segoe UI", 12, "bold")).pack(anchor="w")
    tk.Label(
        frame,
        text="Click in the box, press Ctrl+V, then click Save.\n"
        "It shows as dots. It is stored in this computer's password store, not in a file.",
        justify="left",
        font=("Segoe UI", 9),
    ).pack(anchor="w", pady=(4, 10))
    entry = tk.Entry(frame, show="•", width=52, font=("Segoe UI", 11))
    entry.pack(fill="x")
    count = tk.Label(frame, text="0 characters", font=("Segoe UI", 9), fg="#555555")
    count.pack(anchor="w", pady=(4, 10))

    def update_count(_event: Any = None) -> None:
        count.config(text=f"{len(entry.get().strip())} characters")

    def save() -> None:
        result["value"] = entry.get()
        root.destroy()

    def paste_from_menu(_event: Any) -> str:
        try:
            entry.insert("insert", root.clipboard_get())
        except tk.TclError:
            pass
        update_count()
        return "break"

    entry.bind("<KeyRelease>", update_count)
    entry.bind("<<Paste>>", lambda _e: root.after(10, update_count))
    entry.bind("<Button-3>", paste_from_menu)  # right-click pastes
    entry.bind("<Return>", lambda _e: save())
    root.bind("<Escape>", lambda _e: root.destroy())

    buttons = tk.Frame(frame)
    buttons.pack(anchor="e")
    tk.Button(buttons, text="Cancel", width=10, command=root.destroy).pack(side="right")
    tk.Button(buttons, text="Save", width=10, command=save).pack(side="right", padx=(0, 8))

    root.update_idletasks()
    x = (root.winfo_screenwidth() - root.winfo_width()) // 2
    y = (root.winfo_screenheight() - root.winfo_height()) // 3
    root.geometry(f"+{x}+{y}")
    root.lift()
    entry.focus_force()
    if on_ready is not None:
        on_ready(root, entry, save)
    root.mainloop()
    return result["value"]
