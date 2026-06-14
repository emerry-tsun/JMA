#!/usr/bin/python3
import csv
import re
import sys
import time
from atproto import Client, client_utils

# メッセージ中の @handle と URL を検出するための正規表現。
# handle: 英数字・ハイフン・ドットから成り、語境界(直前が行頭または空白等)で始まるもの。
TOKEN_RE = re.compile(
    r'(?P<mention>(?<![\w@.])@[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9])'
    r'|(?P<url>https?://[^\s]+)'
)

POST_CSV = 'post.csv'
POST_RETRY = 3
POST_INTERVAL = 10


def read_post_csv():
    credentials = {}
    try:
        with open(POST_CSV, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) < 3:
                    continue
                account, username, password = row[0], row[1], row[2]
                credentials[account] = {'username': username, 'password': password}
    except (FileNotFoundError, IOError) as e:
        print(f"Error: Cannot read {POST_CSV}: {e}", file=sys.stderr)
        sys.exit(1)
    return credentials


def build_rich_text(client, message):
    # メッセージを @handle / URL / 通常テキストに分割し、TextBuilder を組み立てる。
    # メンションはハンドルを DID に解決して mention facet を付与する。
    # URL は link facet を付与する。解決に失敗した場合はプレーンテキストとして扱う。
    tb = client_utils.TextBuilder()
    pos = 0
    for m in TOKEN_RE.finditer(message):
        if m.start() > pos:
            tb.text(message[pos:m.start()])
        token = m.group(0)
        if m.lastgroup == 'mention':
            handle = token[1:]  # 先頭の '@' を除く
            try:
                did = client.com.atproto.identity.resolve_handle({'handle': handle}).did
                tb.mention(token, did)
            except Exception as e:
                print(f"Warning: Cannot resolve handle '{handle}': {e}", file=sys.stderr)
                tb.text(token)
        else:  # url
            tb.link(token, token)
        pos = m.end()
    if pos < len(message):
        tb.text(message[pos:])
    return tb


def post_message(username, password, message):
    try:
        client = Client()
        client.login(username, password)
        client.send_post(build_rich_text(client, message))
    except Exception as e:
        print(f"Error: Failed to post: {e}", file=sys.stderr)
        return 1
    return 0


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <input_csv>", file=sys.stderr)
        sys.exit(1)

    input_csv = sys.argv[1]
    credentials = read_post_csv()

    try:
        with open(input_csv, 'r', encoding='utf-8') as f:
            reader = csv.reader(f)
            for row in reader:
                if len(row) < 2:
                    continue
                account, message = row[0], row[1]
                if account not in credentials:
                    print(f"Warning: No credentials for account '{account}', skipping.", file=sys.stderr)
                    continue
                username = credentials[account]['username']
                password = credentials[account]['password']
                for attempt in range(POST_RETRY):
                    result = post_message(username, password, message)
                    if result == 0:
                        print(f"Posted to {account}: {message[:50]}{'...' if len(message) > 50 else ''}")
                        break
                    if attempt < POST_RETRY - 1:
                        time.sleep(POST_INTERVAL)
                else:
                    print(f"Error: Aborted posting to '{account}' after {POST_RETRY} attempts.", file=sys.stderr)
    except (FileNotFoundError, IOError) as e:
        print(f"Error: Cannot read {input_csv}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
