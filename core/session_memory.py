"""Small, in-process session memory shared by every Jarvis subsystem."""

from collections import deque
from dataclasses import dataclass, field


@dataclass
class SessionMemory:
    history: deque = field(default_factory=lambda: deque(maxlen=16))
    active_folder: str | None = None
    last_opened_file: str | None = None
    last_played_media: str | None = None
    last_opened_app: str | None = None
    last_opened_website: str | None = None
    last_search: str | None = None
    youtube_search_query: str | None = None
    youtube_search_results: list[str] = field(default_factory=list)
    current_app: str | None = None
    last_app: str | None = None
    active_window: str | None = None
    active_context: str | None = None
    contexts: dict[str, dict] = field(default_factory=dict)

    @property
    def last_file(self) -> str | None:
        return self.last_opened_file

    @last_file.setter
    def last_file(self, value: str | None) -> None:
        self.last_opened_file = value

    @property
    def last_media(self) -> str | None:
        return self.last_played_media

    @last_media.setter
    def last_media(self, value: str | None) -> None:
        self.last_played_media = value

    @property
    def last_website(self) -> str | None:
        return self.last_opened_website

    @last_website.setter
    def last_website(self, value: str | None) -> None:
        self.last_opened_website = value

    def set_current_app(self, app_name: str | None) -> None:
        if app_name:
            clean = app_name.strip()
            self.activate_context(f"app:{clean.lower()}")
            if self.current_app and self.current_app.lower() != clean.lower():
                self.last_app = self.current_app
            self.current_app = clean
            self.last_opened_app = clean

    def activate_context(self, key: str | None) -> None:
        """Switch to an isolated folder/site/app context."""
        clean = str(key or "").strip().lower()
        if not clean or clean == self.active_context:
            return
        if self.active_context:
            self.contexts[self.active_context] = {
                "last_opened_file": self.last_opened_file,
                "last_played_media": self.last_played_media,
                "last_search": self.last_search,
                "youtube_search_query": self.youtube_search_query,
                "youtube_search_results": list(self.youtube_search_results),
            }
        saved = self.contexts.get(clean, {})
        self.last_opened_file = saved.get("last_opened_file")
        self.last_played_media = saved.get("last_played_media")
        self.last_search = saved.get("last_search")
        self.youtube_search_query = saved.get("youtube_search_query")
        self.youtube_search_results = list(saved.get("youtube_search_results", []))
        self.active_context = clean

    def clear(self) -> None:
        self.history.clear()
        self.active_folder = None
        self.last_opened_file = None
        self.last_played_media = None
        self.last_opened_app = None
        self.last_opened_website = None
        self.last_search = None
        self.youtube_search_query = None
        self.youtube_search_results.clear()
        self.current_app = None
        self.last_app = None
        self.active_window = None
        self.active_context = None
        self.contexts.clear()


session = SessionMemory()


def remember_user(text: str) -> None:
    if text:
        session.history.append({"role": "user", "content": text.strip()})


def pop_last_user() -> None:
    """Remove the most recent user turn if interrupted before responding."""
    if session.history and session.history[-1]["role"] == "user":
        session.history.pop()


def remember_assistant(text: str) -> None:
    if text:
        session.history.append({"role": "assistant", "content": text.strip()})


def ai_messages(current_user_message: str) -> list[dict]:
    """Return one bounded shared history, with the current turn exactly once."""
    messages = list(session.history)
    if not messages or messages[-1]["role"] != "user" or messages[-1]["content"] != current_user_message.strip():
        messages.append({"role": "user", "content": current_user_message.strip()})
    return messages[-16:]


def context_summary() -> str:
    fields = {
        "active folder": session.active_folder,
        "last opened file": session.last_opened_file,
        "last played media": session.last_played_media,
        "last opened app": session.last_opened_app,
        "last opened website": session.last_opened_website,
        "last search": session.last_search,
        "active context": session.active_context,
    }
    return "\n".join(f"{name}: {value}" for name, value in fields.items() if value) or "No local context yet."
