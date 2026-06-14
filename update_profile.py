#!/usr/bin/python3
import csv
import sys
import time
from atproto import Client

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


def update_profile(username, password, description):
    try:
        client = Client()
        client.login(username, password)

        # 既存のプロフィールレコードを取得し、avatar/banner/displayName 等を保持する。
        # get_profile はビューを返すだけで avatar/banner の blob 参照を含まないため、
        # レコード本体を get_record で取得して description のみ差し替える。
        try:
            existing = client.com.atproto.repo.get_record({
                'repo': client.me.did,
                'collection': 'app.bsky.actor.profile',
                'rkey': 'self',
            })
            record = existing.value
        except Exception:
            record = None

        if record is not None:
            record.description = description
        else:
            # レコードが存在しない場合は新規作成
            record = {
                '$type': 'app.bsky.actor.profile',
                'description': description,
            }

        client.com.atproto.repo.put_record({
            'repo': client.me.did,
            'collection': 'app.bsky.actor.profile',
            'rkey': 'self',
            'record': record,
        })
    except Exception as e:
        print(f"Error: Failed to update profile: {e}", file=sys.stderr)
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
                account, description = row[0], row[1]
                if account not in credentials:
                    print(f"Warning: No credentials for account '{account}', skipping.", file=sys.stderr)
                    continue
                username = credentials[account]['username']
                password = credentials[account]['password']
                for attempt in range(POST_RETRY):
                    result = update_profile(username, password, description)
                    if result == 0:
                        print(f"Updated profile for {account}: {description[:50]}{'...' if len(description) > 50 else ''}")
                        break
                    if attempt < POST_RETRY - 1:
                        time.sleep(POST_INTERVAL)
                else:
                    print(f"Error: Aborted updating profile for '{account}' after {POST_RETRY} attempts.", file=sys.stderr)
    except (FileNotFoundError, IOError) as e:
        print(f"Error: Cannot read {input_csv}: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
