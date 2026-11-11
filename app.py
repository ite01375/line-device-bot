import os
import re
import json
import hmac
import time
import threading
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qs

import requests
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    FlexMessage,
    FlexContainer,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, PostbackEvent
from groq import Groq

app = Flask(__name__)

# ⚠️ 金鑰只從環境變數讀取
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GAS_WEB_APP_URL = os.getenv("GAS_WEB_APP_URL")

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

groq_api_key = os.getenv("GROQ_API_KEY")
ai_client = Groq(api_key=groq_api_key, timeout=15.0, max_retries=0) if groq_api_key else None
print(f"🔑 Groq client 已啟用: {ai_client is not None}", flush=True)

GROQ_MODELS = [m for m in [os.getenv("GROQ_MODEL"), "openai/gpt-oss-20b", "openai/gpt-oss-120b"] if m]

# 管理員（可代替任何人歸還）：環境變數 ADMIN_USER_IDS，多人用逗號分隔
ADMIN_USER_IDS = {x.strip() for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()}

# ---------- 逾時提醒設定（皆可用環境變數調整）----------
TW = timezone(timedelta(hours=8))
OVERDUE_HOURS = float(os.getenv("OVERDUE_HOURS", "8"))                 # 借超過幾小時開始提醒
REMIND_INTERVAL_HOURS = float(os.getenv("REMIND_INTERVAL_HOURS", "3"))  # 同一設備每隔幾小時再提醒
QUIET_START = int(os.getenv("QUIET_START", "20"))                       # 安靜時段開始（台灣時間，時）
QUIET_END = int(os.getenv("QUIET_END", "8"))                            # 安靜時段結束
CRON_KEY = os.getenv("CRON_KEY")                                        # 觸發提醒用的密鑰，沒設定＝停用提醒
_last_reminded = {}

MINE_KW = ["我借了什麼", "我的借用", "我借的", "我的設備", "mine"]
MENU_KW = ["選單", "菜單", "面板", "按鈕", "menu", "狀態", "表單", "清單", "查詢", "status"]
RETURN_KW = ["還", "退", "return"]
BORROW_KW = ["借", "拿", "用", "borrow"]
QUESTION_HINTS = ["怎麼", "如何", "什麼", "為什麼", "嗎", "?", "？", "how", "what", "why"]

# ---------- 快取 ----------
DEVICE_TTL = 20
_dev_cache = {"data": {}, "ts": 0.0}
_profile_cache = {}
_seen = []
_seen_lock = threading.Lock()


@app.route("/")
def home():
    return "LINE AI Device Bot with Groq is running successfully!"


# ================= 時間工具 =================
def now_tw():
    return datetime.now(TW)


def parse_ts(v):
    if not v:
        return None
    try:
        return datetime.strptime(str(v).replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=TW)
    except Exception:
        return None


