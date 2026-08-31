"""
Account-aware advice section for the digest.

The digest on its own reads the same whether you run a hundred accounts or none.
This adds one section on the end: given what this account actually uses, what is
worth improving? Nothing else about the digest changes, and the whole thing is
off unless FEATURE_ACCOUNT_ADVICE says otherwise.

Three decisions worth knowing before you read the code, each of which came from
something that went wrong on the way:

**Usage, not spend.** The first cut ranked services by cost, which on the account
this was written against produced nothing at all — three months of billing came
to $0.0014. Cost is the wrong signal for an account inside the free tier; the
services are genuinely in use (CloudFront served 5.6M units over the same
period), they are simply not charged for. Presence is the signal, with cost as an
ordering hint where there is any.

**Usage types, not service names.** Given only a list of service names, a model
has nothing to reason from but the names, and writes "you may not have configured
X, consider configuring X" for every line — a sentence true of every account,
which makes it worth nothing. `PublicIPv4:IdleAddress`, `NatGateway-Hours` and
`AmazonEKS-Hours:extendedSupport` each say something specific and checkable.

**Detection in code, explanation in the model.** Even with usage types in front
of it, the model spent three of its five slots on "check your CloudFront config,
it might reduce costs" while missing both the idle IPv4 address and two regions
of NAT gateway. Pattern matching is deterministic work: `_FLAGGED_PATTERNS` finds
them, and the model's job is to explain what was found.
"""

import collections
import os
from datetime import datetime, timedelta, timezone

import boto3

# Every Cost Explorer call costs $0.01. A single-account run makes two (discovery
# plus detail); an organization makes one more per account detailed below.
LOOKBACK_DAYS = int(os.environ.get('ACCOUNT_ADVICE_LOOKBACK_DAYS', '90'))

# In an organization, how many linked accounts get their own usage-type
# breakdown. The rest are listed with spend only. Ordered by spend, so this is
# "detail the accounts where the money is".
MAX_DETAILED_ACCOUNTS = int(os.environ.get('ACCOUNT_ADVICE_MAX_ACCOUNTS', '5'))

# How many usage types to print per service. Flagged usage types are printed on
# top of this quota rather than competing for it — see format_account_services.
TOP_USAGE_TYPES = int(os.environ.get('ACCOUNT_ADVICE_TOP_USAGE_TYPES', '4'))

# Ceiling on the rendered service listing. An organization with hundreds of
# services across dozens of regions would otherwise build a prompt large enough
# to cost more than everything it saves. Truncation is always announced in the
# prompt itself — a silently short list would read exactly like a complete one.
MAX_LISTING_CHARS = int(os.environ.get('ACCOUNT_ADVICE_MAX_LISTING_CHARS', '12000'))

# Ceiling on flagged signals. Generous, because these are the highest-value lines
# in the whole prompt; it exists so a pathological account cannot blow the budget
# through this path either.
MAX_FLAGS = int(os.environ.get('ACCOUNT_ADVICE_MAX_FLAGS', '40'))

# Services that appear in every account whether or not anyone chose them. They
# say nothing about what this account is for.
_ALWAYS_PRESENT = {
    'Tax',
    'AWS Support (Developer)',
    'AWS Support (Business)',
    'AWS Support (Enterprise)',
}

# Usage types whose name alone is evidence of waste or risk.
_FLAGGED_PATTERNS = (
    ('IdleAddress',        '閒置的公有 IPv4 位址，自 2024-02 起無論是否附掛都要收費',
                           'idle public IPv4 address — billed since Feb 2024 whether attached or not'),
    ('extendedSupport',    'Kubernetes 版本已過標準支援期，正在付延長支援加價',
                           'Kubernetes version past standard support, paying the extended-support surcharge'),
    ('NatGateway-Hours',   'NAT Gateway 按小時計費，閒置也照算',
                           'NAT Gateway bills per hour whether or not traffic flows'),
    ('CapacityUnit-Hrs',   'DynamoDB 佈建容量，用不用都照小時收費（相對於 on-demand）',
                           'DynamoDB provisioned capacity — billed hourly regardless of traffic'),
    ('SnapshotUsage',      'EBS 快照存放費用，常見的遺留成本',
                           'EBS snapshot storage, a common leftover cost'),
)


