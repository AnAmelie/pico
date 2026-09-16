# Pico Photo Bot

Pico is a private Discord worker that accepts one Reddit post URL or direct image URL, downloads supported still images, embeds source attribution with ExifTool, returns the prepared files to Discord, and offers a persistent button for adding them to a curated Google Photos album.

Pico is deterministic and does not use an LLM. It accepts messages only from configured users in one configured channel. It has no slash commands and exposes no HTTP port.

## Behavior and limits

Paste exactly one supported URL as the entire Discord message. Discord `<https://...>` wrappers are accepted. Pico ignores ordinary conversation and rejects extra text or multiple links rather than guessing.

For direct image URLs, Pico follows at most five redirects, rejects URLs resolving to private or otherwise non-public networks, and records the final hostname and URL as attribution. With Reddit API credentials, Pico uses app-only OAuth and the Reddit API rather than scraping HTML. Reddit galleries retain API order and use the post author and canonical permalink. Without Reddit credentials, Pico runs in direct-image-only mode and gives an actionable response for Reddit links.

Supported input formats and limits:

- JPEG, PNG, and single-frame WebP;
- at most 20 images per import;
- at most 10,000,000 bytes per source or prepared image;
- at most 100,000,000 prepared bytes per import;
- at most 50,000,000 decoded pixels per image;
- Discord delivery batches of at most 10 attachments and 24,000,000 bytes.

ExifTool writes the attribution sentence, source URL, and stable archive tag without re-encoding image pixels. JPEG and PNG receive XMP, EXIF, and IPTC metadata where supported; WebP receives XMP metadata. Pico verifies the written XMP values before delivering a file.

Prepared but unused imports expire after seven days. Successful Google Photos uploads delete local image bytes immediately. Source details, upload state, album mapping, persistent Discord control IDs, upload tokens, and Google item IDs remain in SQLite so interrupted and partial uploads can resume safely. The first album selection permanently locks an import to that album.

## Discord application

Create a Discord application and bot, then enable **Message Content Intent**. Install the bot in the private server and grant these permissions in Pico's channel:

- View Channel
- Read Message History
- Send Messages
- Attach Files
- Embed Links

Set `PICO_CHANNEL_ID` to that channel and include the same ID in `DISCORD_CHANNEL_ID`. Pico accepts submissions only from IDs in `DISCORD_USER_ID`.

## Install locally

Python 3.13 and ExifTool are required.

```sh
brew install exiftool
python3.13 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
cp .env.example .env
```

Fill `.env`, create Google Photos OAuth credentials, authorize once, then start Pico:

```sh
.venv/bin/pico-photo-bot-auth
.venv/bin/pico-photo-bot
```

The daemon never launches interactive OAuth. Missing or unusable credentials, a missing token, or a missing `exiftool` executable stops startup with a corrective error.

## Configuration

| Variable | Required | Purpose |
| --- | --- | --- |
| `PICO_DISCORD_BOT_TOKEN` | Yes | Discord bot token. |
| `PICO_CHANNEL_ID` | Yes | Single private intake channel; must also occur in `DISCORD_CHANNEL_ID`. |
| `DISCORD_USER_ID` | Yes | Comma-separated positive Discord user IDs allowed to submit and upload. |
| `DISCORD_CHANNEL_ID` | Yes | Comma-separated positive Discord channel allowlist containing `PICO_CHANNEL_ID`. |
| `DISCORD_GUILD_ID` | No | Development guild ID for configuration compatibility. |
| `PICO_REDDIT_CLIENT_ID` | No | Reddit script-application client ID. Set with the client secret. |
| `PICO_REDDIT_CLIENT_SECRET` | No | Reddit script-application secret. Set with the client ID. |
| `PICO_REDDIT_USER_AGENT` | With Reddit credentials | Descriptive Reddit API identity, for example `pico-photo-bot/0.1 by u/your-reddit-account`. |
| `PICO_ALBUM_NAMES` | Yes | One to 25 unique, comma-separated Google Photos album titles. |
| `PICO_ARCHIVE_SEARCH_TAG` | Yes | Stable `picoarc` plus 12 hexadecimal characters. Never change it after uploads exist. |
| `PICO_DATA_PATH` | No | SQLite path; defaults to `data/pico.db`. |
| `PICO_MEDIA_PATH` | No | Temporary prepared-image directory; defaults to `data/pico-media`. |
| `PICO_GOOGLE_CREDENTIALS_PATH` | No | Desktop OAuth client JSON; defaults to `data/google-photos-credentials.json`. |
| `PICO_GOOGLE_TOKEN_PATH` | No | Authorized-user token JSON; defaults to `data/google-photos-token.json`. |
| `PICO_GOOGLE_CREDENTIALS_BASE64` | Coolify only | Base64 desktop OAuth JSON used to seed an empty persistent volume. |
| `PICO_GOOGLE_TOKEN_BASE64` | Coolify only | Base64 authorized-user token JSON used to seed an empty persistent volume. |
| `LOG_LEVEL` | No | Python log level; defaults to `INFO`. |