def fmt_duration(td):
    minutes = max(int(td.total_seconds() // 60), 0)
    h, m = divmod(minutes, 60)
    if h and m:
        return f"{h} 小時 {m} 分"
    if h:
        return f"{h} 小時"
    return f"{m} 分鐘"


def in_quiet_hours(now):
    if QUIET_START == QUIET_END:
        return False
    h = now.hour
    if QUIET_START > QUIET_END:
        return h >= QUIET_START or h < QUIET_END
    return QUIET_START <= h < QUIET_END


# ================= 試算表 =================
def get_all_devices(force=False):
    now = time.time()
    if not force and _dev_cache["data"] and now - _dev_cache["ts"] < DEVICE_TTL:
        return _dev_cache["data"]
    try:
        r = requests.get(GAS_WEB_APP_URL, timeout=6)
        if r.status_code == 200:
            devices = {}
            for row in r.json():
                name = row["name"]
                devices[name] = {
                    "id": str(row["id"]),
                    "name": name,
                    "status": "Borrowed" if row["status"] == "Borrowed" else "Available",
                    "borrower": row["borrower"] or None,
                    "borrower_id": str(row.get("borrower_id") or "") or None,
                    "borrowed_at": parse_ts(row.get("borrowed_at")),
                    "notify_to": str(row.get("notify_to") or "") or None,
                }
            _dev_cache["data"] = devices
            _dev_cache["ts"] = now
            return devices
    except Exception as e:
        print(f"❌ 讀取設備清單失敗: {e}", flush=True)
    return _dev_cache["data"]


def _write_sheet(payload):
    try:
        r = requests.post(GAS_WEB_APP_URL, json=payload, timeout=20)
        print(f"📝 GAS 回應 {r.status_code}: {r.text[:100]}", flush=True)
    except Exception as e:
        print(f"連線至 Google 試算表失敗: {e}", flush=True)


def update_google_sheet_async(dev_id, dev_name, status, borrower, user_id, operator, notify_to=""):
    if not GAS_WEB_APP_URL:
        return
    payload = {
        "dev_id": dev_id,
        "dev_name": dev_name,
        "status": status,
        "borrower": borrower,
        "user_id": user_id,
        "operator": operator,
        "notify_to": notify_to,
    }
    threading.Thread(target=_write_sheet, args=(payload,), daemon=True).start()


# ================= 借 / 還 =================
def owns(info, user_id, display_name):
    """嚴格判斷：這個設備是不是這個人借的"""
    if info.get("borrower_id"):
        return bool(user_id) and info["borrower_id"] == user_id
    return bool(info.get("borrower")) and info["borrower"] == display_name


def is_borrower(info, user_id, display_name):
    """可否歸還：沒有任何借用人資料時不擋"""
    if not info.get("borrower_id") and not info.get("borrower"):
        return True
    return owns(info, user_id, display_name)


def borrow_device(info, user_id, display_name, notify_to=""):
    if info["status"] == "Available":
        info["status"], info["borrower"], info["borrower_id"] = "Borrowed", display_name, user_id or None
        info["borrowed_at"], info["notify_to"] = now_tw(), notify_to or None
        _dev_cache["ts"] = time.time()
        update_google_sheet_async(info["id"], info["name"], "Borrowed", display_name, user_id, display_name, notify_to)
        return f"沒問題！已幫 {display_name} 登記借用【{info['name']}】，祝使用順利！"
    who = f"（{info['borrower']}）" if info["borrower"] else ""
    return f"不好意思，【{info['name']}】目前已經被借走了喔！{who}"


def return_device(info, user_id, display_name):
    if info["status"] != "Borrowed":
        return f"【{info['name']}】目前本來就是可借用狀態喔！"

    if not (is_borrower(info, user_id, display_name) or user_id in ADMIN_USER_IDS):
        who = info["borrower"] or "其他人"
        return f"【{info['name']}】是 {who} 借的，只有借用人本人可以歸還喔！"

    used = ""
    if info.get("borrowed_at"):
        used = f"\n⏱ 這次借用 {fmt_duration(now_tw() - info['borrowed_at'])}"

    info["status"], info["borrower"], info["borrower_id"] = "Available", None, None
    info["borrowed_at"], info["notify_to"] = None, None
    _last_reminded.pop(info["id"], None)
    _dev_cache["ts"] = time.time()
    update_google_sheet_async(info["id"], info["name"], "Available", "", user_id, display_name)
    return f"感謝 {display_name}！已完成【{info['name']}】的歸還登記。{used}"


# ================= Flex 按鈕介面 =================
GREEN = "#1DB446"
RED = "#E53935"
ROWS_PER_BUBBLE = 6


def _action_data(act, dev_id, view=None):
    d = f"act={act}&id={dev_id}"
    return d + f"&v={view}" if view else d


def _device_row(info, view=None):
    available = info["status"] == "Available"
    color = GREEN if available else RED
    if available:
        status_text = "🟢 可借用"
        label, act = "借用", "borrow"
    else:
        who = f" · {info['borrower']}" if info["borrower"] else ""
        status_text = f"🔴 已借出{who}"
        label, act = "歸還", "return"
    return {
        "type": "box",
        "layout": "horizontal",
        "alignItems": "center",
        "spacing": "md",
        "contents": [
            {
                "type": "box",
                "layout": "vertical",
                "flex": 3,
                "contents": [
                    {"type": "text", "text": info["name"], "weight": "bold", "size": "md", "wrap": True},
                    {"type": "text", "text": status_text, "size": "xs", "color": color, "wrap": True},
                ],
            },
            {
                "type": "button",
                "style": "primary",
                "height": "sm",
                "flex": 2,
                "color": color,
                "action": {"type": "postback", "label": label, "data": _action_data(act, info["id"], view)},
            },
        ],
    }


def _my_row(info):
    held = ""
    if info.get("borrowed_at"):
        held = f"已借 {fmt_duration(now_tw() - info['borrowed_at'])}"
    contents = [{"type": "text", "text": info["name"], "weight": "bold", "size": "md", "wrap": True}]
    if held:
        contents.append({"type": "text", "text": f"⏱ {held}", "size": "xs", "color": "#888888"})
    return {
        "type": "box",
        "layout": "horizontal",
        "alignItems": "center",
        "spacing": "md",
        "contents": [
            {"type": "box", "layout": "vertical", "flex": 3, "contents": contents},
            {
                "type": "button",
                "style": "primary",
                "height": "sm",
                "flex": 2,
                "color": RED,
                "action": {"type": "postback", "label": "歸還", "data": _action_data("return", info["id"], "mine")},
            },
        ],
    }


def _flex_message(contents, alt_text):
    return FlexMessage(
        alt_text=alt_text,
        contents=FlexContainer.from_json(json.dumps(contents, ensure_ascii=False)),
    )


def build_device_flex(devices):
    items = list(devices.values())
    chunks = [items[i:i + ROWS_PER_BUBBLE] for i in range(0, len(items), ROWS_PER_BUBBLE)][:12]
    bubbles = []
    for idx, chunk in enumerate(chunks, 1):
        body_contents = []
        for n, info in enumerate(chunk):
            if n > 0:
                body_contents.append({"type": "separator", "margin": "md"})
            body_contents.append(_device_row(info))
        title = "🏋️ 設備借還" + (f"（{idx}/{len(chunks)}）" if len(chunks) > 1 else "")
        bubbles.append({
            "type": "bubble",
            "size": "mega",
            "header": {
                "type": "box",
                "layout": "vertical",
                "backgroundColor": "#27ACB2",
                "paddingAll": "md",
                "contents": [{"type": "text", "text": title, "weight": "bold", "size": "lg", "color": "#FFFFFF"}],
            },
            "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": body_contents},
            "footer": {
                "type": "box",
                "layout": "vertical",
                "spacing": "sm",
                "contents": [
                    {"type": "button", "style": "secondary", "height": "sm",
                     "action": {"type": "postback", "label": "📋 我的借用", "data": "act=mine"}},
                    {"type": "button", "style": "secondary", "height": "sm",
                     "action": {"type": "postback", "label": "🔄 重新整理", "data": "act=menu"}},
                ],
            },
        })
    contents = bubbles[0] if len(bubbles) == 1 else {"type": "carousel", "contents": bubbles}
    return _flex_message(contents, "🏋️ 設備借還狀態（請在手機查看按鈕）")


def build_my_flex(mine, display_name):
    body_contents = []
    for n, info in enumerate(mine[:10]):
        if n > 0:
            body_contents.append({"type": "separator", "margin": "md"})
        body_contents.append(_my_row(info))
    contents = {
        "type": "bubble",
        "size": "mega",
        "header": {
            "type": "box",
            "layout": "vertical",
            "backgroundColor": "#E53935",
            "paddingAll": "md",
            "contents": [{"type": "text", "text": f"📋 {display_name} 的借用", "weight": "bold",
                          "size": "lg", "color": "#FFFFFF", "wrap": True}],
        },
        "body": {"type": "box", "layout": "vertical", "spacing": "md", "contents": body_contents},
        "footer": {
            "type": "box",
            "layout": "vertical",
            "contents": [{"type": "button", "style": "secondary", "height": "sm",
                          "action": {"type": "postback", "label": "🏋️ 開啟設備選單", "data": "act=menu"}}],
        },
    }
    return _flex_message(contents, "📋 我的借用")


def menu_messages(devices):
    if not devices:
        return [TextMessage(text="目前讀不到設備清單，請稍後再試。")]
    return [build_device_flex(devices)]


def my_messages(devices, user_id, display_name):
    mine = [d for d in devices.values() if d["status"] == "Borrowed" and owns(d, user_id, display_name)]
    if not mine:
        return [TextMessage(text="📋 你目前沒有借用任何設備。\n輸入「選單」就能借設備囉！")]
    return [build_my_flex(mine, display_name)]


# ================= 使用者資訊 =================
def get_user_info(event, line_bot_api):
    src = event.source
    user_id = getattr(src, "user_id", None)
    src_type = getattr(src, "type", "user")
    if not user_id:
        return "", "未知用戶"

    if user_id in _profile_cache:
        return user_id, _profile_cache[user_id]

    display_name = ""
    try:
        if src_type == "group":
            p = line_bot_api.get_group_member_profile(src.group_id, user_id)
        elif src_type == "room":
            p = line_bot_api.get_room_member_profile(src.room_id, user_id)
        else:
            p = line_bot_api.get_profile(user_id)
        display_name = p.display_name or ""
    except Exception as e:
        print(f"⚠️ 無法取得 Profile ({src_type}): {e}", flush=True)

    if display_name:
        _profile_cache[user_id] = display_name
    else:
        display_name = f"用戶_{user_id[-4:]}"
    return user_id, display_name


def get_notify_target(event):
    """提醒要發送到哪裡：群組 > 聊天室 > 個人"""
    src = event.source
    return (
        getattr(src, "group_id", None)
        or getattr(src, "room_id", None)
        or getattr(src, "user_id", None)
        or ""
    )


def find_device(text, devices):
    for name in sorted(devices, key=len, reverse=True):
        if name in text:
            return name
    return None


# ================= AI =================
def ask_ai(text, display_name, devices):
    now = now_tw()
    weekdays = "一二三四五六日"
    today = f"{now.year}年{now.month}月{now.day}日 星期{weekdays[now.weekday()]} {now:%H:%M}（台灣時間）"
    status_lines = "\n".join(
        f"- {d['name']}: {'已借出' if d['status'] == 'Borrowed' else '可借用'}"
        for d in devices.values()
    )
    system_prompt = (
        f"你是一個親切的 LINE 健身房設備管理與 AI 助理，正在跟「{display_name}」對話。\n"
        f"現在時間：{today}\n"
        "請務必使用對方訊息所用的語言回覆（中文、英文、日文、韓文皆可），簡短自然。\n"
        "若使用者想借/還設備，請告訴他輸入「選單」開啟按鈕介面；想查自己借了什麼，輸入「我借了什麼」。\n"
        f"目前設備狀態：\n{status_lines or '（無資料）'}"
    )
    last_err = None
    for model in GROQ_MODELS:
        try:
            resp = ai_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": text},
                ],
                temperature=0.7,
                max_completion_tokens=1024,
                reasoning_effort="low",
            )
            content = (resp.choices[0].message.content or "").strip()
            if content:
                return content
        except Exception as e:
            last_err = e
            print(f"⚠️ 模型 {model} 失敗: {type(e).__name__}: {e}", flush=True)
    raise last_err or RuntimeError("AI 沒有回覆內容")


