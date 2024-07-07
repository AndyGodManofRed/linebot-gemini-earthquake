from fastapi import FastAPI, HTTPException, Request
import logging
import os
import re
import sys
from datetime import datetime
from dotenv import load_dotenv
from linebot.v3.webhook import WebhookParser
from linebot.v3.messaging import (
    AsyncApiClient,
    AsyncMessagingApi,
    Configuration,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent

import uvicorn
import requests
import google.generativeai as genai
from firebase import firebase
import random

logging.basicConfig(level=os.getenv('LOG', 'WARNING'))
logger = logging.getLogger(__file__)

app = FastAPI()

load_dotenv()
channel_secret = os.getenv('LINE_CHANNEL_SECRET', None)
channel_access_token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN', None)
if channel_secret is None:
    print('Specify LINE_CHANNEL_SECRET as environment variable.')
    sys.exit(1)
if channel_access_token is None:
    print('Specify LINE_CHANNEL_ACCESS_TOKEN as環境變數.')
    sys.exit(1)

configuration = Configuration(access_token=channel_access_token)
async_api_client = AsyncApiClient(configuration)
line_bot_api = AsyncMessagingApi(async_api_client)
parser = WebhookParser(channel_secret)

firebase_url = os.getenv('FIREBASE_URL')
gemini_key = os.getenv('GEMINI_API_KEY')
genai.configure(api_key=gemini_key)

scam_templates = [
    "【國泰世華】您的銀行賬戶顯示異常，請立即登入綁定用戶資料，否則賬戶將凍結使用 www.cathay-bk.com",
    "我朋友參加攝影比賽麻煩幫忙投票 http://www.yahoonikk.info/page/vote.pgp?pid=51",
    "登入FB就投票成功了我手機當機 line用不了 想請你幫忙安全認證 幫我收個認證簡訊 謝謝 你LINE的登陸認證密碼記得嗎 認證要用到 確認是本人幫忙認證",
    "您的LINE已違規使用，將在24小時內註銷，請使用谷歌瀏覽器登入電腦網站並掃碼驗證解除違規 www.line-wbe.icu",
    "【台灣自來水公司】貴戶本期水費已逾期，總計新台幣395元整，務請於6月16日前處理繳費，詳情繳費：https://bit.ly/4cnMNtE 若再超過上述日期，將終止供水",
    "萬聖節快樂🎃 活動免費貼圖無限量下載 https://lineeshop.com",
    "【台灣電力股份有限公司】貴戶本期電費已逾期，總計新台幣1058元整，務請於6月14日前處理繳費，詳情繳費：(網址)，若再超過上述日期，將停止收費"
]

@app.get("/health")
async def health():
    return 'ok'

@app.post("/webhooks/line")
async def handle_callback(request: Request):
    signature = request.headers['X-Line-Signature']

    body = await request.body()
    body = body.decode()

    try:
        events = parser.parse(body, signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    for event in events:
        logging.info(event)
        if not isinstance(event, MessageEvent):
            continue
        if not isinstance(event.message, TextMessageContent):
            continue
        text = event.message.text.strip()
        user_id = event.source.user_id

        fdb = firebase.FirebaseApplication(firebase_url, None)
        if event.source.type == 'group':
            user_chat_path = f'chat/{event.source.group_id}'
        else:
            user_chat_path = f'chat/{user_id}'
        chatgpt = fdb.get(user_chat_path, None)

        user_score_path = f'scores/{user_id}'
        user_score = fdb.get(user_score_path, None) or 0

        if text == "出題":
            scam_example, correct_example = generate_examples()
            messages = [{'role': 'bot', 'parts': [scam_example, correct_example]}]
            fdb.put_async(user_chat_path, None, messages)
            reply_msg = f"訊息:\n\n{scam_example}\n\n請判斷這是否為詐騙訊息（請回覆'是'或'否'）"
        elif text == "分數":
            reply_msg = f"你的當前分數是：{user_score}分"
        elif text in ["是", "否"]:
            if chatgpt and len(chatgpt) > 0 and chatgpt[-1]['role'] == 'bot':
                scam_message, correct_message = chatgpt[-1]['parts']
                is_scam = scam_message is not None
                user_response = text == "是"
                
                if user_response == is_scam:
                    user_score += 50
                    fdb.put_async(user_score_path, None, user_score)
                    reply_msg = f"你好棒！你的當前分數是：{user_score}分"
                else:
                    user_score -= 50
                    fdb.put_async(user_score_path, None, user_score)
                    advice = analyze_response(scam_message if is_scam else correct_message, is_scam, user_response)
                    reply_msg = f"這是{'詐騙' if is_scam else '正確'}訊息。分析如下:\n\n{advice}\n\n你的當前分數是：{user_score}分"
            else:
                reply_msg = '目前沒有可供解析的訊息，請先輸入「出題」生成一個範例。'
        elif text == "排行榜":
            reply_msg=get_rank(user_id,firebase_url)
        await line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_msg)]
            ))

    return 'OK'

def generate_examples():
    scam_template = random.choice(scam_templates)
    prompt_scam = (
        f"以下是一個詐騙訊息範例:\n\n{scam_template}\n\n"
        "請根據這個範例生成一個新的、類似的詐騙訊息。保持相似的結構和風格，"
        "但改變具體內容。請確保新生成的訊息具有教育性質，可以用於提高人們對詐騙的警惕性。"
        "只需要生成詐騙訊息本身，不要添加任何額外的說明或指示。"
    )
    prompt_correct = (
        f"請生成一個真實且正確的訊息範例，其風格和結構類似於以下的詐騙訊息範例，但內容是真實且正確的:\n\n{scam_template}"
    )

    model = genai.GenerativeModel('gemini-pro')
    scam_response = model.generate_content(prompt_scam)
    correct_response = model.generate_content(prompt_correct)
    return scam_response.text.strip(), correct_response.text.strip()

