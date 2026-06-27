#!/usr/bin/python3
import requests
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import email.utils
import os
import feedparser
import syslog
import time
import csv
import xml.etree.ElementTree as ET
import re
import multiprocessing
from atproto import Client, client_utils, models

DEBUG = 0
DELAY_START = 20	# 処理開始を20秒待つ
LOCK_TIMEOUT = 540	# ロックのタイムアウト
URL_JMA_PULL = 'https://www.data.jma.go.jp/developer/xml/feed/extra.xml'
XML_BASE = '{http://xml.kishou.go.jp/jmaxml1/}'
ITEM_TITLE = '気象特別警報・警報・注意報'            # VPWW53 旧形式（移行期間中〜2028年頃）
ITEM_TITLE_R06 = '気象警報・注意報（Ｒ０６）'          # VPWW55-61 新形式プレフィックス（確認済）
                                                        # 実タイトル例: '気象警報・注意報（Ｒ０６）（大雨）'
WARNING_TYPE = '気象警報・注意報（市町村等）'           # VPWW54 旧形式
WARNING_TYPE_R06 = '気象警報・注意報（Ｒ０６）（市町村等）' # VPWW55-61 新形式（要確認）
# ─────────────────────────────────────────────────────────────────────────────
# USE_LEGACY_FEED: フィード処理モードの切り替えフラグ
#   True  … VPWW53/54 旧形式のみ処理（2026年5月29日より前）
#   False … VPWW55-61 新形式のみ処理（2026年5月29日以降に False へ変更）
#            ※ False にすると危険警報(L4)が捕捉できるようになる
#            ※ True のままでは旧形式と新形式が二重処理され誤投稿の恐れあり
# ─────────────────────────────────────────────────────────────────────────────
USE_LEGACY_FEED = False
# ─────────────────────────────────────────────────────────────────────────────
# VPWW55-61 電文種別ごとの担当コードマッピング
# 各電文は自担当カテゴリのコードのみ記載するため、別電文担当コードの
# 誤解除・誤遷移検知を防ぐために使用する（USE_LEGACY_FEED=False 時のみ参照）
# ─────────────────────────────────────────────────────────────────────────────
VPWW_RESPONSIBLE = {
    'VPWW55': {'wa': {'10'},               'ww': {'03'},       'wuw': {'43'},       'wew': {'33'}},
    'VPWW56': {'wa': {'29'},               'ww': {'09'},       'wuw': {'49'},       'wew': set()},
    'VPWW57': {'wa': {'19'},               'ww': {'08'},       'wuw': {'48'},       'wew': {'38'}},
    'VPWW58': {'wa': {'13', '15'},         'ww': {'02', '05'}, 'wuw': set(),        'wew': {'32', '35'}},
    'VPWW59': {'wa': {'16'},               'ww': {'07'},       'wuw': set(),        'wew': {'37'}},
    'VPWW60': {'wa': {'12', '13'},         'ww': {'02', '06'}, 'wuw': set(),        'wew': {'32', '36'}},
    'VPWW61': {'wa': {'14', '17', '18', '20', '21', '22', '23', '24', '25', '26', '27'},
               'ww': set(), 'wuw': set(), 'wew': set()},
}
NO_CHANGESTATUS = '変化無'
FORM_URL_JMA_WARNING = 'https://www.jma.go.jp/bosai/warning/#area_type=class20s&area_code={}&lang={}'
BASE_DIR = '/usr/local/emerry/jma/'
LAST_DIR = BASE_DIR + 'last/'
LOCK_FILE = LAST_DIR + "lock"
AREA_CSV = BASE_DIR + 'area.csv'
POST_CSV = BASE_DIR + 'post.csv'
LAST_MODIFIED = LAST_DIR + "last_modified"
POST_RETRY = 3
POST_INTERVAL = 10
# global variables
pref = {}  # pref_name => hash (key=area_code)
area = {}  # area_code => [acct_wa, acct_ww, acct_wuw, acct_wew]
acct_area = {}  # acct => {'lang': 'ja'/'en', 'code': area_code, 'name': pref_name, 'grade': '注意報(Level 2)'/..., 'tag': hash_tag}
post_acct = {}	# acct => { 'bs_username': bs_username, 'bs_passwd': bs_passwd }

def check_last_modified():
    last_modified = 0

    try:
        with open(LAST_MODIFIED, "r") as file:
            last_modified = int(file.read())
            syslog.syslog(syslog.LOG_INFO, f"last last_modified={last_modified}")
    except (FileNotFoundError, IOError) as e:
        syslog.syslog(syslog.LOG_WARNING, f"File '{LAST_MODIFIED}': {e}")

    response = requests.head(URL_JMA_PULL)
    if 'Last-Modified' in response.headers:
        cur_last_modified_str = response.headers['Last-Modified']
        cur_last_modified = int(email.utils.parsedate_to_datetime(cur_last_modified_str).timestamp())
        syslog.syslog(syslog.LOG_INFO, f"current last_modified={cur_last_modified}")
    else:
        syslog.syslog(syslog.LOG_ERR, f"No Last-Modified field in header.")

    if cur_last_modified <= last_modified:
        syslog.syslog(syslog.LOG_INFO, "NO-UPDATE by Last-Modified.")
        return False

    try:
        with open(LAST_MODIFIED, "w") as file:
            file.write(str(cur_last_modified) + "\n")
    except (FileNotFoundError, IOError) as e:
        syslog.syslog(syslog.LOG_ERR, f": File '{LAST_MODIFIED}': {e}")

    return True