# ================= 回覆 =================
def send_reply(line_bot_api, event, messages):
    try:
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(reply_token=event.reply_token, messages=messages[:5])
        )
    except Exception as e:
        print(f"⚠️ reply 失敗，改用 push: {e}", flush=True)
        target = get_notify_target(event)
        if target:
            try:
                line_bot_api.push_message(PushMessageRequest(to=target, messages=messages[:5]))
            except Exception as e2:
                print(f"❌ push 也失敗: {e2}", flush=True)


def seen_before(key):
    if not key:
        return False
    with _seen_lock:
        if key in _seen:
            return True
        _seen.append(key)
        if len(_seen) > 500:
            del _seen[:250]
    return False


# ================= 逾時提醒 =================
def build_overdue_message(items, mention=True):
    """items: [(info, held_timedelta)]；mention=True 時在群組 @ 借用人"""
    lines = ["⏰ 歸還提醒"]
    substitution = {}
    for info, held in items:
        if mention and info.get("borrower_id") and len(substitution) < 20:
            key = f"user{len(substitution) + 1}"
            substitution[key] = {"type": "mention", "mentionee": {"type": "user", "userId": info["borrower_id"]}}
            who = "{" + key + "}"
        else:
            who = info.get("borrower") or "借用人"
        lines.append(f"{who} 借的【{info['name']}】已借用 {fmt_duration(held)}，記得歸還。")
    lines.append("可輸入「我借了什麼」按下歸還 🙏")
    text = "\n".join(lines)
    if substitution:
        return {"type": "textV2", "text": text, "substitution": substitution}
    return {"type": "text", "text": text}


