import re
import os
import time
import threading
import datetime
import pytz
import requests
import schedule
import base64
import urllib.parse
import json
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from flask import Flask, send_file, request, jsonify

# --- 全局配置区 ---
SOURCE_URL = "https://im-imgs-bucket.oss-accelerate.aliyuncs.com/index.js?t_5"
BASE_URL = "http://play.sportsteam368.com"
OUTPUT_M3U_FILE = "/app/output/playlist.m3u"
OUTPUT_TXT_FILE = "/app/output/playlist.txt"
REFRESHED_CHANNELS_FILE = "/app/output/refetched_channels.json"
TARGET_KEY = "ABCDEFGHIJKLMNOPQRSTUVWX"
# ------------------

app = Flask(__name__)

# ==========================================
# 内置轻量级 XXTEA 解密算法
# ==========================================
def str2long(s):
    v = []
    for i in range(0, len(s), 4):
        val = ord(s[i])
        if i + 1 < len(s): val |= ord(s[i+1]) << 8
        if i + 2 < len(s): val |= ord(s[i+2]) << 16
        if i + 3 < len(s): val |= ord(s[i+3]) << 24
        v.append(val)
    return v

def long2str(v):
    s = ""
    for val in v:
        s += chr(val & 0xff)
        s += chr((val >> 8) & 0xff)
        s += chr((val >> 16) & 0xff)
        s += chr((val >> 24) & 0xff)
    return s

def xxtea_decrypt(data, key):
    if not data: return ""
    v = str2long(data)
    k = str2long(key)
    while len(k) < 4: k.append(0)
    
    n = len(v) - 1
    if n < 1: return ""
    z = v[n]
    y = v[0]
    delta = 0x9E3779B9
    q = 6 + 52 // (n + 1)
    sum_val = (q * delta) & 0xffffffff

    while sum_val != 0:
        e = (sum_val >> 2) & 3
        for p in range(n, 0, -1):
            z = v[p - 1]
            mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ ((sum_val ^ y) + (k[(p & 3) ^ e] ^ z))
            y = v[p] = (v[p] - mx) & 0xffffffff
        p = 0
        z = v[n]
        mx = (((z >> 5) ^ (y << 2)) + ((y >> 3) ^ (z << 4))) ^ ((sum_val ^ y) + (k[(p & 3) ^ e] ^ z))
        y = v[0] = (v[0] - mx) & 0xffffffff
        sum_val = (sum_val - delta) & 0xffffffff

    m = v[-1]
    limit = (len(v) - 1) << 2
    if m < limit - 3 or m > limit: return None
    return long2str(v)[:m]

def decrypt_id_to_url(encrypted_id):
    try:
        decoded_id = urllib.parse.unquote(encrypted_id)
        pad = 4 - (len(decoded_id) % 4)
        if pad != 4: decoded_id += "=" * pad
        bin_str = base64.b64decode(decoded_id).decode('latin1')
        decrypted_bin = xxtea_decrypt(bin_str, TARGET_KEY)
        if decrypted_bin:
            json_str = decrypted_bin.encode('latin1').decode('utf-8')
            return json.loads(json_str).get("url")
    except Exception:
        pass
    return None

# ==========================================
# 底层资产提取
# ==========================================
def get_html_from_js(js_url):
    try:
        response = requests.get(js_url, timeout=10)
        response.encoding = 'utf-8'
        return "".join(re.findall(r"document\.write\('(.*?)'\);", response.text))
    except Exception:
        return ""

def extract_from_resource_tree(page):
    for frame in page.frames:
        if 'paps.html?id=' in frame.url:
            return frame.url.split('paps.html?id=')[-1]
    for url in page.evaluate("() => performance.getEntriesByType('resource').map(r => r.name)"):
        if 'paps.html?id=' in url:
            return url.split('paps.html?id=')[-1]
    return None