def read_area():
    # read area.csv
    try:
        with open(AREA_CSV, 'r', encoding='utf-8') as conf:
            pref_t = {}
            for line in conf:
                line = line.strip()
                if line.startswith('PARAM'):
                    continue
                parts = line.split(',')
                # 新形式(15列): acct_wuw / acct_ewuw を追加
                # 旧形式(13列): acct_wuw / acct_ewuw は空文字列（後方互換）
                if len(parts) >= 15:
                    a_code, name, name_e, p_pron, p_name, acct_wa, acct_ww, acct_wuw, acct_wew, acct_ewa, acct_eww, acct_ewuw, acct_ewew, tag, tag_e = parts[0:15]
                else:
                    a_code, name, name_e, p_pron, p_name, acct_wa, acct_ww, acct_wew, acct_ewa, acct_eww, acct_ewew, tag, tag_e = parts[0:13]
                    acct_wuw = ''
                    acct_ewuw = ''

                # area: [ja注意報, ja警報, ja危険警報, ja特別警報, en注意報, en警報, en危険警報, en特別警報]
                tmp = [acct_wa, acct_ww, acct_wuw, acct_wew, acct_ewa, acct_eww, acct_ewuw, acct_ewew]
                pref_t.setdefault(p_name, []).append(a_code)
                area[a_code] = tmp
                # acct_area
                acct_area[acct_wa]  = {'lang': 'ja', 'code': a_code, 'name': name,   'grade': '注意報',  'tag': tag}
                acct_area[acct_ww]  = {'lang': 'ja', 'code': a_code, 'name': name,   'grade': '警報',    'tag': tag}
                if acct_wuw:
                    acct_area[acct_wuw] = {'lang': 'ja', 'code': a_code, 'name': name, 'grade': '危険警報', 'tag': tag}
                acct_area[acct_wew] = {'lang': 'ja', 'code': a_code, 'name': name,   'grade': '特別警報', 'tag': tag}
                acct_area[acct_ewa] = {'lang': 'en', 'code': a_code, 'name': name_e, 'grade': 'Advisory',        'tag': tag_e}
                acct_area[acct_eww] = {'lang': 'en', 'code': a_code, 'name': name_e, 'grade': 'Warning',         'tag': tag_e}
                if acct_ewuw:
                    acct_area[acct_ewuw] = {'lang': 'en', 'code': a_code, 'name': name_e, 'grade': 'Urgent Warning', 'tag': tag_e}
                acct_area[acct_ewew]= {'lang': 'en', 'code': a_code, 'name': name_e, 'grade': 'Emergency Warning', 'tag': tag_e}
    except (FileNotFoundError, IOError) as e:
        syslog.syslog(syslog.LOG_ERR, f": File {AREA_CSV}: {e}")

    # make pref dictionary
    for p_name, area_codes in pref_t.items():
        hash_t = {}
        for a_code in area_codes:
            hash_t[a_code] = 1
        pref[p_name] = hash_t


def read_bs():
    # read bs.csv
    try:
        with open(POST_CSV, 'r', encoding='utf-8') as conf:
            for line in conf:
                line = line.strip()
                parts = line.split(',')
                post_acct[parts[0]] = { 'bs_username': parts[1], 'bs_passwd': parts[2] }
    except (FileNotFoundError, IOError) as e:
        syslog.syslog(syslog.LOG_ERR, f": File {POST_CSV}: {e}")

# def check(feed)
#
# return: ref to array of matched links
#
def check(feed):
    links = {}

    for item in feed.entries:
        # USE_LEGACY_FEED=True : VPWW53/54 旧形式のみ処理
        # USE_LEGACY_FEED=False: VPWW55-61 新形式のみ処理（2026年5月29日以降）
        if USE_LEGACY_FEED:
            if item.title != ITEM_TITLE:
                continue
        else:
            if not item.title.startswith(ITEM_TITLE_R06):
                continue
        for p_name, area_codes in pref.items():
            for a_code in area_codes:
                if p_name in item.description:
#                if True:				# DEBUG
                    links[item.link] = pref[p_name]
                    break
    return links

# def read_last(area_code)
# return ref_last { wa => { code => 1,...}, ww => {...}, wuw => {...}, wew => {...}}
# 新形式(5行): wa, ww, wuw, wew, time
# 旧形式(4行): wa, ww, wew, time  ← 後方互換
def read_last(area_code):
    ref_last = {'wa': {}, 'ww': {}, 'wuw': {}, 'wew': {}}

    try:
        with open(f"{LAST_DIR}{area_code}", 'r') as f:
            lines = f.readlines()

        if len(lines) >= 5:
            # 新形式: wa, ww, wuw, wew, time
            keys_order = ['wa', 'ww', 'wuw', 'wew']
            time_idx = 4
        else:
            # 旧形式: wa, ww, wew, time（wuw は空のまま）
            keys_order = ['wa', 'ww', 'wew']
            time_idx = 3

        for i, k in enumerate(keys_order):
            line = lines[i].strip() if i < len(lines) else ''
            if line:
                ref_last[k] = {code: '' for code in line.split(',')}

        try:
            ref_last['time'] = int(lines[time_idx].strip()) if time_idx < len(lines) else 0
        except (ValueError, IndexError):
            ref_last['time'] = 0
    except FileNotFoundError:
        pass

    return ref_last

# def write_last(area_code, ref_wa, ref_ww, ref_wuw, ref_wew, report_time)
# 新形式(5行): wa, ww, wuw, wew, time
def write_last(area_code, ref_wa, ref_ww, ref_wuw, ref_wew, report_time):
    if not os.path.isdir(LAST_DIR):
        os.mkdir(LAST_DIR, mode=0o755)

    try:
        with open(f"{LAST_DIR}{area_code}", 'w') as f:
            for ref in [ref_wa, ref_ww, ref_wuw, ref_wew]:
                codes = [code for code in ref if not re.search(r'(解除|に切り替え|なし|へ変化)', ref[code])]
                f.write(','.join(codes) + '\n')
            f.write(f"{report_time:d}" + '\n')
    except IOError:
        syslog.syslog(syslog.LOG_ERR, f"Can't open file {LAST_DIR}{area_code}")

    return

