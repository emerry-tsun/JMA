
# JMA (Japan Meteorological Agency Bot)

A Python-based bot for [Bluesky Social](https://bsky.app) that posts weather advisories, warnings, urgent warnings, and emergency warnings for specific areas using information from the Japan Meteorological Agency (JMA).

## Features
- Fetches weather information from JMA's XML feed (supports both legacy VPWW53/54 and new VPWW55–61 formats introduced in May 2026).
- Monitors area-specific status changes based on a configuration file.
- Posts status updates to Bluesky Social for four severity levels:
  - **注意報 / Advisory** (Level 2)
  - **警報 / Warning** (Level 3)
  - **危険警報 / Urgent Warning** (Level 4) — *New from May 2026*
  - **特別警報 / Emergency Warning** (Level 5)

---

## File Structure

### `jma.py`
The main Python script that:
1. Retrieves weather information from JMA's XML feed:  
   [JMA XML Feed](https://www.data.jma.go.jp/developer/xml/feed/extra.xml)  
2. Compares the latest data with previously recorded statuses stored in the `last` directory.
3. Posts status changes to Bluesky Social using credentials and settings in `post.csv`.

#### Example Crontab Configuration:
```bash
* * * * * /usr/local/emerry/jma/jma.py >> /var/tmp/jma.log 2>&1
```

## Configurable Variables in `jma.py`

- **`BASE_DIR`**:  
  Directory where the following files are located:
  - `jma.py`
  - `area.csv`
  - `post.csv`

---

## Process

1. **Access JMA Data**:  
   The script retrieves meteorological information from the Japan Meteorological Agency's XML feed:  
   [https://www.data.jma.go.jp/developer/xml/feed/extra.xml](https://www.data.jma.go.jp/developer/xml/feed/extra.xml)

2. **Check Area Information**:  
   The script checks the status of specified areas defined in `area.csv`.

3. **Post Status Updates**:  
   If any status changes are detected by comparing the current data with previously recorded data in the `last` directory, the script posts the updates to Bluesky Social using information in `post.csv`.

---

### `post_message.py`
A script that posts specified messages to Bluesky Social accounts listed in a CSV file.

#### Usage:
```bash
python3 post_message.py <input_csv>
```

#### Input CSV (command line argument):

| Column | Description |
|--------|-------------|
| **Account** | Account name matching an entry in `post.csv`. |
| **Message** | Message to post. Multi-line messages are supported (enclose in double quotes). |

#### Example input CSV:
```
shinjuku_wa,"本日の天気は晴れです。
気温は25度です。"
shinagawa_ww,大雨警報が発令されました。
```

#### Credentials file (`post.csv` in current directory):
Uses the same `post.csv` format as `jma.py`. The account name in the input CSV is matched against the first column of `post.csv` to retrieve the Bluesky username and password for authentication.

---

### `update_profile.py`
A script that updates the profile description of Bluesky Social accounts listed in a CSV file.

#### Usage:
```bash
python3 update_profile.py <input_csv>
```

#### Input CSV (command line argument):

| Column | Description |
|--------|-------------|
| **Account** | Account name matching an entry in `post.csv`. |
| **Description** | Profile description to set. Multi-line text is supported (enclose in double quotes). |

#### Example input CSV:
```
shinjuku_wa,"新宿区の気象警報・注意報をお知らせします。
Japan Meteorological Agency bot."
shinagawa_ww,品川区の気象警報をお知らせします。
```

#### Credentials file (`post.csv` in current directory):
Uses the same `post.csv` format as `jma.py` and `post_message.py`. The account name in the input CSV is matched against the first column of `post.csv` to retrieve the Bluesky username and password for authentication.

---

## `area.csv`

This file contains information about the areas to monitor and their posting configurations.

### Columns:

| Column Name                                     | Description                                                     |
|-------------------------------------------------|-----------------------------------------------------------------|
| **Area Code**                                   | Area code used in the XML feed.                                 |
| **Japanese Area Name**                          | e.g., 葛飾区                                                    |
| **English Area Name**                           | e.g., Minato-city                                               |
| **Not Used**                                    | Reserved for future use.                                        |
| **Japanese Prefecture Name**                    | Prefecture name in Japanese.                                    |
| **Account for Advisories in Japanese**          | Unique identifier for Japanese advisories. (Level 2)           |
| **Account for Warnings in Japanese**            | Unique identifier for Japanese warnings. (Level 3)             |
| **Account for Urgent Warnings in Japanese**     | Unique identifier for Japanese urgent warnings. (Level 4)      |
| **Account for Emergency Warnings in Japanese**  | Unique identifier for Japanese emergency warnings. (Level 5)   |
| **Account for Advisories in English**           | Unique identifier for English advisories. (Level 2)            |
| **Account for Warnings in English**             | Unique identifier for English warnings. (Level 3)              |
| **Account for Urgent Warnings in English**      | Unique identifier for English urgent warnings. (Level 4)       |
| **Account for Emergency Warnings in English**   | Unique identifier for English emergency warnings. (Level 5)    |
| **Tags in Japanese**                            | Space-separated hashtags (e.g., 葛飾 気象).                    |
| **Tags in English**                             | Space-separated hashtags (e.g., MinatoCity).                   |

> **Note**: The previous 13-column format (without Urgent Warning columns) is still supported for backward compatibility.

---

## `post.csv`

This file contains the account information needed to post updates to Bluesky Social.

### Columns:

| Column Name                      | Description                              |
|----------------------------------|------------------------------------------|
| **Account**                      | Matches an account entry in `area.csv`.  |
| **User Name of Bluesky Social**  | The Bluesky Social username.             |
| **Password of Bluesky Social**   | The Bluesky Social password.             |

---

## JMA Warning System (May 2026 Revision)

From **May 29, 2026**, the JMA overhauled its warning and advisory framework. This bot is updated to support both the legacy format (VPWW53/54, continued until approximately 2028) and the new format (VPWW55–61).

### Warning Severity Levels

| Level | Japanese        | English                   | XML Code Range |
|-------|-----------------|---------------------------|----------------|
| 2     | 注意報           | Advisory                  | 10–29          |
| 3     | 警報             | Warning                   | 02–09          |
| 4     | **危険警報**     | **Urgent Warning** *(New)*| 40–49          |
| 5     | 特別警報         | Emergency Warning         | 30–39          |

### Key Changes in May 2026

| Change | Detail |
|--------|--------|
| **New: 危険警報 (Level 4)** | Inserted between 警報 (L3) and 特別警報 (L5). Codes: 43 (大雨), 48 (高潮), 49 (土砂災害). |
| **New: 土砂災害 codes** | Code 09 (土砂災害警報 L3) and code 29 (土砂災害注意報 L2) added. |
| **Abolished: 洪水注意報/警報** | Codes 04 and 18 discontinued in new format. Retained for VPWW54 transition period. |
| **New XML feed** | VPWW55–61 telegrams added. Feed title filter updated to match `気象警報・注意報（Ｒ０６）…`. |

### State File Format (`last/` directory)

The per-area state files record which warnings are currently active.

| Format | Lines | Description |
|--------|-------|-------------|
| Legacy | 4     | `wa`, `ww`, `wew`, `time` |
| New    | 5     | `wa`, `ww`, `wuw`, `wew`, `time` |

Old 4-line files are automatically read in legacy mode; new files are written in 5-line format.