def load_existing_entries_from_m3u():
    entries = []
    if not os.path.exists(OUTPUT_M3U_FILE):
        return entries

    try:
        with open(OUTPUT_M3U_FILE, "r", encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
    except Exception:
        return entries

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("#EXTINF:") and i + 1 < len(lines):
            stream_url = lines[i + 1]
            channel_name = ""
            group_name = "JRS-未分组"

            if "," in line:
                channel_name = line.split(",", 1)[-1].strip()

            group_match = re.search(r'group-title="([^"]+)"', line)
            if group_match:
                group_name = group_match.group(1).strip()

            if channel_name and stream_url.startswith("http"):
                entries.append({
                    "group_name": group_name,
                    "channel_name": channel_name,
                    "stream_url": stream_url,
                })
            i += 2
            continue
        i += 1
    return entries

def _parse_match_datetime_from_channel_name(channel_name, current_year, tz):
    if not channel_name:
        return None
    match = re.match(r'^(\d{2}-\d{2} \d{2}:\d{2})', channel_name)
    if not match:
        return None

    month_day_time = match.group(1)
    try:
        match_dt = tz.localize(datetime.datetime.strptime(f"{current_year}-{month_day_time}", "%Y-%m-%d %H:%M"))
    except ValueError:
        return None

    if (match_dt - datetime.datetime.now(tz)).days > 180:
        try:
            match_dt = tz.localize(datetime.datetime.strptime(f"{current_year - 1}-{month_day_time}", "%Y-%m-%d %H:%M"))
        except ValueError:
            return None
    return match_dt

def load_refreshed_channels():
    if not os.path.exists(REFRESHED_CHANNELS_FILE):
        return {}
    try:
        with open(REFRESHED_CHANNELS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if isinstance(k, str)}
        if isinstance(data, list):
            return {item: None for item in data if isinstance(item, str)}
    except Exception:
        pass
    return {}

def save_refreshed_channels(refreshed_channels):
    try:
        os.makedirs(os.path.dirname(REFRESHED_CHANNELS_FILE), exist_ok=True)
        with open(REFRESHED_CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(refreshed_channels, f, ensure_ascii=False)
    except Exception:
        pass

def cleanup_refreshed_channels(refreshed_channels, now, tz, window_hours=5):
    if not refreshed_channels:
        return {}
    current_year = now.year
    window_seconds = window_hours * 3600
    cleaned = {}
    for channel_name, refreshed_at in refreshed_channels.items():
        match_dt = _parse_match_datetime_from_channel_name(channel_name, current_year, tz)
        if match_dt and abs((match_dt - now).total_seconds()) > window_seconds:
            continue
        cleaned[channel_name] = refreshed_at
    return cleaned

def keep_entries_within_time_window(existing_entries, now, tz, window_hours=5):
    current_year = now.year
    window_seconds = window_hours * 3600

    kept_entries = []
    removed_count = 0
    for item in existing_entries:
        match_dt = _parse_match_datetime_from_channel_name(item.get("channel_name"), current_year, tz)
        if match_dt and abs((match_dt - now).total_seconds()) > window_seconds:
            removed_count += 1
            continue
        kept_entries.append(item)

    if removed_count > 0:
        print(f"Window cleanup: removed {removed_count} lines outside ±{window_hours} hours.")
    return kept_entries

# ==========================================
# 静默版爬虫主流程
# ==========================================
def generate_playlist():
    tz = pytz.timezone('Asia/Shanghai')
    now = datetime.datetime.now(tz)
    print(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] Task started.")

    html_content = get_html_from_js(SOURCE_URL)
    if not html_content: 
        print("Task aborted: Source unreadable.")
        return

    soup = BeautifulSoup(html_content, 'html.parser')
    matches = soup.select('ul.item.play')
    
    if len(matches) == 0:
        print("Task aborted: No items found.")
        return

    current_year = now.year
    existing_entries = keep_entries_within_time_window(load_existing_entries_from_m3u(), now, tz, window_hours=5)
    refreshed_channels = cleanup_refreshed_channels(load_refreshed_channels(), now, tz, window_hours=5)

    existing_entries_dict = {item["channel_name"]: item for item in existing_entries}
    existing_channel_names = set(existing_entries_dict.keys())

    refresh_candidates = set()
    for channel_name in existing_channel_names:
        match_dt = _parse_match_datetime_from_channel_name(channel_name, current_year, tz)
        if not match_dt:
            continue
        if (now - match_dt).total_seconds() >= 1.5 * 3600 and channel_name not in refreshed_channels:
            refresh_candidates.add(channel_name)

    success_count = 0
    skip_count = 0

    try:
        with sync_playwright() as p:
            # ✅ 增加防内存泄漏关键参数
            browser = p.chromium.launch(
                headless=True, 
                args=[
                    '--no-sandbox', 
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu'
                ]
            )
            
            for match in matches:
                try:
                    time_tag = match.find('li', class_='lab_time')
                    if not time_tag: continue
                    
                    match_time_raw = time_tag.text.strip() 
                    match_time_str = f"{current_year}-{match_time_raw}"
                    match_dt = tz.localize(datetime.datetime.strptime(match_time_str, "%Y-%m-%d %H:%M"))
                    
                    # 抓取窗口：开赛前 2 小时到开赛后 30 分钟
                    time_diff_hours = (match_dt - now).total_seconds() / 3600
                    if not (-2 <= time_diff_hours <= 0.5):
                        continue
                    
                    league_tag = match.find('li', class_='lab_events')
                    league_name = league_tag.find('span', class_='name').text.strip() if league_tag else "综合"
                    group_name = f"JRS-{league_name}"
                    home_team = match.find('li', class_='lab_team_home').find('strong').text.strip()
                    away_team = match.find('li', class_='lab_team_away').find('strong').text.strip()
                    base_channel_name = f"{match_time_raw} {home_team} VS {away_team}"

                    channel_li = match.find('li', class_='lab_channel')
                    target_link = None
                    if channel_li:
                        for a_tag in channel_li.find_all('a', href=True):
                            href_val = a_tag['href']
                            if 'http' in href_val and '/play/' in href_val:
                                target_link = href_val
                                break
                    
                    if not target_link: continue

                    # ✅ 核心修复：为每场比赛开启独立的上下文和页面，阅后即焚，绝不复用
                    context = browser.new_context()
                    page = context.new_page()

                    try:
                        page.goto(target_link, wait_until="load", timeout=15000)
                        page.wait_for_timeout(2000)
                        detail_html = page.content()
                    except Exception:
                        continue # 如果外层页报错，后续也会跳过，finally 仍会执行

                    detail_soup = BeautifulSoup(detail_html, 'html.parser')
                    target_lines = []
                    
                    all_lines = detail_soup.select('a[data-play]')
                    for a in all_lines:
                        a_text = a.text.strip()
                        data_play = a.get('data-play')
                        if data_play and ('高清' in a_text or '蓝光' in a_text or '原画' in a_text):
                            target_lines.append({"name": a_text, "path": data_play})
                    
                    if not target_lines: 
                        continue

                    for line_info in target_lines:
                        final_url = urllib.parse.urljoin(target_link, line_info['path'])
                        specific_channel_name = f"{base_channel_name} - {line_info['name']}"
                        if specific_channel_name in existing_channel_names:
                            if specific_channel_name not in refresh_candidates:
                                skip_count += 1
                                continue
                        
                        try:
                            # 这里复用这一场比赛的专属 page 是可以的，因为一个比赛通常只有 2-3 个线路，不会无限堆积
                            page.goto(final_url, wait_until="load", timeout=15000)
                            page.wait_for_timeout(3000)
                            
                            encrypted_id = extract_from_resource_tree(page)

                            if encrypted_id:
                                real_stream_url = decrypt_id_to_url(encrypted_id)
                                if real_stream_url:
                                    existing_entries_dict[specific_channel_name] = {
                                        "group_name": group_name,
                                        "channel_name": specific_channel_name,
                                        "stream_url": real_stream_url,
                                    }
                                    existing_channel_names.add(specific_channel_name)
                                    if specific_channel_name in refresh_candidates:
                                        refreshed_channels[specific_channel_name] = now.isoformat()
                                    
                                    success_count += 1
                        except Exception:
                            continue
                            
                except Exception:
                    continue
                finally:
                    # ✅ 核心修复：无论本场比赛抓取成功与否，强制清理页面和上下文
                    if 'page' in locals() and not page.is_closed():
                        page.close()
                    if 'context' in locals():
                        context.close()
            
            browser.close()
    except Exception as e:
        print(f"Task encountered an error: {e}")

    # ==========================================
    # 核心机制：原子写入防冲突
    # ==========================================
    os.makedirs(os.path.dirname(OUTPUT_M3U_FILE), exist_ok=True)
    final_entries = list(existing_entries_dict.values())
    m3u_lines = ["#EXTM3U\n"]
    txt_dict = {}
    for item in final_entries:
        group_name = item["group_name"]
        specific_channel_name = item["channel_name"]
        real_stream_url = item["stream_url"]
        m3u_lines.append(f'#EXTINF:-1 tvg-name="{specific_channel_name}" group-title="{group_name}",{specific_channel_name}\n')
        m3u_lines.append(f"{real_stream_url}\n")
        if group_name not in txt_dict:
            txt_dict[group_name] = []
        txt_dict[group_name].append(f"{specific_channel_name},{real_stream_url}")

    if len(final_entries) == 0:
        m3u_lines.append("# 当前时间段无可用直播\n")
        txt_dict["System"] = ["No streams,http://127.0.0.1/error.mp4"]

    tmp_m3u = OUTPUT_M3U_FILE + ".tmp"
    tmp_txt = OUTPUT_TXT_FILE + ".tmp"

    with open(tmp_m3u, 'w', encoding='utf-8') as f:
        f.writelines(m3u_lines)
    with open(tmp_txt, 'w', encoding='utf-8') as f:
        for group, channels in txt_dict.items():
            f.write(f"{group},#genre#\n")
            for ch in channels: f.write(f"{ch}\n")
            
    os.replace(tmp_m3u, OUTPUT_M3U_FILE)
    os.replace(tmp_txt, OUTPUT_TXT_FILE)
    save_refreshed_channels(refreshed_channels)
    
    finish_time = datetime.datetime.now(tz)
    print(f"[{finish_time.strftime('%Y-%m-%d %H:%M:%S')}] Task finished. New {success_count} lines, skipped {skip_count} existing lines, total {len(final_entries)} lines.")


# ==========================================
# 极简 Web 路由
# ==========================================
@app.route('/')
def index():
    return "Service OK", 200

@app.route('/healthz')
def healthz():
    return jsonify({"status": "ok"}), 200

@app.route('/m3u')
def get_m3u():
    try: return send_file(OUTPUT_M3U_FILE, mimetype='application/vnd.apple.mpegurl', as_attachment=False)
    except FileNotFoundError: return "File not found", 404

@app.route('/txt')
def get_txt():
    try: return send_file(OUTPUT_TXT_FILE, mimetype='text/plain', as_attachment=False)
    except FileNotFoundError: return "File not found", 404

@app.route('/debug')
def debug_url():
    target_url = request.args.get('url')
    if not target_url: return "Bad Request", 400
    debug_info = {"target_url": target_url, "extracted_token": None, "decrypted_url": None, "frames_found": [], "resources_found": []}
    try:
        with sync_playwright() as p:
            # ✅ Debug 路由同样增加参数和上下文管理
            browser = p.chromium.launch(
                headless=True, 
                args=[
                    '--no-sandbox', 
                    '--disable-setuid-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu'
                ]
            )
            context = browser.new_context()
            page = context.new_page()
            
            try:
                page.goto(target_url, wait_until="load", timeout=15000)
                page.wait_for_timeout(3000) 
                
                for f in page.frames:
                    debug_info["frames_found"].append(f.url)
                    if 'paps.html?id=' in f.url: debug_info["extracted_token"] = f.url.split('paps.html?id=')[-1]
                
                resource_urls = page.evaluate("() => performance.getEntriesByType('resource').map(r => r.name)")
                debug_info["resources_found"] = resource_urls
                
                if not debug_info["extracted_token"]:
                    for url in resource_urls:
                        if 'paps.html?id=' in url: debug_info["extracted_token"] = url.split('paps.html?id=')[-1]; break
                
                if debug_info["extracted_token"]: debug_info["decrypted_url"] = decrypt_id_to_url(debug_info["extracted_token"])
            finally:
                page.close()
                context.close()
                browser.close()
                
    except Exception as e: 
        debug_info["error"] = str(e)
    return jsonify(debug_info)

def run_scheduler():
    schedule.every(14).minutes.do(generate_playlist)
    while True:
        schedule.run_pending()
        time.sleep(30)

if __name__ == "__main__":
    os.makedirs(os.path.dirname(OUTPUT_M3U_FILE), exist_ok=True)
    if not os.path.exists(OUTPUT_M3U_FILE):
        with open(OUTPUT_M3U_FILE, 'w', encoding='utf-8') as f:
            f.write("#EXTM3U\n#EXTINF:-1,关注博客blog.204090.xyz\nhttps://blog.204090.xyz\n")
    if not os.path.exists(OUTPUT_TXT_FILE):
        with open(OUTPUT_TXT_FILE, 'w', encoding='utf-8') as f:
            f.write("系统提示,#genre#\n关注博客blog.204090.xyz,https://blog.204090.xyz\n")

    threading.Thread(target=generate_playlist, daemon=True).start()
    threading.Thread(target=run_scheduler, daemon=True).start()
    
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    
    port = int(os.getenv("PORT", "5000"))
    print(f"Starting Flask server on 0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, threaded=True)