code_kind = {
    # 警報 (コード02-09)
    '02': '暴風雪',
    '03': '大雨',
    '04': '洪水',       # ※2026年5月廃止（VPWW54移行期間中は継続）
    '05': '暴風',
    '06': '大雪',
    '07': '波浪',
    '08': '高潮',
    '09': '土砂災害',   # ※2026年5月新設（レベル３土砂災害警報）
    # 注意報 (コード10-29)
    '10': '大雨',
    '12': '大雪',
    '13': '風雪',
    '14': '雷',
    '15': '強風',
    '16': '波浪',
    '17': '融雪',
    '18': '洪水',       # ※2026年5月廃止（VPWW54移行期間中は継続）
    '19': '高潮',
    '20': '濃霧',
    '21': '乾燥',
    '22': 'なだれ',
    '23': '低温',
    '24': '霜',
    '25': '着氷',
    '26': '着雪',
    '27': 'その他の',
    '29': '土砂災害',   # ※2026年5月新設（レベル２土砂災害注意報）
    # 特別警報 (コード30-39)
    '32': '暴風雪',
    '33': '大雨',
    '35': '暴風',
    '36': '大雪',
    '37': '波浪',
    '38': '高潮',
    # 危険警報 (コード40-49) ※2026年5月新設
    '40': '氾濫',       # レベル４氾濫危険警報（VXKOii 指定河川洪水予報）
    '43': '大雨',       # レベル４大雨危険警報
    '48': '高潮',       # レベル４高潮危険警報
    '49': '土砂災害',   # レベル４土砂災害危険警報
}

code_kind_e = {
    # Warning (code 02-09)
    '02': 'Snow-storm',
    '03': 'Heavy rain',
    '04': 'Flood',          # Abolished May 2026 (kept for VPWW54 transition)
    '05': 'Storm',
    '06': 'Heavy snow',
    '07': 'High waves',
    '08': 'Storm surge',
    '09': 'Landslide',      # New May 2026 (Level 3 Landslide Warning)
    # Advisory (code 10-29)
    '10': 'Heavy rain',
    '12': 'Heavy snow',
    '13': 'Gale and snow',
    '14': 'Thunderstorm',
    '15': 'Gale',
    '16': 'High waves',
    '17': 'Snow melting',
    '18': 'Flood',          # Abolished May 2026 (kept for VPWW54 transition)
    '19': 'Storm surge',
    '20': 'Dense fog',
    '21': 'Dry air',
    '22': 'Avalanche',
    '23': 'Low temperature',
    '24': 'Frost',
    '25': 'Ice accretion',
    '26': 'Snow accretion',
    '27': 'Other',
    '29': 'Landslide',      # New May 2026 (Level 2 Landslide Advisory)
    # Emergency Warning (code 30-39)
    '32': 'Snow-storm',
    '33': 'Heavy rain',
    '35': 'Storm',
    '36': 'Heavy snow',
    '37': 'High waves',
    '38': 'Storm surge',
    # Urgent Warning (code 40-49) — New May 2026
    '40': 'Flooding',       # Level 4 Flood Urgent Warning (VXKOii)
    '43': 'Heavy rain',     # Level 4 Heavy Rain Urgent Warning
    '48': 'Storm surge',    # Level 4 Storm Surge Urgent Warning
    '49': 'Landslide',      # Level 4 Landslide Urgent Warning
}

# ── 隣接1段階の遷移 ──────────────────────────────────────────────
# 警報(ww) ↔ 注意報(wa)
ww_wa = {
    '03': '10',   # 大雨警報     → 大雨注意報
    '06': '12',   # 大雪警報     → 大雪注意報
    '02': '13',   # 暴風雪警報   → 風雪注意報
    '05': '15',   # 暴風警報     → 強風注意報
    '07': '16',   # 波浪警報     → 波浪注意報
    '04': '18',   # 洪水警報     → 洪水注意報 ※VPWW54移行期間用
    '08': '19',   # 高潮警報     → 高潮注意報
    '09': '29',   # 土砂災害警報 → 土砂災害注意報 ※2026年5月新設
    'status': '解除(注意報へ)',
    'rstatus': '注意報から警報'
}

wa_ww = {
    '10': '03',
    '12': '06',
    '13': '02',
    '15': '05',
    '16': '07',
    '18': '04',   # ※VPWW54移行期間用
    '19': '08',
    '29': '09',   # ※2026年5月新設
    'status': '警報へ変化',
    'rstatus': '警報から注意報'
}

# 危険警報(wuw) ↔ 警報(ww) ※2026年5月新設
ww_wuw = {
    '03': '43',   # 大雨警報     → 大雨危険警報
    '08': '48',   # 高潮警報     → 高潮危険警報
    '09': '49',   # 土砂災害警報 → 土砂災害危険警報
    'status': '危険警報へ変化',
    'rstatus': '危険警報から警報'
}

wuw_ww = {
    '43': '03',
    '48': '08',
    '49': '09',
    'status': '解除(警報へ)',
    'rstatus': '警報から危険警報'
}

# 特別警報(wew) ↔ 危険警報(wuw) ※2026年5月新設
wuw_wew = {
    '43': '33',   # 大雨危険警報 → 大雨特別警報
    '48': '38',   # 高潮危険警報 → 高潮特別警報
    'status': '特別警報へ変化',
    'rstatus': '特別警報から危険警報'
}

wew_wuw = {
    '33': '43',
    '38': '48',
    'status': '危険警報に切り替え',
    'rstatus': '危険警報から特別警報'
}

# ── 2段階スキップの遷移 ────────────────────────────────────────────
# 危険警報(wuw) ↔ 注意報(wa)
wa_wuw = {
    '10': '43',   # 大雨注意報     → 大雨危険警報
    '19': '48',   # 高潮注意報     → 高潮危険警報
    '29': '49',   # 土砂災害注意報 → 土砂災害危険警報
    'status': '危険警報へ変化',
    'rstatus': '危険警報から注意報'
}

