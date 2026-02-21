# src/config.py
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

def _parse_int_list(value: str) -> list[int]:
    if not value:
        return []
    items = []
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            items.append(int(part))
        except ValueError:
            pass
    return items


@dataclass(frozen=True)
class Settings:
    bot_token: str
    database_url: str | None
    admin_tg_ids: list[int]


def load_settings() -> Settings:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN не задан в .env")

    database_url = os.getenv("DATABASE_URL", "").strip() or None
    admin_tg_ids = _parse_int_list(os.getenv("ADMIN_TG_IDS", "").strip())

    return Settings(
        bot_token=bot_token,
        database_url=database_url,
        admin_tg_ids=admin_tg_ids,
    )