`PICO_REDDIT_CLIENT_ID` and `PICO_REDDIT_CLIENT_SECRET` must be set together. When they are set, `PICO_REDDIT_USER_AGENT` must also be non-blank.

## Google Photos OAuth

Enable the **Google Photos Library API** in Google Cloud and create a **Desktop app** OAuth client for the Google account that will own Pico's albums. Save the downloaded JSON at `PICO_GOOGLE_CREDENTIALS_PATH`, then run `pico-photo-bot-auth` from a machine with a browser.

Pico requests only these scopes:

- `https://www.googleapis.com/auth/photoslibrary.appendonly`
- `https://www.googleapis.com/auth/photoslibrary.readonly.appcreateddata`

Current Google Photos Library API rules allow Pico to create, inspect, and append only to albums created by Pico. Existing albums created outside the app are not available to it. The token is written atomically with mode `0600`, and refreshed tokens retain that mode.

## Archive-tag workflow

`PICO_ARCHIVE_SEARCH_TAG` must match `picoarc[a-f0-9]{12}`. Generate it once per deployment and keep it unchanged. Pico prefixes prepared filenames with the tag and embeds it as an XMP subject; JPEG and PNG also receive it as an IPTC keyword.

Google Photos uploads still appear in the main Photos timeline, and the API cannot archive them. After an upload completes:

1. Copy the exact quoted archive tag displayed by Pico.
2. Search Google Photos for that complete tag, including the quotation marks.
3. Select the newly uploaded results.
4. Archive them.

Archived images remain in their album and in search results. Filename search is the supported discovery mechanism; embedded metadata preserves the marker when files leave Google Photos but is not a Google Photos search contract.

## Docker and Coolify

Build and run locally with Compose:

```sh
docker compose build
docker compose up -d
```

The image uses Python 3.13 slim, installs ExifTool and timezone data, and runs as UID/GID 10001. The Compose service mounts the named `pico-data` volume at `/app/data`; it exposes no port and needs no domain or reverse proxy.

For Coolify:

1. Create a Docker Compose resource from this repository and use `/docker-compose.yml`.
2. Add the variables from `.env.example`. Mark the Discord token, Reddit secret, and both base64 OAuth values as secrets.
3. Authorize locally with `pico-photo-bot-auth`.
4. Copy each OAuth JSON file into its corresponding base64 variable without printing the value, for example on macOS:

   ```sh
   base64 -i data/google-photos-credentials.json | pbcopy
   base64 -i data/google-photos-token.json | pbcopy
   ```

5. Deploy and confirm the log contains `Pico connected to Discord as`.

The deployment launcher validates the base64 JSON, writes missing OAuth files atomically with mode `0600`, and never overwrites files already present in the volume. Refreshed tokens therefore survive redeployments.

Run exactly one replica. Never run local and hosted Pico processes simultaneously with the same Discord token: both consumers can receive the same events, while SQLite and the persistent Discord controls assume one active worker.

## Persistent state, backup, and restore

The persistent state consists of:

- `pico.db`, including the `pico_imports`, `pico_files`, and `pico_albums` tables;
- `pico-media/` locally or `media/` in the container, containing incomplete prepared imports;
- `google-photos-credentials.json`;
- `google-photos-token.json`.

The named Docker volume survives ordinary redeployments but is not a backup. Back up all four items together while Pico is stopped. Before copying SQLite, checkpoint its WAL and require a clean integrity check:

```sh
.venv/bin/python - <<'PY'
import sqlite3

with sqlite3.connect('data/pico.db') as connection:
    print(connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())
    print(connection.execute('PRAGMA integrity_check').fetchone()[0])
PY
```

Restore while Pico is stopped, preserve the same relative media paths, require `PRAGMA integrity_check` to return `ok`, and then start one worker. Do not restore only the database if it contains incomplete imports whose files live in the media directory.

## Development checks

```sh
.venv/bin/pytest
```