wuw_wa = {
    '43': '10',
    '48': '19',
    '49': '29',
    'status': '解除(注意報へ)',
    'rstatus': '注意報から危険警報'
}

# 特別警報(wew) ↔ 警報(ww)（wuwをスキップ）
wew_ww = {
    '33': '03',
    '36': '06',
    '32': '02',
    '35': '05',
    '37': '07',
    '38': '08',
    'status': '警報に切り替え',
    'rstatus': '警報から特別警報'
}

ww_wew = {
    '03': '33',
    '06': '36',
    '02': '32',
    '05': '35',
    '07': '37',
    '08': '38',
    'status': '特別警報へ変化',
    'rstatus': '特別警報から警報'
}

# ── 3段階スキップの遷移 ────────────────────────────────────────────
# 特別警報(wew) ↔ 注意報(wa)
wew_wa = {
    '33': '10',
    '36': '12',
    '32': '13',
    '35': '15',
    '37': '16',
    '38': '19',
    'status': '注意報に切り替え',
    'rstatus': '注意報から特別警報'
}

wa_wew = {
    '10': '33',
    '12': '36',
    '13': '32',
    '15': '35',
    '16': '37',
    '19': '38',
    'status': '特別警報へ変化',
    'rstatus': '特別警報から注意報'
}

status_ja_en = {
    '発表': 'Announcement',
    '継続': 'Continuation',
    # 特別警報 ↔ 危険警報 ※2026年5月新設
    '特別警報から危険警報': 'Emergency Warning to Urgent Warning',
    '危険警報から特別警報': 'Urgent Warning to Emergency Warning',
    # 特別警報 ↔ 警報
    '特別警報から警報': 'Emergency Warning to Warning',
    '警報から特別警報': 'Warning to Emergency Warning',
    # 特別警報 ↔ 注意報
    '特別警報から注意報': 'Emergency Warning to Advisory',
    '注意報から特別警報': 'Advisory to Emergency Warning',
    # 危険警報 ↔ 警報 ※2026年5月新設
    '危険警報から警報': 'Urgent Warning to Warning',
    '警報から危険警報': 'Warning to Urgent Warning',
    # 危険警報 ↔ 注意報 ※2026年5月新設
    '危険警報から注意報': 'Urgent Warning to Advisory',
    '注意報から危険警報': 'Advisory to Urgent Warning',
    # 警報 ↔ 注意報
    '警報から注意報': 'Warning to Advisory',
    '注意報から警報': 'Advisory to Warning',
    # 解除・切り替え
    '解除': 'Cancel',
    'なし': 'None',
    '解除(注意報へ)': 'Cancel(to Advisory)',
    '解除(警報へ)': 'Cancel(to Warning)',
    '注意報に切り替え': 'Cancel(to Advisory)',
    '警報に切り替え': 'Cancel(to Warning)',
    '危険警報に切り替え': 'Cancel(to Urgent Warning)',  # ※2026年5月新設
    # 昇格
    '警報へ変化': 'Change to Warning',
    '危険警報へ変化': 'Change to Urgent Warning',        # ※2026年5月新設
    '特別警報へ変化': 'Change to Emergency Warning',
}

def find_element_by_tag(data, tag_list):
    dat = data
    for tag in tag_list:
        for element in dat.iter():
            if re.search(rf"[^A-Za-z\d]{tag}$", element.tag):
                dat = element
                break
        if dat == None:
            return None
    return dat

def find_element_list_by_tag(data, tag):
    element_list = []
    for element in data.iter():
        if re.search(rf"[^A-Za-z\d]{tag}$", element.tag):
            element_list.append(element)
    return element_list

# 戻り
#	公式な発表時刻,
#	-> {
#	    Twitterアカウント -> {
#			"KindCode"=>"KindName,KindStatus",
#				:
#		]
#	}
#

def extract_vpww_type(url):
    """URLからVPWW電文種別を抽出する（例: 'VPWW58'）。不明な場合は None を返す。"""
    m = re.search(r'_(VPWW\d+)_', url)
    return m.group(1) if m else None


def collect_xml(url, ref_area):
    """XMLを解析し、{area_code_text: (report_time, current_state)} を返す。
    report_time が前回処理済みのエリアは除外する（軽量スキップ）。
    """
    response = requests.get(url)
    if response.status_code != 200:
        syslog.syslog(syslog.LOG_ERR, f"Failed to fetch XML from {url}. Status code: {response.status_code}")
        return {}

    root = ET.fromstring(response.content)
    report_datetime = find_element_by_tag(root, ['Report', 'Head', 'ReportDateTime'])
    body            = find_element_by_tag(root, ['Report', 'Body'])
    warning         = find_element_list_by_tag(body, 'Warning')

    report_time = 0
    match = re.match(r'^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\+09:00$', report_datetime.text)
    if match:
        year, month, day, hour, minute, second = map(int, match.groups())
        report_time = int(datetime(year, month, day, hour, minute, second).timestamp())

    result = {}
    accepted_types = {WARNING_TYPE} if USE_LEGACY_FEED else {WARNING_TYPE, WARNING_TYPE_R06}

    for warn_elem in warning:
        if warn_elem.get('type') not in accepted_types:
            continue
        for item_elem in find_element_list_by_tag(warn_elem, 'Item'):
            area_code_text = find_element_by_tag(item_elem, ['Area', 'Code']).text
            if area_code_text not in ref_area:
                continue

            # 古い report_time はスキップ（旧形式では同一タイムスタンプもスキップ）
            ref_last_t = read_last(int(area_code_text))
            if 'time' in ref_last_t:
                if report_time < ref_last_t['time']:
                    continue
                if report_time == ref_last_t['time'] and USE_LEGACY_FEED:
                    continue

            current = {'wa': {}, 'ww': {}, 'wuw': {}, 'wew': {}}
            for kind_elem in find_element_list_by_tag(item_elem, 'Kind'):
                try:
                    kind_elem_code   = int(find_element_by_tag(kind_elem, ['Code']).text)
                    kind_elem_status = find_element_by_tag(kind_elem, ['Status']).text
                    condition        = find_element_by_tag(kind_elem, ['Condition']).text.strip()
                except:
                    continue
                if kind_elem_status == '解除':
                    continue
                code_str = f"{kind_elem_code:02d}"
                if   10 <= kind_elem_code < 30: current['wa' ][code_str] = condition
                elif  2 <= kind_elem_code < 10: current['ww' ][code_str] = condition
                elif 40 <= kind_elem_code < 50: current['wuw'][code_str] = condition
                elif 30 <= kind_elem_code < 40: current['wew'][code_str] = condition

            result[area_code_text] = (report_time, current)

    return result


