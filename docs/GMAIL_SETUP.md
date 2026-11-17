# Connecting Gmail

The assistant reads your own mailbox through Google's `gmail.readonly` scope. It cannot send, delete,
label or change anything. See `PROGRESS.md` for what has been tested against real Gmail.

This guide is for a personal Gmail account, using a Google OAuth client in **Testing** mode.

## What to expect

- **You sign in again about once a week.** Google expires refresh tokens after 7 days for an app in
  Testing mode. The Integrations screen and any answer will say so, and **Connect again** fixes it.
  Avoiding that means publishing the app, and Gmail's restricted scope then needs Google's review.
- **Google shows a warning that the app is unverified.** That is expected for your own Testing app.
- **A mailbox is one person's.** Nobody else in the workspace can use it, admins included. Admins can
  only approve its tools and remove the connection.

## 1. Create the Google OAuth client

In [Google Cloud Console](https://console.cloud.google.com/):

1. Create a project, or pick one.
2. Under **APIs & Services → Library**, enable the **Gmail API**.
3. Under **Google Auth Platform → Audience** (older consoles: **OAuth consent screen**), choose
   **External**, leave it in **Testing**, and add your own address under **Test users**.
4. Under **Data Access**, add the scope `https://www.googleapis.com/auth/gmail.readonly`. Add nothing
   else: a grant of any other scope is refused and given back.
5. Under **Clients** (older consoles: **Credentials**), create an **OAuth client ID** of type
   **Web application**, with this **Authorized redirect URI**:

   ```text
   http://localhost:5173/api/v1/oauth/google/callback
   ```

   It must match exactly. It goes through the web server so your sign-in cookie comes along.

## 2. Configure the server

Add these to `.env`, which git ignores:

```text
GOOGLE_OAUTH_CLIENT_ID=your-client-id
GOOGLE_OAUTH_CLIENT_SECRET=your-client-secret
CREDENTIAL_KEYS={"k1": "output-of-the-command-below"}
CREDENTIAL_ACTIVE_KEY_ID=k1
```

Generate the key with `python -m uv run opsassist generate-credential-key`, and back it up. Losing it
means connecting again. If you deploy somewhere other than localhost, also set
`GOOGLE_OAUTH_REDIRECT_URI` to match the client.

Run migrations and restart the API: `python -m uv run alembic upgrade head`.

## 3. Connect

1. In the app, open **Integrations** and choose **Connect Gmail**.
2. Sign in, accept the unverified-app warning, and allow read access.
3. As a workspace admin, approve each tool as read-only on the same screen. Until then the assistant
   cannot use them.

Then ask something like "Which emails from this week are waiting for my reply?"

## 4. Test against real Gmail (optional)

`tests/integration/test_gmail_live.py` reads your real mailbox, and changes nothing. It needs a
refresh token, so it is skipped unless you provide one. See that file for details.

## Privacy

- With a local model (`LLM_PROVIDER=ollama` on this machine), mail stays on this machine.
- An external model would receive excerpts of your mail. That needs both
  `EXTERNAL_MODEL_DATA_APPROVED=true` and `EXTERNAL_MODEL_MAIL_APPROVED=true`. Without the second, the
  assistant is not offered the mailbox at all.
- Excerpts of up to 1,500 characters from mail used in an answer are stored in your private
  conversation, and withheld once you disconnect. The tool invocation log holds neither your search
  terms nor any mail.
- Attachments are listed by name and size, and never downloaded.

## Disconnecting

**Disconnect** on the Integrations screen revokes access at Google, then deletes the stored token. If
Google cannot be reached the connection is kept, and you can choose to remove it anyway and revoke
access yourself at <https://myaccount.google.com/permissions>.
