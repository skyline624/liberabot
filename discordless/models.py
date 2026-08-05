"""Domain model for a captured Discord message."""
import hashlib
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class DiscordMessage:
    """An immutable captured Discord message.

    Attributes:
        channel_id: Discord channel snowflake ID.
        author: Username of the message author.
        content: Text content of the message.
        timestamp: ISO 8601 timestamp string from the Discord API.
        message_id: Source message snowflake ID (required to build a native forward).
        guild_id: Source guild snowflake ID (native forward reference).
    """

    channel_id: str
    author: str
    content: str
    timestamp: str
    channel_name: str = ""
    guild_name: str = ""
    author_id: str = ""
    author_avatar: str = ""
    message_id: str = ""
    guild_id: str = ""

    @property
    def dedup_key(self) -> str:
        """Unique key for deduplication.

        Uses the source message ID when available — it is exact and, unlike the
        content hash, still distinguishes attachment-only messages (which carry
        no text but are forwardable natively). Falls back to the content hash so
        messages captured without an ID keep their previous behaviour.
        """
        if self.message_id:
            return f"{self.channel_id}:{self.message_id}"
        h = hashlib.md5(
            f"{self.timestamp}:{self.author}:{self.content}".encode()
        ).hexdigest()[:16]
        return f"{self.channel_id}:{h}"

    def to_log_line(self) -> str:
        """Return a human-readable log line for this message."""
        try:
            dt = datetime.fromisoformat(self.timestamp.replace("Z", "+00:00"))
            ts = dt.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, AttributeError):
            ts = self.timestamp
        short = self.channel_id[-8:] if len(self.channel_id) > 8 else self.channel_id
        return f"[{ts}] [#{short}] @{self.author}: {self.content}"