def compare_and_post(area_code_text, report_time, current, responsible, ref_last):
    """集約された current を ref_last と比較し、変化があれば last を更新して acct dict を返す。

    responsible: {wa, ww, wuw, wew} それぞれの担当コード set（新形式時）
                 None = 旧形式（全コードが担当対象）
    """
    wa = {}; ww = {}; wuw = {}; wew = {}
    ewa = {}; eww = {}; ewuw = {}; ewew = {}
    f_wa = 0; f_ww = 0; f_wuw = 0; f_wew = 0

    # 種別のループ（wa=注意報L2, ww=警報L3, wuw=危険警報L4, wew=特別警報L5）
    for kind_str in ['wa', 'ww', 'wuw', 'wew']:
        code_min       = {'wa': 10, 'ww':  2, 'wuw': 40, 'wew': 30}[kind_str]
        code_max       = {'wa': 29, 'ww':  9, 'wuw': 49, 'wew': 39}[kind_str]
        kind_down_str  = {'wa': None, 'ww': 'wa',  'wuw': 'ww',  'wew': 'wuw'}[kind_str]
        kind_down2_str = {'wa': None, 'ww': None,  'wuw': 'wa',  'wew': 'ww' }[kind_str]
        kind_down3_str = {'wa': None, 'ww': None,  'wuw': None,  'wew': 'wa' }[kind_str]
        kind_up_str    = {'wa': 'ww', 'ww': 'wuw', 'wuw': 'wew', 'wew': None}[kind_str]
        kind_up2_str   = {'wa': 'wuw','ww': 'wew', 'wuw': None,  'wew': None}[kind_str]
        kind_up3_str   = {'wa': 'wew','ww': None,  'wuw': None,  'wew': None}[kind_str]
        ref_to_down    = {'wa': {},    'ww': ww_wa,  'wuw': wuw_ww,  'wew': wew_wuw}[kind_str]
        ref_to_down2   = {'wa': {},    'ww': {},     'wuw': wuw_wa,  'wew': wew_ww }[kind_str]
        ref_to_down3   = {'wa': {},    'ww': {},     'wuw': {},      'wew': wew_wa }[kind_str]
        ref_to_up      = {'wa': wa_ww, 'ww': ww_wuw, 'wuw': wuw_wew,'wew': {}     }[kind_str]
        ref_to_up2     = {'wa': wa_wuw,'ww': ww_wew, 'wuw': {},     'wew': {}     }[kind_str]
        ref_to_up3     = {'wa': wa_wew,'ww': {},     'wuw': {},     'wew': {}     }[kind_str]
        ref_kind_out   = {'wa': wa,  'ww': ww,  'wuw': wuw, 'wew': wew }[kind_str]
        ref_kind_out_e = {'wa': ewa, 'ww': eww, 'wuw': ewuw,'wew': ewew}[kind_str]

        for c in range(code_min, code_max + 1):
            code = f"{c:02d}"
            if code in ref_last.get(kind_str, {}):
                # 前回このコードが発表されていた
                if code in current[kind_str]:
                    status = '継続'
                else:
                    # 担当外コードは現状維持（別電文が管理）
                    # responsible=None の旧形式では全コードが担当対象
                    if responsible is not None and code not in responsible[kind_str]:
                        ref_kind_out[code]   = f"{code_kind.get(code, code)},継続"
                        ref_kind_out_e[code] = f"{code_kind_e.get(code, code)},{status_ja_en['継続']}"
                        continue
                    # 遷移先を特定（昇格→降格の順で確認）
                    key   = ref_to_up.get(code)    if ref_to_up   else None
                    key2  = ref_to_up2.get(code)   if ref_to_up2  else None
                    key3  = ref_to_up3.get(code)   if ref_to_up3  else None
                    keyd  = ref_to_down.get(code)  if ref_to_down  else None
                    keyd2 = ref_to_down2.get(code) if ref_to_down2 else None
                    keyd3 = ref_to_down3.get(code) if ref_to_down3 else None
                    if   kind_up_str   and current.get(kind_up_str)   and key   is not None and key   in current[kind_up_str]:
                        status = ref_to_up['status']
                    elif kind_up2_str  and current.get(kind_up2_str)  and key2  is not None and key2  in current[kind_up2_str]:
                        status = ref_to_up2['status']
                    elif kind_up3_str  and current.get(kind_up3_str)  and key3  is not None and key3  in current[kind_up3_str]:
                        status = ref_to_up3['status']
                    elif kind_down_str  and current.get(kind_down_str)  and keyd  is not None and keyd  in current[kind_down_str]:
                        status = ref_to_down['status']
                    elif kind_down2_str and current.get(kind_down2_str) and keyd2 is not None and keyd2 in current[kind_down2_str]:
                        status = ref_to_down2['status']
                    elif kind_down3_str and current.get(kind_down3_str) and keyd3 is not None and keyd3 in current[kind_down3_str]:
                        status = ref_to_down3['status']
                    else:
                        status = '解除'
                    if   kind_str == 'wa':  f_wa  = 1
                    elif kind_str == 'ww':  f_ww  = 1
                    elif kind_str == 'wuw': f_wuw = 1
                    elif kind_str == 'wew': f_wew = 1
                ref_kind_out[code]   = f"{code_kind[code]}{current[kind_str].get(code, '')},{status}"
                ref_kind_out_e[code] = f"{code_kind_e[code]},{status_ja_en[status]}"
            elif code in current[kind_str]:
                # 今回新たにこのコードが発表された → 遷移元を特定
                key   = ref_to_up.get(code)    if ref_to_up   else None
                key2  = ref_to_up2.get(code)   if ref_to_up2  else None
                key3  = ref_to_up3.get(code)   if ref_to_up3  else None
                keyd  = ref_to_down.get(code)  if ref_to_down  else None
                keyd2 = ref_to_down2.get(code) if ref_to_down2 else None
                keyd3 = ref_to_down3.get(code) if ref_to_down3 else None
                if   kind_up_str   and ref_last.get(kind_up_str,   {}) and key   is not None and key   in ref_last[kind_up_str]:
                    status = ref_to_up['rstatus']
                elif kind_up2_str  and ref_last.get(kind_up2_str,  {}) and key2  is not None and key2  in ref_last[kind_up2_str]:
                    status = ref_to_up2['rstatus']
                elif kind_up3_str  and ref_last.get(kind_up3_str,  {}) and key3  is not None and key3  in ref_last[kind_up3_str]:
                    status = ref_to_up3['rstatus']
                elif kind_down_str  and ref_last.get(kind_down_str,  {}) and keyd  is not None and keyd  in ref_last[kind_down_str]:
                    status = ref_to_down['rstatus']
                elif kind_down2_str and ref_last.get(kind_down2_str, {}) and keyd2 is not None and keyd2 in ref_last[kind_down2_str]:
                    status = ref_to_down2['rstatus']
                elif kind_down3_str and ref_last.get(kind_down3_str, {}) and keyd3 is not None and keyd3 in ref_last[kind_down3_str]:
                    status = ref_to_down3['rstatus']
                else:
                    status = '発表'
                if   kind_str == 'wa':  f_wa  = 1
                elif kind_str == 'ww':  f_ww  = 1
                elif kind_str == 'wuw': f_wuw = 1
                elif kind_str == 'wew': f_wew = 1
                ref_kind_out[code]   = f"{code_kind[code]}{current[kind_str][code]},{status}"
                ref_kind_out_e[code] = f"{code_kind_e[code]},{status_ja_en[status]}"

    # area[area_code_text] = [ja_wa, ja_ww, ja_wuw, ja_wew, en_wa, en_ww, en_wuw, en_wew]
    acct = {}
    if f_wa:
        if wa  and area[area_code_text][0]: acct[area[area_code_text][0]] = wa
        if ewa and area[area_code_text][4]: acct[area[area_code_text][4]] = ewa
    if f_ww:
        if ww  and area[area_code_text][1]: acct[area[area_code_text][1]] = ww
        if eww and area[area_code_text][5]: acct[area[area_code_text][5]] = eww
    if f_wuw:
        if wuw and area[area_code_text][2]: acct[area[area_code_text][2]] = wuw
        if ewuw and area[area_code_text][6]: acct[area[area_code_text][6]] = ewuw
    if f_wew:
        if wew and area[area_code_text][3]: acct[area[area_code_text][3]] = wew
        if ewew and area[area_code_text][7]: acct[area[area_code_text][7]] = ewew
    if f_wa or f_ww or f_wuw or f_wew:
        write_last(area_code_text, wa, ww, wuw, wew, report_time)
    return acct