# ────────────────────────────────────────────────────────────
# Cost Explorer
# ────────────────────────────────────────────────────────────
def _ce_client():
    return boto3.client('ce', region_name='us-east-1')  # Cost Explorer is us-east-1 only


def _window(now=None):
    now = now or datetime.now(timezone.utc)
    return ((now - timedelta(days=LOOKBACK_DAYS)).strftime('%Y-%m-%d'),
            now.strftime('%Y-%m-%d'))


def _paginate(ce, **kwargs):
    """Yield every group across every page.

    Pagination matters even though a single small account never sees a second
    page: Cost Explorer caps a response at 1,000 groups, and an organization with
    many services across many regions goes past that. Without this the result is
    a silently short list that reads exactly like a complete one — the advice
    would simply never mention whatever fell off the end.
    """
    while True:
        resp = ce.get_cost_and_usage(**kwargs)
        for period in resp['ResultsByTime']:
            for group in period['Groups']:
                yield group
        token = resp.get('NextPageToken')
        if not token:
            break
        kwargs['NextPageToken'] = token


def discover_linked_accounts(now=None):
    """Return [(account_id, cost)] most-billed first.

    A standalone account comes back as a single entry, which is how the caller
    tells the two cases apart. Worth the extra $0.01: aggregating an
    organization's usage into one undifferentiated list produces advice that
    names a problem without naming the account it lives in, which is not
    actionable and does not announce that it is unactionable.
    """
    start, end = _window(now)
    cost = collections.defaultdict(float)
    for group in _paginate(
        _ce_client(),
        TimePeriod={'Start': start, 'End': end},
        Granularity='MONTHLY',
        Metrics=['UnblendedCost'],
        GroupBy=[{'Type': 'DIMENSION', 'Key': 'LINKED_ACCOUNT'}],
    ):
        cost[group['Keys'][0]] += float(group['Metrics']['UnblendedCost']['Amount'])
    return sorted(cost.items(), key=lambda kv: -kv[1])


def fetch_account_services(now=None, linked_account=None):
    """Return the services used, most-billed first.

    Each entry is {'service', 'cost', 'usage_types', 'all_usage_types'}, where
    'usage_types' is the largest few for display and 'all_usage_types' is
    everything. Services with neither cost nor usage are dropped: Cost Explorer
    lists some of those for accounts that merely have the service enabled.

    `linked_account` filters to one account in an organization. Raises on
    failure; build_advice_section decides what to do about it.
    """
    start, end = _window(now)
    kwargs = {
        'TimePeriod': {'Start': start, 'End': end},
        'Granularity': 'MONTHLY',
        'Metrics': ['UnblendedCost', 'UsageQuantity'],
        # Cost Explorer allows at most two GroupBy dimensions, which is why the
        # account split is a filter on separate calls rather than a third group.
        'GroupBy': [
            {'Type': 'DIMENSION', 'Key': 'SERVICE'},
            {'Type': 'DIMENSION', 'Key': 'USAGE_TYPE'},
        ],
    }
    if linked_account:
        kwargs['Filter'] = {'Dimensions': {'Key': 'LINKED_ACCOUNT',
                                           'Values': [linked_account]}}

    cost  = collections.defaultdict(float)
    usage = collections.defaultdict(lambda: collections.defaultdict(float))
    for group in _paginate(_ce_client(), **kwargs):
        service, usage_type = group['Keys']
        cost[service] += float(group['Metrics']['UnblendedCost']['Amount'])
        qty = float(group['Metrics']['UsageQuantity']['Amount'])
        if qty > 0:
            usage[service][usage_type] += qty

    services = []
    for service in cost:
        if service in _ALWAYS_PRESENT:
            continue
        if cost[service] <= 0 and not usage[service]:
            continue
        ranked = sorted(usage[service].items(), key=lambda kv: -kv[1])
        services.append({
            'service':         service,
            'cost':            cost[service],
            'usage_types':     ranked[:TOP_USAGE_TYPES],
            'all_usage_types': dict(usage[service]),
        })
    services.sort(key=lambda s: (-s['cost'], s['service']))
    return services


# ────────────────────────────────────────────────────────────
# Rendering
# ────────────────────────────────────────────────────────────
def _fmt_qty(q):
    if q >= 1000:
        return f'{q:,.0f}'
    if q >= 1:
        return f'{q:.2f}'.rstrip('0').rstrip('.')
    return f'{q:.3g}'


