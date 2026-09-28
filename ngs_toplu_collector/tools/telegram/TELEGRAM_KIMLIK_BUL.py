#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Show numeric IDs from this bot's recent Telegram updates without saving its token."""

from __future__ import annotations

import getpass
import json
import urllib.error
import urllib.request


def main() -> int:
    token = getpass.getpass("BotFather tokeni (gizli): ").strip()
    if not token or any(c.isspace() for c in token):
        print("Token bos veya gecersiz.")
        return 2
    print("Bota /start veya /durum gondermis olmalisiniz. Yalniz sayisal ID'ler gosterilecek.")
    request = urllib.request.Request(
        "https://api.telegram.org/bot" + token + "/getUpdates?limit=10&timeout=0",
        headers={"User-Agent": "NGS-Telegram-ID-Finder/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            result = json.load(response)
    except urllib.error.HTTPError as exc:
        print("Telegram API HTTP", exc.code, "(token gecersiz veya bot zaten baska bir agent tarafindan dinleniyor olabilir).")
        return 2
    except (urllib.error.URLError, TimeoutError, ValueError):
        print("Telegram API yaniti alinamadi; ag baglantisini kontrol edin.")
        return 2
    if not result.get("ok"):
        print("Telegram API istegi basarisiz.")
        return 2
    seen: set[tuple[int, int]] = set()
    for update in result.get("result") or []:
        message = update.get("message") or update.get("edited_message") or {}
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        try:
            pair = (int(sender["id"]), int(chat["id"]))
        except (KeyError, TypeError, ValueError):
            continue
        if pair not in seen:
            print("allowed_user_id =", pair[0], " | allowed_chat_id =", pair[1])
            seen.add(pair)
    if not seen:
        print("Yeni mesaj bulunamadi. Bota /start gonderip tekrar calistirin; calisan agent varsa once durdurun.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