def post_bs(mssg, lang, username, passwd):
    try:
        client = Client()
        client.login(username, passwd)
        resp = client.send_post(mssg, langs=[ lang ])
    except Exception as e:
        syslog.syslog(syslog.LOG_ERR, f"Failed post to Bluesky: mssg='{mssg}', response={e}")
        return 1
    return 0

# post_by_acct(report_datetime, account_kind, ref_acct_kind[account_kind])
#
# description - make message and post, error handling
STATUS_KEY = {
    '発表': 1,
    'Announcement': 1,
    '継続': 101,
    'Continuation': 101,
    # 降格（高い順）
    '特別警報から危険警報': 2,              # L5→L4 ※2026年5月新設
    'Emergency Warning to Urgent Warning': 2,
    '特別警報から警報': 3,                  # L5→L3
    'Emergency Warning to Warning': 3,
    '特別警報から注意報': 4,                # L5→L2
    'Emergency Warning to Advisory': 4,
    '危険警報から警報': 5,                  # L4→L3 ※2026年5月新設
    'Urgent Warning to Warning': 5,
    '危険警報から注意報': 6,                # L4→L2 ※2026年5月新設
    'Urgent Warning to Advisory': 6,
    '警報から注意報': 7,                    # L3→L2
    'Warning to Advisory': 7,
    # 昇格（低い順）
    '警報へ変化': 8,                        # L2→L3
    'Change to Warning': 8,
    '注意報から警報': 8,                    # rstatus: 警報が注意報から昇格
    'Advisory to Warning': 8,
    '危険警報へ変化': 9,                    # →L4 ※2026年5月新設
    'Change to Urgent Warning': 9,
    '注意報から危険警報': 9,               # rstatus: 危険警報が注意報から昇格 ※2026年5月新設
    'Advisory to Urgent Warning': 9,
    '警報から危険警報': 9,                  # rstatus: 危険警報が警報から昇格 ※2026年5月新設
    'Warning to Urgent Warning': 9,
    '特別警報へ変化': 10,                   # →L5
    'Change to Emergency Warning': 10,
    '注意報から特別警報': 10,              # rstatus: 特別警報が注意報から昇格
    'Advisory to Emergency Warning': 10,
    '警報から特別警報': 10,                 # rstatus: 特別警報が警報から昇格
    'Warning to Emergency Warning': 10,
    '危険警報から特別警報': 10,             # rstatus: 特別警報が危険警報から昇格 ※2026年5月新設
    'Urgent Warning to Emergency Warning': 10,
    # 解除・切り替え
    '解除': 11,
    'Cancel': 11,
    '解除(注意報へ)': 12,
    'Cancel(to Advisory)': 12,
    '注意報に切り替え': 12,
    '危険警報に切り替え': 12,               # ※2026年5月新設
    'Cancel(to Urgent Warning)': 12,
    '解除(警報へ)': 13,
    'Cancel(to Warning)': 13,
    '警報に切り替え': 13,
    '発表警報・注意報はなし': -1,
    'なし': -1,
    'None': -1
}
# grade → 警戒レベル表記の対応
GRADE_LEVEL = {
    '注意報': 'Level 2',
    '警報': 'Level 3',
    '危険警報': 'Level 4',
    '特別警報': 'Level 5',
    'Advisory': 'Level 2',
    'Warning': 'Level 3',
    'Urgent Warning': 'Level 4',
    'Emergency Warning': 'Level 5',
}
# ステータス文字列中の等級名 → area[] のアカウント列インデックス（ja=0..3 / en は +4）
GRADE_INDEX_JA = {'注意報': 0, '警報': 1, '危険警報': 2, '特別警報': 3}
GRADE_INDEX_EN = {'Advisory': 0, 'Warning': 1, 'Urgent Warning': 2, 'Emergency Warning': 3}
# 等級名の抽出（長い名称を優先して部分一致の誤検出を防ぐ）
GRADE_RE_JA = re.compile(r'特別警報|危険警報|警報|注意報')
GRADE_RE_EN = re.compile(r'Emergency Warning|Urgent Warning|Advisory|Warning')

