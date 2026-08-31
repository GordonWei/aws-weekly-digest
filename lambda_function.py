"""
AWS Weekly Digest — Lambda Function
自動彙整 AWS What's New + Blog → Bedrock AI 分析 → 多管道輸出
V1：Lambda + Bedrock + SES + S3
"""

import json
import os
import html
import re
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import boto3

# ── 設定（環境變數驅動）──────────────────────────────────────
CONFIG = {
    'BEDROCK_MODEL_ID': os.environ.get('BEDROCK_MODEL_ID', 'us.amazon.nova-pro-v1:0'),
    'RECIPIENT_EMAIL':  os.environ.get('RECIPIENT_EMAIL', ''),
    'SENDER_EMAIL':     os.environ.get('SENDER_EMAIL', ''),
    'S3_BUCKET':        os.environ.get('S3_BUCKET', ''),
    'DAYS_LOOKBACK':    int(os.environ.get('DAYS_LOOKBACK', '7')),
    'MAX_WHATS_NEW':    int(os.environ.get('MAX_WHATS_NEW', '80')),
    'BEDROCK_REGION':   os.environ.get('BEDROCK_REGION', 'us-east-1'),

    # ── LLM Provider ────────────────────────────────────────
    # 'bedrock'（預設，行為與過去完全相同）或 'openai_compatible'
    # openai_compatible 一套設定同時吃 Gemini（OpenAI 相容端點）、
    # 本機 LM Studio（原生相容）、自架 LiteLLM，不必為每家各寫一個 SDK。
    'LLM_PROVIDER':      os.environ.get('LLM_PROVIDER', 'bedrock'),
    'LLM_BASE_URL':      os.environ.get('LLM_BASE_URL', ''),
    'LLM_MODEL':         os.environ.get('LLM_MODEL', ''),
    # API key 存 SSM（比照 LinkedIn token）。刻意設成空字串＝明確宣告不需要認證
    # （例如本機 LM Studio）；維持預設路徑但參數不存在則視為設定錯誤，直接失敗。
    'LLM_API_KEY_PARAM': os.environ.get('LLM_API_KEY_PARAM', '/aws-weekly-digest/llm-api-key'),
    'LLM_TIMEOUT':       int(os.environ.get('LLM_TIMEOUT', '240')),

    # ── 輸出管道開關（false = 預留，代碼已就位）────────────
    'FEATURES': {
        'SEND_EMAIL':             os.environ.get('FEATURE_SEND_EMAIL',      'true') == 'true',
        'EMBED_CONTENT_IN_EMAIL': os.environ.get('FEATURE_EMBED_CONTENT',   'true') == 'true',
        'SAVE_TO_S3':             os.environ.get('FEATURE_SAVE_TO_S3',      'true') == 'true',
        'POST_TO_LINKEDIN':       os.environ.get('FEATURE_POST_TO_LINKEDIN','false') == 'true',
        'POST_TO_WEBHOOK':        os.environ.get('FEATURE_POST_TO_WEBHOOK', 'false') == 'true',
    },
}


# ────────────────────────────────────────────────────────────
# 主程式
# ────────────────────────────────────────────────────────────
def lambda_handler(event, context):
    print('開始產生 AWS Weekly Digest...')
    try:
        whats_new  = fetch_aws_whats_new()
        blog_posts = fetch_aws_blog_posts()
        print(f"抓取完成：{len(whats_new)} 條 What's New，{len(blog_posts)} 篇 Blog")

        if not whats_new:
            raise ValueError("本週 What's New 為空，請確認 RSS Feed 是否正常。")

        digest_content = generate_digest(whats_new, blog_posts)
        print(f"{CONFIG['LLM_PROVIDER']} 分析完成")

        s3_url = None
        if CONFIG['FEATURES']['SAVE_TO_S3'] and CONFIG['S3_BUCKET']:
            s3_url = save_to_s3(digest_content)
            print(f'S3 存檔：{s3_url}')

        if CONFIG['FEATURES']['SEND_EMAIL']:
            send_email(digest_content, s3_url, len(whats_new), len(blog_posts))
            print('Email 已寄出')

        if CONFIG['FEATURES']['POST_TO_LINKEDIN']:
            post_to_linkedin(digest_content)

        if CONFIG['FEATURES']['POST_TO_WEBHOOK']:
            post_to_webhook(digest_content, s3_url)

        print('完成！')
        return {'statusCode': 200, 'body': 'OK'}

    except Exception as e:
        print(f'錯誤：{e}')
        _send_error_email(str(e))
        raise


