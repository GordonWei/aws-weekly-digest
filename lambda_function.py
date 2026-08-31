"""
AWS Weekly Digest — Lambda Function

Pulls AWS What's New and the AWS News Blog for the past week, hands them to an
LLM to write a categorized digest, then emails it and archives a copy in S3.

Lambda + Bedrock (or any OpenAI-compatible endpoint) + SES + S3.
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

# ── Configuration (entirely environment-driven) ─────────────
CONFIG = {
    'BEDROCK_MODEL_ID': os.environ.get('BEDROCK_MODEL_ID', 'us.amazon.nova-pro-v1:0'),
    'RECIPIENT_EMAIL':  os.environ.get('RECIPIENT_EMAIL', ''),
    'SENDER_EMAIL':     os.environ.get('SENDER_EMAIL', ''),
    'S3_BUCKET':        os.environ.get('S3_BUCKET', ''),
    'DAYS_LOOKBACK':    int(os.environ.get('DAYS_LOOKBACK', '7')),
    'MAX_WHATS_NEW':    int(os.environ.get('MAX_WHATS_NEW', '80')),
    'BEDROCK_REGION':   os.environ.get('BEDROCK_REGION', 'us-east-1'),

    # Language the digest is written in. Each value has its own prompt (see
    # _PROMPT_BUILDERS); this is not a "reply in X" suffix bolted onto one
    # English prompt.
    'DIGEST_LANGUAGE':  os.environ.get('DIGEST_LANGUAGE', 'en'),

    # ── LLM provider ────────────────────────────────────────
    # 'bedrock' (the default, and behaves exactly as it always has) or
    # 'openai_compatible'. The second one covers Gemini, a local LM Studio and
    # a self-hosted LiteLLM through the same code path, so there is no separate
    # SDK per vendor.
    'LLM_PROVIDER':      os.environ.get('LLM_PROVIDER', 'bedrock'),
    'LLM_BASE_URL':      os.environ.get('LLM_BASE_URL', ''),
    'LLM_MODEL':         os.environ.get('LLM_MODEL', ''),
    # The API key lives in SSM, same as the LinkedIn token. Setting this to an
    # empty string is how you declare "this endpoint needs no auth" — a local
    # LM Studio, say. Leaving the default path in place but never creating the
    # parameter is a misconfiguration, and it fails loudly instead of quietly
    # going out unauthenticated.
    'LLM_API_KEY_PARAM': os.environ.get('LLM_API_KEY_PARAM', '/aws-weekly-digest/llm-api-key'),
    'LLM_TIMEOUT':       int(os.environ.get('LLM_TIMEOUT', '240')),

    # ── Output channels (false = wired up but switched off) ─
    'FEATURES': {
        'SEND_EMAIL':             os.environ.get('FEATURE_SEND_EMAIL',      'true') == 'true',
        'EMBED_CONTENT_IN_EMAIL': os.environ.get('FEATURE_EMBED_CONTENT',   'true') == 'true',
        'SAVE_TO_S3':             os.environ.get('FEATURE_SAVE_TO_S3',      'true') == 'true',
        'POST_TO_LINKEDIN':       os.environ.get('FEATURE_POST_TO_LINKEDIN','false') == 'true',
        'POST_TO_WEBHOOK':        os.environ.get('FEATURE_POST_TO_WEBHOOK', 'false') == 'true',
    },
}


# ────────────────────────────────────────────────────────────
# Entry point
# ────────────────────────────────────────────────────────────
def lambda_handler(event, context):
    print('Generating AWS Weekly Digest...')
    try:
        whats_new  = fetch_aws_whats_new()
        blog_posts = fetch_aws_blog_posts()
        print(f"Fetched {len(whats_new)} What's New items and {len(blog_posts)} blog posts")

        if not whats_new:
            raise ValueError("No What's New items for this week — check whether the RSS feed is healthy.")

        digest_content = generate_digest(whats_new, blog_posts)
        print(f"{CONFIG['LLM_PROVIDER']} finished the analysis")

        s3_url = None
        if CONFIG['FEATURES']['SAVE_TO_S3'] and CONFIG['S3_BUCKET']:
            s3_url = save_to_s3(digest_content)
            print(f'Archived to S3: {s3_url}')

        if CONFIG['FEATURES']['SEND_EMAIL']:
            send_email(digest_content, s3_url, len(whats_new), len(blog_posts))
            print('Email sent')

        if CONFIG['FEATURES']['POST_TO_LINKEDIN']:
            post_to_linkedin(digest_content)

        if CONFIG['FEATURES']['POST_TO_WEBHOOK']:
            post_to_webhook(digest_content, s3_url)

        print('Done.')
        return {'statusCode': 200, 'body': 'OK'}

    except Exception as e:
        print(f'Failed: {e}')
        _send_error_email(str(e))
        raise


# ────────────────────────────────────────────────────────────
# Fetch: AWS What's New RSS
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
        print(f"[What's New] fetch failed: {e}")
        return []


# ────────────────────────────────────────────────────────────
# Fetch: AWS News Blog RSS (soft-fails on purpose — see the except below)
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

        # Strip the namespace declarations so ElementTree stops choking on them.
        # Stripping only the xmlns declarations is not enough, and that is exactly
        # the bug this project shipped with: every item still carries prefixed tags
        # like <dc:creator> and <content:encoded>, so the moment the declarations
        # are gone ElementTree throws "unbound prefix". The tag prefixes therefore
        # get stripped too — nothing below reads those fields anyway, only
        # title/link/description/pubDate.
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
        print(f'[Blog] skipped: {e}')
        return []


# ────────────────────────────────────────────────────────────
# Prompt assembly and digest generation
#
# The digest language is a deployment parameter, not something baked into the
# code. Each language gets its own prompt rather than an English prompt with
# "reply in X" bolted on the end — the section headings and the per-item fields
# are part of the output contract, so they have to be written in the target
# language to come back reliably.
#
# Adding a language means writing a builder and registering it in
# _PROMPT_BUILDERS below. Nothing else needs to change.
# ────────────────────────────────────────────────────────────
def _prompt_en(whats_new, blog_posts):
    today      = _fmt_date(datetime.now())
    week_range = _week_range()

    wn_text = '\n\n'.join(
        f"[WN{i+1}] {item['title']}\nSummary: {item['summary']}\nLink: {item['link']}"
        for i, item in enumerate(whats_new)
    )
    blog_text = '\n\n'.join(
        f"[B{i+1}] {item['title']}\n{item['description']}\nLink: {item['link']}"
        for i, item in enumerate(blog_posts)
    ) if blog_posts else '(no new blog posts this week)'

    return f"""You are a principal cloud architect who knows Amazon Web Services deeply, and a strong technical writer.
