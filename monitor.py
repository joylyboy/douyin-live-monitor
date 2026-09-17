import json
import os
import re
import time
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import quote

from curl_cffi.requests import Session
from ab_sign import ab_sign  # 仅 API 兜底时使用

STATE_FILE = "state.json"

# 保证 state.json 始终相对于脚本所在目录解析（本地/CI 工作目录不一致时更稳）
os.chdir(os.path.dirname(os.path.abspath(__file__)))
BARK_KEY = os.environ.get("BARK_KEY", "")         # 仅走 Secret，绝不硬编码；本地测试留空则不推送
BARK_ICON = ("https://is1-ssl.mzstatic.com/image/thumb/Purple211/v4/6d/03/2e/"
             "6d032edd-6bb9-7a40-2d1c-32e9ee919563/AppIcon-0-0-1x_U007emarketing-"
             "0-0-0-7-0-0-85-220.png/512x512bb.jpg")
BARK_SOUND = "tiptoes"

UA = ("Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/116.0.5845.97 Safari/537.36 Core/1.116.567.400 QQBrowser/19.7.6764.400")

# ============ 直播间配置：在这里加一行即可 ============
# 支持三种写法（任选其一），脚本会自动识别并归一为直播间标识：
#   1) 数字房间号 / 抖音号(用户名)： "125093611494" 或 "yall1102"
#   2) 直播间链接：                 "https://live.douyin.com/yall1102"
#   3) 主页/分享短链：               "https://v.douyin.com/CeiU5cbX"
# 推荐写成 {"url": "...", "name": "昵称"}，这样通知里显示你指定的名字；
# 主页短链且没写 name 时，会自动用 sec_uid 反查到的昵称。
SOURCES = [
    {"url": "https://live.douyin.com/125093611494", "name": "子成老师讲家庭教育"},
    # {"url": "https://live.douyin.com/yall1102", "name": "yall1102 主播"},
    # {"url": "https://v.douyin.com/CeiU5cbX", "name": "喜剧电影笑不停"},
]


def new_session():
    """用 curl_cffi 伪装 Chrome TLS 指纹，绕过抖音 WAF 对 requests/urllib 的识别。"""
    return Session(impersonate="chrome")


def normalize_source(src):
    if isinstance(src, dict):
        return src.get("url", ""), src.get("name", "")
    return src, ""


def resolve_identifier(raw):
    """把任意输入（数字/用户名/完整链接）归一为 live.douyin.com 用的标识符。
    返回 (identifier, sec_uid_or_None)。识别不出返回 (None, None)。"""
    raw = (raw or "").strip()
    if not raw:
        return None, None
    if raw.startswith("http"):
        # 主页/分享短链：跟重定向拿 sec_uid
        if "v.douyin.com" in raw or "iesdouyin.com/share/user" in raw:
            try:
                r = new_session().get(raw, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
                                      timeout=20, allow_redirects=True)
                m = re.search(r"sec_uid=([^&\s]+)", r.url)
                if m:
                    return m.group(1), m.group(1)
            except Exception as e:
                print(f"[resolve] {raw} 重定向解析失败: {e}")
            return None, None
        # 直播间链接 live.douyin.com/xxx
        m = re.search(r"live\.douyin\.com/([^/?#]+)", raw)
        if m:
            return m.group(1), None
        # 主页链接 www.douyin.com/user/<sec_uid>
        m = re.search(r"douyin\.com/user/([^/?#]+)", raw)
        if m:
            return m.group(1), m.group(1)
        # 其它带 sec_uid 的链接
        m = re.search(r"sec_uid=([^&\s]+)", raw)
        if m:
            return m.group(1), m.group(1)
    # 纯数字房间号 / 抖音号(用户名)
    return raw, None


def get_nickname_by_secuid(sec_uid):
    """用 sec_uid 反查昵称（主页短链没写 name 时用于通知显示）。失败返回 None。"""
    if not sec_uid:
        return None
    try:
        url = f"https://www.iesdouyin.com/web/api/v2/user/info/?sec_uid={sec_uid}"
        r = new_session().get(url, headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9",
                                            "Referer": "https://www.douyin.com/"}, timeout=20)
        d = r.json()
        return d.get("user_info", {}).get("nickname")
    except Exception as e:
        print(f"[nickname] sec_uid={sec_uid} 反查失败: {e}")
        return None


def build_targets():
    """把 SOURCES 解析成 [(identifier, display_name), ...]。"""
    targets = []
    for src in SOURCES:
        url, name = normalize_source(src)
        ident, sec_uid = resolve_identifier(url)
        if not ident:
            print(f"[skip] 无法识别的源: {src}")
            continue
        if not name and sec_uid:
            name = get_nickname_by_secuid(sec_uid) or ident
        elif not name:
            name = ident
        targets.append((ident, name))
    return targets


