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

# ============ 直播间配置：只改 rooms.txt，不用碰本文件 ============
# rooms.txt 每行一个房间，格式：  直播间链接或标识, 显示名
#   例： https://live.douyin.com/125093611494, 子成老师讲家庭教育
#   显示名可省略（短链会自动用 sec_uid 反查昵称）
#   支持：数字房号/抖音号、live.douyin.com/xxx、v.douyin.com/xxx 短链
#   # 开头为注释，空行忽略
# 注意：rooms.txt 必须随仓库提交，CI 才能读到。
ROOMS_FILE = "rooms.txt"
# 兜底默认（仅当 rooms.txt 缺失时使用，避免本地直接跑报错）
FALLBACK_SOURCES = [
    {"url": "https://live.douyin.com/125093611494", "name": "子成老师讲家庭教育"},
]


def load_sources():
    """从 rooms.txt 读取监控列表；缺失则回退到 FALLBACK_SOURCES。"""
    try:
        with open(ROOMS_FILE, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return FALLBACK_SOURCES
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        if "," in ln:
            url, name = ln.split(",", 1)
            out.append({"url": url.strip(), "name": name.strip()})
        else:
            out.append({"url": ln, "name": ""})
    return out or FALLBACK_SOURCES


SOURCES = load_sources()


def new_session():
    """用 curl_cffi 伪装 Chrome TLS 指纹，绕过抖音 WAF 对 requests/urllib 的识别。"""
    return Session(impersonate="chrome")


def normalize_source(src):
    if isinstance(src, dict):
        return src.get("url", ""), src.get("name", "")
    return src, ""


def _extract_secuid(s):
    """从文本/URL 抽 sec_uid：兼容 查询形式(sec_uid=xxx) 与 路径形式
    (www.douyin.com/user/xxx 或 www.iesdouyin.com/share/user/xxx)。"""
    if not s:
        return None
    m = re.search(r"sec_uid=([^&\s\"'<>]+)", s)
    if m:
        return m.group(1)
    m = re.search(r"(?:douyin\.com/user|iesdouyin\.com/share/user)/([^/?#\"'<>]+)", s)
    if m:
        return m.group(1)
    return None


def _extract_live_id(s):
    """从文本/URL 抽 live.douyin.com 房间标识，过滤 reflow 等非房号串；
    仅当标识为纯数字（真实房号）时才返回，避免把 sec_uid 误当房号。"""
    if not s:
        return None
    m = re.search(r"live\.douyin\.com/([^/?#\"'<>]+)", s)
    if m and m.group(1) not in ("", "reflow") and m.group(1).isdigit():
        return m.group(1)
    return None


def resolve_identifier(raw):
    """把任意输入（数字/用户名/完整链接）归一为 live.douyin.com 用的标识符。
    返回 (identifier, sec_uid_or_None)。识别不出返回 (None, None)。"""
    raw = (raw or "").strip()
    if not raw:
        return None, None
    if raw.startswith("http"):
        # v.douyin.com / iesdouyin 分享短链：跟重定向。抖音短链走 meta/JS 跳转，
        # curl_cffi 的 r.url 可能仍停在短链上，所以要同时查 r.url 与 r.text。
        if "v.douyin.com" in raw or "iesdouyin.com/share" in raw:
            try:
                r = new_session().get(raw, headers={"User-Agent": UA,
                                                    "Accept-Language": "zh-CN,zh;q=0.9"},
                                      timeout=20, allow_redirects=True)
                # 短链若直接跳到直播间(live.douyin.com/数字)，直接用数字房号最稳
                live_id = _extract_live_id(r.url) or _extract_live_id(r.text)
                if live_id:
                    return live_id, None
                # 否则拿到 sec_uid，交给监控阶段：用户开播时抖音会把 sec_uid 页面
                # 重定向到 live.douyin.com/{数字房号}，那时我们再锁定数字房号长期监控
                sec = _extract_secuid(r.url) or _extract_secuid(r.text)
                if sec:
                    return sec, sec
            except Exception as e:
                print(f"[resolve] {raw} 短链解析失败: {e}")
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
    """把 SOURCES 解析成 [(identifier, display_name, sec_uid), ...]。
    sec_uid 仅对 v.douyin.com 短链房间非空，用于监控时锁定其真实数字房号。"""
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
        targets.append((ident, name, sec_uid))
    return targets


# 真实直播流地址：离线房间页面里这些计数为 0，直播房间大量出现（已用两个房间对比验证）
STREAM_RE = re.compile(r'https?://[^\s"\'\\<>]+?\.(?:m3u8|flv)')

# 从直播间网页 SSR 中提取真实的 room_id / user_id，用于构造唤起 App 的 URL Scheme
# 兼容页面的 room_id / roomId / user_id / userId 等多种写法
ROOM_ID_RE = re.compile(r'room_?id["\']?\s*[:=]\s*"?(\d{6,})"?', re.IGNORECASE)
USER_ID_RE = re.compile(r'user_?id["\']?\s*[:=]\s*"?(\d{6,})"?', re.IGNORECASE)


def extract_room_info(t):
    """从直播间网页 HTML 中提取 (room_id, user_id)，取不到返回 (None, None)。"""
    rid = ROOM_ID_RE.search(t)
    uid = USER_ID_RE.search(t)
    return (rid.group(1) if rid else None,
            uid.group(1) if uid else None)


def _extract_redirect_to_room(t):
    """从 sec_uid 用户页 HTML 中抽出「页面级跳转」目标数字房号。
    抖音开播时常把 live.douyin.com/{sec_uid} 用 meta refresh 或 JS location 跳到
    live.douyin.com/{数字房号}；curl_cffi 不跟 JS 跳转，这里手动识别。
    只认「页面级导航」(meta refresh / window.location 等)，不认侧边栏 <a> 链接，避免误匹配。"""
    if not t:
        return None
    # 1) <meta http-equiv="refresh" ... url=...>
    m = re.search(r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]*url=["\']?([^"\'>\s]+)', t, re.IGNORECASE)
    if m:
        rm = re.search(r"live\.douyin\.com/(\d{6,})", m.group(1))
        if rm:
            return rm.group(1)
    # 2) JS 显式跳转：window.location / location.href / document.location = "...live.douyin.com/数字"
    for kw in (r"window\.location", r"location\.href", r"document\.location",
               r"self\.location", r"top\.location"):
        m = re.search(kw + r"\s*=\s*[\"']([^\"']*live\.douyin\.com/\d{6,}[^\"']*)[\"']", t)
        if m:
            rm = re.search(r"live\.douyin\.com/(\d{6,})", m.group(1))
            if rm:
                return rm.group(1)
    return None


def get_live_via_page(s, identifier):
    """主方法：抓直播间网页，用“真实直播流地址(m3u8/flv)是否存在”判直播。
    关键发现：抖音网页 SSR 的 liveStatus 字段对【正在直播】的房间也返回 'normal'（不可信）。
    但离线房间页面里 m3u8/flv/pull_url 等流地址计数为 0，直播房间则大量存在。
    因此以“页面含真实流地址”作为在播的可靠信号；页面取不到或异常则返回 None（防误报）。
    identifier 可以是数字房间号、抖音号(用户名)或 sec_uid。
    返回 (is_live, room_id, user_id, resolved_room_id)：
      resolved_room_id 是抖音把 sec_uid 用户页跳转/重定向到 live.douyin.com/{数字} 时抽到的真实房号，
      用于把短链房间锁定为数字房号长期监控（短链房间只有开播那一刻才暴露数字房号）。"""
    try:
        r = s.get(f"https://live.douyin.com/{identifier}",
                  headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
                  timeout=15)
        if r.status_code != 200:
            return None, None, None, None
        t = r.text
        if len(t) < 50000:        # 疑似被 WAF 拦截/重定向的短页面，不误判为下播
            return None, None, None, None
        # HTTP 重定向后的真实房号（如 301 到 live.douyin.com/{数字}）
        resolved = None
        m = re.search(r"live\.douyin\.com/(\d{6,})", r.url)
        if m:
            resolved = m.group(1)
        # 当前页本身就是在播直播间（数字房号页，或 sec_uid 页直接渲染了直播间）
        if STREAM_RE.search(t) or 'pull_url' in t:
            rid, uid = extract_room_info(t)
            if not rid and resolved:
                rid = resolved
            return True, rid, uid, resolved
        # 当前页无流：可能是 sec_uid 用户页被「页面级跳转」到了数字房号直播间。
        # 手动解析跳转目标并去目标页复核，确保在播瞬间也能锁定真实房号。
        target = _extract_redirect_to_room(t)
        if target and target != str(identifier):
            try:
                r2 = s.get(f"https://live.douyin.com/{target}",
                           headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
                           timeout=15)
                if r2.status_code == 200 and len(r2.text) >= 50000:
                    if STREAM_RE.search(r2.text) or 'pull_url' in r2.text:
                        rid, uid = extract_room_info(r2.text)
                        if not rid:
                            rid = target
                        return True, rid, uid, target
                    # 目标页存在但当前未播：数字房号稳定，缓存以便后续直接监控（更稳）
                    return False, None, None, target
            except Exception as e:
                print(f"[{identifier}] follow redirect to {target} error: {e}")
        return False, None, None, resolved     # 页面正常返回但无任何流地址 → 未开播
    except Exception as e:
        print(f"[{identifier}] page parse error: {e}")
        return None, None, None, None


def get_live_via_api(s, identifier):
    """兜底方法：webcast/room/web/enter（a_bogus 签名）。
    部分网络环境（如某些住宅 IP）可用；被风控的 IP 会返回空 body，此时返回 None。
    返回 (is_live, room_id)：拿不到 is_live 时两者都返回 None。"""
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
            return None, None
        data = r.json().get("data", {})
        arr = data.get("data")
        if isinstance(arr, list) and arr:
            item = arr[0]
            rid = item.get("room_id") or item.get("id")
            return item.get("status") == 2, (str(rid) if rid else None)
        return None, None
    except Exception as e:
        print(f"[{identifier}] api error: {e}")
        return None, None


def get_live_status(identifier, s):
    """先试网页解析（稳），失败再试 API（兜底）。都拿不到返回 None（防误报：本次不通知）。
    返回 (is_live, room_id, user_id, resolved_room_id)。"""
    v, rid, uid, resolved = get_live_via_page(s, identifier)
    if v is not None:
        return v, rid, uid, resolved
    a, a_rid = get_live_via_api(s, identifier)
    if a is not None:
        return a, a_rid, None, a_rid
    return None, None, None, None


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f), False
    except FileNotFoundError:
        return {"rooms": {}}, True     # 首次运行标记


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def build_open_url(room_id, user_id, identifier):
    """构造点击 Bark 通知后跳转到对应直播间的地址。
    - 能拿到真实 room_id 时，优先用抖音 URL Scheme 直接唤起 App 进入直播间：
      snssdk1128://live?room_id=X&user_id=Y（iOS / 安卓通用，实测 aweme:// 在部分 iOS 上无法打开）。
    - 兜底用 live.douyin.com 网页链接（Safari/浏览器再唤起 App，多一步但更稳）。"""
    if room_id:
        q = f"room_id={room_id}"
        if user_id:
            q += f"&user_id={user_id}"
        return "snssdk1128://live?" + q
    return f"https://live.douyin.com/{identifier}"


