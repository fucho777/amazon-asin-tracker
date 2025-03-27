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

def sign_request(host, path, payload, target="GetItems"):
    """PA-APIリクエストに署名を生成"""
    logger.debug(f"APIリクエスト署名生成: {target}")
    # リクエスト日時
    amz_date = datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    datestamp = datetime.utcnow().strftime('%Y%m%d')
    
    # 署名に必要な値
    service = 'ProductAdvertisingAPI'
    algorithm = 'AWS4-HMAC-SHA256'
    canonical_uri = path
    canonical_querystring = ''
    
    # ターゲットを設定
    api_target = f"com.amazon.paapi5.v1.ProductAdvertisingAPIv1.{target}"
    
    # ヘッダーの準備
    headers = {
        'host': host,
        'x-amz-date': amz_date,
        'content-encoding': 'amz-1.0',
        'content-type': 'application/json; charset=utf-8',
        'x-amz-target': api_target
    }
    
    # カノニカルリクエストの作成
    canonical_headers = '\n'.join([f"{k}:{v}" for k, v in sorted(headers.items())]) + '\n'
    signed_headers = ';'.join(sorted(headers.keys()))
    
    # ペイロードのSHA256ハッシュ
    payload_hash = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    
    # カノニカルリクエスト
    canonical_request = '\n'.join([
        'POST',
        canonical_uri,
        canonical_querystring,
        canonical_headers,
        signed_headers,
        payload_hash
    ])
    
    # 署名の作成
    credential_scope = f"{datestamp}/{REGION}/{service}/aws4_request"
    string_to_sign = '\n'.join([
        algorithm,
        amz_date,
        credential_scope,
        hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()
    ])
    
    # 署名キーの生成
    def sign(key, msg):
        return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()
    
    signing_key = sign(('AWS4' + PA_API_SECRET).encode('utf-8'), datestamp)
    signing_key = sign(signing_key, REGION)
    signing_key = sign(signing_key, service)
    signing_key = sign(signing_key, 'aws4_request')
    
    # 署名の計算
    signature = hmac.new(signing_key, string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
    
    # 認証ヘッダーの生成
    auth_header = (
        f"{algorithm} "
        f"Credential={PA_API_KEY}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, "
        f"Signature={signature}"
    )
    
    # ヘッダーに認証情報を追加
    headers['Authorization'] = auth_header
    
    return headers

def call_pa_api(endpoint, payload, target):
    """PA-APIを呼び出す共通関数（リトライ処理付き）"""
    host = "webservices.amazon.co.jp"
    path = f"/paapi5/{endpoint}"
    url = f"https://{host}{path}"
    
    payload_json = json.dumps(payload)
    
    # リトライ処理
    for attempt in range(MAX_RETRIES):
        try:
            headers = sign_request(host, path, payload_json, target)
            
            logger.debug(f"PA-API呼び出し: {target} (試行 {attempt+1}/{MAX_RETRIES})")
            response = requests.post(url, headers=headers, data=payload_json, timeout=10)
            
            if response.status_code == 429:
                wait_time = API_WAIT_TIME * (2 ** attempt)  # 指数バックオフ
                logger.warning(f"API制限に達しました。{wait_time}秒待機します。")
                time.sleep(wait_time)
                continue
                
            if response.status_code != 200:
                logger.error(f"PA-API エラー: ステータスコード {response.status_code}")
                logger.error(f"エラー詳細: {response.text[:500]}...")
                
                if attempt < MAX_RETRIES - 1:
                    time.sleep(API_WAIT_TIME)
                    continue
                return None
            
            data = response.json()
            
            # エラーチェック
            if "Errors" in data:
                error_msg = data['Errors'][0].get('Message', 'Unknown error')
                error_code = data['Errors'][0].get('Code', 'Unknown code')
                logger.error(f"PA-API エラー: {error_code} - {error_msg}")
                
                if attempt < MAX_RETRIES - 1:
                    time.sleep(API_WAIT_TIME)
                    continue
                return None
            
            return data
            
        except requests.exceptions.RequestException as e:
            logger.error(f"リクエストエラー: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(API_WAIT_TIME)
                continue
            return None
        except json.JSONDecodeError as e:
            logger.error(f"JSONデコードエラー: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(API_WAIT_TIME)
                continue
            return None
        except Exception as e:
            logger.error(f"予期せぬエラー: {e}")
            if attempt < MAX_RETRIES - 1:
                time.sleep(API_WAIT_TIME)
                continue
            return None
        finally:
            # 最後の試行でなければ待機（レート制限対策）
            if attempt < MAX_RETRIES - 1:
                time.sleep(API_WAIT_TIME)
            
    return None

def get_product_info_batch(asin_list):
    """指定したASINのリストから商品情報を一括取得"""
    if not pa_api_ready:
        logger.error("PA-API認証情報が不足しているため、検索できません")
        return {}
    
    # リクエストペイロード - GetItems APIで有効なリソースのみを指定
    payload = {
        "ItemIds": asin_list,
        "Resources": [
            "ItemInfo.Title",
            "Offers.Listings.Price",
            "Offers.Listings.SavingBasis",
            "Images.Primary.Large",
            "Offers.Listings.Availability.Message",
            "Offers.Listings.DeliveryInfo.IsAmazonFulfilled",
            "Offers.Listings.MerchantInfo"
        ],
        "PartnerTag": PARTNER_TAG,
        "PartnerType": "Associates",
        "Marketplace": MARKETPLACE
    }
    
    logger.info(f"商品情報取得中... ASIN: {', '.join(asin_list)}")
    
    data = call_pa_api("getitems", payload, "GetItems")
    
    if not data:
        return {}
    
    # 返却値の初期化
    result = {}
    
    # 検索結果がない場合
    if "ItemsResult" not in data or "Items" not in data["ItemsResult"]:
        logger.error(f"商品情報が見つかりませんでした: {', '.join(asin_list)}")
        return {}
        
    # 各商品の情報を処理
    for item in data["ItemsResult"]["Items"]:
        asin = item.get("ASIN")
        if not asin:
            continue
        
        # タイトルを取得
        title = "不明"
        if "ItemInfo" in item and "Title" in item["ItemInfo"] and "DisplayValue" in item["ItemInfo"]["Title"]:
            title = item["ItemInfo"]["Title"]["DisplayValue"]
        
        # 現在価格を取得
        current_price = None
        if "Offers" in item and "Listings" in item["Offers"] and len(item["Offers"]["Listings"]) > 0:
            listing = item["Offers"]["Listings"][0]
            if "Price" in listing and "Amount" in listing["Price"]:
                current_price = float(listing["Price"]["Amount"])
        
        # 元の価格を取得（SavingBasisから）
        original_price = None
        if "Offers" in item and "Listings" in item["Offers"] and len(item["Offers"]["Listings"]) > 0:
            listing = item["Offers"]["Listings"][0]
            if "SavingBasis" in listing and "Amount" in listing["SavingBasis"]:
                original_price = float(listing["SavingBasis"]["Amount"])
                
        # 在庫状況を取得
        availability = "不明"
        is_in_stock = False
        if "Offers" in item and "Listings" in item["Offers"] and len(item["Offers"]["Listings"]) > 0:
            listing = item["Offers"]["Listings"][0]
            if "Availability" in listing and "Message" in listing["Availability"]:
                availability = listing["Availability"]["Message"]
                # 「在庫あり」を含む場合は在庫ありと判定
                is_in_stock = "在庫あり" in availability or "通常配送無料" in availability or "お届け予定" in availability
                
        # 販売元情報を取得
        seller = "不明"
        is_amazon = False
        if "Offers" in item and "Listings" in item["Offers"] and len(item["Offers"]["Listings"]) > 0:
            listing = item["Offers"]["Listings"][0]
            if "MerchantInfo" in listing and "Name" in listing["MerchantInfo"]:
                seller = listing["MerchantInfo"]["Name"]
                is_amazon = seller == "Amazon" or seller == "Amazon.co.jp"
        
        # 商品画像を取得
        image_url = None
        if "Images" in item and "Primary" in item["Images"] and "Large" in item["Images"]["Primary"]:
            image_url = item["Images"]["Primary"]["Large"]["URL"]
        
        # 商品詳細URLを取得
        detail_url = f"https://www.amazon.co.jp/dp/{asin}?tag={PARTNER_TAG}"
        if "DetailPageURL" in item:
            detail_url = item["DetailPageURL"]
            # URLにアフィリエイトタグが含まれていない場合は追加
            if "?tag=" not in detail_url and "&tag=" not in detail_url and PARTNER_TAG:
                url_separator = "&" if "?" in detail_url else "?"
                detail_url = f"{detail_url}{url_separator}tag={PARTNER_TAG}"
        
        # 商品情報を格納
        result[asin] = {
            "asin": asin,
            "title": title,
            "current_price": current_price,
            "original_price": original_price,
            "image_url": image_url,
            "detail_page_url": detail_url,
            "availability": availability,
            "is_in_stock": is_in_stock,
            "seller": seller,
            "is_amazon": is_amazon,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
    
    return result

def load_asin_list():
    """ASINリストを読み込む"""
    # デフォルトリスト
    default_list = {
        "min_discount_percent": MIN_DISCOUNT_PERCENT,
        "amazon_only": False,  # デフォルトでは全ての販売元を対象
        "tracking_asins": []
    }
    
    try:
        # ファイルが存在し、正しいJSON形式であれば読み込む
        if not os.path.exists(ASIN_LIST_FILE):
            logger.warning(f"{ASIN_LIST_FILE}が存在しません。デフォルト設定を使用します。")
            # サンプルのASINをいくつか追加
            default_list["tracking_asins"] = [
                "B0CC944LHR",   # スチームアイロン
                "B0C65KM3ZT",   # アイウォーマー
                "B08JKFH23G",   # バスタオル
                "B002VPUOOE",   # ジョニーウォーカー
                "B004Y9IXZW"    # コカ・コーラ
            ]
            
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

def load_previous_results():
    """前回の検索結果を読み込む（重複投稿防止・在庫変化検知用）"""
    try:
        if not os.path.exists(RESULTS_FILE):
            # 結果ファイルが存在しない場合は空のファイルを作成
            with open(RESULTS_FILE, 'w', encoding='utf-8') as f:
                f.write("[]")
            logger.info("結果ファイルが存在しないため、新規作成しました")
            return []
            
        with open(RESULTS_FILE, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            if not content:  # 空ファイルの場合
                return []
            return json.loads(content)
    except (FileNotFoundError, json.JSONDecodeError):
        logger.warning(f"{RESULTS_FILE}が見つからないか不正な形式です。新しいファイルを作成します。")
        # 空の結果ファイルを作成
        with open(RESULTS_FILE, 'w', encoding='utf-8') as f:
            f.write("[]")
        return []
    except Exception as e:
        logger.error(f"結果ファイル読み込みエラー: {e}")
        return []

def create_stock_history():
    """前回の在庫状況を辞書形式で取得"""
    stock_history = {}
    previous_results = load_previous_results()
    
    for item in previous_results:
        asin = item.get("asin")
        if asin:
            stock_history[asin] = {
                "is_in_stock": item.get("is_in_stock", False),
                "availability": item.get("availability", "不明"),
                "price": item.get("current_price")
            }
    
    return stock_history

def calculate_discount(product_info):
    """割引情報を計算して追加"""
    discounted_products = []
    
    for asin, product in product_info.items():
        current_price = product.get("current_price")
        original_price = product.get("original_price")
        
        # 価格情報が不完全な場合はスキップ
        if current_price is None or original_price is None or original_price <= current_price:
            continue
        
        # 割引額と割引率を計算
        discount_amount = original_price - current_price
        discount_percent = (discount_amount / original_price) * 100
        
        # 割引情報を追加
        product["discount_amount"] = discount_amount
        product["discount_percent"] = discount_percent
        
        # 分析用に追加
        discounted_products.append(product)
    
    return discounted_products

def setup_twitter_api():
    """Twitter APIの設定"""
    if not twitter_ready:
        logger.warning("Twitter認証情報が不足しています。Twitter投稿はスキップされます。")
        return None
        
    try:
        # v2 API用の設定
        client = tweepy.Client(
            consumer_key=TWITTER_CONSUMER_KEY,
            consumer_secret=TWITTER_CONSUMER_SECRET,
            access_token=TWITTER_ACCESS_TOKEN,
            access_token_secret=TWITTER_ACCESS_TOKEN_SECRET
        )
        
        # 認証テスト - v2 APIのユーザー情報取得で検証
        try:
            me = client.get_me()
            if me.data:
                logger.info(f"Twitter API v2認証成功: @{me.data.username}")
                return client
            else:
                logger.error("Twitter認証に失敗しました")
                return None
        except Exception as e:
            logger.error(f"Twitter認証テストエラー: {e}")
            return None
    except Exception as e:
        logger.error(f"Twitter API認証エラー: {e}")
        return None

def post_to_twitter(client, product, notification_type="discount"):
    """Xに商品情報を投稿"""
    if not client:
        logger.error("Twitter APIクライアントが初期化されていません")
        return False
    
    try:
        # 投稿文を作成（通知タイプに応じて内容を変更）
        if notification_type == "discount":
            # 割引情報の投稿
            discount_percent = product["discount_percent"]
            current_price = product["current_price"]
            original_price = product["original_price"]
            discount_amount = product["discount_amount"]
            
            post = f"🔥【{discount_percent:.1f}%オフ】Amazon割引情報🔥 #PR\n\n"
            post += f"{product['title'][:80]}...\n\n"
            post += f"✅ 現在価格: {current_price:,.0f}円\n"
            post += f"❌ 元の価格: {original_price:,.0f}円\n"
            post += f"💰 割引額: {discount_amount:,.0f}円\n\n"
            post += f"🛒 商品ページ: {product['detail_page_url']}\n\n"
        
        elif notification_type == "instock":
            # 入荷情報の投稿
            current_price = product.get("current_price", 0)
            availability = product.get("availability", "在庫あり")
            seller = product.get("seller", "")
            
            post = f"📦【入荷速報】Amazonで在庫復活！📦 #PR\n\n"
            post += f"{product['title'][:80]}...\n\n"
            if current_price:
                post += f"💲 価格: {current_price:,.0f}円\n"
            post += f"📋 在庫状況: {availability}\n"
            if seller:
                post += f"🏪 販売: {seller}\n"
            post += f"\n🛒 商品ページ: {product['detail_page_url']}\n\n"
        
        else:
            # その他の変更（汎用フォーマット）
            post = f"📢【商品情報更新】Amazon商品情報📢 #PR\n\n"
            post += f"{product['title'][:80]}...\n\n"
            if product.get("current_price"):
                post += f"💲 価格: {product['current_price']:,.0f}円\n"
            post += f"📋 在庫状況: {product.get('availability', '不明')}\n\n"
            post += f"🛒 商品ページ: {product['detail_page_url']}\n\n"
        
        # 投稿が280文字を超える場合は調整
        if len(post) > 280:
            title_max = 50  # タイトルを固定で50文字に制限
            short_title = product['title'][:title_max] + "..."
            post = post.replace(f"{product['title'][:80]}...", short_title)
        
        if DRY_RUN:
            logger.info(f"【シミュレーション】X投稿内容: {post[:100]}...")
            return True
        
        # 最大3回までリトライ
        for attempt in range(MAX_RETRIES):
            try:
                # v2 APIでツイート
                response = client.create_tweet(text=post)
                if response.data and 'id' in response.data:
                    tweet_id = response.data['id']
                    logger.info(f"Xに投稿しました: ID={tweet_id} {product['title'][:30]}...")
                    return True
                else:
                    logger.error("X投稿に失敗: レスポンスにツイートIDがありません")
                    
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(5)
                        continue
                    return False
            except tweepy.errors.TweepyException as e:
                logger.error(f"TweepyエラーでX投稿に失敗: {e}")
                
                if attempt < MAX_RETRIES - 1:
                    time.sleep(5)
                    continue
                return False
            except Exception as e:
                logger.error(f"X投稿エラー: {e}")
                
                if attempt < MAX_RETRIES - 1:
                    time.sleep(5)
                    continue
                return False
            
    except Exception as e:
        logger.error(f"X投稿エラー: {e}")
        return False

def get_threads_access_token():
    """Threads APIのアクセストークンを取得"""
    try:
        # 長期アクセストークンが既に存在する場合はそれを使用
        if THREADS_LONG_LIVED_TOKEN:
            logger.info("Threads認証: 長期アクセストークンを使用します")
            return THREADS_LONG_LIVED_TOKEN
        
        # クライアント認証情報が不足している場合はエラー
        if not THREADS_APP_ID or not THREADS_APP_SECRET:
            raise ValueError("Threads API認証情報が不足しています")
        
        # アクセストークンリクエストURL
        token_url = "https://graph.facebook.com/v18.0/oauth/access_token"
        
        # リクエストパラメータ
        params = {
            "client_id": THREADS_APP_ID,
            "client_secret": THREADS_APP_SECRET,
            "grant_type": "client_credentials"
        }
        
        # POSTリクエストを送信
        logger.info("Threads認証: アクセストークンをリクエスト中...")
        response = requests.get(token_url, params=params)
        
        # レスポンスを確認
        if response.status_code == 200:
            response_data = response.json()
            access_token = response_data.get("access_token")
            logger.info("Threads認証: クライアントアクセストークンを取得しました")
            return access_token
        else:
            error_msg = f"アクセストークン取得エラー: ステータスコード {response.status_code}, レスポンス: {response.text}"
            logger.error(f"Threads認証: {error_msg}")
            raise ValueError(error_msg)
            
    except Exception as e:
        logger.error(f"Threads認証エラー: {e}")
        return None

def post_to_threads(product, notification_type="discount"):
    """Threadsに投稿（Meta Graph API経由）"""
    if not threads_ready:
        logger.warning("Threads認証情報が不足しています。Threads投稿はスキップされます。")
        return False
        
    try:
        # アクセストークン取得
        access_token = get_threads_access_token()
        if not access_token:
            logger.error("Threads投稿: アクセストークンが取得できません")
            return False
        
        logger.info(f"Threads投稿: ステップ1 - コンテナID作成中...（通知タイプ: {notification_type}）")
        
        # 投稿文を作成（通知タイプに応じて内容を変更）
        if notification_type == "discount":
            # 割引情報の投稿
            discount_percent = product["discount_percent"]
            current_price = product["current_price"]
            original_price = product["original_price"]
            discount_amount = product["discount_amount"]
            
            text = f"🔥【{discount_percent:.1f}%オフ】Amazon割引情報🔥\n\n"
            text += f"{product['title']}\n\n"
            text += f"✅ 現在価格: {current_price:,.0f}円\n"
            text += f"❌ 元の価格: {original_price:,.0f}円\n"
            text += f"💰 割引額: {discount_amount:,.0f}円\n\n"
            text += f"🛒 商品ページ: {product['detail_page_url']}\n\n"
            text += f"#Amazonセール #お買い得 #タイムセール #PR"
        
        elif notification_type == "instock":
            # 入荷情報の投稿
            current_price = product.get("current_price", 0)
            availability = product.get("availability", "在庫あり")
            seller = product.get("seller", "")
            
            text = f"📦【入荷速報】Amazonで在庫復活！📦\n\n"
            text += f"{product['title']}\n\n"
            if current_price:
                text += f"💲 価格: {current_price:,.0f}円\n"
            text += f"📋 在庫状況: {availability}\n"
            if seller:
                text += f"🏪 販売: {seller}\n"
            text += f"\n🛒 商品ページ: {product['detail_page_url']}\n\n"
            text += f"#Amazon入荷 #在庫あり #お買い逃しなく #PR"
        
        else:
            # その他の変更（汎用フォーマット）
            text = f"📢【商品情報更新】Amazon商品情報📢\n\n"
            text += f"{product['title']}\n\n"
            if product.get("current_price"):
                text += f"💲 価格: {product['current_price']:,.0f}円\n"
            text += f"📋 在庫状況: {product.get('availability', '不明')}\n\n"
            text += f"🛒 商品ページ: {product['detail_page_url']}\n\n"
            text += f"#Amazon #商品情報 #PR"
        
        if DRY_RUN:
            logger.info(f"【シミュレーション】Threads投稿内容: {text[:100]}...")
            return True
            
        # 最大3回までリトライ
        for attempt in range(MAX_RETRIES):
            try:
                # ステップ1: コンテナID作成
                upload_url = f"https://graph.threads.net/v1.0/{THREADS_INSTAGRAM_ACCOUNT_ID}/threads"
                upload_params = {
                    "access_token": access_token,
                    "media_type": "TEXT",
                    "text": text
                }
                
                # 画像URLがある場合は追加
                if "image_url" in product and product["image_url"]:
                    upload_params["media_type"] = "IMAGE"
                    upload_params["image_url"] = product["image_url"]
                
                # リクエスト送信
                upload_response = requests.post(upload_url, data=upload_params, timeout=15)
                
                if upload_response.status_code != 200:
                    error_msg = f"コンテナ作成エラー: ステータスコード {upload_response.status_code}, レスポンス: {upload_response.text}"
                    logger.error(f"Threads投稿: {error_msg}")
                    
                    if attempt < MAX_RETRIES - 1:
                        wait_time = 5 * (attempt + 1)  # 5秒、10秒、15秒と待機時間を増やす
                        logger.info(f"リトライ待機中... {wait_time}秒")
                        time.sleep(wait_time)
                        continue
                    return False
                
                # コンテナIDの取得
                try:
                    creation_data = upload_response.json()
                    container_id = creation_data.get("id")
                    if not container_id:
                        logger.error("Threads投稿: コンテナIDが取得できませんでした")
                        
                        if attempt < MAX_RETRIES - 1:
                            time.sleep(5)
                            continue
                        return False
                except Exception as e:
                    logger.error(f"Threads投稿: コンテナIDの解析に失敗 - {e}")
                    
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(5)
                        continue
                    return False
                
                logger.info(f"Threads投稿: コンテナID取得成功: {container_id}")
                
                # ステップ2: 投稿の公開
                logger.info("Threads投稿: ステップ2 - 投稿公開中...")
                publish_url = f"https://graph.threads.net/v1.0/{THREADS_INSTAGRAM_ACCOUNT_ID}/threads_publish"
                publish_params = {
                    "access_token": access_token,
                    "creation_id": container_id
                }
                
                # リクエスト送信
                publish_response = requests.post(publish_url, data=publish_params, timeout=15)
                
                if publish_response.status_code != 200:
                    error_msg = f"公開エラー: ステータスコード {publish_response.status_code}, レスポンス: {publish_response.text}"
                    logger.error(f"Threads投稿: {error_msg}")
                    
                    if attempt < MAX_RETRIES - 1:
                        time.sleep(5)
                        continue
                    return False
                
                # 公開成功
                logger.info(f"Threadsに投稿しました: {product['title'][:30]}...")
                return True
                
            except requests.exceptions.RequestException as e:
                logger.error(f"Threads投稿ネットワークエラー: {e}")
                
                if attempt < MAX_RETRIES - 1:
                    time.sleep(5)
                    continue
                return False
                
            except Exception as e:
                logger.error(f"Threads投稿エラー: {e}")
                
                if attempt < MAX_RETRIES - 1:
                    time.sleep(5)
                    continue
                return False
        
        return False
        
    except Exception as e:
        logger.error(f"Threads投稿エラー: {e}")
        return False

def main():
    """メイン処理"""
    parser = argparse.ArgumentParser(description='Amazon ASIN Tracker - 指定したASIN商品の割引情報と入荷状況をチェック')
    parser.add_argument('--dry-run', action='store_true', help='投稿せずに実行（テスト用）')
    parser.add_argument('--debug', action='store_true', help='デバッグモードで実行（詳細ログ出力）')
    parser.add_argument('--min-discount', type=float, help=f'最小割引率（デフォルト: {MIN_DISCOUNT_PERCENT}%）')
    parser.add_argument('--add', help='ASINを指定して追跡リストに追加（カンマ区切りで複数指定可能）')
    parser.add_argument('--add-file', help='ASINリストが記載されたファイルから一括追加（1行1ASIN形式）')
    parser.add_argument('--stock-only', action='store_true', help='入荷検知のみ行う（割引情報はチェックしない）')
    parser.add_argument('--discount-only', action='store_true', help='割引検知のみ行う（入荷情報はチェックしない）')
    parser.add_argument('--amazon-only', action='store_true', help='Amazonが販売している商品のみを対象にする')
    parser.add_argument('--no-twitter', action='store_true', help='Twitter投稿を無効化')
    parser.add_argument('--no-threads', action='store_true', help='Threads投稿を無効化')
    args = parser.parse_args()
    
    # コマンドライン引数の反映
    global DEBUG_MODE, DRY_RUN
    
    if args.debug:
        DEBUG_MODE = True
        logging.getLogger().setLevel(logging.DEBUG)
        logger.info("デバッグモードが有効化されました")
    
    if args.dry_run:
        DRY_RUN = True
        logger.info("ドライランモードが有効化されました（SNSには投稿されません）")
    
    # ASINを追加する処理
    if args.add or args.add_file:
        config = load_asin_list()
        added_count = 0
        
        # コマンドラインからの追加処理
        if args.add:
            # カンマ区切りの場合は分割
            asin_list = [asin.strip() for asin in args.add.split(',')]
            
            for asin in asin_list:
                if not asin:  # 空の文字列はスキップ
                    continue
                    
                if asin not in config["tracking_asins"]:
                    config["tracking_asins"].append(asin)
                    logger.info(f"ASINを追加しました: {asin}")
                    added_count += 1
                else:
                    logger.info(f"ASINは既に追跡リストに含まれています: {asin}")
        
        # ファイルからの追加処理
        if args.add_file:
            file_asins = load_asin_list_from_file(args.add_file)
            for asin in file_asins:
                if asin not in config["tracking_asins"]:
                    config["tracking_asins"].append(asin)
                    logger.info(f"ASINを追加しました: {asin}")
                    added_count += 1
                else:
                    logger.info(f"ASINは既に追跡リストに含まれています: {asin}")
        
        if added_count > 0:
            with open(ASIN_LIST_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
            logger.info(f"合計 {added_count}件のASINを追加しました")
        return
    
    try:
        # 設定を読み込む
        config = load_asin_list()
        
        # 最小割引率を設定
        min_discount = MIN_DISCOUNT_PERCENT
        if args.min_discount:
            min_discount = args.min_discount
        elif "min_discount_percent" in config:
            min_discount = config["min_discount_percent"]
        
        logger.info(f"最小割引率: {min_discount}%")
        
        # 追跡するASINがなければ終了
        tracking_asins = config.get("tracking_asins", [])
        if not tracking_asins:
            logger.error("追跡するASINが設定されていません")
            return
        
        logger.info(f"追跡ASIN数: {len(tracking_asins)}")
        
        # 前回の検索結果を読み込む（重複投稿防止・在庫変化検知用）
        previous_results = load_previous_results()
        previous_asins = {item["asin"] for item in previous_results if "asin" in item}
        
        # 前回の在庫状況を取得
        stock_history = create_stock_history()
        
        # Twitter APIクライアントの初期化
        twitter_client = None
        if not args.no_twitter and twitter_ready:
            twitter_client = setup_twitter_api()
            if twitter_client:
                logger.info("Twitterクライアントを初期化しました")
            else:
                logger.warning("Twitterクライアントの初期化に失敗しました")
        
        # PA-APIは一度に最大10ASINまで取得可能なので、バッチ処理
        product_info = {}
        for i in range(0, len(tracking_asins), MAX_BATCH_SIZE):
            batch = tracking_asins[i:i+MAX_BATCH_SIZE]
            # API呼び出し制限を考慮して待機
            if i > 0:
                time.sleep(API_WAIT_TIME)
            batch_info = get_product_info_batch(batch)
            product_info.update(batch_info)
        
        # 在庫状況の変化を検知（入荷検知）
        newly_in_stock = []
        
        if not args.discount_only:  # 割引検知のみでなければ入荷チェック
            for asin, product in product_info.items():
                # Amazonのみフィルタリング
                amazon_only = args.amazon_only or config.get("amazon_only", False)
                if amazon_only and not product.get("is_amazon", False):
                    logger.info(f"Amazon以外の販売元のため対象外: {product['title'][:30]}... ({asin}) - 販売元: {product.get('seller', '不明')}")
                    continue
                    
                # 前回の在庫状況と比較
                if asin in stock_history:
                    previous_stock = stock_history[asin]
                    # 前回在庫切れで、今回在庫ありの場合
                    if not previous_stock["is_in_stock"] and product["is_in_stock"]:
                        logger.info(f"入荷検知: {product['title'][:30]}... ({asin})")
                        newly_in_stock.append(product)
                elif product["is_in_stock"]:
                    # 初めて情報を取得した商品で在庫ありの場合
                    logger.info(f"新規商品で在庫あり: {product['title'][:30]}... ({asin})")
                    # 新規商品はここでは通知しない（必要に応じて変更可）
        
        # 割引情報を計算・処理
        new_discounted_items = []
        
        if not args.stock_only:  # 在庫チェックのみでなければ割引チェック
            # 割引情報を計算
            all_discounted_items = calculate_discount(product_info)
            
            # Amazonのみフィルタリング
            if args.amazon_only or config.get("amazon_only", False):
                all_discounted_items = [item for item in all_discounted_items if item.get("is_amazon", False)]
                logger.info(f"Amazonが販売する商品のみに絞り込み: {len(all_discounted_items)}件")
            
            # 最小割引率でフィルタリング
            filtered_items = [item for item in all_discounted_items if item.get("discount_percent", 0) >= min_discount]
            
            # 前回投稿済みの商品を除外
            new_discounted_items = [item for item in filtered_items if item["asin"] not in previous_asins]
            
            # 割引率順にソート
            new_discounted_items.sort(key=lambda x: x["discount_percent"], reverse=True)
            
            if new_discounted_items:
                logger.info(f"合計 {len(new_discounted_items)}件の新しい割引商品が見つかりました")
        
        # 結果を保存 - 在庫情報も含めてすべての商品情報を保存
        all_products = list(product_info.values())
        
        # すでに投稿されたものと今回投稿されるものにフラグを立てる
        for product in all_products:
            product["posted"] = product["asin"] in previous_asins
        
        # 前回の結果で今回取得していないものは保持（レスポンスが取得できなかった場合など）
        for old_product in previous_results:
            if old_product["asin"] not in product_info:
                all_products.append(old_product)
        
        # 結果を保存
        save_results(all_products)
        
        # 更新がなければ終了
        if not newly_in_stock and not new_discounted_items:
            logger.info("新しい入荷商品や割引商品は見つかりませんでした")
            return
        
        # SNSに投稿（ドライランでなければ）
        if not DRY_RUN:
            # 入荷商品の投稿
            if newly_in_stock:
                post_limit_stock = min(5, len(newly_in_stock))
                logger.info(f"入荷商品 {post_limit_stock}件を投稿します")
                
                for i, product in enumerate(newly_in_stock[:post_limit_stock]):
                    logger.info(f"入荷商品 {i+1}/{post_limit_stock} を投稿: {product['title'][:30]}...")
                    
                    # Twitter投稿
                    if twitter_client and not args.no_twitter:
                        twitter_result = post_to_twitter(twitter_client, product, notification_type="instock")
                        logger.info(f"Twitter投稿結果(入荷): {'成功' if twitter_result else '失敗'}")
                    
                    # Threads投稿
                    if threads_ready and not args.no_threads:
                        threads_result = post_to_threads(product, notification_type="instock")
                        logger.info(f"Threads投稿結果(入荷): {'成功' if threads_result else '失敗'}")
                    
                    # 連続投稿を避けるために待機
                    time.sleep(5)
            
            # 割引商品の投稿
            if new_discounted_items:
                post_limit_discount = min(5, len(new_discounted_items))
                logger.info(f"割引商品 {post_limit_discount}件を投稿します")
                
                for i, product in enumerate(new_discounted_items[:post_limit_discount]):
                    logger.info(f"割引商品 {i+1}/{post_limit_discount} を投稿: {product['title'][:30]}...")
                    
                    # Twitter投稿
                    if twitter_client and not args.no_twitter:
                        twitter_result = post_to_twitter(twitter_client, product, notification_type="discount")
                        logger.info(f"Twitter投稿結果(割引): {'成功' if twitter_result else '失敗'}")
                    
                    # Threads投稿
                    if threads_ready and not args.no_threads:
                        threads_result = post_to_threads(product, notification_type="discount")
                        logger.info(f"Threads投稿結果(割引): {'成功' if threads_result else '失敗'}")
                    
                    # 連続投稿を避けるために待機
                    time.sleep(5)
        else:
            logger.info("ドライラン: SNSへの投稿はスキップされました")
            
            # 入荷情報の表示
            if newly_in_stock:
                print("\n" + "="*70)
                print(f"【入荷検知結果: {len(newly_in_stock)}件】")
                print("="*70)
                
                for i, product in enumerate(newly_in_stock, 1):
                    print(f"\n{i}. {product['title']}")
                    print(f"   ASIN: {product['asin']}")
                    if product.get("current_price"):
                        print(f"   価格: {product['current_price']:,.0f}円")
                    print(f"   在庫状況: {product['availability']}")
                    print(f"   販売元: {product['seller']}")
                    print(f"   URL: {product['detail_page_url']}")
                    
                    if "image_url" in product and product["image_url"]:
                        print(f"   画像: {product['image_url']}")
            
            # 割引情報の表示
            if new_discounted_items:
                print("\n" + "="*70)
                print(f"【割引検知結果: {len(new_discounted_items)}件】")
                print("="*70)
                
                for i, product in enumerate(new_discounted_items, 1):
                    print(f"\n{i}. {product['title']}")
                    print(f"   ASIN: {product['asin']}")
                    print(f"   現在価格: {product['current_price']:,.0f}円")
                    print(f"   元の価格: {product['original_price']:,.0f}円")
                    print(f"   割引額: {product['discount_amount']:,.0f}円 ({product['discount_percent']:.1f}%オフ)")
                    print(f"   URL: {product['detail_page_url']}")
                    
                    if "image_url" in product and product["image_url"]:
                        print(f"   画像: {product['image_url']}")
            
            print("\n" + "="*70)
        
        logger.info("==== 処理が完了しました ====")
    
    except KeyboardInterrupt:
        logger.info("ユーザーによる中断を検出しました。プログラムを終了します。")
    except Exception as e:
        logger.error(f"予期しないエラーが発生しました: {e}", exc_info=True)

if __name__ == "__main__":
    main()
