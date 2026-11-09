import os
import re
import json
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

# 可用環境變數 GROQ_MODEL 指定模型；失敗時依序嘗試後備模型
GROQ_MODELS = [m for m in [os.getenv("GROQ_MODEL"), "openai/gpt-oss-20b", "openai/gpt-oss-120b"] if m]

# 這些關鍵字（短句）會顯示按鈕選單
MENU_KW = ["選單", "菜單", "面板", "按鈕", "menu", "狀態", "表單", "清單", "查詢", "status"]
RETURN_KW = ["還", "退", "return"]
BORROW_KW = ["借", "拿", "用", "borrow"]
QUESTION_HINTS = ["怎麼", "如何", "什麼", "為什麼", "嗎", "?", "？", "how", "what", "why"]

# ---------- 快取 ----------
DEVICE_TTL = 20
_dev_cache = {"data": {}, "ts": 0.0}
_profile_cache = {}
_seen = []               # 已處理的事件 id（去重）
_seen_lock = threading.Lock()


@app.route("/")
def home():
    return "LINE AI Device Bot with Groq is running successfully!"


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


def update_google_sheet_async(dev_id, dev_name, status, borrower, user_id, operator):
    if not GAS_WEB_APP_URL:
        return
    payload = {
        "dev_id": dev_id,
        "dev_name": dev_name,
        "status": status,
        "borrower": borrower,
        "user_id": user_id,
        "operator": operator,
    }
    threading.Thread(target=_write_sheet, args=(payload,), daemon=True).start()


# ================= 借 / 還 =================
def borrow_device(info, user_id, display_name):
    if info["status"] == "Available":
        info["status"], info["borrower"] = "Borrowed", display_name
        _dev_cache["ts"] = time.time()
        update_google_sheet_async(info["id"], info["name"], "Borrowed", display_name, user_id, display_name)
        return f"沒問題！已幫 {display_name} 登記借用【{info['name']}】，祝使用順利！"
    who = f"（{info['borrower']}）" if info["borrower"] else ""
    return f"不好意思，【{info['name']}】目前已經被借走了喔！{who}"


def return_device(info, user_id, display_name):
    if info["status"] == "Borrowed":
        info["status"], info["borrower"] = "Available", None
        _dev_cache["ts"] = time.time()
        update_google_sheet_async(info["id"], info["name"], "Available", "", user_id, display_name)
        return f"感謝 {display_name}！已完成【{info['name']}】的歸還登記。"
    return f"【{info['name']}】目前本來就是可借用狀態喔！"


# ================= Flex 按鈕介面 =================
GREEN = "#1DB446"
RED = "#E53935"
ROWS_PER_BUBBLE = 6


def _device_row(info):
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
                "action": {
                    "type": "postback",
                    "label": label,
                    "data": f"act={act}&id={info['id']}",
                },
            },
        ],
    }


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
                "contents": [{
                    "type": "button",
                    "style": "secondary",
                    "height": "sm",
                    "action": {"type": "postback", "label": "🔄 重新整理", "data": "act=menu"},
                }],
            },
        })
    contents = bubbles[0] if len(bubbles) == 1 else {"type": "carousel", "contents": bubbles}
    return FlexMessage(
        alt_text="🏋️ 設備借還狀態（請在手機查看按鈕）",
        contents=FlexContainer.from_json(json.dumps(contents, ensure_ascii=False)),
    )


def menu_messages(devices):
    if not devices:
        return [TextMessage(text="目前讀不到設備清單，請稍後再試。")]
    return [build_device_flex(devices)]


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


def find_device(text, devices):
    for name in sorted(devices, key=len, reverse=True):
        if name in text:
            return name
    return None


# ================= AI =================
def ask_ai(text, display_name, devices):
    now = datetime.now(timezone(timedelta(hours=8)))
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
        "若使用者想借/還設備，請告訴他輸入「選單」開啟按鈕介面，或輸入「借 設備名稱」/「還 設備名稱」。\n"
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
        src = event.source
        target = (
            getattr(src, "group_id", None)
            or getattr(src, "room_id", None)
            or getattr(src, "user_id", None)
        )
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
                messages.append(TextMessage(text="找不到這個設備，請重新開啟選單。"))
            else:
                result = borrow_device(info, user_id, display_name) if act == "borrow" \
                    else return_device(info, user_id, display_name)
                messages.append(TextMessage(text=result))
                messages += menu_messages(devices)  # 附上最新的按鈕選單
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

        # 1. 按鈕選單
        if len(text) <= 10 and any(k in lower for k in MENU_KW):
            messages += menu_messages(devices)

        # 2. 文字指令：還
        elif target_dev and not is_question and any(k in lower for k in RETURN_KW):
            messages.append(TextMessage(text=return_device(devices[target_dev], user_id, display_name)))

        # 3. 文字指令：借
        elif target_dev and not is_question and any(k in lower for k in BORROW_KW):
            messages.append(TextMessage(text=borrow_device(devices[target_dev], user_id, display_name)))

        # 4. 只說「借 / 還」沒給設備 → 直接給按鈕選單
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