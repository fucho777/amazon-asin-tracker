import os
import json
import logging
import requests
import hashlib
import hmac
import argparse
import tweepy
import time
import sys
from datetime import datetime
from dotenv import load_dotenv

# 設定変数
DEBUG_MODE = False  # 本番環境ではFalse
DRY_RUN = False     # Trueの場合、SNSへの投稿をシミュレートのみ

# ログ設定
logging.basicConfig(
    level=logging.DEBUG if DEBUG_MODE else logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("asin_tracker.log"),
        logging.StreamHandler()  # 標準出力にも表示
    ]
)
logger = logging.getLogger("asin-tracker")

# スクリプト開始時のメッセージ
logger.info("==== Amazon ASIN Tracker 実行開始 ====")
logger.info(f"実行時刻: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# 環境変数の読み込み
load_dotenv()
logger.info("環境変数を読み込みました")

# PA-API設定
PA_API_KEY = os.getenv("PA_API_KEY")
PA_API_SECRET = os.getenv("PA_API_SECRET")
PARTNER_TAG = os.getenv("PARTNER_TAG")
MARKETPLACE = "www.amazon.co.jp"
REGION = "us-west-2"  # PA-APIのリージョン

# 認証情報の存在チェック（値は表示しない）
logger.info("Amazon PA-API認証情報チェック:")
pa_api_ready = all([PA_API_KEY, PA_API_SECRET, PARTNER_TAG])
logger.info(f"  PA_API_KEY: {'設定済み' if PA_API_KEY else '未設定'}")
logger.info(f"  PA_API_SECRET: {'設定済み' if PA_API_SECRET else '未設定'}")
logger.info(f"  PARTNER_TAG: {'設定済み' if PARTNER_TAG else '未設定'}")
logger.info(f"  PA-API利用準備: {'OK' if pa_api_ready else 'NG - 必要な認証情報が不足しています'}")

if not pa_api_ready:
    logger.error("PA-API認証情報が不足しています。環境変数を確認してください。")
    if not DEBUG_MODE:
        sys.exit(1)

# X (Twitter) API設定
TWITTER_CONSUMER_KEY = os.getenv("TWITTER_CONSUMER_KEY")
TWITTER_CONSUMER_SECRET = os.getenv("TWITTER_CONSUMER_SECRET")
TWITTER_ACCESS_TOKEN = os.getenv("TWITTER_ACCESS_TOKEN")
TWITTER_ACCESS_TOKEN_SECRET = os.getenv("TWITTER_ACCESS_TOKEN_SECRET")

# Twitter認証情報のチェック
twitter_ready = all([TWITTER_CONSUMER_KEY, TWITTER_CONSUMER_SECRET, 
                    TWITTER_ACCESS_TOKEN, TWITTER_ACCESS_TOKEN_SECRET])
logger.info("Twitter API認証情報チェック:")
logger.info(f"  Twitter API利用準備: {'OK' if twitter_ready else 'NG - 投稿機能は無効'}")

# Threads API設定（Meta Graph API）
THREADS_APP_ID = os.getenv("THREADS_APP_ID")
THREADS_APP_SECRET = os.getenv("THREADS_APP_SECRET")
THREADS_LONG_LIVED_TOKEN = os.getenv("THREADS_LONG_LIVED_TOKEN")
THREADS_INSTAGRAM_ACCOUNT_ID = os.getenv("THREADS_INSTAGRAM_ACCOUNT_ID")

# Threads認証情報のチェック
threads_token_ready = bool(THREADS_LONG_LIVED_TOKEN)
threads_app_ready = all([THREADS_APP_ID, THREADS_APP_SECRET])
threads_account_ready = bool(THREADS_INSTAGRAM_ACCOUNT_ID)
threads_ready = (threads_token_ready or threads_app_ready) and threads_account_ready

logger.info("Threads API認証情報チェック:")
logger.info(f"  Threads API利用準備: {'OK' if threads_ready else 'NG - 投稿機能は無効'}")

# 設定
ASIN_LIST_FILE = "tracking_asins.json"
RESULTS_FILE = "asin_results.json"
MIN_DISCOUNT_PERCENT = 15  # デフォルトの最小割引率
API_WAIT_TIME = 3  # APIリクエスト間の待機時間（秒）
MAX_BATCH_SIZE = 10  # PA-APIの1回のリクエストで取得できる最大ASIN数
MAX_RETRIES = 3  # API呼び出し失敗時の最大リトライ回数
MAX_RESULTS_STORED = 500  # 保存する最大結果数

# ファイルの存在確認
logger.info("必要なファイルの確認:")
logger.info(f"  {ASIN_LIST_FILE}: {'存在します' if os.path.exists(ASIN_LIST_FILE) else '見つかりません'}")
logger.info(f"  {RESULTS_FILE}: {'存在します' if os.path.exists(RESULTS_FILE) else '見つかりません - 新規作成されます'}")

def load_asin_list_from_file(filename):
    """ファイルからASINリストを読み込む（1行1ASIN形式）"""
    asins = []
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            for line in f:
                # コメント行と空行をスキップ
                line = line.strip()
                if line and not line.startswith('#'):
                    asins.append(line)
        return asins
    except FileNotFoundError:
        logger.error(f"ファイルが見つかりません: {filename}")
        return []
    except Exception as e:
        logger.error(f"ファイル読み込みエラー: {e}")
        return []

def load_asin_list():
    """ASINリストを読み込む"""
    # デフォルトリスト
    default_list = {
        "min_discount_percent": MIN_DISCOUNT_PERCENT,
        "amazon_only": False,  # デフォルトでは全ての販売元を対象
        "tracking_asins": [
            "B0CC944LHR",   # スチームアイロン
            "B0C65KM3ZT",   # アイウォーマー
            "B08JKFH23G",   # バスタオル
            "B002VPUOOE",   # ジョニーウォーカー
            "B004Y9IXZW"    # コカ・コーラ
        ]
    }
    
    try:
        # ファイルが存在し、正しいJSON形式であれば読み込む
        if not os.path.exists(ASIN_LIST_FILE):
            logger.warning(f"{ASIN_LIST_FILE}が存在しません。デフォルト設定を使用します。")
            
            # 設定ファイルを保存
            with open(ASIN_LIST_FILE, 'w', encoding='utf-8') as f:
                json.dump(default_list, f, ensure_ascii=False, indent=2)
            return default_list
        
        with open(ASIN_LIST_FILE, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:  # 空ファイルの場合
                raise json.JSONDecodeError("Empty file", "", 0)
            return json.loads(content)
    except json.JSONDecodeError as e:
        # 不正なJSON形式の場合
        logger.warning(f"{ASIN_LIST_FILE}が不正な形式です: {e}。デフォルト設定を使用します。")
        
        # 設定ファイルを保存
        with open(ASIN_LIST_FILE, 'w', encoding='utf-8') as f:
            json.dump(default_list, f, ensure_ascii=False, indent=2)
        return default_list
    except Exception as e:
        logger.warning(f"設定ファイル読み込みエラー: {e}。デフォルト設定を使用します。")
        
        # 設定ファイルを保存
        with open(ASIN_LIST_FILE, 'w', encoding='utf-8') as f:
            json.dump(default_list, f, ensure_ascii=False, indent=2)
        return default_list

def save_results(all_products):
    """検索結果を保存"""
    try:
        # 最大保存件数を制限
        limited_products = all_products[:MAX_RESULTS_STORED]
        
        with open(RESULTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(limited_products, f, ensure_ascii=False, indent=2)
        logger.info(f"検索結果を {RESULTS_FILE} に保存しました")
        return True
    except Exception as e:
        logger.error(f"結果保存エラー: {e}")
        return False
