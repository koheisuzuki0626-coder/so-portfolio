#!/usr/bin/env python3
"""Discordの会話履歴を、開発側（Claude Codeのチャット）から直接読むための道具。

なぜ要るか:
    不具合の調査のたびにスクリーンショットを送ってもらうのが手間だった。
    ボットがログをGitHubへ push する仕組みもあるが、それは
    「Macのボットが生きていて、最新コードで動いている」ことが前提になる。
    ボットが落ちている時ほど中身を見たいので、それでは足りない。
    この道具は Discord API を直接叩くので、ボットの状態に関係なく読める。

使い方:
    export DISCORD_READ_TOKEN=...          # Botトークン（下の注意を参照）
    python3 tools/read_discord.py --list                 # チャンネル一覧
    python3 tools/read_discord.py --channel <ID> -n 100  # 直近100件
    python3 tools/read_discord.py --channel <ID> --since 2026-08-01

トークンについて:
    ・チャットに貼らず、実行環境の環境変数に設定すること
    ・必要な権限は「メッセージ履歴を読む」だけ。送信権限は要らない
    ・Botが参加していて、かつ閲覧できるチャンネルしか読めない
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://discord.com/api/v10"
JST = timezone(timedelta(hours=9))


def _token():
    for k in ("DISCORD_READ_TOKEN", "DISCORD_ORCH_TOKEN", "DISCORD_BOT_TOKEN"):
        v = os.getenv(k)
        if v:
            return v.strip()
    sys.exit(
        "トークンが設定されていません。\n"
        "  export DISCORD_READ_TOKEN=<Botトークン>\n"
        "※チャットに貼らず、環境変数に設定してください。"
    )


def _get(path, params=None):
    url = f"{API}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bot {_token()}",
        "User-Agent": "DiscordBot (local-reader, 1.0)",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        if e.code == 401:
            sys.exit("トークンが無効です（401）。Botトークンか確認してください。")
        if e.code == 403:
            sys.exit(f"このチャンネルを読む権限がありません（403）。{body}")
        sys.exit(f"Discord APIエラー {e.code}: {body}")
    except Exception as e:  # noqa: BLE001
        sys.exit(f"接続に失敗しました: {e}")


def list_channels():
    """Botが参加しているサーバーと、読めるテキストチャンネルを並べる。"""
    guilds = _get("/users/@me/guilds")
    if not guilds:
        print("Botが参加しているサーバーがありません。")
        return
    for g in guilds:
        print(f"\n■ {g.get('name')}  (guild_id={g.get('id')})")
        for c in _get(f"/guilds/{g['id']}/channels"):
            if c.get("type") in (0, 5):        # 0=テキスト, 5=アナウンス
                print(f"   #{c.get('name'):<24} channel_id={c.get('id')}")


def fetch(channel_id, limit, since=None):
    """新しい順に取得して、古い順に並べ替えて返す。"""
    out, before = [], None
    while len(out) < limit:
        params = {"limit": min(100, limit - len(out))}
        if before:
            params["before"] = before
        page = _get(f"/channels/{channel_id}/messages", params)
        if not page:
            break
        out.extend(page)
        before = page[-1]["id"]
        if since and _ts(page[-1]) < since:
            break
        if len(page) < 100:
            break
    msgs = list(reversed(out))
    if since:
        msgs = [m for m in msgs if _ts(m) >= since]
    return msgs


def _ts(m):
    return datetime.fromisoformat(m["timestamp"].replace("Z", "+00:00"))


def show(msgs):
    for m in msgs:
        who = (m.get("author") or {}).get("global_name") \
            or (m.get("author") or {}).get("username", "?")
        if (m.get("author") or {}).get("bot"):
            who += "[bot]"
        t = _ts(m).astimezone(JST).strftime("%m-%d %H:%M")
        body = (m.get("content") or "").strip()
        for a in m.get("attachments") or []:
            body += f"\n    [添付] {a.get('filename')} {a.get('url')}"
        for e in m.get("embeds") or []:
            ttl = e.get("title") or ""
            if ttl:
                body += f"\n    [埋込] {ttl}"
        print(f"{t} {who}: {body or '(本文なし)'}")
    print(f"\n--- {len(msgs)}件 ---")


def main():
    ap = argparse.ArgumentParser(description="Discordの会話履歴を読む")
    ap.add_argument("--list", action="store_true", help="チャンネル一覧を表示")
    ap.add_argument("--channel", help="読むチャンネルID")
    ap.add_argument("-n", "--limit", type=int, default=50, help="件数（既定50）")
    ap.add_argument("--since", help="この日付以降だけ（例: 2026-08-01）")
    a = ap.parse_args()
    if a.list:
        return list_channels()
    if not a.channel:
        return ap.print_help()
    since = None
    if a.since:
        since = datetime.fromisoformat(a.since).replace(tzinfo=JST)
    show(fetch(a.channel, a.limit, since))


if __name__ == "__main__":
    main()
