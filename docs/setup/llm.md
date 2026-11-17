# Setup: LLM-assisted clip detection

## What it is for

ClipForge suggests short clips from a video's transcript. By default it uses
an offline heuristic (`CLIP_DETECTOR=heuristic`) that needs no account. With
`CLIP_DETECTOR=llm` it asks a large language model for engaging moments,
with titles and reasons. The LLM is optional: when its settings are missing
or a request fails, ClipForge logs a warning and uses the heuristic detector
instead, so nothing breaks without it.

Two kinds of API are supported, chosen with `LLM_PROVIDER`:

| Value               | API                                                        |
| ------------------- | ---------------------------------------------------------- |
| `openai_compatible` | OpenAI-style `/chat/completions` (OpenAI and compatible providers or local servers) |
| `anthropic`         | Anthropic Messages API (`/messages`)                       |

Nothing in this guide is needed for the automated tests or for local
development.

**Privacy and cost.** Transcript text (segment times and words, no file
names or user details) is sent to the provider, and requests are billed per
token. Long transcripts are split into several requests. Check the
provider's data-retention terms before enabling this for real users.

Put every value in `backend/.env` (git-ignored) or in the deployment
environment / GitHub repository secrets. Never put a real key in a committed
file, an issue, or a pull request.

## Values you need

| Setting                              | Description                                                      |
| ------------------------------------ | ---------------------------------------------------------------- |
| `CLIP_DETECTOR`                      | `llm`                                                            |
| `LLM_PROVIDER`                       | `openai_compatible` or `anthropic`                               |
| `LLM_API_BASE_URL`                   | The API base URL including the version path (see below)          |
| `LLM_API_KEY`                        | Your API key (keep it secret)                                    |
| `LLM_MODEL`                          | The model name                                                   |
| `LLM_TIMEOUT_SECONDS`                | Optional, default `60`                                           |
| `LLM_MAX_PROMPT_CHARS`               | Optional, default `24000`; lower it for models with a small context |
| `LLM_MAX_OUTPUT_TOKENS`              | Optional, default `2048`                                         |
| `LLM_INPUT_COST_PER_MILLION_TOKENS`  | Optional, your provider's input price in USD (for cost logging)  |
| `LLM_OUTPUT_COST_PER_MILLION_TOKENS` | Optional, your provider's output price in USD                    |

## Option A: Anthropic

1. Create an account at <https://console.anthropic.com/> and sign in.
2. Add credits under **Settings → Billing** (the API is paid per token;
   check the pricing page first and set a low spend limit under
   **Settings → Limits** while testing).
3. Open **Settings → API keys**, click **Create Key**, name it `clipforge`
   and copy it immediately (it is shown only once).
4. Add to `backend/.env`:

   ```bash
   CLIP_DETECTOR=llm
   LLM_PROVIDER=anthropic
   LLM_API_BASE_URL=<your-llm-api-base-url>
   LLM_API_KEY=<your-llm-api-key>
   LLM_MODEL=<your-llm-model>
   ```

   For Anthropic the base URL is `https://api.anthropic.com/v1`; pick a
   current model name from the Anthropic documentation.

## Option B: an OpenAI-compatible API

1. Create an account with the provider (for OpenAI:
   <https://platform.openai.com/>) and sign in.
2. Add billing (for OpenAI: **Settings → Billing**) and set a usage limit.
   Some compatible providers have a free tier; prefer it while testing.
3. Create an API key (for OpenAI: **Dashboard → API keys → Create new secret
   key**) and copy it immediately.
4. Add to `backend/.env`:

   ```bash
   CLIP_DETECTOR=llm
   LLM_PROVIDER=openai_compatible
   LLM_API_BASE_URL=<your-llm-api-base-url>
   LLM_API_KEY=<your-llm-api-key>
   LLM_MODEL=<your-llm-model>
   ```

   For OpenAI the base URL is `https://api.openai.com/v1`.

A server on the same machine (for example a local model server exposing an
OpenAI-compatible API at `http://localhost:11434/v1`) may use `http://`, and
the key may then be left unset. Every other URL must use `https://`.

## For deployment

Store the same names as GitHub repository secrets (**Settings → Secrets and
variables → Actions → New repository secret**) or in your hosting platform's
secret settings. Restart the API and the Celery worker after changing them.

## How to confirm it works

From the `backend/` directory, with the settings above in `backend/.env`:

```bash
python -c "from app.clips import get_clip_detector, ClipDetectionOptions as O; from app.transcription import TranscriptionSegment as S; d = get_clip_detector(); s = [S(start=i * 5.0, end=i * 5.0 + 5, text='This is a surprising tip about saving time!') for i in range(24)]; print(len(d.detect(s, 120.0, O.from_settings()))); print(d.last_run)"
```

It prints the number of clips and the run record. `fallback_used=False` and
your provider and model mean the LLM was used. `fallback_used=True` shows
why it was not in `failure_category`: `configuration` (a setting is missing
or invalid; the log line `llm_clip_detector_unconfigured` names it),
`authentication` (wrong key), `rate_limited`, `server_error`, `timeout`,
`network` or `invalid_output` (the model did not return valid JSON twice).
Note that this sends the sample text to the provider and is billed.