def push_raw(to, message):
    r = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}", "Content-Type": "application/json"},
        json={"to": to, "messages": [message]},
        timeout=10,
    )
    return r


def check_overdue():
    now = now_tw()
    if in_quiet_hours(now):
        return f"安靜時段（{QUIET_START}:00–{QUIET_END}:00），不提醒"

    devices = get_all_devices(force=True)
    groups = {}  # target -> [(info, held)]
    for info in devices.values():
        if info["status"] != "Borrowed" or not info.get("borrowed_at"):
            continue
        held = now - info["borrowed_at"]
        if held < timedelta(hours=OVERDUE_HOURS):
            continue
        last = _last_reminded.get(info["id"])
        if last and now - last < timedelta(hours=REMIND_INTERVAL_HOURS):
            continue
        target = info.get("notify_to") or info.get("borrower_id")
        if not target:
            print(f"⚠️ {info['name']} 逾時但沒有可提醒的對象", flush=True)
            continue
        groups.setdefault(target, []).append((info, held))

    sent = 0
    for target, items in groups.items():
        is_group = target[:1] in ("C", "R")
        msg = build_overdue_message(items, mention=is_group)
        try:
            r = push_raw(target, msg)
            if r.status_code != 200 and msg["type"] == "textV2":
                print(f"⚠️ 提及(@)失敗 {r.status_code} {r.text[:120]}，改用純文字", flush=True)
                r = push_raw(target, build_overdue_message(items, mention=False))
            if r.status_code == 200:
                sent += 1
                for info, _ in items:
                    _last_reminded[info["id"]] = now
            else:
                print(f"❌ 提醒失敗 {r.status_code}: {r.text[:200]}", flush=True)
        except Exception as e:
            print(f"❌ 提醒例外: {e}", flush=True)

    return f"已發送 {sent} 則提醒（逾時設備 {sum(len(v) for v in groups.values())} 件）"