Using this week's ({week_range}) AWS What's New entries and blog posts below, write a digest suitable for sharing with a technical audience.
Output the digest itself and nothing else — no preamble, no introduction, no commentary about what you are doing.

## AWS What's New ({len(whats_new)} entries)
{wn_text}

## AWS Blog Posts ({len(blog_posts)} posts)
{blog_text}

---

## Output format (follow it exactly, write in English, no emoji in headings)

# AWS Weekly Digest | {today}

> This week: [2-3 sentences: which services shipped something significant, the overall direction of the week, anything that moves cost or architecture]

---

## By the numbers
- Compute (EC2 / Lambda / Container): N
- Storage & Database (S3 / RDS / DynamoDB): N
- AI & Machine Learning (Bedrock / SageMaker): N
- Networking & Security (VPC / CloudFront / IAM / WAF): N
- Data & Analytics (Redshift / Athena / Kinesis): N
- Developer Tools & Other: N

---

## Compute (EC2 / Lambda / Container)

### [feature name]
**In one line**: [what this update actually does]
**Why it matters**: [the real effect on an engineer or a business, under 30 words]
**Cost impact**: [none / reduces cost / adds cost / needs evaluation]
**Status**: [GA / Preview / Deprecated]
**Availability**: [global / selected regions / specific region names]
**Official link**: [the corresponding link]