# ────────────────────────────────────────────────────────────
# 資料抓取：AWS What's New RSS
# ────────────────────────────────────────────────────────────
def fetch_aws_whats_new():
    url = 'https://aws.amazon.com/about-aws/whats-new/recent/feed/'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'AWS-Weekly-Digest/1.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode('utf-8', errors='replace')

        start = raw.find('<rss')
        if start == -1:
            return []

        root    = ET.fromstring(raw[start:])
        channel = root.find('channel')
        if channel is None:
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(days=CONFIG['DAYS_LOOKBACK'])
        items  = []

        for item in channel.findall('item'):
            title = (item.findtext('title') or '').strip()
            link  = item.findtext('link') or ''
            desc  = _strip_html(item.findtext('description') or '')[:400]
            pub   = _parse_rss_date(item.findtext('pubDate') or '')

            if title and pub and pub >= cutoff:
                items.append({'title': title, 'summary': desc, 'link': link, 'published': pub})

        return items[:CONFIG['MAX_WHATS_NEW']]

    except Exception as e:
        print(f"[What's New] 抓取失敗：{e}")
        return []


# ────────────────────────────────────────────────────────────
# 資料抓取：AWS Blog RSS（失敗靜默跳過）
# ────────────────────────────────────────────────────────────
def fetch_aws_blog_posts():
    url = 'https://aws.amazon.com/blogs/aws/feed/'
    try:
        req = urllib.request.Request(url, headers={'User-Agent': 'AWS-Weekly-Digest/1.0'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode('utf-8', errors='replace')

        start = raw.find('<rss')
        if start == -1:
            return []

        # 移除 namespace 宣告，避免 ElementTree 解析問題
        # 注意：只刪 xmlns 宣告不夠——items 裡仍有 <dc:creator>、<content:encoded> 等
        # 帶 prefix 的標籤，宣告一消失 ElementTree 就會拋出 "unbound prefix"。
        # 這裡把標籤的 prefix 也一併剝掉（反正只取 title/link/description/pubDate，用不到那些欄位）。
        clean = re.sub(r'\s+xmlns(?::[a-z]+)?="[^"]*"', '', raw[start:])
        clean = re.sub(r'</?[a-zA-Z0-9]+:', lambda m: m.group(0).replace(':', ''), clean)
        root    = ET.fromstring(clean)
        channel = root.find('channel')
        if channel is None:
            return []

        cutoff = datetime.now(timezone.utc) - timedelta(days=CONFIG['DAYS_LOOKBACK'])
        items  = []

        for item in channel.findall('item'):
            title = (item.findtext('title') or '').strip()
            link  = item.findtext('link') or ''
            desc  = _strip_html(item.findtext('description') or '')[:250]
            pub   = _parse_rss_date(item.findtext('pubDate') or '')

            if title and pub and pub >= cutoff:
                items.append({'title': title, 'description': desc, 'link': link})

        return items[:15]

    except Exception as e:
        print(f'[Blog] 跳過：{e}')
        return []


# ────────────────────────────────────────────────────────────
# Bedrock AI 呼叫（支援 Nova 與 Claude 系列）
# ────────────────────────────────────────────────────────────
def generate_digest(whats_new, blog_posts):
    today      = _fmt_date(datetime.now())
    week_range = _week_range()

    wn_text = '\n\n'.join(
        f"[WN{i+1}] {item['title']}\n摘要：{item['summary']}\n連結：{item['link']}"
        for i, item in enumerate(whats_new)
    )
    blog_text = '\n\n'.join(
        f"[B{i+1}] {item['title']}\n{item['description']}\n連結：{item['link']}"
        for i, item in enumerate(blog_posts)
    ) if blog_posts else '（本週無新 Blog）'

    prompt = f"""你是一位精通 Amazon Web Services 的首席雲端架構師，同時也是優秀的技術寫作者。
請根據以下本週（{week_range}）的 AWS What's New 和 Blog，整理一份供技術社群分享的週報。
請直接輸出週報內容，不要有任何前言、自我介紹或說明文字。

## AWS What's New（共 {len(whats_new)} 條）
{wn_text}

## AWS Blog Posts（共 {len(blog_posts)} 篇）
{blog_text}

---

## 輸出格式（嚴格遵守，使用繁體中文，標題請勿使用 emoji）

# AWS Weekly Digest｜{today}

> 本週總覽：[2-3 句：哪些服務有重大更新、本週整體方向感、有無影響成本或架構的重要變化]

---

## 數字總覽
- Compute（EC2 / Lambda / Container）：N 條
- Storage & Database（S3 / RDS / DynamoDB）：N 條
- AI & Machine Learning（Bedrock / SageMaker）：N 條
- Networking & Security（VPC / CloudFront / IAM / WAF）：N 條
- Data & Analytics（Redshift / Athena / Kinesis）：N 條
- Developer Tools & Other：N 條

---

## Compute（EC2 / Lambda / Container）

### [功能名稱]
**一句話重點**：[說明這個更新做了什麼]
**商業/技術價值**：[對工程師或企業的實際影響，30 字以內]
**費用影響**：[無影響 / 降低成本 / 新增費用 / 需評估]
**狀態**：[GA / Preview / Deprecated]
**可用區域**：[全球 / 部分區域 / 具體區域名稱]
**官方連結**：[對應的連結]

[此分類下列出 3-6 條最重要的更新，過濾純文件修正]

---

## Storage & Database（S3 / RDS / DynamoDB）
[同上格式，3-6 條]

---

## AI & Machine Learning（Bedrock / SageMaker）
[同上格式，3-6 條]

---

## Networking & Security（VPC / CloudFront / IAM / WAF）
[同上格式，3-5 條]

---

## Data & Analytics（Redshift / Athena / Kinesis）
[同上格式，3-5 條]

---

## Developer Tools & Other
[同上格式，3-5 條]

---

## 本週精選 Blog
[列出 2-3 篇最值得閱讀的 Blog，格式：**標題**：一句話說明為何值得看 → 連結]

---

## 本週架構師觀點
[選出本週最值得關注的 1 個更新，解釋它對企業雲架構師的深層意義，約 100 字，用第一人稱「我認為...」。如有費用影響請特別說明。]

---
AWS Weekly Digest Bot｜{today}
"""

    return _invoke_llm(prompt)


# ────────────────────────────────────────────────────────────
# LLM 呼叫層
#
# 這一層刻意「失敗就拋例外」，不做優雅降級。
# 原因是本專案 V1 的真實 bug 就出在相反的寫法：一個為了容錯而寫的 except
# 靜默吞掉解析錯誤，於是每次執行都回 200、信照寄、S3 照寫，只有一個區塊
# 永遠是空的，唯一症狀是一行沒人有理由去看的 CloudWatch log。
# 摘要內容是這封信的全部價值，拿不到就該讓整個執行失敗——lambda_handler
# 的 except 會寄出錯誤通知信並 re-raise，那才是應有的行為。
# ────────────────────────────────────────────────────────────
MAX_TOKENS  = 8192
TEMPERATURE = 0.3


def _invoke_llm(prompt):
    provider = CONFIG['LLM_PROVIDER']
    if provider == 'bedrock':
        return _invoke_bedrock(prompt)
    if provider == 'openai_compatible':
        return _invoke_openai_compatible(prompt)
    raise ValueError(
        f"未知的 LLM_PROVIDER：{provider!r}（可用：bedrock / openai_compatible）"
    )


def _invoke_bedrock(prompt):
    bedrock  = boto3.client('bedrock-runtime', region_name=CONFIG['BEDROCK_REGION'])
    model_id = CONFIG['BEDROCK_MODEL_ID']

    if 'nova' in model_id or model_id.startswith('amazon'):
        body = json.dumps({
            'messages': [{'role': 'user', 'content': [{'text': prompt}]}],
            'inferenceConfig': {'temperature': TEMPERATURE, 'maxTokens': MAX_TOKENS},
        })
        response = bedrock.invoke_model(modelId=model_id, body=body)
        result   = json.loads(response['body'].read())
        return result['output']['message']['content'][0]['text']
    else:  # Claude on Bedrock
        body = json.dumps({
            'anthropic_version': 'bedrock-2023-05-31',
            'max_tokens': MAX_TOKENS,
            'temperature': TEMPERATURE,
            'messages': [{'role': 'user', 'content': prompt}],
        })
        response = bedrock.invoke_model(modelId=model_id, body=body)
        result   = json.loads(response['body'].read())
        return result['content'][0]['text']


# ────────────────────────────────────────────────────────────
# OpenAI 相容端點（Gemini / LM Studio / 自架 LiteLLM 共用同一段）
#
# 設定方式：
#   LLM_PROVIDER=openai_compatible
#   LLM_BASE_URL=<端點，含 /v1；結尾有沒有斜線都可以>
#   LLM_MODEL=<模型名稱>
#   LLM_API_KEY_PARAM=<SSM 參數路徑，預設 /aws-weekly-digest/llm-api-key>
#                      設成空字串代表「這個端點不需要認證」（例如本機 LM Studio）
#
# 已知端點：
#   Gemini     https://generativelanguage.googleapis.com/v1beta/openai
#   LM Studio  http://<host>:1234/v1        ← Lambda 在雲上，需要對外可達的位址
#   LiteLLM    http://<host>:4000/v1
# ────────────────────────────────────────────────────────────
def _invoke_openai_compatible(prompt):
    base_url = CONFIG['LLM_BASE_URL'].rstrip('/')
    model    = CONFIG['LLM_MODEL']

    missing = [k for k, v in (('LLM_BASE_URL', base_url), ('LLM_MODEL', model)) if not v]
    if missing:
        raise ValueError(
            f"LLM_PROVIDER=openai_compatible 但缺少必要設定：{', '.join(missing)}"
        )

    headers = {'Content-Type': 'application/json'}
    key_param = CONFIG['LLM_API_KEY_PARAM']
    if key_param:
        # 參數路徑有設就必須拿得到——拿不到是設定錯誤，不是可以略過的情況
        ssm = boto3.client('ssm')
        try:
            api_key = ssm.get_parameter(Name=key_param, WithDecryption=True)['Parameter']['Value']
        except Exception as e:
            raise RuntimeError(
                f'讀取 SSM 參數 {key_param} 失敗：{e}。'
                f'若該端點本來就不需要認證，請把 LLM_API_KEY_PARAM 設成空字串明確宣告。'
            ) from e
        headers['Authorization'] = f'Bearer {api_key}'

    body = json.dumps({
        'model': model,
        'messages': [{'role': 'user', 'content': prompt}],
        'temperature': TEMPERATURE,
        'max_tokens': MAX_TOKENS,
    }).encode('utf-8')

    url = f'{base_url}/chat/completions'
    req = urllib.request.Request(url, data=body, headers=headers, method='POST')

    try:
        with urllib.request.urlopen(req, timeout=CONFIG['LLM_TIMEOUT']) as resp:
            raw = resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        detail = e.read().decode('utf-8', errors='replace')[:500]
        raise RuntimeError(f'{model} 回應 HTTP {e.code}：{detail}') from e
    except Exception as e:
        raise RuntimeError(f'呼叫 {url} 失敗：{e}') from e

    try:
        result  = json.loads(raw)
        content = result['choices'][0]['message']['content']
    except Exception as e:
        raise RuntimeError(f'{model} 回應格式非預期：{raw[:500]}') from e

    if not content or not content.strip():
        raise RuntimeError(f'{model} 回應內容為空（finish_reason='
                           f'{result["choices"][0].get("finish_reason")!r}）')

    return content


# ────────────────────────────────────────────────────────────
# 輸出 A：S3 存檔
# ────────────────────────────────────────────────────────────
def save_to_s3(content):
    today   = _fmt_date(datetime.now(), '%Y-%m-%d')
    key     = f'digests/{today}/aws-weekly-digest.md'
    s3      = boto3.client('s3')
    s3.put_object(
        Bucket=CONFIG['S3_BUCKET'], Key=key,
        Body=content.encode('utf-8'), ContentType='text/markdown; charset=utf-8',
    )
    return f's3://{CONFIG["S3_BUCKET"]}/{key}'


# ────────────────────────────────────────────────────────────
# 輸出 B：Email（SES，含完整 HTML 內容）
# ────────────────────────────────────────────────────────────
def send_email(digest_content, s3_url, wn_count, blog_count):
    today   = _fmt_date(datetime.now())
    s3_link = (f'<p style="margin:12px 0"><a href="{s3_url}" style="color:#FF9900;font-weight:600">S3 原始存檔</a></p>'
               if s3_url else '')
    body_html = markdown_to_html(digest_content) if CONFIG['FEATURES']['EMBED_CONTENT_IN_EMAIL'] else ''

    html_body = f"""
<div style="font-family:-apple-system,Arial,sans-serif;max-width:680px;margin:0 auto;color:#232F3E">
  <div style="background:#232F3E;padding:20px 32px;border-radius:8px 8px 0 0">
    <h1 style="margin:0;color:white;font-size:20px;font-weight:700">AWS Weekly Digest</h1>
    <p style="margin:6px 0 0;color:#FF9900;font-size:14px">{today}｜What's New {wn_count} 條，Blog {blog_count} 篇</p>
  </div>
  <div style="background:#fff;padding:24px 32px;border:1px solid #e0e0e0;border-top:none;border-radius:0 0 8px 8px">
    {s3_link}
    <div style="margin-top:16px">{body_html}</div>
    <p style="margin:24px 0 0;font-size:12px;color:#999;border-top:1px solid #f0f0f0;padding-top:12px">
      AWS Weekly Digest Bot｜自動產生，請審閱後再分享
    </p>
  </div>
</div>"""

    ses = boto3.client('ses', region_name=CONFIG['BEDROCK_REGION'])
    ses.send_email(
        Source=CONFIG['SENDER_EMAIL'],
        Destination={'ToAddresses': [CONFIG['RECIPIENT_EMAIL']]},
        Message={
            'Subject': {'Data': f'[AWS] Weekly Digest {today}', 'Charset': 'UTF-8'},
            'Body': {
                'Html': {'Data': html_body,                        'Charset': 'UTF-8'},
                'Text': {'Data': f'AWS Weekly Digest {today}\n（請以 HTML 郵件查看）', 'Charset': 'UTF-8'},
            },
        },
    )


def _send_error_email(error_msg):
    try:
        ses = boto3.client('ses', region_name=CONFIG['BEDROCK_REGION'])
        ses.send_email(
            Source=CONFIG['SENDER_EMAIL'],
            Destination={'ToAddresses': [CONFIG['RECIPIENT_EMAIL']]},
            Message={
                'Subject': {'Data': '[AWS] Weekly Digest 產生失敗', 'Charset': 'UTF-8'},
                'Body':    {'Text': {'Data': f'錯誤：{error_msg}', 'Charset': 'UTF-8'}},
            },
        )
    except Exception as e:
        print(f'Error email 也寄失敗：{e}')


# ────────────────────────────────────────────────────────────
# 輸出 C：LinkedIn（預留，FEATURE_POST_TO_LINKEDIN=false）
# 啟用步驟：
#   1. 建立 LinkedIn Developer App，取得 OAuth Access Token
#   2. aws ssm put-parameter --name /aws-weekly-digest/linkedin-access-token \
#        --type SecureString --value <TOKEN>
#   3. aws ssm put-parameter --name /aws-weekly-digest/linkedin-person-urn \
#        --type String --value <URN>
#   4. 設定環境變數 FEATURE_POST_TO_LINKEDIN=true
# ────────────────────────────────────────────────────────────
def post_to_linkedin(content):
    ssm = boto3.client('ssm')
    try:
        token  = ssm.get_parameter(Name='/aws-weekly-digest/linkedin-access-token', WithDecryption=True)['Parameter']['Value']
        urn    = ssm.get_parameter(Name='/aws-weekly-digest/linkedin-person-urn')['Parameter']['Value']
    except Exception as e:
        print(f'[LinkedIn] 缺少 SSM 參數，跳過：{e}')
        return None

    post_text = markdown_to_linkedin(content)[:2950]
    payload   = json.dumps({
        'author': f'urn:li:person:{urn}',
        'lifecycleState': 'PUBLISHED',
        'specificContent': {
            'com.linkedin.ugc.ShareContent': {
                'shareCommentary':  {'text': post_text},
                'shareMediaCategory': 'NONE',
            },
        },
        'visibility': {'com.linkedin.ugc.MemberNetworkVisibility': 'PUBLIC'},
    }).encode('utf-8')

    req = urllib.request.Request(
        'https://api.linkedin.com/v2/ugcPosts', data=payload,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json',
                 'X-Restli-Protocol-Version': '2.0.0'},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read())
            print(f'[LinkedIn] 發文成功：{result.get("id")}')
            return result.get('id')
    except Exception as e:
        print(f'[LinkedIn] 發文失敗：{e}')
        return None


# ────────────────────────────────────────────────────────────
# 輸出 D：Webhook（預留，FEATURE_POST_TO_WEBHOOK=false）
# 啟用步驟：
#   1. 建立 n8n / Make Webhook
#   2. aws ssm put-parameter --name /aws-weekly-digest/webhook-url \
#        --type SecureString --value <URL>
#   3. 設定環境變數 FEATURE_POST_TO_WEBHOOK=true
# Payload: { title, content, linkedInText, s3Url, generatedAt }
# ────────────────────────────────────────────────────────────
def post_to_webhook(content, s3_url):
    ssm = boto3.client('ssm')
    try:
        webhook_url = ssm.get_parameter(Name='/aws-weekly-digest/webhook-url', WithDecryption=True)['Parameter']['Value']
    except Exception as e:
        print(f'[Webhook] 缺少 SSM 參數，跳過：{e}')
        return

    payload = json.dumps({
        'title':        f'AWS Weekly Digest {_fmt_date(datetime.now())}',
        'content':      content,
        'linkedInText': markdown_to_linkedin(content)[:2950],
        's3Url':        s3_url or '',
        'generatedAt':  datetime.now(timezone.utc).isoformat(),
    }).encode('utf-8')

    req = urllib.request.Request(
        webhook_url, data=payload,
        headers={'Content-Type': 'application/json'}, method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            print('[Webhook] 已送出')
    except Exception as e:
        print(f'[Webhook] 失敗：{e}')


# ────────────────────────────────────────────────────────────
# Markdown → HTML（Email 用，AWS 橘色主題）
# ────────────────────────────────────────────────────────────
def markdown_to_html(markdown):
    def _unescape(s):
        return s.replace(r'\*','*').replace(r'\_','_').replace(r'\#','#').replace(r'\[','[').replace(r'\]',']')
    def _bold(s):
        return re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', s)

    out = []
    for raw in markdown.split('\n'):
        line = _unescape(raw)
        if re.match(r'^\|[-| :]+\|$', line.strip()):
            continue
        if line.startswith('# '):
            out.append(f'<h1 style="color:#232F3E;font-size:20px;margin:20px 0 6px;font-weight:700">{_bold(line[2:])}</h1>')
        elif line.startswith('## '):
            out.append(f'<h2 style="color:#FF9900;font-size:15px;font-weight:700;margin:20px 0 4px;padding-bottom:4px;border-bottom:2px solid #FFF3E0">{_bold(line[3:])}</h2>')
        elif line.startswith('### '):
            out.append(f'<h3 style="color:#232F3E;font-size:14px;font-weight:600;margin:12px 0 2px">{_bold(line[4:])}</h3>')
        elif line.startswith('> '):
            out.append(f'<blockquote style="border-left:3px solid #FF9900;margin:8px 0;padding:8px 14px;background:#FFFBF0;color:#444;font-style:italic;border-radius:0 4px 4px 0">{_bold(line[2:])}</blockquote>')
        elif line.startswith('---'):
            out.append('<hr style="border:none;border-top:1px solid #e0e0e0;margin:14px 0">')
        elif line.startswith('- '):
            out.append(f'<li style="margin:2px 0;color:#333;font-size:14px;line-height:1.6">{_bold(line[2:])}</li>')
        elif line.startswith('|'):
            cells = [c.strip() for c in line.split('|') if c.strip()]
            if cells:
                out.append(f'<p style="margin:2px 0;font-size:13px;color:#555">{" &nbsp;|&nbsp; ".join(cells)}</p>')
        elif not line.strip():
            out.append('<div style="height:4px"></div>')
        else:
            out.append(f'<p style="margin:3px 0;color:#333;font-size:14px;line-height:1.6">{_bold(line)}</p>')

    return '\n'.join(out)


# ────────────────────────────────────────────────────────────
# Markdown → LinkedIn 純文字
# ────────────────────────────────────────────────────────────
def markdown_to_linkedin(markdown):
    def _unescape(s):
        return s.replace(r'\*','*').replace(r'\_','_').replace(r'\#','#').replace(r'\[','[').replace(r'\]',']')
    def _strip(s):
        return re.sub(r'\*\*?([^*]+)\*\*?', r'\1', s)

    lines = []
    for raw in markdown.split('\n'):
        line = _unescape(raw)
        if re.match(r'^\|[-| :]+\|$', line.strip()):
            continue
        if line.startswith('# '):    lines.append('📋 ' + _strip(line[2:]).upper())
        elif line.startswith('## '): lines.append('\n【' + _strip(line[3:]) + '】')
        elif line.startswith('### '):lines.append('◆ ' + _strip(line[4:]))
        elif line.startswith('> '): lines.append(_strip(line[2:]))
        elif line.startswith('---'): lines.append('─────────────────')
        elif line.startswith('- '): lines.append('• ' + _strip(line[2:]))
        elif line.startswith('|'):
            cells = [c.strip() for c in line.split('|') if c.strip()]
            if cells: lines.append(' | '.join(cells))
        else: lines.append(_strip(line))

    return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()


# ────────────────────────────────────────────────────────────
# 工具函式
# ────────────────────────────────────────────────────────────
def _strip_html(text):
    if not text:
        return ''
    text = re.sub(r'<[^>]+>', ' ', text)
    text = html.unescape(text)
    return re.sub(r'\s+', ' ', text).strip()

def _parse_rss_date(date_str):
    for fmt in ('%a, %d %b %Y %H:%M:%S %z', '%a, %d %b %Y %H:%M:%S GMT', '%Y-%m-%dT%H:%M:%S%z'):
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None

def _fmt_date(dt, fmt='%Y/%m/%d'):
    return dt.strftime(fmt)

def _week_range():
    today = datetime.now()
    return f'{_fmt_date(today - timedelta(days=CONFIG["DAYS_LOOKBACK"]))} - {_fmt_date(today)}'
