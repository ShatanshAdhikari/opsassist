# Connecting GitHub

The GitHub integration uses a GitHub App with read-only permissions. Creating and installing the
app happens in your browser. This has been tested against a fake GitHub only until the live test
below has been run: see `PROGRESS.md`.

## 1. Create the app

Go to <https://github.com/settings/apps/new> (or your organization's settings) and set:

- **Name:** anything unique. **Homepage URL:** anything, for example your repository URL.
- **Webhook:** untick **Active**. Nothing here uses webhooks yet.
- **Repository permissions**, all **Read-only**: Checks, Contents, Issues, Pull requests. Metadata
  is included automatically. Give it no write permissions: tokens are requested read-only anyway,
  so write access would be unused risk.
- **Where can this app be installed:** Only on this account.

After creating it, note the **App ID**, then under **Private keys** choose **Generate a private
key**. A `.pem` file downloads.

**Treat that file as a password.** Store it outside the repository. `*.pem` is ignored by git here,
but a copy in OneDrive still syncs.

## 2. Install it

In the app's settings choose **Install App**, then install on **Only select repositories** and pick
the repositories to expose. The URL afterwards ends in `/installations/<number>`. That number is the
**installation ID**.

## 3. Test against real GitHub

From `backend/`, using the disposable test database:

```powershell
$env:GITHUB_TEST_APP_ID="123456"
$env:GITHUB_TEST_INSTALLATION_ID="7890123"
$env:GITHUB_TEST_PRIVATE_KEY_FILE="C:\path\to\key.pem"
$env:GITHUB_TEST_REPO="owner/name"
uv run pytest -m live_github tests/integration/test_github_live.py -v
```

This reads from the repository and confirms the token cannot write or see other repositories. It
creates nothing on GitHub.

## 4. Configure the application

Generate an encryption key and add both lines to `.env`, then restart the API:

```powershell
uv run opsassist generate-credential-key
```

```
CREDENTIAL_KEYS={"k1":"<generated key>"}
CREDENTIAL_ACTIVE_KEY_ID=k1
```

**Back this key up separately from the database.** Without it, stored credentials cannot be
decrypted, and you would need to re-enter them. Anyone holding both it and a database copy can
decrypt them.

## 5. Connect

As an owner or admin, open **Integrations** and choose **Connect GitHub**. Enter the App ID, the
installation ID and the repositories, one per line as `owner/name`, then choose the `.pem` key file.
The screen saves the connection, finds its tools and checks it, and reports each step. Then approve
the read-only tools, or choose **Approve all**. The assistant cannot use a tool until it is approved.
Only tools the provider marks read-only can be approved there.

The same steps through the API, with an owner or admin session:

1. `POST /api/v1/workspaces/{id}/connections/github` with `slug`, `name`, `app_id`,
   `installation_id`, `private_key` (the file's contents) and `repositories` (`["owner/name"]`).
2. `POST /connections/{id}/verify`.
3. `POST /tool-servers/{tool_server_id}/discover`.
4. `POST /tools/{id}/review` for each tool, with its `definition_hash`, `risk_level: 0`,
   `access: "read"` and `enabled: true`.
5. `POST /tools/{id}/invoke` with `arguments`.

## Who can see what

**Any workspace member with `tools:invoke` can read every repository on the connection**, including
private ones, whatever their own GitHub access. The platform does not check GitHub permissions per
person yet. So list only repositories that everyone in the workspace may read. ADR-0012 explains
this decision.

## Rotating and revoking

- **New app key:** generate one on GitHub, `PUT /connections/{id}/key`, then delete the old key on
  GitHub.
- **Change repositories:** `PUT /connections/{id}/repositories`. Repositories must also be selected
  in the installation.
- **New encryption key:** add it to `CREDENTIAL_KEYS`, make it active, run
  `uv run opsassist rotate-credential-keys` until it reports 0, then remove the old key.
- **Revoke:** `DELETE /connections/{id}` destroys the stored key and removes the tools. **Also
  uninstall the app on GitHub**, because deleting here does not revoke anything there, and an issued
  token stays valid at GitHub for up to an hour.