def _is_flagged(usage_type):
    lowered = usage_type.lower()
    return any(needle.lower() in lowered for needle, _, _ in _FLAGGED_PATTERNS)


def flag_usage_types(services, lang, account_label=None):
    """Return lines naming usage types that are evidence of waste or risk.

    Scans every usage type, not the truncated display list. The highest-signal
    names are often the smallest quantities — `PublicIPv4:IdleAddress` was 0.089
    on the account this was written against, nowhere near the top four — so
    flagging off the display list would miss exactly the ones worth flagging,
    and would miss more of them the larger the account. Fixing this doubled the
    flag count (7 to 14) on a 34-service account.

    Deliberately conservative: only names that mean the same thing in every
    account, and never a guess at configuration.
    """
    zh     = lang == 'zh-TW'
    prefix = f'[{account_label}] ' if account_label else ''
    found  = []
    for s in services:
        for usage_type, qty in sorted(s['all_usage_types'].items(), key=lambda kv: -kv[1]):
            for needle, reason_zh, reason_en in _FLAGGED_PATTERNS:
                if needle.lower() in usage_type.lower():
                    reason = reason_zh if zh else reason_en
                    found.append(
                        f'- {prefix}`{usage_type}` = {_fmt_qty(qty)}'
                        f'（{s["service"]}）：{reason}' if zh else
                        f'- {prefix}`{usage_type}` = {_fmt_qty(qty)} '
                        f'({s["service"]}): {reason}')
                    break
    return found


def format_account_services(services, lang, budget=None):
    """Render services and their usage types, within a character budget.

    Each service shows its largest few usage types plus any flagged ones that
    did not make that cut — flagged lines are the reason this prompt is worth
    sending, so they are never squeezed out by a display quota.

    Cost is printed only where it is real money. Quantities always appear
    attached to their usage type and never alone, because they are not
    comparable across services — CloudFront counts requests, EKS counts
    cluster-hours — and a bare '5657085' beside a bare '2.01' reads as
    importance. The prompt says so in words as well.

    Returns (text, omitted_count). Truncation is reported, never silent.
    """
    zh     = lang == 'zh-TW'
    budget = MAX_LISTING_CHARS if budget is None else budget

    lines = []
    used  = 0
    for index, s in enumerate(services):
        block = [f"- {s['service']}"]
        if s['cost'] >= 0.01:
            block[0] += (f"（近 {LOOKBACK_DAYS} 天 ${s['cost']:.2f}）" if zh
                         else f" (${s['cost']:.2f} in the last {LOOKBACK_DAYS} days)")

        shown = {ut for ut, _ in s['usage_types']}
        for usage_type, qty in s['usage_types']:
            block.append(f'    {usage_type} = {_fmt_qty(qty)}')
        for usage_type, qty in sorted(s['all_usage_types'].items(), key=lambda kv: -kv[1]):
            if usage_type not in shown and _is_flagged(usage_type):
                block.append(f'    {usage_type} = {_fmt_qty(qty)}')

        rendered = '\n'.join(block)
        if used + len(rendered) > budget and lines:
            return '\n'.join(lines), len(services) - index
        lines.append(rendered)
        used += len(rendered) + 1

    return '\n'.join(lines), 0


