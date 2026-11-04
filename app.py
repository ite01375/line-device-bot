import os
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

# 首頁路由（讓直接打開網址時不會出現 404）
@app.route("/")
def home():
    return "LINE Bot is running successfully!"

# 請把下方字串替換成您的 LINE Channel Access Token 與 Channel Secret
CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN', 'emDP838ZCo+/MTkIgB/YLAJXsUhSV0p5Al/g1ltQk9bu8C7LU76IRKqCuCc5jZGH2Mna8Sv3AcABaODALtOhSINOq8L+ds2yZGJsIFX5QMx11dlrwIdDtWSvBcIfF5+lKMPJgsPuP8EOd98rk7unewdB04t89/1O/w1cDnyilFU=')
CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET', 'c3ff21ce567e4b2edd5ab3916d7fee53')

configuration = Configuration(access_token=CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# 設備模擬資料庫
devices = {
    "P01": {"name": "MacBook Pro", "status": "Available", "borrower": None},
    "P02": {"name": "iPad Air", "status": "Available", "borrower": None}
}

LANG = {
    "zh": {
        "help": "【設備借用指令】\n- 借用: /borrow [設備編號]\n- 歸還: /return [設備編號]\n- 狀態: /status\n- 切換語言: /lang [zh/en/ja]",
        "borrow_success": "成功借用設備：{name}",
        "borrow_fail": "設備 {name} 目前已被借走或不存在。",
        "return_success": "成功歸還設備：{name}",
        "not_borrowed": "此設備目前未被借用。",
        "lang_changed": "已切換語言為中文。"
    },
    "en": {
        "help": "[Device Commands]\n- Borrow: /borrow [ID]\n- Return: /return [ID]\n- Status: /status\n- Lang: /lang [zh/en/ja]",
        "borrow_success": "Successfully borrowed: {name}",
        "borrow_fail": "Device {name} is already borrowed or not found.",
        "return_success": "Successfully returned: {name}",
        "not_borrowed": "This device was not checked out.",
        "lang_changed": "Language switched to English."
    },
    "ja": {
        "help": "【備品貸出コマンド】\n- 借りる: /borrow [ID]\n- 返却: /return [ID]\n- ステータス: /status\n- 言語切替: /lang [zh/en/ja]",
        "borrow_success": "備品を借りました: {name}",
        "borrow_fail": "備品 {name} はすでに貸出中か存在しません。",
        "return_success": "備品を返却しました: {name}",
        "not_borrowed": "この備品は貸出されていません。",
        "lang_changed": "言語を日本語に切り替えました。"
    }
}

current_lang = "zh"

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
    global current_lang
    text = event.message.text.strip()
    
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        try:
            profile = line_bot_api.get_profile(event.source.user_id)
            user_name = profile.display_name
        except Exception:
            user_name = "User"

    reply_text = ""
    
    if text.startswith("/help") or text.startswith("help"):
        reply_text = LANG[current_lang]["help"]
    elif text.startswith("/lang"):
        parts = text.split()
        if len(parts) > 1 and parts[1] in LANG:
            current_lang = parts[1]
            reply_text = LANG[current_lang]["lang_changed"]
    elif text.startswith("/status"):
        reply_text = "📦 Device Status / 設備狀態:\n"
        for dev_id, info in devices.items():
            status_str = "Available (可用)" if info["status"] == "Available" else f"Borrowed by {info['borrower']}"
            reply_text += f"- [{dev_id}] {info['name']}: {status_str}\n"
    elif text.startswith("/borrow"):
        parts = text.split()
        if len(parts) > 1:
            dev_id = parts[1].upper()
            if dev_id in devices and devices[dev_id]["status"] == "Available":
                devices[dev_id]["status"] = "Borrowed"
                devices[dev_id]["borrower"] = user_name
                reply_text = LANG[current_lang]["borrow_success"].format(name=devices[dev_id]["name"])
            else:
                reply_text = LANG[current_lang]["borrow_fail"].format(name=dev_id)
    elif text.startswith("/return"):
        parts = text.split()
        if len(parts) > 1:
            dev_id = parts[1].upper()
            if dev_id in devices and devices[dev_id]["status"] == "Borrowed":
                devices[dev_id]["status"] = "Available"
                devices[dev_id]["borrower"] = None
                reply_text = LANG[current_lang]["return_success"].format(name=devices[dev_id]["name"])
            else:
                reply_text = LANG[current_lang]["not_borrowed"]

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