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

# 他の関数は前回のスクリプトと同じ（sign_request, call_pa_api, get_product_info_batch等）

# 以下に変更した部分を追記

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
        notification_targets = []

        if not args.discount_only:  # 割引検知のみでなければ入荷チェック
            for asin, product in product_info.items():
                # Amazonのみフィルタリング
                amazon_only = args.amazon_only or config.get("amazon_only", False)
                if amazon_only and not product.get("is_amazon", False):
                    logger.info(f"Amazon以外の販売元のため対象外: {product['title'][:30]}... ({asin}) - 販売元: {product.get('seller', '不明')}")
                    continue
                
                # Amazonが販売し、在庫ありの場合は常に通知対象に追加
                if product.get("is_amazon", False) and product["is_in_stock"]:
                    logger.info(f"Amazon在庫あり: {product['title'][:30]}... ({asin})")
                    notification_targets.append(product)

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
            
            # 割引率順にソート
            new_discounted_items = sorted(filtered_items, key=lambda x: x["discount_percent"], reverse=True)
            
            if new_discounted_items:
                logger.info(f"合計 {len(new_discounted_items)}件の割引商品が見つかりました")
        
        # 結果を保存 - すべての商品情報を保存
        all_products = list(product_info.values())
        save_results(all_products)
        
        # 更新がなければ終了
        if not notification_targets and not new_discounted_items:
            logger.info("新しい入荷商品や割引商品は見つかりませんでした")
            return
        
        # 投稿する商品がある場合のみTwitterクライアントを初期化
        twitter_client = None
        if not DRY_RUN and not args.no_twitter and twitter_ready:
            logger.info("投稿する商品があるため、Twitterクライアントを初期化します")
            twitter_client = setup_twitter_api()
            if twitter_client:
                logger.info("Twitterクライアントを初期化しました")
            else:
                logger.warning("Twitterクライアントの初期化に失敗しました")
        
        # SNSに投稿（ドライランでなければ）
        if not DRY_RUN:
            # Amazon在庫商品の投稿
            if notification_targets:
                post_limit = min(5, len(notification_targets))
                logger.info(f"Amazon在庫商品 {post_limit}件を投稿します")
                
                for i, product in enumerate(notification_targets[:post_limit]):
                    logger.info(f"Amazon在庫商品 {i+1}/{post_limit} を投稿: {product['title'][:30]}...")
                    
                    # Twitter投稿
                    if twitter_client and not args.no_twitter:
                        twitter_result = post_to_twitter(twitter_client, product, notification_type="instock")
                        logger.info(f"Twitter投稿結果(在庫): {'成功' if twitter_result else '失敗'}")
                    
                    # Threads投稿
                    if threads_ready and not args.no_threads:
                        threads_result = post_to_threads(product, notification_type="instock")
                        logger.info(f"Threads投稿結果(在庫): {'成功' if threads_result else '失敗'}")
                    
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
            if notification_targets:
                print("\n" + "="*70)
                print(f"【Amazon在庫商品: {len(notification_targets)}件】")
                print("="*70)
                
                for i, product in enumerate(notification_targets, 1):
                    print(f"\n{i}. {product['title']}")
                    print(f"   ASIN: {product['asin']}")
                    if product.get("current_price"):
                        print(f"   価格: {product['current_price']:,.0f}円")
                    print(f"   在庫状況: {product['availability']}")
                    print(f"   販売元: {product['seller']}")
                    print(f"   URL: {product['detail_page_url']}")
            
            # 割引情報の表示
            if new_discounted_items:
                print("\n" + "="*70)
                print(f"【割引商品: {len(new_discounted_items)}件】")
                print("="*70)
                
                for i, product in enumerate(new_discounted_items, 1):
                    print(f"\n{i}. {product['title']}")
                    print(f"   ASIN: {product['asin']}")
                    print(f"   現在価格: {product['current_price']:,.0f}円")
                    print(f"   元の価格: {product['original_price']:,.0f}円")
                    print(f"   割引額: {product['discount_amount']:,.0f}円 ({product['discount_percent']:.1f}%オフ)")
                    print(f"   URL: {product['detail_page_url']}")
                    
            print("\n" + "="*70)
        
        logger.info("==== 処理が完了しました ====")
    
    except KeyboardInterrupt:
        logger.info("ユーザーによる中断を検出しました。プログラムを終了します。")
    except Exception as e:
        logger.error(f"予期しないエラーが発生しました: {e}", exc_info=True)

if __name__ == "__main__":
    main()