def analyze_response(text, is_scam, user_response):
    if user_response == is_scam:
        # 如果用户回答正确
        if is_scam:
            prompt = (
                f"以下是一個詐騙訊息:\n\n{text}\n\n"
                "請分析這條訊息，並提供詳細的辨別建議。包括以下幾點：\n"
                "1. 這條訊息中的可疑元素\n"
                "2. 為什麼這些元素是可疑的\n"
                "3. 如何識別類似的詐騙訊息\n"
                "4. 面對這種訊息時應該採取什麼行動\n"
                "請以教育性和提醒性的語氣回答，幫助人們提高警惕。"
                "不要使用任何粗體或任何特殊格式，例如＊或是-，不要使用markdown語法，只需使用純文本。不要使用破折號，而是使用數字列表。"
            )
        else:
            prompt = (
                f"以下是一個真實且正確的訊息:\n\n{text}\n\n"
                "請分析這條訊息，並提供詳細的辨別建議。包括以下幾點：\n"
                "1. 這條訊息中的真實元素\n"
                "2. 為什麼這些元素是真實的\n"
                "3. 如何識別類似的真實訊息\n"
                "4. 面對這種訊息時應該採取什麼行動\n"
                "請以教育性和提醒性的語氣回答，幫助人們提高辨別真實訊息的能力。"
                "不要使用任何粗體或任何特殊格式，例如＊或是-，不要使用markdown語法，只需使用純文本。不要使用破折號，而是使用數字列表。"
            )
    else:
        # 如果用户回答错误
        if is_scam:
            prompt = (
                f"以下是一個詐騙訊息:\n\n{text}\n\n"
                "用教育性和提醒性的語氣，指出這是詐騙訊息。請提供詳細的辨別建議。包括以下幾點：\n"
                "1. 這條訊息中的可疑元素\n"
                "2. 為什麼這些元素是可疑的\n"
                "3. 如何識別類似的詐騙訊息\n"
                "4. 面對這種訊息時應該採取什麼行動\n"
                "請以教育性和提醒性的語氣回答，幫助人們提高警惕。"
                "不要使用任何粗體或任何特殊格式，例如＊或是-，不要使用markdown語法，只需使用純文本。不要使用破折號，而是使用數字列表。"
            )
        else:
            prompt = (
                f"以下是一個真實且正確的訊息:\n\n{text}\n\n"
                "用教育性和提醒性的語氣，指出這是真實且正確的訊息。請提供詳細的辨別建議。包括以下幾點：\n"
                "1. 這條訊息中的真實元素\n"
                "2. 為什麼這些元素是真實的\n"
                "3. 如何識別類似的真實訊息\n"
                "4. 面對這種訊息時應該採取什麼行動\n"
                "請以教育性和提醒性的語氣回答，幫助人們提高辨別真實訊息的能力。"
                "不要使用任何粗體或任何特殊格式，例如＊或是-，不要使用markdown語法，只需使用純文本。不要使用破折號，而是使用數字列表。"
            )
    model = genai.GenerativeModel('gemini-pro')
    response = model.generate_content(prompt)
    return response.text.strip()


def get_sorted_scores(firebase_url,path):

    fdb = firebase.FirebaseApplication(firebase_url, None)
    # 從 Firebase 獲取 score 節點下的所有資料
    scores = fdb.get(path, None)
    
    if scores:
        # 將資料轉換成 (user, score) 的列表
        score_list = [(user, score) for user, score in scores.items()]
        # 按照分數進行排序，從高到低
        sorted_score_list = sorted(score_list, key=lambda x: x[1], reverse=True)
        return sorted_score_list
    else:
        return []


def get_rank(current_user_id,firebase_url):

    # 設定表格的欄位寬度
    rank_width = 7
    user_width = 14
    score_width = 11
    total_width = rank_width + user_width + score_width + 4  # 包括分隔符號

    sorted_scores = get_sorted_scores(firebase_url,'scores/')

    # 初始化表格字串
    table_str = ''

    # 表格頂部邊界
    table_str += '+' + '-' * total_width + '+\n'
    table_str += '|' + "排行榜".center(total_width-3) + '|\n'
    table_str += '+' + '-' * total_width + '+\n'
    table_str += f"|{'排名'.center(rank_width)}|{'User'.center(user_width)}|{'Score'.center(score_width)}|\n"
    table_str += '+' + '-' * rank_width + '+' + '-' * user_width + '+' + '-' * score_width + '+\n'

    if sorted_scores:
        i = 1
        for user, score in sorted_scores:
            # 標記當前使用者
            if user == current_user_id:
                user_display = f'*{user[:user_width]}*'
            else:
                user_display = user[:5]

            table_str += f"|{str(i).center(rank_width)}|{user_display.center(user_width)}|{str(score).center(score_width)}|\n"
            table_str += '+' + '-' * rank_width + '+' + '-' * user_width + '+' + '-' * score_width + '+\n'
            i += 1
    else:
        table_str += '|' + '目前無人上榜'.center(total_width) + '|\n'
        table_str += '+' + '-' * total_width + '+\n'
    return table_str

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