# ────────────────────────────────────────────────────────────
# Prompts
# ────────────────────────────────────────────────────────────
def _advice_prompt_zh_tw(listing, spend_note, flagged='', scope_note=''):
    return f"""你是一位資深 AWS 解決方案架構師，正在替一位技術主管檢視他的 AWS 用量。
{scope_note}
## 近 {LOOKBACK_DAYS} 天的實際用量（來自 Cost Explorer，服務 + 用量類型）

{listing}

{spend_note}
{flagged}
### 怎麼讀這份資料

- 縮排那行是 **usage type**，前綴是區域代碼（`USE1`=us-east-1、`APS1`=ap-southeast-1、
  `APN1`=ap-northeast-1、`APS3`=ap-south-1、`APE2`=ap-east-2、`EUS1`=eu-south-1 等），
  沒有前綴代表 us-east-1 或全域。同一個服務出現多個區域前綴，就是資源散在多區。
- **數量不能跨服務比大小**——CloudFront 數的是請求數，EKS 數的是叢集小時，
  兩個數字放在一起沒有意義。只在同一個服務內部比較。
- 這份資料只看得到「用了什麼、用了多少」，**看不到任何設定內容**。

---

請針對這份用量提出優化與改善建議。要求：

- **每一條都必須指向上面某一個具體的 usage type，並把它寫進建議裡。**
  講不出對應 usage type 的建議就不要寫。
- **如果上面有「已標記的訊號」區塊，那幾條要優先寫，而且要寫在最前面。**
  那是程式碼直接從 usage type 名稱判定出來的既成事實，不是推測。
- 只談清單上有的服務。不要建議他「導入」清單上沒有的新服務。
- 挑 3-5 條最值得動手的，依投入產出比排序，不要每個服務寫一條。
- 每條格式：

**[服務名] 一句話結論**
- **看到什麼**：[引用具體的 usage type 與數量，說明它代表什麼]
- **建議動作**：[具體到可以直接執行的程度]
- **預期效果**：[省錢／降風險／省維運時間，擇一講清楚。若省不了錢就誠實說「這條不是為了省錢」]

- ⚠️ 不要寫「可能沒有設定 X，建議設定 X」這種從服務名硬猜設定的建議——
  你看不到他的設定，這種話對每個帳號都成立，等於沒說。
- **不要硬套「幫你省錢」**。值得講的是閒置資源、跑在延長支援上的版本、
  散在多區的資源、以及開了沒在用的東西；金額小就誠實說金額小。
- 直接輸出建議內容，不要前言、不要自我介紹、不要結尾客套。
- 使用繁體中文，標題不要使用 emoji。
"""


def _advice_prompt_en(listing, spend_note, flagged='', scope_note=''):
    return f"""You are a senior AWS solutions architect reviewing an engineering
leader's AWS usage.
{scope_note}
## Usage over the last {LOOKBACK_DAYS} days (Cost Explorer, service + usage type)

{listing}

{spend_note}
{flagged}
### How to read this

- The indented lines are **usage types**. The prefix is a region code (`USE1` =
  us-east-1, `APS1` = ap-southeast-1, `APN1` = ap-northeast-1, and so on); no
  prefix means us-east-1 or global. One service under several prefixes means
  resources spread across regions.
- **Quantities are not comparable across services** — CloudFront counts
  requests, EKS counts cluster-hours. Only compare within one service.
- This data shows what was used and how much. It shows **nothing about
  configuration**.

---

Give optimization and improvement advice based on this usage. Rules:

- **Every item must point at a specific usage type above and name it.** If you
  cannot tie a recommendation to one, do not write it.
- **If a "flagged signals" block appears above, cover those first and put them
  at the top.** Those are established facts derived from the usage type names in
  code, not inferences.
- Only discuss services on the list. Never suggest adopting a new service.
- Pick the 3-5 highest-leverage items, ordered by return on effort. Do not write
  one item per service.
- Format each as:

**[Service] one-line conclusion**
- **What I see**: [quote the usage type and quantity, and say what it implies]
- **Suggested action**: [concrete enough to act on]
- **Expected result**: [cost, risk, or maintenance time — pick one and be clear.
  If it saves no money, say so plainly]

- Do not write "you may not have configured X, consider configuring X". You
  cannot see their configuration, and that sentence is true of every account,
  which makes it worth nothing.
- Do not force a "save money" angle. What is worth raising is idle resources,
  versions on extended support, resources scattered across regions, and things
  switched on but unused. If the amounts are small, say so.
- Output the advice directly: no preamble, no sign-off.
"""


_ADVICE_PROMPTS = {'zh-TW': _advice_prompt_zh_tw, 'en': _advice_prompt_en}


