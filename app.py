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
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent
from groq import Groq

app = Flask(__name__)

@app.route("/")
def home():
    return "LINE AI Device Bot with Groq is running successfully!"

CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN', 'emDP838ZCo+/MTkIgB/YLAJXsUhSV0p5Al/g1ltQk9bu8C7LU76IRKqCuCc5jZGH2Mna8Sv3AcABaODALtOhSINOq8L+ds2yZGJsIFX5QMx11dlrwIdDtWSvBcIfF5+lKMPJgsPuP8EOd98rk7unewdB04t89/1O/w1cDnyilFU=')
CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET', 'c3ff21ce567e4b2edd5ab3916d7fee53')
GAS_WEB_APP_URL = os.getenv('GAS_WEB_APP_URL', 'https://script.google.com/macros/s/AKfycbxNmWQ_gyyZsqg-6fXaz6PU0MPIwAUVYGDB05JrBfk/exec')

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# 初始化 Groq AI Client
groq_api_key = os.getenv('GROQ_API_KEY')
print(f"🔑 Groq API Key 是否存在: {bool(groq_api_key)}", flush=True)
ai_client = Groq(api_key=groq_api_key) if groq_api_key else None

def get_all_devices():
    """動態從 Google 試算表 Devices 分頁取得最新設備清單"""
    print(f"🔍 正在連線請求 GAS 網址: {GAS_WEB_APP_URL}", flush=True)
    try:
        response = requests.get(GAS_WEB_APP_URL)
        if response.status_code == 200:
            rows = response.json()
            devices_dict = {}
            for row in rows:
                name = row["name"]
                devices_dict[name] = {
                    "id": row["id"],
                    "name": name,
                    "status": "Borrowed" if row["status"] == "Borrowed" else "Available",
                    "borrower": row["borrower"] if row["borrower"] else None
                }
            return devices_dict
    except Exception as e:
        print(f"❌ 動態讀取設備清單失敗: {e}", flush=True)
    return {}

def update_google_sheet(dev_id, dev_name, status, borrower):
    """更新 Devices 狀態，並在 History 寫入一筆歷史紀錄"""
    if not GAS_WEB_APP_URL:
        return
    
    payload = {
        "dev_id": dev_id,
        "dev_name": dev_name,
        "status": status,
        "borrower": borrower
    }
    
    try:
        requests.post(GAS_WEB_APP_URL, json=payload)
    except Exception as e:
        print(f"連線至 Google 試算表失敗: {e}", flush=True)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    except Exception:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    raw_text = event.message.text.strip()
    
    # 清除 LINE 群組標註機器人的字串（例如 @健身小秘書）
    text = re.sub(r'@\S+\s*', '', raw_text).strip()
    print(f"💬 處理後的訊息內容: {text}", flush=True)

    devices = get_all_devices()
    
    user_id = event.source.user_id
    display_name = ""
    
    # 嘗試取得 LINE 使用者顯示名稱
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        try:
            profile = line_bot_api.get_profile(user_id)
            if profile and profile.display_name:
                display_name = profile.display_name
        except Exception as e:
            print(f"⚠️ 無法取得 Profile: {e}", flush=True)

    # 備用對應
    if not display_name:
        if "Uc63582a" in user_id:
            display_name = "林大衛"
        else:
            display_name = f"會員_{user_id[-4:]}"

    borrower_name = display_name
    reply_text = ""
    
    # 1. 查詢狀態指令
    if any(keyword in text for keyword in ["狀態", "表單", "清單", "查詢", "status"]):
        reply_text = "📦 目前設備借還狀態:\n"
        for key, info in devices.items():
            status_str = "🟢 可借用" if info["status"] == "Available" else f"🔴 已被借用"
            reply_text += f"- {info['name']}: {status_str}\n"

    # 2. 還設備指令
    elif any(kw in text for kw in ["還", "退", "return"]):
        target_dev = None
        for key in devices:
            if key in text:
                target_dev = key
                break
        
        if target_dev:
            dev_info = devices[target_dev]
            if dev_info["status"] == "Borrowed":
                reply_text = f"感謝 {display_name}！已幫您完成【{dev_info['name']}】的歸還登記。"
                update_google_sheet(dev_info["id"], dev_info["name"], "Available", "")
            else:
                reply_text = f"【{dev_info['name']}】目前本來就是可借用狀態，沒有被借出喔！"
        else:
            reply_text = "找不到您要歸還的設備名稱，請輸入「還 設備名稱」。"

    # 3. 借設備指令
    elif any(kw in text for kw in ["借", "拿", "用", "borrow"]):
        target_dev = None
        for key in devices:
            if key in text:
                target_dev = key
                break
        
        if target_dev:
            dev_info = devices[target_dev]
            if dev_info["status"] == "Available":
                reply_text = f"沒問題！已幫 {display_name} 登記借用【{dev_info['name']}】祝您使用順利！"
                update_google_sheet(dev_info["id"], dev_info["name"], "Borrowed", borrower_name)
            else:
                reply_text = f"不好意思，【{dev_info['name']}】目前已經被借走了喔！"
        else:
            reply_text = f"收到您的借用需求，但找不到您想借的設備名稱，請輸入「借 設備名稱」。"

    # 4. 其他所有對話：交給 Groq (Llama 3.3) 進行超高速多國語言 AI 自由聊天
    else:
        if ai_client:
            try:
                print(f"🤖 正在透過 Groq 呼叫 Llama 3 產生回覆...", flush=True)
                chat_completion = ai_client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[
                        {
                            "role": "system",
                            "content": f"你是一個親切的 LINE 設備管理與 AI 助理。使用者名稱是 {display_name}。請以對方使用的語言（支援中文、英文、日文、韓文）親切、自然地回覆。"
                        },
                        {
                            "role": "user",
                            "content": text
                        }
                    ],
                    temperature=0.7,
                    max_tokens=500
                )
                reply_text = chat_completion.choices[0].message.content
                print(f"🤖 Groq 回覆成功", flush=True)
            except Exception as e:
                print(f"❌ Groq API 發生錯誤: {e}", flush=True)
                reply_text = f"哈囉 {display_name}！您可以對我說「借 設備名稱」、「還 設備名稱」或各種問題。"
        else:
            reply_text = f"哈囉 {display_name}！您可以直接對我說：\n1. 「借 設備名稱」\n2. 「還 設備名稱」\n3. 「查詢狀態」"

    if reply_text:
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)]
                )
            )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)