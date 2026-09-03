# AWS Weekly Digest

> **TL;DR** — A Lambda that runs every Friday and does two things: it reads the
> week's AWS What's New and News Blog feeds and writes you a categorized digest,
> and it reads **your own account's Cost Explorer usage** and tells you what is
> worth fixing in it. What shipped, and what it means for what you already run,
> in one email. SES for delivery, S3 for the archive, deployed with SAM. No
> dependencies beyond boto3 and the Python standard library.

The LLM can be Amazon Bedrock (default) or any OpenAI-compatible endpoint —
Gemini, a local LM Studio instance, a self-hosted LiteLLM — via a single
`openai_compatible` provider, so there is no separate SDK per vendor.

I built this because I kept meaning to read the AWS What's New feed and never
did. But keeping up with what shipped is only half of why I wanted to read it.
The other half is what any of it means for the things I am already running —
and that half never arrives on its own, because the feed does not know what I
have. So the digest now also looks at my own account's usage and tells me what
is worth fixing there. It is still a small personal tool, not a product.

## How it works

```
  AWS What's New RSS  ─┐
                       ├─→ ┌──────────────────────────┐ ─→ LLM ─→ digest  ─┐
  AWS News Blog RSS   ─┘   │  Lambda                  │                    ├─→ SES → your inbox
                           │  (Python 3.12, arm64)    │                    └─→ S3  → digests/<date>/
  Cost Explorer  ────────→ └──────────────────────────┘ ─→ LLM ─→ advice  ─┘
  (your account's usage)              ▲
                        EventBridge Scheduler (Fri 17:00 Asia/Taipei)
```

Two halves, one email. The **digest** half is the same for everybody: what AWS
shipped this week, categorized. The **advice** half is the one only your account
can produce — it reads which services and usage types you actually billed over
the last 90 days and says what is worth fixing: idle IPv4 addresses, clusters on
extended support, NAT gateways left behind, provisioned capacity nothing is
using.

Neither half is the point on its own. Knowing what AWS released does not tell
you whether it matters to you, and a feed cannot tell you that because it does
not know what you have.

