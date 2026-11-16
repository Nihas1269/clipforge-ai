# Setup: transactional email (SMTP)

## What it is for

ClipForge sends two kinds of email:

- **Email verification**: after registration (and on
  `POST /api/v1/auth/verify-email/request`) the user gets a link that
  confirms they own the address.
- **Password reset**: `POST /api/v1/auth/password-reset/request` emails a
  single-use link for choosing a new password.

Nothing in this guide is needed for the automated tests or for basic local
development. The default `EMAIL_PROVIDER=fake` records messages in memory and
sends nothing. It is refused in production (`APP_ENV=production`): there, the
email endpoints answer `503 email_unavailable` until SMTP is configured, while
registration and login keep working.

## Values you need

| Setting                | Description                                                         |
| ---------------------- | ------------------------------------------------------------------- |
| `EMAIL_PROVIDER`       | `smtp`                                                              |
| `EMAIL_FROM`           | Sender, e.g. `ClipForge <no-reply@your-domain>` (a verified sender) |
| `FRONTEND_URL`         | Public URL of the web app, e.g. `https://app.your-domain`           |
| `SMTP_HOST`            | SMTP server host name                                               |
| `SMTP_PORT`            | `587` for STARTTLS (default) or `465` for implicit TLS              |
| `SMTP_SECURITY`        | `starttls` (default), `ssl` (port 465), or `none` (local only)      |
| `SMTP_USERNAME`        | SMTP user name (set together with the password)                     |
| `SMTP_PASSWORD`        | SMTP password or API key (keep it secret)                           |
| `SMTP_TIMEOUT_SECONDS` | Connection/command timeout, default `10`                            |

Emailed links have the form `<FRONTEND_URL>/verify-email#token=...` and
`<FRONTEND_URL>/reset-password#token=...`. The token is in the URL fragment,
which browsers never send to a server; the frontend page reads it and posts it
to `/api/v1/auth/verify-email/confirm` or `/api/v1/auth/password-reset/confirm`.

## Option A: a transactional email provider (production)

Any provider with SMTP access works, for example Amazon SES, Postmark,
Brevo, Mailgun or SendGrid. Most offer a free tier or a sandbox/test mode;
use that first. The steps are similar everywhere:

1. Create an account with the provider.
2. **Verify your sending domain** (or at least a single sender address). The
   provider shows DNS records (SPF, DKIM, often DMARC) to add at your DNS
   host. Wait until the dashboard shows the domain as verified. The sender
   address you use for `EMAIL_FROM` must be on this domain.
3. Find the **SMTP** section of the dashboard (often under "SMTP relay",
   "SMTP settings" or "Integrations → SMTP") and note the host and port.
   Use port `587` with `SMTP_SECURITY=starttls`, or `465` with
   `SMTP_SECURITY=ssl`.
4. Create **SMTP credentials** there (some providers call them an SMTP API
   key). Copy the user name (`SMTP_USERNAME`) and password (`SMTP_PASSWORD`);
   the password is usually shown only once.
5. If the account starts in a sandbox (Amazon SES does), it can only send to
   verified recipient addresses. Test with such an address, then request
   production access from the provider.

## Option B: Mailpit (local development, no account)

To see the emails locally, run [Mailpit](https://mailpit.axllent.org/), a
local SMTP server with a web inbox, for example with Docker:

```bash
docker run --rm -p 1025:1025 -p 8025:8025 axllent/mailpit
```

and set in `backend/.env`:

```bash
EMAIL_PROVIDER=smtp
EMAIL_FROM=ClipForge <no-reply@localhost>
FRONTEND_URL=http://localhost:3000
SMTP_HOST=localhost
SMTP_PORT=1025
SMTP_SECURITY=none
```

Open `http://localhost:8025` to read the messages. `SMTP_SECURITY=none` is
refused in production.

## Where to put the values

- Local development: `backend/.env` (git-ignored). Never put real values in
  `backend/.env.example` or any other committed file.
- Deployment: the hosting platform's environment variables, or GitHub
  repository secrets (Settings → Secrets and variables → Actions) that your
  deployment workflow passes to the backend. `SMTP_PASSWORD` must be stored
  as a secret.

Restart the backend after changing them.

## How to confirm it works

1. Start the backend with the values set.
2. Register a test account with an address you can read
   (`POST /api/v1/auth/register`). A verification email should arrive.
3. Request a password reset for that address
   (`POST /api/v1/auth/password-reset/request`) and check that the email
   arrives and the link points at your `FRONTEND_URL`.
4. If nothing arrives, check the backend logs:
   - `email_not_configured` names the missing settings;
   - `email_send_failed` / `email_delivery_failed` include the error type
     (for example `SMTPAuthenticationError` for wrong credentials or
     `TimeoutError` for a blocked port). Logs never contain recipients or
     tokens.