# 真实直播流地址：离线房间页面里这些计数为 0，直播房间大量出现（已用两个房间对比验证）
STREAM_RE = re.compile(r'https?://[^\s"\'\\<>]+?\.(?:m3u8|flv)')


def get_live_via_page(s, identifier):
    """主方法：抓直播间网页，用“真实直播流地址(m3u8/flv)是否存在”判直播。
    关键发现：抖音网页 SSR 的 liveStatus 字段对【正在直播】的房间也返回 'normal'（不可信）。
    但离线房间页面里 m3u8/flv/pull_url 等流地址计数为 0，直播房间则大量存在。
    因此以“页面含真实流地址”作为在播的可靠信号；页面取不到或异常则返回 None（防误报）。
    identifier 可以是数字房间号、抖音号(用户名)或 sec_uid——live.douyin.com 都能解析。"""
    try:
        r = s.get(f"https://live.douyin.com/{identifier}",
                  headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
                  timeout=15)
        if r.status_code != 200:
            return None
        t = r.text
        if len(t) < 50000:        # 疑似被 WAF 拦截/重定向的短页面，不误判为下播
            return None
        if STREAM_RE.search(t) or 'pull_url' in t:
            return True
        return False               # 页面正常返回但无任何流地址 → 未开播
    except Exception as e:
        print(f"[{identifier}] page parse error: {e}")
        return None


def get_live_via_api(s, identifier):
    """兜底方法：webcast/room/web/enter（a_bogus 签名）。
    部分网络环境（如某些住宅 IP）可用；被风控的 IP 会返回空 body，此时返回 None。"""
    try:
        params = {
            "aid": "6383", "app_name": "douyin_web", "live_id": "1",
            "device_platform": "web", "language": "zh-CN",
            "browser_language": "zh-CN", "browser_platform": "Win32",
            "browser_name": "Chrome", "browser_version": "116.0.0.0",
            "web_rid": identifier, "msToken": "",
        }
        api = f"https://live.douyin.com/webcast/room/web/enter/?{urllib.parse.urlencode(params)}"
        a_bogus = ab_sign(urllib.parse.urlparse(api).query, UA)
        api += "&a_bogus=" + a_bogus
        r = s.get(api, headers={
            "User-Agent": UA,
            "Referer": f"https://live.douyin.com/{identifier}",
            "Accept": "application/json, text/plain, */*",
        }, timeout=15)
        if r.status_code != 200 or not r.text.strip():
            return None
        data = r.json().get("data", {})
        arr = data.get("data")
        if isinstance(arr, list) and arr:
            return arr[0].get("status") == 2
        return None
    except Exception as e:
        print(f"[{identifier}] api error: {e}")
        return None


def get_live_status(identifier, s):
    """先试网页解析（稳），失败再试 API（兜底）。都拿不到返回 None（防误报：本次不通知）。"""
    v = get_live_via_page(s, identifier)
    if v is not None:
        return v
    return get_live_via_api(s, identifier)


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f), False
    except FileNotFoundError:
        return {"rooms": {}}, True     # 首次运行标记


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_bark(title, body):
    if not BARK_KEY:
        print(f"[bark skipped] {title} / {body}")
        return
    url = (f"https://api.day.app/{BARK_KEY}/"
           f"{quote(title, safe='')}/{quote(body, safe='')}"
           f"?sound={BARK_SOUND}&icon={quote(BARK_ICON, safe='')}")
    for _ in range(3):                 # 失败重试，避免漏通知
        try:
            if Session().get(url, timeout=5).status_code == 200:
                return
        except Exception as e:
            print("bark error:", e)
        time.sleep(2)


def main():
    state, first_run = load_state()
    rooms_state = state.setdefault("rooms", {})
    s = new_session()                  # 单会话：首次访问网页会自动种下 ttwid 等 cookie
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    hhmm = now.strftime("%H:%M")       # 北京时间

    for ident, name in build_targets():
        prev = rooms_state.get(ident, {}).get("is_live", False)
        current = get_live_status(ident, s)
        if current is None:
            print(f"[{name}] 状态获取失败/无法判定，跳过")   # 防误报：不更新、不通知
            continue
        entry = rooms_state.setdefault(ident, {"name": name, "is_live": False})
        if not first_run:              # 首次只记录，避免部署瞬间误报
            if not prev and current:
                send_bark(f"{hhmm}开播", f"直播间({name})")
            elif prev and not current:
                send_bark(f"{hhmm}下播", f"直播间({name})")
        entry.update({"name": name, "is_live": current,
                      "last_change": now.isoformat()})

    save_state(state)


if __name__ == "__main__":
    main()
