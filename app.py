import os
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

app = Flask(__name__)

# 首頁路由
@app.route("/")
def home():
    return "LINE AI Device Bot is running successfully!"

# LINE 金鑰設定
CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN', 'emDP838ZCo+/MTkIgB/YLAJXsUhSV0p5Al/g1ltQk9bu8C7LU76IRKqCuCc5jZGH2Mna8Sv3AcABaODALtOhSINOq8L+ds2yZGJsIFX5QMx11dlrwIdDtWSvBcIfF5+lKMPJgsPuP8EOd98rk7unewdB04t89/1O/w1cDnyilFU=')
CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET', 'c3ff21ce567e4b2edd5ab3916d7fee53')

# Google Apps Script 網頁應用程式網址
GAS_WEB_APP_URL = os.getenv('GAS_WEB_APP_URL', 'https://script.google.com/macros/s/AKfycbxNmWQ_gyyZsqg-6fXaz6PU0MPIwAUVYGDB05JrBfk/dev')

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# 設備模擬資料庫 (名稱對應您的設備)
devices = {
    "插銷": {"id": "P01", "name": "插銷", "status": "Available", "borrower": None},
    
}

def update_google_sheet(dev_id, status, borrower):
    """將借還狀態同步至 Google 試算表"""
    if not GAS_WEB_APP_URL:
        print("未設定 GAS_WEB_APP_URL")
        return
    
    payload = {
        "dev_id": dev_id,
        "status": status,
        "borrower": borrower
    }
    
    try:
        response = requests.post(GAS_WEB_APP_URL, json=payload)
        print("Google 試算表更新回應:", response.text)
    except Exception as e:
        print("連線至 Google 試算表失敗:", e)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError as e:
        app.logger.error(f"Invalid signature error: {e}")
        abort(400)
    except Exception as e:
        app.logger.error(f"Other error: {e}")
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = event.message.text.strip()
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        try:
            profile = line_bot_api.get_profile(event.source.user_id)
            user_name = profile.display_name
        except Exception:
            user_name = "訪客"

    reply_text = ""
    
    # 支援口語化辨識：包含「借」或「拿」
    if "借" in text or "拿" in text:
        target_dev = None
        for key in devices:
            if key in text:
                target_dev = key
                break
        
        if target_dev:
            dev_info = devices[target_dev]
            if dev_info["status"] == "Available":
                dev_info["status"] = "Borrowed"
                dev_info["borrower"] = user_name
                reply_text = f"沒問題！已幫 {user_name} 登記借用【{dev_info['name']}】祝您使用順利！"
                # 同步到 Google 試算表
                update_google_sheet(dev_info["id"], "Borrowed", user_name)
            else:
                reply_text = f"不好意思，【{dev_info['name']}】目前已經被 {dev_info['borrower']} 借走了喔！"
        else:
            reply_text = f"收到您的借用需求，但找不到您想借的設備名稱。您可以試著說：「借插銷」"

    # 支援口語化辨識：包含「還」
    elif "還" in text:
        target_dev = None
        for key in devices:
            if key in text:
                target_dev = key
                break
        
        if target_dev:
            dev_info = devices[target_dev]
            if dev_info["status"] == "Borrowed":
                dev_info["status"] = "Available"
                dev_info["borrower"] = None
                reply_text = f"感謝 {user_name}！已幫您完成【{dev_info['name']}】的歸還登記。"
                # 同步到 Google 試算表（清空借用人）
                update_google_sheet(dev_info["id"], "Available", "")
            else:
                reply_text = f"【{dev_info['name']}】目前本來就是可借用狀態，沒有被借出喔！"
        else:
            reply_text = "找不到您要歸還的設備名稱。"

    # 查詢狀態指令
    elif "狀態" in text or "表單" in text or "清單" in text:
        reply_text = "📦 目前設備借還狀態:\n"
        for key, info in devices.items():
            status_str = "🟢 可借用" if info["status"] == "Available" else f"🔴 已被 {info['borrower']} 借用"
            reply_text += f"- {info['name']}: {status_str}\n"
    else:
        reply_text = f"哈囉 {user_name}！您可以直接對我說：\n1. 「借插銷」\n2. 「還插銷」\n3. 「查詢狀態」"

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