import re
import subprocess
import time


def normalize(text):
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def extract_contact(command):
    command = normalize(command)

    patterns = [
        r"^open (.+?)'s chat in whatsapp$",
        r"^open (.+?)s chat in whatsapp$",
        r"^open (.+?)'s chat on whatsapp$",
        r"^open (.+?)s chat on whatsapp$",
        r"^open whatsapp chat with (.+)$",
        r"^open whatsapp chat of (.+)$",
        r"^open chat with (.+) on whatsapp$",
        r"^open chat with (.+) in whatsapp$",
        r"^show (.+?)'s chat in whatsapp$",
        r"^show (.+?)s chat in whatsapp$",
        r"^go to (.+?)'s chat in whatsapp$",
        r"^go to (.+?)s chat in whatsapp$",
        r"^whatsapp chat with (.+)$",
        r"^chat with (.+) on whatsapp$",
    ]

    for pattern in patterns:
        match = re.match(pattern, command)

        if match:
            return match.group(1).strip()

    return None


def open_whatsapp():
    """Launch WhatsApp Desktop."""

    try:
        subprocess.Popen(
            [
                "powershell.exe",
                "-NoProfile",
                "-WindowStyle",
                "Hidden",
                "-Command",
                "Start-Process 'whatsapp:'",
            ],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        time.sleep(1.5)

        return True

    except Exception as e:
        print(f"❌ WhatsApp launch error: {e}")
        return False


def open_whatsapp_chat(contact):
    """Open a WhatsApp contact using the desktop UI."""

    try:
        import pyautogui
        pyautogui.FAILSAFE = False
    except ImportError:
        return (
            "PyAutoGUI isn't installed. "
            "Run pip install pyautogui"
        )

    if not open_whatsapp():
        return "I couldn't open WhatsApp"

    time.sleep(1.5)

    try:
        # Focus WhatsApp search
        pyautogui.hotkey("ctrl", "f")
        time.sleep(0.5)

        # Clear previous search
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.1)

        # Type or paste contact name
        try:
            import pyperclip
            pyperclip.copy(contact)
            pyautogui.hotkey("ctrl", "v")
        except Exception:
            pyautogui.write(
                contact,
                interval=0.03,
            )

        time.sleep(1.0)

        # Select first matching result
        pyautogui.press("enter")

        return f"Opening {contact}'s WhatsApp chat."
    except Exception as e:
        print(f"⚠️ WhatsApp chat automation warning: {e}")
        return f"Opened WhatsApp for {contact}."




def handle_whatsapp_command(command):
    contact = extract_contact(command)

    if not contact:
        return None

    return open_whatsapp_chat(contact)