# ────────────────────────────────────────────────────────────
# Assembly
# ────────────────────────────────────────────────────────────
def _collect(lang):
    """Gather (services_for_listing, flag_lines, scope_note).

    Single account and organization take the same shape on the way out, so the
    prompt builder does not have to care which it is looking at.
    """
    zh       = lang == 'zh-TW'
    accounts = discover_linked_accounts()

    if len(accounts) <= 1:
        return fetch_account_services(), None, ''

    detailed = accounts[:MAX_DETAILED_ACCOUNTS]
    rest     = accounts[MAX_DETAILED_ACCOUNTS:]

    merged, flags = {}, []
    for account_id, _ in detailed:
        services = fetch_account_services(linked_account=account_id)
        flags.extend(flag_usage_types(services, lang, account_label=account_id))
        for s in services:
            # Prefix the service name with the account so the listing keeps the
            # attribution the flags have. Advice that names a problem without
            # naming the account it lives in is not actionable.
            key = f"{s['service']} @ {account_id}"
            merged[key] = dict(s, service=key)

    services = sorted(merged.values(), key=lambda s: (-s['cost'], s['service']))

    lines = [f'共 {len(accounts)} 個帳號，已逐一分析花費前 {len(detailed)} 個：'] if zh else \
            [f'{len(accounts)} accounts in this organization; the top {len(detailed)} by spend are detailed individually:']
    for account_id, cost in detailed:
        lines.append(f'- {account_id}：${cost:.2f}' if zh else f'- {account_id}: ${cost:.2f}')
    if rest:
        total = sum(c for _, c in rest)
        lines.append(
            f'- 其餘 {len(rest)} 個帳號合計 ${total:.2f}，本次未取用量明細，'
            f'**不要對它們提出建議**。' if zh else
            f'- {len(rest)} further accounts totalling ${total:.2f}, with no usage detail '
            f'fetched — **do not give advice about those**.')

    header = '### 帳號範圍（AWS Organizations）' if zh else '### Scope (AWS Organizations)'
    return services, flags, '\n' + header + '\n\n' + '\n'.join(lines) + '\n'


def build_advice_section(lang, invoke_llm):
    """Return (markdown_section, warning). Never raises.

    A failed Cost Explorer call must not cost you the weekly digest — the digest
    is the product and this section is an addition to it. But this codebase
    already shipped one bug where an except written for resilience swallowed a
    parse error and mailed a permanently empty section for weeks, so the failure
    comes back as a warning for the caller to log and surface, rather than being
    quietly absorbed here.
    """
    builder = _ADVICE_PROMPTS.get(lang)
    if builder is None:
        return '', f'no advice prompt for DIGEST_LANGUAGE={lang!r}'

    try:
        services, flags, scope_note = _collect(lang)
    except Exception as e:                                    # noqa: BLE001
        return '', f'account advice skipped: {type(e).__name__}: {e}'

    if not services:
        return '', 'account advice skipped: Cost Explorer returned no services'

    zh    = lang == 'zh-TW'
    total = sum(s['cost'] for s in services)
    if zh:
        spend_note = f'近 {LOOKBACK_DAYS} 天總花費：${total:.4f} USD。'
        heading    = '## 你的帳號：優化與改善建議'
    else:
        spend_note = f'Total spend over the last {LOOKBACK_DAYS} days: ${total:.4f} USD.'
        heading    = '## Your account: what to improve'

    listing, omitted = format_account_services(services, lang)
    if omitted:
        spend_note += (f'\n\n⚠️ 服務數量超出提示長度預算，上面依花費排序列出前 '
                       f'{len(services) - omitted} 個，另有 {omitted} 個未列出——'
                       f'**不要對未列出的服務提出建議**。' if zh else
                       f'\n\nNote: the service list exceeded the prompt budget. The '
                       f'{len(services) - omitted} highest-spend services are listed above '
                       f'and {omitted} are omitted — **do not give advice about the omitted ones**.')

    if flags is None:
        flags = flag_usage_types(services, lang)
    flagged = ''
    if flags:
        capped = flags[:MAX_FLAGS]
        title  = ('### 已標記的訊號（程式碼從 usage type 名稱直接判定，非推測）' if zh
                  else '### Flagged signals (derived in code from usage type names, not inferred)')
        flagged = '\n' + title + '\n\n' + '\n'.join(capped) + '\n'
        if len(flags) > len(capped):
            flagged += (f'\n（另有 {len(flags) - len(capped)} 條同類訊號未列出）\n' if zh
                        else f'\n({len(flags) - len(capped)} further signals of the same kinds not listed)\n')

    body = invoke_llm(builder(listing, spend_note, flagged, scope_note))
    return f'\n\n---\n\n{heading}\n\n{body}\n', ''