[List the 3-6 most important updates in this category. Skip pure documentation fixes.]

---

## Storage & Database (S3 / RDS / DynamoDB)
[same format, 3-6 entries]

---

## AI & Machine Learning (Bedrock / SageMaker)
[same format, 3-6 entries]

---

## Networking & Security (VPC / CloudFront / IAM / WAF)
[same format, 3-5 entries]

---

## Data & Analytics (Redshift / Athena / Kinesis)
[same format, 3-5 entries]

---

## Developer Tools & Other
[same format, 3-5 entries]

---

## Blog picks
[The 2-3 posts most worth reading, formatted as: **title**: one line on why it is worth your time -> link]

---

## Architect's take
[Pick the single update most worth paying attention to this week and explain what it really means for an enterprise cloud architect. Around 100 words, first person, "I think...". Call out any cost impact explicitly.]

---
AWS Weekly Digest Bot | {today}
"""


def _prompt_zh_tw(whats_new, blog_posts):
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

    return f"""你是一位精通 Amazon Web Services 的首席雲端架構師，同時也是優秀的技術寫作者。
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


_PROMPT_BUILDERS = {
    'en':    _prompt_en,
    'zh-TW': _prompt_zh_tw,
}


def generate_digest(whats_new, blog_posts):
    lang    = CONFIG['DIGEST_LANGUAGE']
    builder = _PROMPT_BUILDERS.get(lang)
    if builder is None:
        raise ValueError(
            f"Unknown DIGEST_LANGUAGE: {lang!r} "
            f"(expected one of {', '.join(sorted(_PROMPT_BUILDERS))})"
        )
    return _invoke_llm(builder(whats_new, blog_posts))


