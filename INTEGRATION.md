# KoCharacters — Integration Guide for External Services

This document explains the data produced by the KoCharacters KOReader plugin and how to consume it from another service or Claude instance.

---

## Recommended transfer format: ZIP

The best format for sending KoCharacters data to another service is a **ZIP file** containing:

```
characters.json       ← full character database (raw JSON)
portraits/
  giordano_bruno.png
  philip_sidney.png
  ...
```

**Why ZIP:**
- Single file to transfer, no session state required
- Preserves the relative path between the JSON and portrait files (`portrait_file` field references filenames inside `portraits/`)
- The plugin already produces this exact structure via the "Export as ZIP" menu option, or the raw files can be fetched directly from the Kindle over SFTP and zipped externally

---

## Where the files live on the device

All files for a book are stored under a single subdirectory named after the book title:

```
<koreader_data>/kocharacters/<book_id>/
  characters.json        ← character database
  scanned.json           ← which pages have been scanned (internal, not needed)
  book_context.txt       ← auto-built genre/era/setting summary (plain text)
  portraits/             ← AI-generated portrait images (PNG)
    giordano_bruno.png
    philip_sidney.png
    ...
  characters.html        ← rendered HTML export (optional)
  characters.zip         ← ZIP export (optional, if already exported)
```

`<book_id>` is derived from the sanitized book title + a numeric hash, e.g. `Heresy_7708`.

On a Kindle, `<koreader_data>` is typically `/mnt/us/koreader`.

---

## characters.json — full schema

The file is a **JSON array** of character objects. All fields except `name` are optional and may be absent, empty string, or empty array.

```json
[
  {
    "name": "Giordano Bruno",
    "aliases": ["Fra Giordano", "Filippo Bruno"],
    "role": "protagonist",
    "occupation": "Dominican friar, philosopher",
    "physical_description": "Twenty-eight years old, holds a doctorate in theology.",
    "personality": "Intellectually arrogant, defiant, and quick-witted...",
    "relationships": [
      "Friend of Sir Philip Sidney",
      "Patronized by King Henri",
      "Subject of interest to Francis Walsingham"
    ],
    "first_appearance_quote": "\"Fra Giordano! I order you to come out this instant...\"",
    "user_notes": "Optional free-text notes added by the reader",
    "portrait_file": "giordano_bruno.png",
    "source_page": 285,
    "first_seen_page": 6,
    "unlocked": true,
    "needs_cleanup": false
  },
  ...
]
```

### Field reference

| Field | Type | Description |
|---|---|---|
| `name` | string | Full name or best available name. Always present. |
| `aliases` | string[] | Nicknames, titles, alternate names used in the text |
| `role` | string | One of: `protagonist`, `antagonist`, `supporting`, `unknown` |
| `occupation` | string | Job title or societal role (e.g. `blacksmith`, `spy`, `physician`) |
| `physical_description` | string | Appearance details synthesised from explicit text descriptions |
| `personality` | string | Stable character traits inferred from behaviour — written as traits, not event logs |
| `relationships` | string[] | Connections to other named characters, each as a short phrase (e.g. `"Friend of Philip Sidney"`) |
| `first_appearance_quote` | string | Short verbatim quote from the text where the character first appears |
| `user_notes` | string | Free-text notes added manually by the reader. Preserve as-is, never overwrite. |
| `portrait_file` | string | Filename of the generated portrait inside the `portraits/` folder. May be absent if no portrait has been generated. |
| `source_page` | number | Page number of the most recent character update |
| `first_seen_page` | number | Page number where the character was first encountered |
| `unlocked` | boolean | Spoiler protection flag. `false` = the character was first seen beyond the reader's current position. Treat locked characters with care if building a spoiler-aware UI. |
| `needs_cleanup` | boolean | Internal flag indicating the record may contain redundant merged text that hasn't been deduped yet. Can be ignored by consumers. |

---

## book_context.txt

Plain text file, 2–4 sentences. Auto-built by Gemini from page scanning. Describes the book's genre, setting, country/region, and historical era. Example:

```
The story is set in Elizabethan England (circa 1583), primarily in London,
with earlier scenes in a Neapolitan Dominican monastery. It is a historical
thriller blending espionage, Renaissance philosophy, and religious persecution.
The protagonist is Giordano Bruno, an Italian philosopher and former friar
working as a spy for Francis Walsingham.
```

---

## portraits/

- Format: **PNG** (always, regardless of source)
- Naming: lowercased character name with all non-alphanumeric characters replaced by underscores
  - `"Giordano Bruno"` → `giordano_bruno.png`
  - `"The Man with No Ears"` → `the_man_with_no_ears.png`
- Dimensions: 1024 × 1024 pixels (square)
- Generated by: Google Imagen API (`imagen-4.0-*` models)
- The canonical filename for a character is in their `portrait_file` field. Use that as the primary lookup; fall back to the name-derived filename only if `portrait_file` is absent.

---

## Instructions for a consuming Claude instance

When you receive a ZIP file from KoCharacters:

1. **Unzip** — extract `characters.json` and the `portraits/` folder
2. **Parse `characters.json`** — it is a UTF-8 JSON array. Every object has at minimum a `name` field.
3. **Match portraits** — for each character, check `portrait_file` first; if absent or file not found, derive the filename from the name (lowercase, non-alphanumeric → underscore, append `.png`)
4. **Respect `user_notes`** — this is reader-authored content. Never overwrite or discard it.
5. **Respect `unlocked`** — if building a spoiler-aware feature, treat characters where `unlocked` is `false` as hidden by default
6. **Text fields** — `physical_description` and `personality` are synthesised prose. `relationships` is an array of short natural-language phrases.
7. **`book_context.txt`** (if included) — use as background context for any AI calls about the book's setting or era

