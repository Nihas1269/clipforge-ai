# Razorpay (paid subscriptions)

## What it is for

ClipForge sells the **pro** plan as a monthly subscription through
[Razorpay](https://razorpay.com). The backend needs a Razorpay account for:

- `POST /api/v1/billing/checkout` — starts a subscription checkout,
- `POST /api/v1/billing/verify` — confirms the payment after checkout,
- `POST /api/v1/billing/cancel` — cancels at the end of the billing period.

Without these settings the rest of ClipForge works normally; the billing
endpoints answer `503 billing_not_configured`.

| Setting | Secret? | Used for |
| --- | --- | --- |
| `RAZORPAY_KEY_ID` | no (shown to the browser) | API login and opening Razorpay Checkout |
| `RAZORPAY_KEY_SECRET` | **yes** | API login and checking payment signatures |
| `RAZORPAY_PRO_PLAN_ID` | no | The Razorpay plan (price) of the pro plan |
| `RAZORPAY_WEBHOOK_SECRET` | **yes** | Checking webhook signatures (a different value from the key secret) |

## Use test mode first

Razorpay has a free **Test Mode**. Test keys start with `rzp_test_`, no real
money moves, and you can pay with Razorpay's test cards and UPI ids. Do
everything below in Test Mode first; switch to Live Mode only after the
account is activated (KYC) and everything works in test mode.

## Step by step

1. **Create an account.** Sign up at <https://dashboard.razorpay.com/signup>
   and log in. In the top bar of the dashboard, switch the mode toggle to
   **Test Mode**.
2. **Create the pro plan.** In the dashboard go to **Subscriptions → Plans →
   Create Plan**. Choose billing frequency **Monthly**, every **1** month, and
   enter the price in INR. This price is what customers pay; it should match
   `BILLING_PRO_PRICE_MINOR` (in paise, for example 999.00 INR = `99900`),
   which ClipForge shows but never sends to Razorpay. Save, then copy the
   plan id (it looks like `plan_...`). This is `RAZORPAY_PRO_PLAN_ID`.
3. **Generate API keys.** Go to **Account & Settings → API Keys** (under
   "Website and app settings") and click **Generate Test Key**. Copy the
   **Key Id** (`rzp_test_...`) and the **Key Secret**. The secret is shown
   only once; if you lose it, generate a new key.
4. **(Optional for now) Create a webhook.** Go to **Account & Settings →
   Webhooks → Add New Webhook**. Choose your own random **Secret** (for
   example from `python -c "import secrets; print(secrets.token_urlsafe(32))"`)
   — this is `RAZORPAY_WEBHOOK_SECRET` and must **not** be the key secret.
   ClipForge does not process webhooks yet, so the URL can be added later
   when that endpoint exists.
5. **Put the values in your local `backend/.env`** (git-ignored), never in a
   committed file, issue or pull request:

   ```bash
   RAZORPAY_PRO_PLAN_ID=<your-razorpay-pro-plan-id>
   RAZORPAY_KEY_ID=<your-razorpay-key-id>
   RAZORPAY_KEY_SECRET=<your-razorpay-key-secret>
   RAZORPAY_WEBHOOK_SECRET=<your-razorpay-webhook-secret>
   ```

   For a deployment, store the same values as secrets of the deployment
   environment (for example GitHub repository secrets used by the CI/CD
   workflow), not in files.
6. **Restart the backend** so the settings are loaded.

## Confirm it works (test mode)

1. Log in to ClipForge and call `POST /api/v1/billing/checkout` with
   `{"plan_code": "pro"}`. The answer contains `provider`, `key_id` and
   `subscription_id` (`sub_...`), and a subscription with status **Created**
   appears under **Subscriptions** in the Razorpay dashboard.
2. Open Razorpay Checkout in the browser with that `key_id` and
   `subscription_id`, and pay with a Razorpay test card.
3. Send the three values Checkout returns (`razorpay_payment_id`,
   `razorpay_subscription_id`, `razorpay_signature`) to
   `POST /api/v1/billing/verify`. Once Razorpay shows the subscription as
   **Active**, the answer is `200` with `"status": "active"`. While Razorpay
   still shows it as Created or Authenticated (no charge yet), or Pending (a
   charge failed and Razorpay is retrying), the answer is `202` with
   `"status": "pending"`; call verify again a little later.
4. Call `POST /api/v1/billing/cancel`. The answer has `"status":
   "cancelled"`, and the dashboard shows the subscription as cancelling at
   the end of the cycle. Pro access lasts until `current_period_end`.

A `503 billing_not_configured` answer means a setting is missing or the key
id does not start with `rzp_test_`/`rzp_live_`; the server log entry
`billing_not_configured` names the missing setting (never its value).

A `502 payment_provider_error` with a server log entry
`razorpay_request_failed` and `status_code` `401` means Razorpay rejected the
key id / key secret pair (for example a regenerated secret, or a test key id
with a live secret). ClipForge cannot tell this from other Razorpay errors, so
it is not reported as `billing_not_configured`; check both values.

## Things to verify manually in the test account

The automated tests use a fake Razorpay API, so check these by hand once:

- the subscription created by checkout uses the right plan and price;
- after a test payment Razorpay reports the subscription as `active` with a
  current period, and `/verify` activates it;
- cancelling sets "cancel at cycle end" in the dashboard;
- after cancelling an unpaid checkout subscription in the dashboard, a new
  checkout returns a new `subscription_id`;
- cancelling through ClipForge after the subscription was already cancelled
  in the dashboard answers `200` (Razorpay refuses the second cancel with a
  `4xx`, and ClipForge then confirms the cancelled state);
- after paying a test checkout **without** calling `/verify` (close the
  tab), a new `POST /api/v1/billing/checkout` answers `409
  subscription_exists` and the subscription becomes `active` in ClipForge
  (and `/cancel` works for it);
- after changing `RAZORPAY_PRO_PLAN_ID` while a checkout is `authenticated`,
  the next checkout cancels the old subscription at once in the dashboard
  (Razorpay accepts `cancel_at_cycle_end=0` for an unpaid subscription);
- `RAZORPAY_SUBSCRIPTION_TOTAL_COUNT` (default 60 cycles) is accepted by
  Razorpay for a monthly plan;
- what ClipForge's cancel answers after the subscription was already set to
  "cancel at cycle end" in the dashboard. Razorpay may still report it as
  `active` and refuse the second cancel with a `4xx`; ClipForge then answers
  `502 payment_provider_error` on every retry until the cycle ends, and the
  local status has to be corrected by hand (or by the future webhook).

## Known limitations

- A user's first checkout stores a `pending` subscription row before calling
  Razorpay. If that call fails, the row stays without a Razorpay id. It grants
  nothing, and the user's next checkout reuses it.
- A cancelled subscription keeps pro access until `current_period_end`. Until
  then, checkout answers `409 subscription_exists`, so the user cannot
  subscribe again before the paid period ends.
- Renewals are not synced yet (no webhooks), so pro access ends locally at
  the end of the first paid period even though Razorpay keeps charging.
  Checkout then fetches the existing Razorpay subscription and answers
  `409 subscription_exists` while Razorpay can still charge it; a new
  subscription is created only after Razorpay reports the old one
  `cancelled`, `completed`, `expired` or `halted`. Verify this in the test
  account once webhooks are in place.
- Razorpay's `pending` status (a failed charge being retried) is treated like
  a checkout that has not been paid yet: it grants no access, verify answers
  `202`, and checkout offers the same subscription again.
- If the browser never sends the checkout result to `/verify` (tab closed,
  network error), the paid subscription stays `pending` until the user starts
  a checkout again or cancels: both read Razorpay's state and activate it
  when Razorpay reports it `active` for the configured plan. There is no
  background sync until webhooks are processed.
- If creating a subscription times out or the connection drops after
  Razorpay received the request, Razorpay may have created a subscription
  ClipForge never stored; the next checkout creates another one. These stay
  unpaid in **Created** state. Find them under **Subscriptions** by the
  `user_id` note and cancel them.
- A checkout that Razorpay reports as **Paused** (or `active` for a plan id
  other than `RAZORPAY_PRO_PLAN_ID`) is neither activated nor replaced:
  checkout answers `409 checkout_unsettled` and cancel `409
  no_active_subscription`. Resume or cancel it in the dashboard; once it is
  cancelled there, the next checkout starts a new one.

## Going live

Complete Razorpay's account activation, switch the dashboard to **Live
Mode**, create the plan again there (live plans have their own ids), generate
live keys (`rzp_live_...`) and replace all four values in the production
environment.
