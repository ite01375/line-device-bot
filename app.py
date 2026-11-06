import os
import re
import requests
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from groq import Groq

app = Flask(__name__)

# ⚠️ 金鑰只從環境變數讀取，不要寫死在程式碼裡
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
GAS_WEB_APP_URL = os.getenv("GAS_WEB_APP_URL")

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

groq_api_key = os.getenv("GROQ_API_KEY")
ai_client = Groq(api_key=groq_api_key) if groq_api_key else None
print(f"🔑 Groq client 已啟用: {ai_client is not None}", flush=True)

STATUS_KW = ["狀態", "表單", "清單", "查詢", "status"]
RETURN_KW = ["還", "退", "return"]
BORROW_KW = ["借", "拿", "用", "borrow"]
QUESTION_HINTS = ["怎麼", "如何", "什麼", "為什麼", "嗎", "?", "？", "how", "what", "why"]


@app.route("/")
def home():
    return "LINE AI Device Bot with Groq is running successfully!"


def get_all_devices():
    try:
        response = requests.get(GAS_WEB_APP_URL, timeout=8)
        if response.status_code == 200:
            devices = {}
            for row in response.json():
                name = row["name"]
                devices[name] = {
                    "id": row["id"],
                    "name": name,
                    "status": "Borrowed" if row["status"] == "Borrowed" else "Available",
                    "borrower": row["borrower"] or None,
                }
            return devices
    except Exception as e:
        print(f"❌ 讀取設備清單失敗: {e}", flush=True)
    return {}


def update_google_sheet(dev_id, dev_name, status, borrower, user_id):
    if not GAS_WEB_APP_URL:
        return
    payload = {
        "dev_id": dev_id,
        "dev_name": dev_name,
        "status": status,
        "borrower": borrower,
        "user_id": user_id,  # ← 新增：LINE userId
    }
    try:
        r = requests.post(GAS_WEB_APP_URL, json=payload, timeout=8)
        print(f"📝 GAS 回應 {r.status_code}: {r.text[:100]}", flush=True)
    except Exception as e:
        print(f"連線至 Google 試算表失敗: {e}", flush=True)


def get_user_info(event, line_bot_api):
    """回傳 (user_id, display_name)，依來源類型使用正確的 Profile API"""
    src = event.source
    user_id = getattr(src, "user_id", None)
    src_type = getattr(src, "type", "user")
    display_name = ""

    if user_id:
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

    if not display_name:
        display_name = f"用戶_{user_id[-4:]}" if user_id else "未知用戶"
    return user_id or "", display_name


def find_device(text, devices):
    """名稱長的優先比對，避免短名稱誤中"""
    for name in sorted(devices, key=len, reverse=True):
        if name in text:
            return name
    return None


def ask_ai(text, display_name, devices):
    status_lines = "\n".join(
        f"- {d['name']}: {'已借出' if d['status'] == 'Borrowed' else '可借用'}"
        for d in devices.values()
    )
    system_prompt = (
        f"你是一個親切的 LINE 健身房設備管理與 AI 助理，正在跟「{display_name}」對話。\n"
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


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception as e:
        print(f"❌ handler 錯誤: {e}", flush=True)
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
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

        # 1. 查詢狀態（短句才算指令）
        if len(text) <= 10 and any(k in lower for k in STATUS_KW):
            reply_text = "📦 目前設備借還狀態:\n"
            for info in devices.values():
                s = "🟢 可借用" if info["status"] == "Available" else "🔴 已被借用"
                reply_text += f"- {info['name']}: {s}\n"

        # 2. 還設備：需有設備名稱 + 動作詞，且不是疑問句
        elif target_dev and not is_question and any(k in lower for k in RETURN_KW):
            info = devices[target_dev]
            if info["status"] == "Borrowed":
                reply_text = f"感謝 {display_name}！已完成【{info['name']}】的歸還登記。"
                update_google_sheet(info["id"], info["name"], "Available", "", user_id)
            else:
                reply_text = f"【{info['name']}】目前本來就是可借用狀態喔！"

        # 3. 借設備
        elif target_dev and not is_question and any(k in lower for k in BORROW_KW):
            info = devices[target_dev]
            if info["status"] == "Available":
                reply_text = f"沒問題！已幫 {display_name} 登記借用【{info['name']}】，祝使用順利！"
                update_google_sheet(info["id"], info["name"], "Borrowed", display_name, user_id)
            else:
                reply_text = f"不好意思，【{info['name']}】目前已經被借走了喔！"

        # 4. 以「借 / 還」開頭但沒給設備名稱 → 提示
        elif re.match(r"^(借|還|borrow|return)\b?", lower) and not is_question:
            names = "、".join(devices.keys()) or "（無資料）"
            reply_text = f"請輸入「借 設備名稱」或「還 設備名稱」。\n可用設備：{names}"

        # 5. 其餘全部交給 AI
        else:
            if ai_client:
                try:
                    reply_text = ask_ai(text, display_name, devices)
                except Exception as e:
                    print(f"❌ Groq 錯誤: {type(e).__name__}: {e}", flush=True)
                    reply_text = f"哈囉 {display_name}！您可以說「借 設備名稱」、「還 設備名稱」或「查詢狀態」。"
            else:
                reply_text = f"哈囉 {display_name}！您可以說：\n1. 「借 設備名稱」\n2. 「還 設備名稱」\n3. 「查詢狀態」"

        if reply_text:
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text[:4900])],
                )
            )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)