
# JMA (Japan Meteorological Agency Bot)

A Python-based bot for [Bluesky Social](https://bsky.app) that posts weather advisories and warnings for specific areas using information from the Japan Meteorological Agency (JMA).

## Features
- Fetches weather advisory and warning information from JMA's XML feed.
- Monitors area-specific status changes based on a configuration file.
- Posts status updates (e.g., advisories, warnings, emergency warnings) to Bluesky Social.

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

| Column Name                           | Description                                        |
|---------------------------------------|----------------------------------------------------|
| **Area Code**                         | Area code used in the XML feed.                   |
| **Japanese Area Name**                | e.g., 葛飾区                                       |
| **English Area Name**                 | e.g., Minato-city                                  |
| **Not Used**                          | Reserved for future use.                          |
| **Japanese Prefecture Name**          | Prefecture name in Japanese.                      |
| **Account for Advisories in Japanese**| Unique identifier for Japanese advisories.        |
| **Account for Warnings in Japanese**  | Unique identifier for Japanese warnings.          |
| **Account for Emergency Warnings in Japanese** | Unique identifier for Japanese emergency warnings. |
| **Account for Advisories in English** | Unique identifier for English advisories.         |
| **Account for Warnings in English**   | Unique identifier for English warnings.           |
| **Account for Emergency Warnings in English** | Unique identifier for English emergency warnings. |
| **Tags in Japanese**                  | Space-separated tags (e.g., 葛飾 気象).            |
| **Tags in English**                   | Space-separated tags (e.g., MinatoCity).          |

---

## `post.csv`

This file contains the account information needed to post updates to Bluesky Social.

### Columns:

| Column Name                 | Description                            |
|-----------------------------|----------------------------------------|
| **Account**                 | Matches an account entry in `area.csv`. |
| **User Name of Bluesky Social** | The Bluesky Social username.        |
| **Password of Bluesky Social**   | The Bluesky Social password.        |