def linkify_status(status_text, acct):
    """ステータス文字列を [(text, url_or_None), ...] のセグメント列へ分解する。
    投稿元アカウント以外の等級名（例「警報から注意報」の注意報）を、
    同一地域・該当等級・同一言語のアカウントのプロフィールへの link facet とする。
    """
    ja        = acct_area[acct]['lang'] == 'ja'
    grade_idx = GRADE_INDEX_JA if ja else GRADE_INDEX_EN
    grade_re  = GRADE_RE_JA   if ja else GRADE_RE_EN
    cur_grade = acct_area[acct]['grade']
    accts     = area.get(acct_area[acct]['code'], [])
    base      = 0 if ja else 4

    segs = []
    pos = 0
    for m in grade_re.finditer(status_text):
        token = m.group(0)
        # 投稿元自身の等級（××側）はリンクしない
        if token == cur_grade:
            continue
        idx = base + grade_idx[token]
        target = accts[idx] if idx < len(accts) else ''
        # 該当等級のアカウント未設定時はプレーンのまま
        if not target or target not in post_acct:
            continue
        if m.start() > pos:
            segs.append((status_text[pos:m.start()], None))
        url = f"https://bsky.app/profile/{post_acct[target]['bs_username']}"
        segs.append((token, url))
        pos = m.end()
    if pos < len(status_text):
        segs.append((status_text[pos:], None))
    if not segs:
        segs = [(status_text, None)]
    return segs

def post_by_acct(report_datetime, acct, ref_code_status):
    ja = acct_area[acct]['lang'] == 'ja'

    kind_str = {}
    status = {}
    cnt = 50
    delimiter = '、' if ja else ', '

    for code, value in ref_code_status.items():
        match = re.fullmatch(r'(.*),([^,]+)', value)
        if match:
            name, st = match.groups()
            key = STATUS_KEY.get(st, cnt)
            cnt += 1 if key == cnt else 0
            kind_str[key] = kind_str.get(key, '') + delimiter + name
            status[key] = st

    grade_level = f'{acct_area[acct]["grade"]}({GRADE_LEVEL.get(acct_area[acct]["grade"], "")})'
    # segments: [(text, url_or_None), ...] — url 付きは link facet として組み立てる
    segments = [(f'【{acct_area[acct]["name"]}：{grade_level}】\n' if ja
                 else f'% {acct_area[acct]["name"]} : {grade_level} %\n', None)]

    for k in sorted(status.keys()):
        if k < 0:
            continue
        kind_str[k] = re.sub(r'^[、,]', '', kind_str[k])
        if ja:
            ob, cb = ('《', '》') if k < 10 else ('‥', '‥') if k < 50 else ('｛', '｝')
        else:
            ob, cb = ('[', ']') if k < 10 else ('-', '-') if k < 50 else ('{', '}')
        grade = re.match(r'(.+)へ変化', status[k]) or re.search(r'Change to (.+)$', status[k])
        grade = grade.group(1) if grade else acct_area[acct]['grade']
        # ステータス部のみ等級名をリンク化（前後の装飾・種別名・等級はプレーン）
        segments.append((ob, None))
        segments.extend(linkify_status(status[k], acct))
        if ja:
            segments.append((f'{cb} {kind_str[k]} {grade}\n', None))
        else:
            segments.append((f'{cb} {kind_str[k]}\n', None))

    local_tz = datetime.now().astimezone().tzinfo
    dt = datetime.fromtimestamp(report_datetime, local_tz)
    formatted_time = dt.strftime('%Y-%m-%d %H:%M')
    segments.append((f" ({formatted_time})", None))

    body = ''.join(t for t, _ in segments)

    tb = client_utils.TextBuilder()
    if len(body) > 299:
        # 上限超過時は安全側でリンクを諦めて切り詰める（facet 境界の破損を回避）
        tb = tb.text(body[:299] + '…')
    else:
        for t, u in segments:
            tb = tb.link(t, u) if u else tb.text(t)

    if len(tb.build_text()) + len(acct_area[acct]['tag']) + 3 < 299:
        tb = tb.text(f"\n ")
        for tag in acct_area[acct]["tag"].split(' '):
            tb = tb.tag('#' + tag + ' ', tag)

    mssg = f'\n[気象庁サイトへ]' if ja else f'\n[To JMA site]'
    if len(tb.build_text()) + len(mssg) < 299:
        tb = tb.link(mssg, FORM_URL_JMA_WARNING.format(acct_area[acct]['code'], acct_area[acct]['lang']))

    for _ in range(POST_RETRY):
        result = post_bs(tb, 'ja-JP' if ja else 'en-US', post_acct[acct]['bs_username'], post_acct[acct]['bs_passwd'])
        if result == 0:
            return
        time.sleep(POST_INTERVAL)
    syslog.syslog(syslog.LOG_ERR, f"ERROR: Aborted to post to {acct}.")