The advice half ships switched off — it costs a little money to run and needs an
IAM permission the digest does not, so turning it on should be your decision
rather than a surprise on your bill. See [Account advice](#account-advice) for
how to enable it, and why it is built on usage types rather than spend.

Two optional output channels — LinkedIn and a generic webhook — are wired up
but off by default. See the feature flags below.

## Prerequisites

- **SES**: both `SenderEmail` and `RecipientEmail` must be verified identities.
  In the SES sandbox you need to verify both, even if they are the same address.
- **Bedrock** (if using the default provider): model access enabled in your
  account for whichever model you pick. Read the IAM note below before you
  assume your policy is right.
- **SAM CLI** and credentials for the target account.
- **SSM Parameter Store** (only if `LLM_PROVIDER=openai_compatible` and your
  endpoint needs a key, or if you enable the LinkedIn channel).

## Deploy

```bash
cp samconfig.toml.example samconfig.toml   # then edit the emails
sam build
sam deploy
```

`samconfig.toml` is gitignored because it carries your real addresses.

Note that `capabilities` must be `CAPABILITY_NAMED_IAM`, not `CAPABILITY_IAM` —
the template declares an explicit IAM `RoleName`, which bumps the requirement.
This is the first thing that will stop a deploy if you write the config by hand.

## Configuration

All settings are environment variables on the Lambda, set through SAM parameters.

| Variable | Default | Notes |
|---|---|---|
| `RECIPIENT_EMAIL` | — | Must be SES-verified |
| `SENDER_EMAIL` | — | Must be SES-verified |
| `S3_BUCKET` | `''` | Empty disables archiving even if the flag is on |
| `DAYS_LOOKBACK` | `7` | How far back to pull feed items |
| `MAX_WHATS_NEW` | `80` | Cap on What's New items sent to the model |
| `BEDROCK_REGION` | `us-east-1` | Also the region used for SES |
| `BEDROCK_MODEL_ID` | `us.amazon.nova-pro-v1:0` | Nova and Claude payload shapes are both handled |
| `LLM_PROVIDER` | `bedrock` | Or `openai_compatible` |
| `LLM_BASE_URL` | `''` | Required for `openai_compatible` |
| `LLM_MODEL` | `''` | Required for `openai_compatible` |
| `LLM_API_KEY_PARAM` | `/aws-weekly-digest/llm-api-key` | SSM path. Set to empty string to declare "no auth needed" (e.g. local LM Studio) |
| `LLM_TIMEOUT` | `240` | Seconds to wait on the LLM. Keep it under the 600s function timeout, or Lambda dies before the call gives up and you lose the failure email with it |
| `DIGEST_LANGUAGE` | `en` | `en` or `zh-TW`. See below |
| `ADVICE_MODEL_ID` | `us.anthropic.claude-opus-5` | Model for the account advice section. Must be an inference profile ID |
| `BEDROCK_READ_TIMEOUT` | `240` | botocore's 60s default is not enough for a thinking model |
| `ACCOUNT_ADVICE_LOOKBACK_DAYS` | `90` | Cost Explorer window for the advice section |
| `ACCOUNT_ADVICE_MAX_ACCOUNTS` | `5` | In an organization, how many linked accounts get a usage breakdown |

Feature flags: `FEATURE_SEND_EMAIL`, `FEATURE_EMBED_CONTENT`,
`FEATURE_SAVE_TO_S3`, `FEATURE_POST_TO_LINKEDIN`, `FEATURE_POST_TO_WEBHOOK`,
`FEATURE_ACCOUNT_ADVICE`.

### Account advice

Set `FEATURE_ACCOUNT_ADVICE=true` to turn this half on. It needs
`ce:GetCostAndUsage` (already in the template) and costs $0.01 per Cost Explorer
call plus one extra model invocation — around $9/year at one run a week on
Claude Opus 5. That is the whole reason it ships off: it spends money and widens
the IAM policy, and neither should happen to you by default.

It is built on **usage types**, not spend. A list of service names gives a model
nothing to reason from but the names, and it will write "you may not have
configured X, consider configuring X" for every line — a sentence true of every
account. `PublicIPv4:IdleAddress`, `AmazonEKS-Hours:extendedSupport` and
`NatGateway-Hours` each say something specific and checkable. Detection of those
is pattern matching in `account_context.py`, not something the model is asked to
spot; the model's job is to explain what was found.

In an AWS Organization it fetches the top `ACCOUNT_ADVICE_MAX_ACCOUNTS` linked
accounts separately and labels every finding with its account, because advice
that names a problem without naming the account it lives in is not actionable.
Accounts past that limit are listed with spend only, and the prompt says so.

**The model choice matters more than the cost here.** On identical data, Nova
Pro spent three of five slots on "check your config, it might reduce costs" and
missed an idle IPv4 address; Claude Opus 5 worked out that 2,160 hours = 90 days
× 24 (a table provisioned around the clock), that extended-support hours equal to
cluster hours mean the cluster was created on an already-expired version, and
that snapshots in regions with no `BoxUsage` are leftovers from deleted
instances. Set `ADVICE_MODEL_ID` to a cheaper model if you want, but that is
what you are trading away.

### Digest language

`DIGEST_LANGUAGE` picks the language the digest is written in. It defaults to
`en`; the other value shipped today is `zh-TW` (Traditional Chinese).

Set it through the SAM parameter, same as everything else:

```bash
sam deploy --parameter-overrides 'DigestLanguage="zh-TW" ...'
```

Careful with that: `parameter_overrides` in `samconfig.toml` is one flat string,
and dropping a parameter from it makes CloudFormation quietly fall back to the
template default rather than telling you. Pass the whole set every time.

One setting covers everything a reader sees. The email wrapper — subject lines,
the counts under the header, the footer, the plain-text part, the failure
notification — comes from `_EMAIL_STRINGS` keyed on the same value, so you never
end up with an English digest inside a Chinese-labelled email. The advice
section follows the same value through `_ADVICE_PROMPTS` in
`account_context.py`. Code comments and CloudWatch log lines stay in English
regardless; those are for whoever is reading the repo, not for the recipient.

Each language has its own prompt in `lambda_function.py` (`_prompt_en`,
`_prompt_zh_tw`) rather than one English prompt with "reply in X" appended. The
section headings and per-item field names are part of the output contract — the
Markdown-to-HTML and Markdown-to-LinkedIn converters read that structure back —
so they have to be written in the target language to come back reliably.

To add a language: write a builder and register it in `_PROMPT_BUILDERS`, add
the matching entry to `_EMAIL_STRINGS`, add an advice-prompt builder to
`_ADVICE_PROMPTS` in `account_context.py`, then add the value to `AllowedValues`
on `DigestLanguage` in `template.yaml`. Miss the third one and the digest
arrives in the new language with the advice section still in English — an
unknown language raises at run time, but a *missing* one there returns a warning
and no section, which is quieter than it sounds.

## Four traps worth knowing about

The first two survived `sam build` and `sam validate` and only showed up on a
real invoke. The last two are quieter still — this account is too small to ever
trigger either one, so they were found by asking "would this hold up on a
bigger account?" before publishing the code, not by watching anything break.
If you are building something similar, these are the parts of this repo
actually worth your time.

### 1. Cross-Region Inference profile IDs are not model ARNs

Nova Pro is invoked through a cross-region inference profile — you pass
`us.amazon.nova-pro-v1:0` rather than a plain model ID. It is tempting to
substitute that same string into the IAM resource ARN:

```yaml
Resource:
  - !Sub 'arn:aws:bedrock:${BedrockRegion}::foundation-model/${BedrockModelId}'
```

That fails, and the error message tells you why if you read the resource
carefully:

```
AccessDeniedException: ... is not authorized to perform: bedrock:InvokeModel
on resource: arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-pro-v1:0
```

No `us.` prefix. IAM checks permission on the *underlying* foundation model the
profile routes to, and that ARN never carries the `us.`/`eu.`/`apac.` prefix —
the prefix only exists on the profile.

There is a second half to this. Rather than guessing which regions the profile
routes to, ask:

```python
import boto3
c = boto3.client('bedrock', region_name='us-east-1')
r = c.get_inference_profile(inferenceProfileIdentifier='us.amazon.nova-pro-v1:0')
for m in r['models']:
    print(m['modelArn'])
```

It returns three regions, not one. Granting only the region you invoke from
leaves an intermittent failure waiting for the day Bedrock routes elsewhere.
The template grants the real foundation-model ARNs with a region wildcard, plus
the inference-profile ARN separately.

### 2. Stripping XML namespace declarations without stripping the prefixes

The AWS blog feed declares `dc:`, `content:`, `slash:` and friends on the root
element. To avoid namespace-aware parsing, the original code stripped the
`xmlns` declarations before handing the document to `ElementTree`:

```python
clean = re.sub(r'\s+xmlns(?::[a-z]+)?="[^"]*"', '', raw[start:])
```

The declarations go away. The prefixed *tags* inside every `<item>` do not —
`<dc:creator>` is still sitting there, and with nothing left to define what
`dc:` means, `ElementTree` throws `unbound prefix` on the first one.

The reason this is worth writing down is not the regex. It is that the whole
fetch sits inside a `try/except` that exists on purpose: if the blog feed is
down, the digest should still ship with the What's New section rather than
failing the run. So every invoke returned 200. The email went out. The S3 file
was written. One section was permanently empty, and the only trace anywhere was
a single log line nobody had a reason to read.

The fix strips the prefixes off the tags too:

```python
clean = re.sub(r'</?[a-zA-Z0-9]+:', lambda m: m.group(0).replace(':', ''), clean)
```

A `try/except` written for resilience is also a great place for a bug to live
forever, and `StatusCode: 200` is not a test result.

### 3. Cost Explorer's 1,000-group cap has no pagination by default

`GetCostAndUsage` returns at most 1,000 groups per call and signals more data
is available with a `NextPageToken` — it does not raise, warn, or otherwise
tell you anything was cut. The original `account_context.py` only ever read
the first page. On this account, `SERVICE + USAGE_TYPE` never comes close to
1,000 groups, so this never fired here. It would on a large-enough account,
and the symptom would be a shorter-than-it-should-be usage list that looks
completely normal — advice generated from it would simply never mention
whatever fell off the end. Fixed with a loop over `NextPageToken` until it
comes back empty.

### 4. Flagging ran against the truncated display list, not the full one

Each service's usage types are trimmed to the top 4 by cost for display, to
keep the prompt readable. `flag_usage_types()` was iterating that same
trimmed list — which means the smallest, most-signal-bearing usage types were
exactly the ones most likely to be cut before the flagging logic ever saw
them. `PublicIPv4:IdleAddress` sits at usage `0.089` on this account, nowhere
near a top-4-by-cost cutoff. Fixed by scanning `all_usage_types` (the
untruncated set) for flags while still only *displaying* the top 4 per
service. On this same account, the flag count went from 7 to 14.

## Limitations

- Two hardcoded feeds (AWS What's New, AWS News Blog). No config for adding more.
- Weekly schedule only.
- The blog fetch still fails soft by design — if it breaks, you get a digest
  with no blog section and one line in CloudWatch. That tradeoff is deliberate,
  but now you know to go look at that line.
- An empty What's New feed raises and sends an error email; an empty blog feed
  does not.
- The LinkedIn and webhook channels are written but off by default and have seen
  much less use than email and S3.
- The account advice section sees billing data only. It knows what your account
  used and how much; it cannot see a single configuration value, so it is told
  to say when it is inferring and never to guess at settings.
- Its flagged-signal list is five patterns long. Those five mean the same thing
  in every account, which is why they are matched in code — but plenty of other
  waste has no such tell and will only be caught if the model happens to spot it.
- On an account inside the free tier the advice is technically correct and
  financially pointless. The findings are real; the amounts are rounding errors.
  It gets useful in proportion to how much you actually run.

## License

MIT — see [LICENSE](LICENSE).
