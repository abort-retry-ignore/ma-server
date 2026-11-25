"""The plugin's own small database: saved play queues, ratings and the image-link secret."""

from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass, field

from music_assistant.helpers.database import DatabaseConnection
from music_assistant.helpers.json import json_dumps, json_loads

from .constants import DB_FILE

TABLE_META = "meta"
TABLE_PLAY_QUEUE = "play_queue"
TABLE_RATINGS = "ratings"
META_IMAGE_SECRET = "image_secret"


@dataclass
class SavedPlayQueue:
    """A play queue a client saved for a user."""

    ids: list[str] = field(default_factory=list)
    current_index: int | None = None
    position: int = 0
    changed: float = 0.0
    changed_by: str = ""


class Storage:
    """Persistent plugin data, kept in its own database file in MA's data folder."""

    def __init__(self, storage_path: str) -> None:
        """
        Initialize the storage.

        :param storage_path: MA's data folder.
        """
        self._path = os.path.join(storage_path, DB_FILE)
        self._db: DatabaseConnection | None = None
        self.image_secret = b""

    async def setup(self) -> None:
        """Open the database and create the tables."""
        db = DatabaseConnection(self._path)
        await db.setup(cache_size_kib=512, mmap_size_bytes=0)
        await db.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE_META} (key TEXT PRIMARY KEY, value TEXT)"
        )
        await db.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE_PLAY_QUEUE} ("
            "user_id TEXT PRIMARY KEY, ids TEXT NOT NULL, current_index INTEGER, "
            "position INTEGER NOT NULL DEFAULT 0, changed REAL NOT NULL, "
            "changed_by TEXT NOT NULL DEFAULT '')"
        )
        await db.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE_RATINGS} ("
            "user_id TEXT NOT NULL, item_id TEXT NOT NULL, rating INTEGER NOT NULL, "
            "PRIMARY KEY (user_id, item_id))"
        )
        await db.commit()
        self._db = db
        row = await db.get_row(TABLE_META, {"key": META_IMAGE_SECRET})
        if row:
            self.image_secret = bytes.fromhex(row["value"])
        else:
            self.image_secret = secrets.token_bytes(32)
            await db.insert_or_replace(
                TABLE_META, {"key": META_IMAGE_SECRET, "value": self.image_secret.hex()}
            )

    async def close(self) -> None:
        """Close the database."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def get_play_queue(self, user_id: str) -> SavedPlayQueue:
        """
        Return the user's saved play queue (empty when none was saved).

        :param user_id: The MA user id.
        """
        assert self._db is not None
        row = await self._db.get_row(TABLE_PLAY_QUEUE, {"user_id": user_id})
        if not row:
            return SavedPlayQueue()
        return SavedPlayQueue(
            ids=list(json_loads(row["ids"])),
            current_index=row["current_index"],
            position=int(row["position"]),
            changed=float(row["changed"]),
            changed_by=str(row["changed_by"]),
        )

    async def save_play_queue(
        self,
        user_id: str,
        ids: list[str],
        current_index: int | None,
        position: int,
        client: str,
    ) -> None:
        """
        Save the user's play queue, replacing the previous one.

        :param user_id: The MA user id.
        :param ids: The Subsonic song IDs in queue order.
        :param current_index: Index of the current song, if any.
        :param position: Playback position in the current song, in milliseconds.
        :param client: The client that saved it.
        """
        assert self._db is not None
        await self._db.insert_or_replace(
            TABLE_PLAY_QUEUE,
            {
                "user_id": user_id,
                "ids": json_dumps(ids),
                "current_index": current_index,
                "position": position,
                "changed": time.time(),
                "changed_by": client,
            },
        )

    async def get_ratings(self, user_id: str) -> dict[str, int]:
        """
        Return the user's ratings by Subsonic ID.

        :param user_id: The MA user id.
        """
        assert self._db is not None
        rows = await self._db.get_rows(TABLE_RATINGS, {"user_id": user_id}, limit=0)
        return {str(r["item_id"]): int(r["rating"]) for r in rows}

    async def set_rating(self, user_id: str, item_id: str, rating: int) -> None:
        """
        Set (or with 0, remove) the user's rating of an item.

        :param user_id: The MA user id.
        :param item_id: The Subsonic ID.
        :param rating: 1 to 5, or 0 to remove the rating.
        """
        assert self._db is not None
        if rating <= 0:
            await self._db.delete(TABLE_RATINGS, {"user_id": user_id, "item_id": item_id})
            return
        await self._db.insert_or_replace(
            TABLE_RATINGS, {"user_id": user_id, "item_id": item_id, "rating": rating}
        )
