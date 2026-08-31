# AWS Weekly Digest

> **TL;DR** — A Lambda that runs every Friday, pulls the AWS What's New and
> AWS News Blog RSS feeds for the past week, hands them to an LLM to write a
> categorized digest, then emails it to you and archives a copy in S3.
> Deployed with SAM. No dependencies beyond boto3 and the Python standard
> library.

The LLM can be Amazon Bedrock (default) or any OpenAI-compatible endpoint —
Gemini, a local LM Studio instance, a self-hosted LiteLLM — via a single
`openai_compatible` provider, so there is no separate SDK per vendor.

I built this because I kept meaning to read the AWS What's New feed and never
did. It is a small personal tool, not a product.

## How it works

```
  AWS What's New RSS  ─┐
                       ├─→  Lambda (Python 3.12, arm64)  ─→  LLM  ─┬─→  SES  → your inbox
  AWS News Blog RSS   ─┘         ▲                                 └─→  S3   → digests/<date>/
                                 │
                    EventBridge Scheduler (Fri 17:00 Asia/Taipei)
```

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
| `LLM_TIMEOUT` | `240` | Seconds. Lambda timeout is 300 |
| `DIGEST_LANGUAGE` | `en` | `en` or `zh-TW`. See below |

Feature flags: `FEATURE_SEND_EMAIL`, `FEATURE_EMBED_CONTENT`,
`FEATURE_SAVE_TO_S3`, `FEATURE_POST_TO_LINKEDIN`, `FEATURE_POST_TO_WEBHOOK`.

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

The email wrapper — subject line, the counts under the header, the footer —
follows the same setting (`_EMAIL_STRINGS`), so you do not end up with an
English digest inside a Chinese-labelled email.

Each language has its own prompt in `lambda_function.py` (`_prompt_en`,
`_prompt_zh_tw`) rather than one English prompt with "reply in X" appended. The
section headings and per-item field names are part of the output contract — the
Markdown-to-HTML and Markdown-to-LinkedIn converters read that structure back —
so they have to be written in the target language to come back reliably.

To add a language, write a builder and register it in `_PROMPT_BUILDERS`, add
the matching entry to `_EMAIL_STRINGS`, then add the value to `AllowedValues` on
`DigestLanguage` in `template.yaml`. An
unknown value raises at run time rather than silently producing something in the
wrong language.

## Two traps worth knowing about

Both of these survived `sam build` and `sam validate` and only showed up on a
real invoke. If you are building something similar, these are the parts of this
repo actually worth your time.

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

## License

MIT — see [LICENSE](LICENSE).
