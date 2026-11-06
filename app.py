import os
import re
import time
import threading
from datetime import datetime, timezone, timedelta

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
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
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

STATUS_KW = ["狀態", "表單", "清單", "查詢", "status"]
RETURN_KW = ["還", "退", "return"]
BORROW_KW = ["借", "拿", "用", "borrow"]
QUESTION_HINTS = ["怎麼", "如何", "什麼", "為什麼", "嗎", "?", "？", "how", "what", "why"]

# ---------- 快取 ----------
DEVICE_TTL = 20
_dev_cache = {"data": {}, "ts": 0.0}
_profile_cache = {}
_seen_msgs = []          # 已處理的 message id（去重）
_seen_lock = threading.Lock()


@app.route("/")
def home():
    return "LINE AI Device Bot with Groq is running successfully!"


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
                    "id": row["id"],
                    "name": name,
                    "status": "Borrowed" if row["status"] == "Borrowed" else "Available",
                    "borrower": row["borrower"] or None,
                }
            _dev_cache["data"] = devices
            _dev_cache["ts"] = now
            return devices
    except Exception as e:
        print(f"❌ 讀取設備清單失敗: {e}", flush=True)
    return _dev_cache["data"]  # 失敗時使用舊資料


def _write_sheet(payload):
    try:
        r = requests.post(GAS_WEB_APP_URL, json=payload, timeout=20)
        print(f"📝 GAS 回應 {r.status_code}: {r.text[:100]}", flush=True)
    except Exception as e:
        print(f"連線至 Google 試算表失敗: {e}", flush=True)


def update_google_sheet_async(dev_id, dev_name, status, borrower, user_id, operator):
    """背景寫入，不阻塞回覆"""
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
        "若使用者想借/還設備，請告訴他輸入「借 設備名稱」或「還 設備名稱」。\n"
        f"目前設備狀態：\n{status_lines or '（無資料）'}"
    )
    resp = ai_client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        temperature=0.7,
        max_tokens=500,
    )
    return resp.choices[0].message.content


def send_reply(line_bot_api, event, text):
    text = text[:4900]
    try:
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(reply_token=event.reply_token, messages=[TextMessage(text=text)])
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
                line_bot_api.push_message(
                    PushMessageRequest(to=target, messages=[TextMessage(text=text)])
                )
            except Exception as e2:
                print(f"❌ push 也失敗: {e2}", flush=True)


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        # 一律回 200，避免 LINE 重送事件造成重複借還
        print(f"❌ handler 錯誤: {type(e).__name__}: {e}", flush=True)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    # 去重
    msg_id = event.message.id
    with _seen_lock:
        if msg_id in _seen_msgs:
            return
        _seen_msgs.append(msg_id)
        if len(_seen_msgs) > 500:
            del _seen_msgs[:250]

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
        reply_text = ""
        sheet_job = None

        if len(text) <= 10 and any(k in lower for k in STATUS_KW):
            reply_text = "📦 目前設備借還狀態:\n"
            for info in devices.values():
                s = "🟢 可借用" if info["status"] == "Available" else "🔴 已被借用"
                reply_text += f"- {info['name']}: {s}\n"

        elif target_dev and not is_question and any(k in lower for k in RETURN_KW):
            info = devices[target_dev]
            if info["status"] == "Borrowed":
                reply_text = f"感謝 {display_name}！已完成【{info['name']}】的歸還登記。"
                info["status"], info["borrower"] = "Available", None
                _dev_cache["ts"] = time.time()
                sheet_job = (info["id"], info["name"], "Available", "", user_id, display_name)
            else:
                reply_text = f"【{info['name']}】目前本來就是可借用狀態喔！"

        elif target_dev and not is_question and any(k in lower for k in BORROW_KW):
            info = devices[target_dev]
            if info["status"] == "Available":
                reply_text = f"沒問題！已幫 {display_name} 登記借用【{info['name']}】，祝使用順利！"
                info["status"], info["borrower"] = "Borrowed", display_name
                _dev_cache["ts"] = time.time()
                sheet_job = (info["id"], info["name"], "Borrowed", display_name, user_id, display_name)
            else:
                reply_text = f"不好意思，【{info['name']}】目前已經被借走了喔！"

        elif re.match(r"^(借|還|borrow|return)", lower) and not is_question:
            names = "、".join(devices.keys()) or "（無資料）"
            reply_text = f"請輸入「借 設備名稱」或「還 設備名稱」。\n可用設備：{names}"

        else:
            if ai_client:
                try:
                    reply_text = ask_ai(text, display_name, devices)
                except Exception as e:
                    print(f"❌ Groq 錯誤: {type(e).__name__}: {e}", flush=True)
                    reply_text = f"哈囉 {display_name}！您可以說「借 設備名稱」、「還 設備名稱」或「查詢狀態」。"
            else:
                reply_text = f"哈囉 {display_name}！您可以說：\n1. 「借 設備名稱」\n2. 「還 設備名稱」\n3. 「查詢狀態」"

        # 先回覆，再寫試算表
        if reply_text:
            send_reply(line_bot_api, event, reply_text)
        if sheet_job:
            update_google_sheet_async(*sheet_job)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)