@app.route("/cron/overdue")
def cron_overdue():
    key = request.args.get("key", "")
    if not CRON_KEY or not hmac.compare_digest(key, CRON_KEY):
        abort(403)
    return check_overdue()


# ================= Webhook =================
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        print(f"❌ handler 錯誤: {type(e).__name__}: {e}", flush=True)
    return "OK"


@handler.add(PostbackEvent)
def handle_postback(event):
    if seen_before(getattr(event, "webhook_event_id", None)):
        return

    params = {k: v[0] for k, v in parse_qs(event.postback.data or "").items()}
    act = params.get("act")
    dev_id = params.get("id")
    view = params.get("v")
    print(f"🔘 按鈕: {params}", flush=True)

    devices = get_all_devices()

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        user_id, display_name = get_user_info(event, line_bot_api)
        print(f"👤 {display_name} / {user_id}", flush=True)

        messages = []
        if act in ("borrow", "return"):
            info = next((d for d in devices.values() if d["id"] == dev_id), None)
            if not info:
                messages.append(TextMessage(text=f"{display_name}：找不到這個設備，請輸入「選單」重新開啟。"))
            else:
                label = "借用" if act == "borrow" else "歸還"
                before = info["status"]
                if act == "borrow":
                    result = borrow_device(info, user_id, display_name, get_notify_target(event))
                else:
                    result = return_device(info, user_id, display_name)
                # 標明是誰按的（按鈕不會在聊天室留下文字，避免看起來像機器人自己亂發）
                messages.append(TextMessage(text=f"👤 {display_name} 按了「{label}」【{info['name']}】\n{result}"))
                # 操作有生效才附上最新卡片；沒生效（舊卡片、非本人等）只回短訊息，避免洗版
                if info["status"] != before:
                    if view == "mine":
                        messages += my_messages(devices, user_id, display_name)
                    else:
                        messages += menu_messages(devices)
        elif act == "mine":
            messages += my_messages(devices, user_id, display_name)
        else:
            messages += menu_messages(devices)

        send_reply(line_bot_api, event, messages)


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    if seen_before(event.message.id):
        return

    raw_text = event.message.text.strip()
    text = re.sub(r"@\S+\s*", "", raw_text).strip()
    print(f"💬 訊息: {text}", flush=True)
    if not text:
        return

    devices = get_all_devices()

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        user_id, display_name = get_user_info(event, line_bot_api)
        print(f"👤 {display_name} / {user_id}", flush=True)

        lower = text.lower()
        is_question = any(h in lower for h in QUESTION_HINTS)
        target_dev = find_device(text, devices)
        messages = []

        # 0. 我借了什麼
        if len(text) <= 10 and not target_dev and any(k in lower for k in MINE_KW):
            messages += my_messages(devices, user_id, display_name)

        # 1. 按鈕選單
        elif len(text) <= 10 and any(k in lower for k in MENU_KW):
            messages += menu_messages(devices)

        # 2. 文字指令：還
        elif target_dev and not is_question and any(k in lower for k in RETURN_KW):
            messages.append(TextMessage(text=return_device(devices[target_dev], user_id, display_name)))

        # 3. 文字指令：借
        elif target_dev and not is_question and any(k in lower for k in BORROW_KW):
            messages.append(TextMessage(
                text=borrow_device(devices[target_dev], user_id, display_name, get_notify_target(event))
            ))

        # 4. 只說「借 / 還」沒給設備 → 給按鈕選單
        elif re.match(r"^(借|還|borrow|return)", lower) and not is_question:
            messages.append(TextMessage(text="請點選下方按鈕，或輸入「借 設備名稱」/「還 設備名稱」。"))
            messages += menu_messages(devices)

        # 5. 其餘交給 AI
        else:
            if ai_client:
                try:
                    messages.append(TextMessage(text=ask_ai(text, display_name, devices)[:4900]))
                except Exception as e:
                    print(f"❌ Groq 錯誤: {type(e).__name__}: {e}", flush=True)
                    messages.append(TextMessage(text=f"哈囉 {display_name}！輸入「選單」就能用按鈕借還設備喔。"))
            else:
                messages.append(TextMessage(text=f"哈囉 {display_name}！輸入「選單」就能用按鈕借還設備喔。"))

        send_reply(line_bot_api, event, messages)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)