### MAIN ###
syslog.syslog(syslog.LOG_INFO, "START")
time.sleep(DELAY_START)

######################
# 排他制御(厳密でない)
if os.path.exists(LOCK_FILE) and (os.stat(LOCK_FILE).st_mtime + LOCK_TIMEOUT > time.time()):
    syslog.syslog(syslog.LOG_ERR, "Aborted by exclusion of lock file.")
    exit(0)

# ロックファイルを作成
with open(LOCK_FILE, "w"):
    pass

if not check_last_modified():
    os.unlink(LOCK_FILE)
    exit(0)
#######################
    
# エリアファイル読込み
read_area()
read_bs()


# 気象庁から随時XMLを取得
feed = feedparser.parse(URL_JMA_PULL)
if not feed:
    syslog.syslog(syslog.LOG_ERR, "atom/rss parse error")
    exit()

# DEBUG: save to data file
if DEBUG:
    t = time.localtime()
    data_file = f'/var/tmp/push_{t.tm_year}{t.tm_mon:02d}{t.tm_mday:02d}{t.tm_hour:02d}{t.tm_min:02d}{t.tm_sec:02d}_{os.getpid()}.debug'
    try:
        with open(data_file, "w", encoding="utf-8") as f:
            f.write("== Feed ==\n")
            f.write(f"Title     : {feed.feed.title or ''}\n")
            f.write(f"Content   : {feed.feed.description or ''}\n")
            f.write(f"Modified  : {feed.feed.modified or ''}\n")
            f.write(f"Copyright : {feed.feed.copyright or ''}\n")
            f.write(f"Link      : {feed.feed.link or ''}\n")
            f.write(f"Lang      : {feed.feed.language or ''}\n")
            for item in feed.entries:
                f.write("-- Entry -\n")
                f.write(f"Title    : {item.title or ''}\n")
                f.write(f"Content  : {item.description or ''}\n")
                f.write(f"Modified : {item.modified or ''}\n")
                f.write(f"Author   : {item.author or ''}\n")
                f.write(f"ID       : {item.guid or ''}\n")
                f.write(f"Link     : {item.link or ''}\n")
            f.write("==========\n")
    except IOError:
        syslog.syslog(syslog.LOG_ERR, f"fail to open data file {data_file}")

ref_links = check(feed)

# ── Phase 1: 全リンクのXMLを解析し (エリア, report_time) ごとに集約 ──────────
# 同一イベントの複数電文（VPWW58/59/61 など同一 report_time）をマージすることで
# 1イベント1投稿を実現する
area_events = {}   # area_code_text -> { report_time -> {'current': ..., 'vpww_types': set()} }

for link, ref_area in ref_links.items():
    syslog.syslog(syslog.LOG_INFO, f"DEBUG: LINK={link}, PARAM={':'.join(ref_area.keys())}")
    vpww_type = extract_vpww_type(link)
    result = collect_xml(link, ref_area)
    for area_code_text, (report_time, current) in result.items():
        area_events.setdefault(area_code_text, {})
        ev = area_events[area_code_text]
        ev.setdefault(report_time, {'current': {'wa': {}, 'ww': {}, 'wuw': {}, 'wew': {}}, 'vpww_types': set()})
        for k in ['wa', 'ww', 'wuw', 'wew']:
            ev[report_time]['current'][k].update(current[k])
        if vpww_type:
            ev[report_time]['vpww_types'].add(vpww_type)

# ── Phase 2: エリアごとに時系列順で比較・投稿 ────────────────────────────────
for area_code_text, events in area_events.items():
    ref_last = read_last(int(area_code_text))
    for report_time in sorted(events.keys()):
        # 古い report_time は改めてスキップ
        if 'time' in ref_last and report_time < ref_last['time']:
            continue

        event     = events[report_time]
        vpww_types = event['vpww_types']

        # 担当コードの集合を構築（新形式のみ）
        # 複数電文の担当コードを合算し「このイベントの担当範囲外」を判定する
        if not USE_LEGACY_FEED and vpww_types:
            responsible = {
                k: set().union(*(VPWW_RESPONSIBLE.get(t, {}).get(k, set()) for t in vpww_types))
                for k in ['wa', 'ww', 'wuw', 'wew']
            }
        else:
            responsible = None  # 旧形式: 全コードが担当対象

        ref_acct = compare_and_post(area_code_text, report_time, event['current'], responsible, ref_last)

        for acct_name, code_status in ref_acct.items():
            syslog.syslog(syslog.LOG_INFO, f"POST {report_time}, {acct_name}, {code_status}")
            if acct_name:
                post_by_acct(report_time, acct_name, code_status)

        if ref_acct:
            ref_last = read_last(int(area_code_text))  # 次の report_time 処理のために更新

#######################
os.unlink(LOCK_FILE)
#######################
syslog.syslog(syslog.LOG_INFO, "END")
exit(0)

### END ###
