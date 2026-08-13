# Vendored: xfgryujk/blivedm

- Upstream: https://github.com/xfgryujk/blivedm
- Branch: dev (the default branch; master is the stale release branch)
- Commit: `0da0c10fc50ed0ccd3e68c65f6a503cf3ca4198b` — 2026-08-12, "开放平台接口修复礼物解析错误"
- Vendored: 2026-08-13, unmodified except a one-line `# UPSTREAM:` header per file
- License: MIT (see LICENSE beside this file, kept verbatim)

## Why vendored instead of a git dependency

Upstream pins `aiohttp~=3.9.0` while we require `aiohttp>=3.13` (plan §5.1);
a dependency install would force the downgrade. The package uses relative
imports throughout, so a verbatim copy works unchanged. Its runtime deps
(aiohttp, brotli, pure-protobuf, yarl-via-aiohttp) are all satisfied by our
environment. Do NOT `pip install blivedm` — the PyPI name is squatted by an
unrelated, wrong package (upstream README warns about this).

## Field verification results (2026-08-13, against this commit)

- `DanmakuMessage.uid_crc32` ← `info[0][7]` — the masked-uid fallback identity
  (plan §5.2 correction 1). Timestamp is milliseconds. No `id_str` on the
  model; `rnd` is the dedup-ish id.
- `SEND_GIFT_V2` dispatches through `GiftMessage.batch_from_command_v2` and
  feeds the SAME `_on_gift` callback as V1 (handlers.py) — one callback covers
  both. `tid` is the transaction id for V1/V2 merging.
- `SuperChatMessage.price` is already CNY (do not divide by 1000). `id` is the
  handle for `SUPER_CHAT_MESSAGE_DELETE`, which has its own callback.
- `USER_TOAST_MSG_V2.price` is gold seeds per unit (divide by 1000 for CNY);
  `source` field: a purchase emits source=0 then source=2 — keep 0, drop 2.
  Legacy `USER_TOAST_MSG` is in upstream's own ignore list; `GUARD_BUY` still
  dispatches and needs a merge window on our side.
- WBI signing uses `urllib.parse.urlencode` (hand-rolled), not yarl query
  encoding — insensitive to yarl version drift. yarl is only used for
  `cookie_jar.filter_cookies(yarl.URL(...))`.

## How to re-vendor (quarterly discipline, plan §5.1)

1. `git clone --depth 1 --branch dev https://github.com/xfgryujk/blivedm /tmp/blivedm-vendor`
2. Record the new sha; replace `blivedm/` here wholesale; keep LICENSE.
3. Re-add the `# UPSTREAM:` header line to every .py with the new sha.
4. Update this file, then run the mapping unit tests
   (tests/unit/test_bili_translate.py) — they go through upstream's
   `from_command` parsers on purpose, so a behavioural change upstream trips
   them before it reaches a live room.
