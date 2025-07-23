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
ITEM_TITLE = '気象特別警報・警報・注意報'
WARNING_TYPE = '気象警報・注意報（市町村等）'
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
area = {}  # area_code => [acct_wa, acct_ww, acct_wew]
acct_area = {}  # acct => {'lang': 'ja'/'en', 'code': area_code, 'name': pref_name, 'grade': '注意報'/..., 'tag': hash_tag}
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
                a_code, name, name_e, p_pron, p_name, acct_wa, acct_ww, acct_wew, acct_ewa, acct_eww, acct_ewew, tag, tag_e = parts[0:13]

                # area
                tmp = [acct_wa, acct_ww, acct_wew, acct_ewa, acct_eww, acct_ewew]
                pref_t.setdefault(p_name, []).append(a_code)
                area[a_code] = tmp
                # acct_area
                acct_area[acct_wa] = {'lang': 'ja', 'code': a_code, 'name': name, 'grade': '注意報', 'tag': tag}
                acct_area[acct_ww] = {'lang': 'ja', 'code': a_code, 'name': name, 'grade': '警報', 'tag': tag}
                acct_area[acct_wew] = {'lang': 'ja', 'code': a_code, 'name': name, 'grade': '特別警報', 'tag': tag}
                acct_area[acct_ewa] = {'lang': 'en', 'code': a_code, 'name': name_e, 'grade': 'Advisory', 'tag': tag_e}
                acct_area[acct_eww] = {'lang': 'en', 'code': a_code, 'name': name_e, 'grade': 'Warning', 'tag': tag_e}
                acct_area[acct_ewew] = {'lang': 'en', 'code': a_code, 'name': name_e, 'grade': 'Emergency Warning', 'tag': tag_e}
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
        if item.title != ITEM_TITLE:
            continue
        for p_name, area_codes in pref.items():
            for a_code in area_codes:
                if p_name in item.description:
#                if True:				# DEBUG
                    links[item.link] = pref[p_name]
                    break
    return links

# def read_last(area_code)
# return ref_last { wa => { code => 1,...}}
def read_last(area_code):
    ref_last = {'wa': {}, 'ww': {}, 'wew': {}}

    try:
        with open(f"{LAST_DIR}{area_code}", 'r') as f:
            for k in ['wa', 'ww', 'wew']:
                line = f.readline().strip()
                tmp = {}
                if line != '':
                    for code in line.split(','):
                        tmp[code] = ''
                ref_last[k] = tmp
            try:
                ref_last['time'] = int(f.readline().strip())
            except ValueError:
                ref_last['time'] = 0
    except FileNotFoundError:
        pass

    return ref_last

# def write_last(area_code, ref_wa, ref_ww, ref_wew)
#
def write_last(area_code, ref_wa, ref_ww, ref_wew, report_time):
    if not os.path.isdir(LAST_DIR):
        os.mkdir(LAST_DIR, mode=0o755)

    try:
        with open(f"{LAST_DIR}{area_code}", 'w') as f:
            for ref in [ref_wa, ref_ww, ref_wew]:
                codes = [code for code in ref if not re.search(r'(解除|に切り替え|なし|へ変化)', ref[code])]
                f.write(','.join(codes) + '\n')
            f.write(f"{report_time:d}" + '\n')
    except IOError:
        syslog.syslog(syslog.LOG_ERR, f"Can't open file {LAST_DIR}{area_code}")

    return

code_kind = {
    '02': '暴風雪',
    '03': '大雨',
    '04': '洪水',
    '05': '暴風',
    '06': '大雪',
    '07': '波浪',
    '08': '高潮',
    '10': '大雨',
    '12': '大雪',
    '13': '風雪',
    '14': '雷',
    '15': '強風',
    '16': '波浪',
    '17': '融雪',
    '18': '洪水',
    '19': '高潮',
    '20': '濃霧',
    '21': '乾燥',
    '22': 'なだれ',
    '23': '低温',
    '24': '霜',
    '25': '着氷',
    '26': '着雪',
    '27': 'その他の',
    '32': '暴風雪',
    '33': '大雨',
    '35': '暴風',
    '36': '大雪',
    '37': '波浪',
    '38': '高潮',
}