# ────────────────────────────────────────────────────────────
# LLM call layer
#
# This layer raises on failure on purpose. No graceful degradation here.
#
# The reason is the real bug this project shipped with, which was the opposite
# pattern: an except block written for resilience quietly swallowed a parser
# error. Every invoke returned 200. Email sent, S3 file written, StatusCode 200
# all the way down — with one section permanently empty. The only symptom
# anywhere was a single CloudWatch log line you would have to already suspect
# something to go and read.
#
# The summary is the entire value of the email. If it cannot be produced the run
# should fail: the except in lambda_handler sends an error notification and
# re-raises, which is the behaviour you actually want.
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
        f"Unknown LLM_PROVIDER: {provider!r} (expected 'bedrock' or 'openai_compatible')"
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
# OpenAI-compatible endpoints — Gemini, LM Studio and a self-hosted LiteLLM all
# go through this one function.
#
# Configure it with:
#   LLM_PROVIDER=openai_compatible
#   LLM_BASE_URL=<endpoint including /v1; a trailing slash is fine either way>
#   LLM_MODEL=<model name>
#   LLM_API_KEY_PARAM=<SSM parameter path, defaults to /aws-weekly-digest/llm-api-key>
#                      an empty string means "this endpoint needs no auth"
#
# Endpoints known to work:
#   Gemini     https://generativelanguage.googleapis.com/v1beta/openai
#   LM Studio  http://<host>:1234/v1   <- the Lambda runs in AWS, so this has to
#                                         be an address reachable from there
#   LiteLLM    http://<host>:4000/v1
# ────────────────────────────────────────────────────────────
def _invoke_openai_compatible(prompt):
    base_url = CONFIG['LLM_BASE_URL'].rstrip('/')
    model    = CONFIG['LLM_MODEL']

    missing = [k for k, v in (('LLM_BASE_URL', base_url), ('LLM_MODEL', model)) if not v]
    if missing:
        raise ValueError(
            f"LLM_PROVIDER=openai_compatible but required settings are missing: {', '.join(missing)}"
        )

    headers = {'Content-Type': 'application/json'}
    key_param = CONFIG['LLM_API_KEY_PARAM']
    if key_param:
        # A configured parameter path has to resolve. Failing to read it is a
        # misconfiguration, not something to shrug off and continue without.
        ssm = boto3.client('ssm')
        try:
            api_key = ssm.get_parameter(Name=key_param, WithDecryption=True)['Parameter']['Value']
        except Exception as e:
            raise RuntimeError(
                f'Could not read SSM parameter {key_param}: {e}. '
                f'If this endpoint genuinely needs no auth, set LLM_API_KEY_PARAM '
                f'to an empty string to say so explicitly.'
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
        raise RuntimeError(f'{model} returned HTTP {e.code}: {detail}') from e
    except Exception as e:
        raise RuntimeError(f'Call to {url} failed: {e}') from e

    try:
        result  = json.loads(raw)
        content = result['choices'][0]['message']['content']
    except Exception as e:
        raise RuntimeError(f'Unexpected response shape from {model}: {raw[:500]}') from e

    if not content or not content.strip():
        raise RuntimeError(f'{model} returned empty content (finish_reason='
                           f'{result["choices"][0].get("finish_reason")!r})')

    return content


# ────────────────────────────────────────────────────────────
# Output A: S3 archive
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
# Output B: email via SES, with the digest inlined as HTML
#
# The wrapper text follows DIGEST_LANGUAGE too. An English digest inside a
# Chinese-labelled email reads like a bug, so the chrome and the content stay in
# step. Falls back to English for a language that has a prompt but no strings
# here yet.
# ────────────────────────────────────────────────────────────
_EMAIL_STRINGS = {
    'en': {
        'subject':      '[AWS] Weekly Digest {today}',
        's3_link':      'Raw archive in S3',
        'subhead':      "{today} | {wn_count} What's New, {blog_count} blog posts",
        'footer':       'AWS Weekly Digest Bot — generated automatically, read it before you share it',
        'text_part':    'AWS Weekly Digest {today}\n(this email is meant to be read as HTML)',
        'error_subject':'[AWS] Weekly Digest failed',
        'error_body':   'Error: {error_msg}',
    },
    'zh-TW': {
        'subject':      '[AWS] Weekly Digest {today}',
        's3_link':      'S3 原始存檔',
        'subhead':      "{today}｜What's New {wn_count} 條，Blog {blog_count} 篇",
        'footer':       'AWS Weekly Digest Bot｜自動產生，請審閱後再分享',
        'text_part':    'AWS Weekly Digest {today}\n（請以 HTML 郵件查看）',
        'error_subject':'[AWS] Weekly Digest 產生失敗',
        'error_body':   '錯誤：{error_msg}',
    },
}


def _email_strings():
    return _EMAIL_STRINGS.get(CONFIG['DIGEST_LANGUAGE'], _EMAIL_STRINGS['en'])


def send_email(digest_content, s3_url, wn_count, blog_count):
    today   = _fmt_date(datetime.now())
    t       = _email_strings()
    s3_link = (f'<p style="margin:12px 0"><a href="{s3_url}" style="color:#FF9900;font-weight:600">{t["s3_link"]}</a></p>'
               if s3_url else '')
    body_html = markdown_to_html(digest_content) if CONFIG['FEATURES']['EMBED_CONTENT_IN_EMAIL'] else ''

    html_body = f"""
<div style="font-family:-apple-system,Arial,sans-serif;max-width:680px;margin:0 auto;color:#232F3E">
  <div style="background:#232F3E;padding:20px 32px;border-radius:8px 8px 0 0">
    <h1 style="margin:0;color:white;font-size:20px;font-weight:700">AWS Weekly Digest</h1>
    <p style="margin:6px 0 0;color:#FF9900;font-size:14px">{t["subhead"].format(today=today, wn_count=wn_count, blog_count=blog_count)}</p>
  </div>
  <div style="background:#fff;padding:24px 32px;border:1px solid #e0e0e0;border-top:none;border-radius:0 0 8px 8px">
    {s3_link}
    <div style="margin-top:16px">{body_html}</div>
    <p style="margin:24px 0 0;font-size:12px;color:#999;border-top:1px solid #f0f0f0;padding-top:12px">
      {t["footer"]}
    </p>
  </div>
</div>"""

    ses = boto3.client('ses', region_name=CONFIG['BEDROCK_REGION'])
    ses.send_email(
        Source=CONFIG['SENDER_EMAIL'],
        Destination={'ToAddresses': [CONFIG['RECIPIENT_EMAIL']]},
        Message={
            'Subject': {'Data': t['subject'].format(today=today), 'Charset': 'UTF-8'},
            'Body': {
                'Html': {'Data': html_body,                        'Charset': 'UTF-8'},
                'Text': {'Data': t['text_part'].format(today=today), 'Charset': 'UTF-8'},
            },
        },
    )


def _send_error_email(error_msg):
    try:
        t   = _email_strings()
        ses = boto3.client('ses', region_name=CONFIG['BEDROCK_REGION'])
        ses.send_email(
            Source=CONFIG['SENDER_EMAIL'],
            Destination={'ToAddresses': [CONFIG['RECIPIENT_EMAIL']]},
            Message={
                'Subject': {'Data': t['error_subject'], 'Charset': 'UTF-8'},
                'Body':    {'Text': {'Data': t['error_body'].format(error_msg=error_msg), 'Charset': 'UTF-8'}},
            },
        )
    except Exception as e:
        print(f'Could not send the error email either: {e}')


# ────────────────────────────────────────────────────────────
# Output C: LinkedIn — off by default (FEATURE_POST_TO_LINKEDIN=false)
# To switch it on:
#   1. Create a LinkedIn developer app and get an OAuth access token
#   2. aws ssm put-parameter --name /aws-weekly-digest/linkedin-access-token \
#        --type SecureString --value <TOKEN>
#   3. aws ssm put-parameter --name /aws-weekly-digest/linkedin-person-urn \
#        --type String --value <URN>
#   4. Set FEATURE_POST_TO_LINKEDIN=true
# ────────────────────────────────────────────────────────────
def post_to_linkedin(content):
    ssm = boto3.client('ssm')
    try:
        token  = ssm.get_parameter(Name='/aws-weekly-digest/linkedin-access-token', WithDecryption=True)['Parameter']['Value']
        urn    = ssm.get_parameter(Name='/aws-weekly-digest/linkedin-person-urn')['Parameter']['Value']
    except Exception as e:
        print(f'[LinkedIn] SSM parameters missing, skipping: {e}')
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
            print(f'[LinkedIn] posted: {result.get("id")}')
            return result.get('id')
    except Exception as e:
        print(f'[LinkedIn] post failed: {e}')
        return None


# ────────────────────────────────────────────────────────────
# Output D: generic webhook — off by default (FEATURE_POST_TO_WEBHOOK=false)
# To switch it on:
#   1. Create an n8n / Make webhook
#   2. aws ssm put-parameter --name /aws-weekly-digest/webhook-url \
#        --type SecureString --value <URL>
#   3. Set FEATURE_POST_TO_WEBHOOK=true
# Payload: { title, content, linkedInText, s3Url, generatedAt }
# ────────────────────────────────────────────────────────────
def post_to_webhook(content, s3_url):
    ssm = boto3.client('ssm')
    try:
        webhook_url = ssm.get_parameter(Name='/aws-weekly-digest/webhook-url', WithDecryption=True)['Parameter']['Value']
    except Exception as e:
        print(f'[Webhook] SSM parameter missing, skipping: {e}')
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
            print('[Webhook] sent')
    except Exception as e:
        print(f'[Webhook] failed: {e}')


# ────────────────────────────────────────────────────────────
# Markdown → HTML for the email body (AWS orange theme)
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
# Markdown → plain text for LinkedIn
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
# Helpers
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
