# Setup: speech-to-text (transcription) providers

## What it is for

ClipForge turns the audio of an uploaded video into a timed transcript
(segments and word timings), which later features use for subtitles and clip
detection. The provider is chosen with `TRANSCRIPTION_PROVIDER`:

| Value               | What it does                                                            |
| ------------------- | ----------------------------------------------------------------------- |
| `fake` (default)    | Placeholder text, no account, no network. Refused in production.        |
| `openai_compatible` | Sends the audio to an OpenAI-compatible `/audio/transcriptions` API.    |
| `local_whisper`     | Runs Whisper (faster-whisper) on the worker machine; no account needed. |

Nothing in this guide is needed for the automated tests or for basic local
development: they use `fake`. In production you must choose one of the real
providers. If a required value is missing, the app still starts, but
transcription fails with an error that names the missing setting.

Put every value in `backend/.env` (git-ignored) or in the deployment
environment / GitHub repository secrets. Never put a real key in a committed
file, an issue, or a pull request.

## Option A: a hosted OpenAI-compatible API

### Values you need

| Setting                         | Description                                                   |
| ------------------------------- | ------------------------------------------------------------- |
| `TRANSCRIPTION_PROVIDER`        | `openai_compatible`                                           |
| `TRANSCRIPTION_API_BASE_URL`    | The API base URL, ending before `/audio/transcriptions`       |
| `TRANSCRIPTION_API_KEY`         | Your API key (keep it secret)                                 |
| `TRANSCRIPTION_MODEL`           | A model that supports `verbose_json` with word timestamps     |
| `TRANSCRIPTION_TIMEOUT_SECONDS` | Optional, default `300`                                       |
| `TRANSCRIPTION_MAX_UPLOAD_MB`   | Optional, default `24`; set it to the provider's upload limit or lower |
| `TRANSCRIPTION_CHUNK_SECONDS`   | Optional, default `600`                                       |

The model must support `response_format=verbose_json` with
`timestamp_granularities[]=word`. For example, OpenAI's `whisper-1` does;
some newer OpenAI transcription models only return plain text or JSON and
will not work. Check your provider's documentation.

### Steps (OpenAI as an example; other compatible providers are similar)

1. Create an account at <https://platform.openai.com/> and sign in.
2. Add billing under **Settings → Billing** (the API is paid per minute of
   audio; check the current pricing page first). Some other compatible
   providers offer a free tier; prefer it while testing.
3. Open **API keys** (under your profile or **Dashboard → API keys**) and
   click **Create new secret key**. Give it a name such as `clipforge`, and
   restrict its permissions to audio if the dashboard offers that.
4. Copy the key immediately (it is shown only once).
5. Add to `backend/.env`:

   ```bash
   TRANSCRIPTION_PROVIDER=openai_compatible
   TRANSCRIPTION_API_BASE_URL=<your-transcription-api-base-url>
   TRANSCRIPTION_API_KEY=<your-transcription-api-key>
   TRANSCRIPTION_MODEL=<your-transcription-model>
   ```

   For OpenAI the base URL is `https://api.openai.com/v1` and the model
   `whisper-1`. For deployment, store the same names as GitHub repository
   secrets (**Settings → Secrets and variables → Actions → New repository
   secret**) or in your hosting platform's secret settings.
6. Restart the API and the Celery worker so they read the new settings.

The base URL must use `https://`. `http://` is allowed only for a server on
the same machine (`localhost`, `127.0.0.1`, `::1`), for example a
self-hosted OpenAI-compatible Whisper server; for such a local server the
API key may be left unset.

Files larger than `TRANSCRIPTION_MAX_UPLOAD_MB` are split into chunks with
FFmpeg, so FFmpeg and ffprobe must be installed on the worker (they already
are for metadata extraction).

## Option B: local Whisper (no account)

1. On the worker machine, from the `backend/` directory, install the
   optional packages (several hundred MB):

   ```bash
   pip install -r requirements-ml.txt
   ```

2. Add to `backend/.env`:

   ```bash
   TRANSCRIPTION_PROVIDER=local_whisper
   TRANSCRIPTION_MODEL=small
   ```

   `TRANSCRIPTION_MODEL` is a Whisper model size (`tiny`, `base`, `small`,
   `medium`, `large-v3`) or the path of a downloaded model directory. Larger
   models are more accurate but much slower on a CPU and need more memory.
3. The first transcription downloads the model from the Hugging Face Hub
   (once; it is cached). For machines without internet access, download the
   model directory in advance and set `TRANSCRIPTION_MODEL` to its path.
4. Restart the Celery worker.

Audio never leaves the machine with this option.

## How to confirm it works

From the `backend/` directory, with the settings above in `backend/.env` and
a short audio or video file of your own:

```bash
python -c "from app.transcription import get_transcription_provider as g; r = g().transcribe('<path-to-a-short-audio-file>'); print(r.provider, r.model, r.language, len(r.segments))"
```

It prints the provider, model, detected language and number of segments.
A missing setting prints a `TranscriptionConfigurationError` naming it; a
wrong key gives a `TranscriptionProviderError` mentioning HTTP 401. Note that
the hosted option sends this file to the provider and is billed.