code_kind_e = {
    '02': 'Snow-storm',
    '03': 'Heavy rain',
    '04': 'Flood',
    '05': 'Storm',
    '06': 'Heavy snow',
    '07': 'High waves',
    '08': 'Storm surge',
    '10': 'Heavy rain',
    '12': 'Heavy snow',
    '13': 'Gale and snow',
    '14': 'Thunderstorm',
    '15': 'Gale',
    '16': 'High waves',
    '17': 'Snow melting',
    '18': 'Flood',
    '19': 'Storm surge',
    '20': 'Dense fog',
    '21': 'Dry air',
    '22': 'Avalanche',
    '23': 'Low temperature',
    '24': 'Frost',
    '25': 'Ice accretion',
    '26': 'Snow accretion',
    '27': 'Other',
    '32': 'Snow-storm',
    '33': 'Heavy rain',
    '35': 'Storm',
    '36': 'Heavy snow',
    '37': 'High waves',
    '38': 'Storm surge',
}

ww_wa = {
    '03': '10',
    '06': '12',
    '02': '13',
    '05': '15',
    '07': '16',
    '04': '18',
    '08': '19',
    'status': '解除(注意報へ)',
    'rstatus': '注意報から警報'
}

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

wa_ww = {
    '10': '03',
    '12': '06',
    '13': '02',
    '15': '05',
    '16': '07',
    '18': '04',
    '19': '08',
    'status': '警報へ変化',
    'rstatus': '警報から注意報'
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
    '特別警報から警報': 'Emergency Warning to Warning',
    '警報から特別警報': 'Warning to Warning Emergency',
    '特別警報から注意報': 'Emergency Warning to Advisory',
    '注意報から特別警報': 'Advisory to Emergency Warning',
    '警報から注意報': 'Warning to Advisory',
    '注意報から警報': 'Advisory to Warning',
    '解除': 'Cancel',
    'なし': 'None',
    '解除(注意報へ)': 'Cancel(to Advisory)',
    '解除(警報へ)': 'Cancel(to Warning)',
    '注意報に切り替え': 'Cancel(to Advisory)',
    '警報に切り替え': 'Cancel(to Warning)',
    '警報へ変化': 'Change to Warning',
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

def fetch_xml(url, ref_area):
    response = requests.get(url)
    if response.status_code != 200:
        syslog.syslog(syslog.LOG_ERR, f"Failed to fetch XML from {url}. Status code: {response.status_code}")
        return None

    root = ET.fromstring(response.content)
    report_datetime = find_element_by_tag(root, ['Report', 'Head', 'ReportDateTime'])

    body = find_element_by_tag(root, ['Report', 'Body'])

    warning = find_element_list_by_tag(body, 'Warning')

    acct = {}
    report_time = 0

    match = re.match(r'^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})\+09:00$', report_datetime.text)
    if match:
        year, month, day, hour, minute, second = map(int, match.groups())
        report_time = int(datetime(year, month, day, hour, minute, second).timestamp())

    for warn_elem in warning:
        warn_type = warn_elem.get('type')
        if warn_type != WARNING_TYPE:			# 市区町村タイプだけ
            continue
        item = find_element_list_by_tag(warn_elem, 'Item')
        for item_elem in item:				# 市区町村のループ
            #   Kind -> list
            area_code_text = find_element_by_tag(item_elem, ['Area', 'Code']).text

            if area_code_text not in ref_area:
                continue

            area_code = int(area_code_text)
            ref_last = read_last(area_code)		# 前回情報取得

            if 'time' in ref_last:
                if report_time < ref_last['time']:
                    continue
                elif report_time == ref_last['time']:
                    continue

            current = {'wa': {}, 'ww': {}, 'wew': {}}
            kind = find_element_list_by_tag(item_elem, 'Kind')
            for kind_elem in kind:				# 各注意報、警報、特別警報
                try:
                    kind_elem_code = int(find_element_by_tag(kind_elem, ['Code']).text)
                    kind_elem_name = find_element_by_tag(kind_elem, ['Name']).text
                    kind_elem_status = find_element_by_tag(kind_elem, ['Status']).text
                    kind_elem_condition = find_element_by_tag(kind_elem, ['Condition']).text.strip()
                except:							# Codeが空の場合
                    continue
                if kind_elem_status == '解除':
                    continue
                condition = ""						# 付帯条件
                # Chat-GPT
                if isinstance(kind_elem.get('Condition'), list):
                    ref_cond = kind_elem['Condition']
                    condition += '(' + ','.join(ref_cond) + ')'
                else:
                    condition = kind_elem_condition
		# My own codes
                if 10 <= kind_elem_code < 30:				 # 注意報
                    current['wa'][f"{kind_elem_code:02d}"] = condition
                elif 2 <= kind_elem_code < 10:				 # 警報
                    current['ww'][f"{kind_elem_code:02d}"] = condition
                elif 30 <= kind_elem_code < 40:				 # 特別警報
                    current['wew'][f"{kind_elem_code:02d}"] = condition

            wa = {}
            ww = {}
            wew = {}
            ewa = {}
            eww = {}
            ewew = {}
            f_wa = 0							# 「継続」以外→1
            f_ww = 0
            f_wew = 0

	    # 種別のループ
            for kind_str in ['wa', 'ww', 'wew']:
		# 種別パラメータ設定
                code_min = {'wa': 10, 'ww': 2, 'wew': 30}[kind_str]
                code_max = {'wa': 29, 'ww': 9, 'wew': 39}[kind_str]
                kind_down_str = {'wa': None, 'ww': 'wa', 'wew': 'ww'}[kind_str]
                kind_down2_str = {'wa': None, 'ww': None, 'wew':  'wa'}[kind_str]
                kind_up_str = {'wa': 'ww', 'ww': 'wew', 'wew': None}[kind_str]
                kind_up2_str = {'wa': 'wew', 'ww': None, 'wew': None}[kind_str]
                ref_to_down = {'wa': {}, 'ww': ww_wa, 'wew': wew_ww}[kind_str]
                ref_to_down2 = {'wa': {}, 'ww': {}, 'wew': wew_wa}[kind_str]
                ref_to_up = {'wa': wa_ww, 'ww': ww_wew, 'wew': {}}[kind_str]
                ref_to_up2 = {'wa': wa_wew, 'ww': {}, 'wew': {}}[kind_str]
                ref_kind_out = {'wa': wa, 'ww': ww, 'wew': wew}[kind_str]
                ref_kind_out_e = {'wa': ewa, 'ww': eww, 'wew': ewew}[kind_str]
                ref_f_out = {'wa': f_wa, 'ww': f_ww, 'wew': f_wew}[kind_str]

		# 種別に属するCodeのループ
                for c in range(code_min, code_max + 1):
                    code = f"{c:02d}"
                    if code in ref_last.get(kind_str, {}):
                        if code in current[kind_str]:
                            status = '継続'
                        else:
                            key = ref_to_up.get(code) if ref_to_up else None
                            key2 = ref_to_up2.get(code) if ref_to_up2 else None
                            keyd = ref_to_down.get(code) if ref_to_down else None
                            keyd2 = ref_to_down2.get(code) if ref_to_down2 else None

                            if kind_up_str and current.get(kind_up_str) and key is not None and key in current[kind_up_str]:
                                # DEBUG 2025.05.04
                                status = ref_to_up['status']
                            elif kind_up2_str and current.get(kind_up2_str) and key2 is not None and key2 in current[kind_up2_str]:
                                status = ref_to_up2['status']
                            elif kind_down_str and current.get(kind_down_str) and keyd is not None and keyd in current[kind_down_str]:
                                status = ref_to_down['status']
                            elif kind_down2_str and current.get(kind_down2_str) and keyd2 is not None and keyd2 in current[kind_down2_str]:
                                status = ref_to_down2['status']
                            else:
                                status = '解除'

                            if kind_str == 'wa':
                                f_wa = 1
                            elif kind_str == 'ww':
                                f_ww = 1
                            elif kind_str == 'wew':
                                f_wew = 1
                        ref_kind_out[code] = f"{code_kind[code]}{current[kind_str].get(code, '')},{status}"
                        ref_kind_out_e[code] = f"{code_kind_e[code]},{status_ja_en[status]}"
                    elif code in current[kind_str]:
                        key = ref_to_up.get(code) if ref_to_up else None
                        key2 = ref_to_up2.get(code) if ref_to_up2 else None
                        keyd = ref_to_down.get(code) if ref_to_down else None
                        keyd2 = ref_to_down2.get(code) if ref_to_down2 else None

                        if kind_up_str and ref_last.get(kind_up_str, {}) and key is not None and key in ref_last[kind_up_str]:
                            status = ref_to_up['rstatus']
                        elif kind_up2_str and ref_last.get(kind_up2_str, {}) and key2 is not None and key2 in ref_last[kind_up2_str]:
                            status = ref_to_up2['rstatus']
                        elif kind_down_str and ref_last.get(kind_down_str, {}) and keyd is not None and keyd in ref_last[kind_down_str]:
                            status = ref_to_down['rstatus']
                        elif kind_down2_str and ref_last.get(kind_down2_str, {}) and keyd2 is not None and keyd2 in ref_last[kind_down2_str]:
                            status = ref_to_down2['rstatus']
                        else:
                            status = '発表'
#                        ref_f_out = 1
                        if kind_str == 'wa':
                            f_wa = 1
                        elif kind_str == 'ww':
                            f_ww = 1
                        elif kind_str == 'wew':
                            f_wew = 1
                        ref_kind_out[code] = f"{code_kind[code]}{current[kind_str][code]},{status}"
                        ref_kind_out_e[code] = f"{code_kind_e[code]},{status_ja_en[status]}"

            if f_wa:
                if wa:
                    acct[area[area_code_text][0]] = wa if area[area_code_text][0] else None
                if ewa:
                    acct[area[area_code_text][3]] = ewa if area[area_code_text][3] else None
                write_last(area_code_text, wa, ww, wew, report_time)
            if f_ww:
                if ww:
                    acct[area[area_code_text][1]] = ww if area[area_code_text][1] else None
                if eww:
                    acct[area[area_code_text][4]] = eww if area[area_code_text][4] else None
                write_last(area_code_text, wa, ww, wew, report_time)
            if f_wew:
                if wew:
                    acct[area[area_code_text][2]] = wew if area[area_code_text][2] else None
                if ewew:
                    acct[area[area_code_text][5]] = ewew if area[area_code_text][5] else None
                write_last(area_code_text, wa, ww, wew, report_time)

    return report_time, acct

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
    '特別警報から警報': 2,
    'Emergency Warning to Warning': 2,
    '特別警報から注意報': 3,
    'Emergency Warning to Advisory': 3,
    '警報から注意報': 4,
    'Warning to Advisory': 4,
    '警報へ変化': 5,
    'Change to Warning': 5,
    '特別警報へ変化': 6,
    'Change to Emergency Warning': 6,
    '解除': 11,
    'Cancel': 11,
    '解除(注意報へ)': 12,
    'Cancel(to Advisory)': 12,
    '注意報に切り替え': 12,
    '解除(警報へ)': 13,
    'Cancel(to Warning)': 13,
    '警報に切り替え': 13,
    '発表警報・注意報はなし': -1,
    'なし': -1,
    'None': -1
}
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

    mssg = f'【{acct_area[acct]["name"]}：{acct_area[acct]["grade"]}】\n' if ja else f'% {acct_area[acct]["name"]} : {acct_area[acct]["grade"]} %\n'

    for k in sorted(status.keys()):
        if k < 0:
            continue
        kind_str[k] = re.sub(r'^[、,]', '', kind_str[k])
        if k < 10:
            form = '《{}》 {} {}\n' if ja else '[{}] {}\n'
        elif k < 50:
            form = '‥{}‥ {} {}\n' if ja else '-{}- {}\n'
        else:
            form = '｛{}｝ {} {}\n' if ja else '{{{}}} {}\n'
        grade = re.match(r'(.+)へ変化', status[k]) or re.search(r'Change to (.+)$', status[k])
        grade = grade.group(1) if grade else acct_area[acct]['grade']
        mssg += form.format(status[k], kind_str[k], grade)

    local_tz = datetime.now().astimezone().tzinfo
    dt = datetime.fromtimestamp(report_datetime, local_tz)
    formatted_time = dt.strftime('%Y-%m-%d %H:%M')
    mssg += f" ({formatted_time})"

    if len(mssg) > 299:
        mssg = mssg[:299] + '…'

    tb = client_utils.TextBuilder()
    tb = tb.text(mssg)

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
for link, ref_area in ref_links.items():
    syslog.syslog(syslog.LOG_INFO, f"DEBUG: LINK={link}, PARAM={':'.join(ref_area.keys())}")
    report_datetime, ref_acct_kind = fetch_xml(link, ref_area)

    # Post
    for acct_name in ref_acct_kind:
        syslog.syslog(syslog.LOG_INFO, f"POST {report_datetime}, {acct_name}, {ref_acct_kind[acct_name]}")
        if not acct_name:
            continue
        post_by_acct(report_datetime, acct_name, ref_acct_kind[acct_name])

#######################
os.unlink(LOCK_FILE)
#######################
syslog.syslog(syslog.LOG_INFO, "END")
exit(0)

### END ###