def send_bark(title, body, open_url=None):
    if not BARK_KEY:
        print(f"[bark skipped] {title} / {body}")
        return
    url = (f"https://api.day.app/{BARK_KEY}/"
           f"{quote(title, safe='')}/{quote(body, safe='')}"
           f"?sound={BARK_SOUND}&icon={quote(BARK_ICON, safe='')}")
    if open_url:                       # 点击通知跳转：URL Scheme 唤起抖音进直播间
        url += f"&url={quote(open_url, safe='')}"
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
    secuid_map = state.setdefault("secuid_map", {})   # sec_uid -> 数字房号（开播时锁定）
    s = new_session()                  # 单会话：首次访问网页会自动种下 ttwid 等 cookie
    now = datetime.now(ZoneInfo("Asia/Shanghai"))
    hhmm = now.strftime("%H:%M")       # 北京时间

    for ident, name, sec_uid in build_targets():
        # 短链(sec_uid)房间：优先用已锁定的数字房号监控（数字房号这条路 100% 可靠）
        effective = ident
        if sec_uid and secuid_map.get(sec_uid):
            effective = secuid_map[sec_uid]
        current, room_id, user_id, resolved = get_live_status(effective, s)
        if current is None:
            print(f"[{name}] 状态获取失败/无法判定，跳过")   # 防误报：不更新、不通知
            continue
        # 监控过程中若发现 sec_uid 对应的真实数字房号（开播跳转/页面曝光），永久缓存，
        # 之后直接用数字房号监控（这条路 100% 可靠），摆脱不稳定的 sec_uid 用户页。
        if sec_uid:
            rid_candidate = resolved or (room_id if room_id and room_id.isdigit() else None)
            if rid_candidate and rid_candidate != sec_uid:
                secuid_map[sec_uid] = rid_candidate
        prev = rooms_state.get(ident, {}).get("is_live", False)
        entry = rooms_state.setdefault(ident, {"name": name, "is_live": False})
        if not first_run:              # 首次只记录，避免部署瞬间误报
            open_url = build_open_url(room_id, user_id, effective)
            if not prev and current:
                send_bark(f"{hhmm}开播", f"直播间({name})", open_url)
            elif prev and not current:
                send_bark(f"{hhmm}下播", f"直播间({name})", open_url)
        entry.update({"name": name, "is_live": current,
                      "last_change": now.isoformat()})

    save_state(state)


if __name__ == "__main__":
    main()
